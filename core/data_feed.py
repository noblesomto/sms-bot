import threading
import time
import logging
from datetime import datetime, timezone
from typing import Optional

import requests
import pandas as pd

from config import settings

logger = logging.getLogger(__name__)

# ── Cache ─────────────────────────────────────────────────────────────────────
_cache: dict = {}

_CACHE_TTL_BY_TF: dict = {
    "5min":  4 * 60,       # 4 min
    "15min": 4 * 60,       # 4 min
    "1h":    4 * 60,       # 4 min
    "4h":    3 * 60 * 60,  # 3 h (Twelve Data fallback path only)
}
CACHE_TTL = 4 * 60

# One 15min fetch per pair is the shared base for 15min/1h/4h — resampling
# locally keeps every timeframe live-fresh (in-progress candle close = live
# price) at a third of the Yahoo call volume of per-timeframe fetches.
_BASE_TTL = 4 * 60            # under the 5-min scan interval → fresh every scan
_BASE_MAX_STALE = 30 * 60     # serve a stale base this old before burning
                              # Twelve Data credits when Yahoo throttles
_BASE_OUTPUTSIZE = 10000      # keep the full 60d of 15m bars (~5500) so
                              # resampled 1h/4h still get 200+ candles

# ── Twelve Data rate limiter (fallback only) ──────────────────────────────────
_td_lock = threading.Lock()
_td_last_call: float = 0.0
_TD_INTERVAL: float = 60.0 / 8   # 7.5 s — Basic 8 plan

# ── yfinance helpers ──────────────────────────────────────────────────────────
# yfinance interval strings and the look-back period needed for outputsize=200
_YF_INTERVAL: dict = {
    "5min": "5m", "15min": "15m", "1h": "1h",
    "4h": "1h",   # 4H is resampled from 1H data
}
# Minimum history period to guarantee 200+ candles (accounts for weekends)
_YF_PERIOD: dict = {
    "5min": "5d", "15min": "60d", "1h": "60d", "4h": "60d",
}


# Yahoo Finance doesn't carry XAUUSD=X spot any more — use COMEX gold futures
# GC=F tracks XAU/USD spot within $2-5; structure is identical for SMC analysis.
_YF_SYMBOL_OVERRIDE: dict = {
    "XAU/USD": "GC=F",   # COMEX gold futures
    "XAG/USD": "SI=F",   # COMEX silver futures
    "NAS100":  "NQ=F",   # Nasdaq 100 futures
    "US30":    "YM=F",   # Dow Jones futures
}


def _to_yf_symbol(pair: str) -> str:
    """'EUR/USD' → 'EURUSD=X', 'XAU/USD' → 'GC=F'"""
    return _YF_SYMBOL_OVERRIDE.get(pair, pair.replace("/", "") + "=X")


def data_source_note(pair: str) -> str:
    """Human-readable note on where this pair's candles come from — shown on
    charts so futures-proxied pairs (gold = GC=F, etc.) aren't mistaken for
    the broker's spot feed."""
    if pair in _YF_SYMBOL_OVERRIDE:
        return f"{_YF_SYMBOL_OVERRIDE[pair]} futures (proxy for {pair} spot)"
    return f"{pair} spot"


def _fetch_yfinance(pair: str, timeframe: str, outputsize: int) -> Optional[pd.DataFrame]:
    """
    Primary data source — Yahoo Finance via yfinance.

    Completely free, no API key, no daily credit limit.
    4H data is resampled from 1H candles (Yahoo has no native 4H interval).
    """
    try:
        import yfinance as yf
    except ImportError:
        logger.error("yfinance not installed — run: pip install yfinance")
        return None

    symbol = _to_yf_symbol(pair)
    yf_interval = _YF_INTERVAL.get(timeframe)
    if not yf_interval:
        logger.error(f"yfinance: unsupported timeframe '{timeframe}'")
        return None

    period = _YF_PERIOD.get(timeframe, "60d")

    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period=period, interval=yf_interval, auto_adjust=True)

        if hist is None or hist.empty:
            logger.warning(f"yfinance: no data returned for {pair}/{timeframe} (symbol={symbol})")
            return None

        # Resample to 4H from the fetched 1H data
        if timeframe == "4h":
            hist = (
                hist.resample("4h", origin="epoch")
                .agg({"Open": "first", "High": "max", "Low": "min",
                      "Close": "last", "Volume": "sum"})
                .dropna(subset=["Open", "Close"])
            )

        hist = hist.reset_index()

        # Column names differ slightly between versions: "Datetime" vs "Date"
        date_col = "Datetime" if "Datetime" in hist.columns else "Date"
        hist = hist.rename(columns={
            date_col: "datetime",
            "Open": "open", "High": "high",
            "Low": "low", "Close": "close", "Volume": "volume",
        })

        # Ensure UTC-aware datetime
        if hist["datetime"].dt.tz is None:
            hist["datetime"] = hist["datetime"].dt.tz_localize("UTC")
        else:
            hist["datetime"] = hist["datetime"].dt.tz_convert("UTC")

        cols = ["open", "high", "low", "close", "volume", "datetime"]
        hist = hist[[c for c in cols if c in hist.columns]]

        # Fill missing volume (forex pairs return 0 or NaN)
        if "volume" not in hist.columns:
            hist["volume"] = 0.0
        else:
            hist["volume"] = pd.to_numeric(hist["volume"], errors="coerce").fillna(0.0)

        hist = hist.sort_values("datetime").reset_index(drop=True)
        if len(hist) > outputsize:
            hist = hist.tail(outputsize).reset_index(drop=True)

        return hist

    except Exception as e:
        logger.error(f"yfinance error {pair}/{timeframe}: {type(e).__name__}: {e}")
        return None


# ── Twelve Data fallback ──────────────────────────────────────────────────────

def _rate_limit_td() -> None:
    global _td_last_call
    with _td_lock:
        wait = _TD_INTERVAL - (time.time() - _td_last_call)
        if wait > 0:
            time.sleep(wait)
        _td_last_call = time.time()


def _fetch_twelvedata(pair: str, timeframe: str, outputsize: int) -> Optional[pd.DataFrame]:
    """
    Fallback — only called when yfinance fails.
    Subject to 800 credits/day on the Basic 8 plan.
    """
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": pair,
        "interval": timeframe,
        "outputsize": outputsize,
        "apikey": settings.TWELVE_DATA_API_KEY,
        "timezone": "UTC",
        "format": "JSON",
    }

    for attempt in range(3):
        _rate_limit_td()
        try:
            resp = requests.get(url, params=params, timeout=30)

            if resp.status_code == 429:
                backoff = 60 * (attempt + 1)
                logger.warning(
                    f"[{datetime.now(timezone.utc)}] TwelveData 429 for {pair}/{timeframe} "
                    f"— waiting {backoff}s"
                )
                time.sleep(backoff)
                continue

            resp.raise_for_status()
            data = resp.json()

            if "code" in data:
                code = data["code"]
                if code == 429:
                    backoff = 60 * (attempt + 1)
                    logger.warning(f"TwelveData JSON 429 — waiting {backoff}s")
                    time.sleep(backoff)
                    continue
                logger.error(f"TwelveData API error {pair}/{timeframe}: {data}")
                return None

            if "values" not in data:
                logger.error(f"TwelveData: no values for {pair}/{timeframe}: {data}")
                return None

            df = pd.DataFrame(data["values"])
            df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
            df = df.sort_values("datetime").reset_index(drop=True)

            for col in ("open", "high", "low", "close"):
                df[col] = pd.to_numeric(df[col], errors="coerce")

            if "volume" not in df.columns:
                df["volume"] = 0.0
            else:
                df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0.0)

            return df

        except (requests.exceptions.RequestException, ValueError) as e:
            logger.error(
                f"TwelveData fetch error {pair}/{timeframe} attempt {attempt + 1}/3: "
                f"{type(e).__name__}: {e}"
            )
            if attempt < 2:
                time.sleep(2 ** attempt * 2)

    logger.error(f"TwelveData failed for {pair}/{timeframe} after 3 attempts")
    return None


# ── Public interface ──────────────────────────────────────────────────────────

_RESAMPLE_RULE = {"1h": "1h", "4h": "4h"}


def _resample_ohlcv(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    return (
        df.set_index("datetime")
        .resample(rule, origin="epoch")
        .agg({"open": "first", "high": "max", "low": "min",
              "close": "last", "volume": "sum"})
        .dropna(subset=["open", "close"])
        .reset_index()
    )


def _get_base(pair: str, force_fresh: bool = False) -> Optional[pd.DataFrame]:
    """Shared 15m candle history per pair (60d) that all timeframes derive from.

    On a Yahoo failure a recent stale base (< _BASE_MAX_STALE) is served rather
    than escalating straight to Twelve Data — an up-to-30-min-old candle set is
    still usable for structure and beats burning the 800/day credit budget on
    a transient throttle.
    """
    key = ("__base__", pair)
    now = time.time()
    entry = _cache.get(key)

    if not force_fresh and entry and now - entry[1] < _BASE_TTL:
        return entry[0]

    df = _fetch_yfinance(pair, "15min", _BASE_OUTPUTSIZE)
    if df is not None and not df.empty:
        _cache[key] = (df, time.time())
        return df

    if entry and now - entry[1] < _BASE_MAX_STALE:
        age_min = (now - entry[1]) / 60
        logger.warning(
            f"[{datetime.now(timezone.utc)}] yfinance failed for {pair} — "
            f"serving {age_min:.0f}-min-old cached base"
        )
        return entry[0]
    return None


def get_candles(pair: str, timeframe: str, outputsize: int = 200,
                force_fresh: bool = False) -> Optional[pd.DataFrame]:
    """
    Fetch OHLCV candles — yfinance primary (free), Twelve Data fallback.

    15min/1h/4h are all derived from one shared 15m base fetch per pair
    (see `_get_base`), so a full scan cycle costs one Yahoo call per pair
    while every timeframe's in-progress candle stays live. 5min keeps a
    direct fetch path (can't be derived from 15m data).
    `force_fresh=True` bypasses the cache read (the result still refreshes
    the cache) — used for signal charts.
    """
    if timeframe in ("15min", "1h", "4h"):
        base = _get_base(pair, force_fresh)
        if base is not None:
            df = base if timeframe == "15min" else _resample_ohlcv(base, _RESAMPLE_RULE[timeframe])
            return df.tail(outputsize).reset_index(drop=True)

    cache_key = (pair, timeframe)
    now = time.time()
    cache_ttl = _CACHE_TTL_BY_TF.get(timeframe, CACHE_TTL)

    if not force_fresh and cache_key in _cache:
        cached_df, cached_at = _cache[cache_key]
        if now - cached_at < cache_ttl:
            return cached_df

    # Direct path: 5min timeframe, or base fetch + recent stale cache both failed
    if timeframe == "5min":
        df = _fetch_yfinance(pair, timeframe, outputsize)
        if df is not None and not df.empty:
            _cache[cache_key] = (df, time.time())
            return df

    # ── Fallback: Twelve Data (800 credits/day) ───────────────────────────────
    logger.warning(
        f"[{datetime.now(timezone.utc)}] yfinance failed for {pair}/{timeframe} "
        f"— falling back to Twelve Data"
    )
    df = _fetch_twelvedata(pair, timeframe, outputsize)
    if df is not None and not df.empty:
        logger.info(f"TwelveData fallback succeeded for {pair}/{timeframe}")
        _cache[cache_key] = (df, time.time())
        return df

    logger.error(
        f"[{datetime.now(timezone.utc)}] Both yfinance and TwelveData failed for "
        f"{pair}/{timeframe} — skipping this scan cycle"
    )
    return None


def test_data_feed():
    """Live smoke test — fetches a small sample from yfinance for each pair."""
    pairs = [
        "EUR/USD", "GBP/USD", "USD/JPY", "XAU/USD",
        "EUR/JPY", "GBP/JPY", "AUD/USD", "GBP/CAD", "EUR/CAD",
    ]
    print("yfinance smoke test (5min, 10 candles):")
    for p in pairs:
        df = _fetch_yfinance(p, "5min", 10)
        if df is not None and not df.empty:
            print(f"  {p:12} OK  last_close={df['close'].iloc[-1]:.5f}  rows={len(df)}")
        else:
            print(f"  {p:12} FAIL")

    print("\nyfinance 4H resample test (EUR/USD):")
    df = _fetch_yfinance("EUR/USD", "4h", 10)
    if df is not None:
        print(f"  OK  rows={len(df)}  last={df['datetime'].iloc[-1]}")
    else:
        print("  FAIL")


if __name__ == "__main__":
    test_data_feed()
