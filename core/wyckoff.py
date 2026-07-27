"""
Wyckoff Phase Detection — price-action only (no volume required).

Detects accumulation/distribution trading ranges, Springs (stop hunts below
range support), and UTADs (stop hunts above range resistance) using pure OHLC
data. Volume-based VSA is intentionally excluded because forex pairs return
zero volume from Twelve Data's API.

Integration: call get_wyckoff_context(df, ob_at_price) after analyze_structure()
has run on the dataframe (swing_high / swing_low columns must be present).
"""
import logging
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

# Minimum range width as a fraction of price — avoids micro-consolidations
MIN_RANGE_WIDTH_PCT = 0.005   # 0.5%
# Rolling window used to define the current trading range (in candles)
RANGE_WINDOW = 20
# Minimum candles that must close inside the range for it to be "valid"
MIN_INSIDE_CANDLES = 12
# How many recent candles to scan for a Spring / UTAD
SPRING_LOOKBACK = 5
# Minimum wick breach past the range boundary, as a fraction of range width —
# filters out trivial 1-3% noise wicks that technically poke outside the
# range but aren't a real stop-hunt. Evidenced by the 2026-07-27 XAU/USD
# backtest: every failing "Spring" breached range_low by only 1.5-2.8% of
# range width (docs/superpowers/backtests/2026-07-27-fix3-spring-tightening.md).
MIN_BREACH_PCT = 0.03


def identify_trading_range(df: pd.DataFrame) -> Optional[dict]:
    """
    Locate the most recent consolidation zone in the last 80 candles.

    Algorithm:
    - Use the last RANGE_WINDOW candles to define the high/low of the range.
    - Verify that MIN_INSIDE_CANDLES of the last MIN_INSIDE_CANDLES candles
      closed inside the range (price was genuinely contained, not just passing through).
    - Determine the preceding trend from the candles before the range — this
      establishes whether the range is an accumulation (after bearish trend) or
      distribution (after bullish trend) context.

    Returns None if no valid range is found.
    """
    if "swing_high" not in df.columns:
        return None

    recent = df.tail(80)
    n = len(recent)
    if n < RANGE_WINDOW + 10:
        return None

    range_seg = recent.iloc[-RANGE_WINDOW:]
    range_high = float(range_seg["high"].max())
    range_low = float(range_seg["low"].min())

    if range_low <= 0:
        return None

    # Range must be meaningfully wide
    if (range_high - range_low) / range_low < MIN_RANGE_WIDTH_PCT:
        return None

    # At least 75% of the last MIN_INSIDE_CANDLES candles must close inside
    last_n = recent.iloc[-MIN_INSIDE_CANDLES:]
    inside = sum(
        1 for _, row in last_n.iterrows()
        if range_low * 0.995 <= float(row["close"]) <= range_high * 1.005
    )
    if inside < int(MIN_INSIDE_CANDLES * 0.75):
        return None

    # Preceding trend from candles before the range window
    pre = recent.iloc[: n - RANGE_WINDOW]
    if len(pre) < 5:
        return None

    pre_start = float(pre["close"].iloc[0])
    pre_end = float(pre["close"].iloc[-1])
    pct = (pre_end - pre_start) / pre_start

    if abs(pct) < 0.003:
        preceding_trend = "RANGING"
    else:
        preceding_trend = "BULLISH" if pct > 0 else "BEARISH"

    return {
        "range_high": round(range_high, 5),
        "range_low": round(range_low, 5),
        "range_mid": round((range_high + range_low) / 2, 5),
        "candle_count": RANGE_WINDOW,
        "preceding_trend": preceding_trend,
    }


def detect_spring(df: pd.DataFrame, trading_range: dict) -> Optional[dict]:
    """
    Detect a Wyckoff Spring in the last SPRING_LOOKBACK candles.

    A Spring = a candle wicks BELOW range_low by a meaningful margin (sweeping
    sell-side liquidity, not just noise) then closes back AT OR ABOVE
    range_low within the same or next 1-2 candles. This is the institutional
    stop hunt that precedes markup in accumulation.

    Equivalent to the SMC concept of a sell-side liquidity sweep with recovery.

    Returns None if no spring found.
    """
    if not trading_range:
        return None

    range_low = trading_range["range_low"]
    range_high = trading_range["range_high"]
    range_width = range_high - range_low
    recent = df.tail(SPRING_LOOKBACK)

    for i in range(len(recent)):
        row = recent.iloc[i]
        lo = float(row["low"])
        cl = float(row["close"])

        breach = range_low - lo
        if lo < range_low and breach >= MIN_BREACH_PCT * range_width and cl >= range_low:
            spring_low = lo

            # Check whether the spring low was subsequently retested on the
            # following 1-2 candles (retest = higher confidence)
            tested = False
            for _, nxt in recent.iloc[i + 1: i + 3].iterrows():
                if spring_low * 0.998 <= float(nxt["low"]) <= range_low:
                    tested = True
                    break

            return {
                "detected": True,
                "spring_low": round(spring_low, 5),
                "tested": tested,
                "strength": "STRONG" if cl > range_low else "WEAK",
            }

    return None


def detect_utad(df: pd.DataFrame, trading_range: dict) -> Optional[dict]:
    """
    Detect a Wyckoff UTAD (Upthrust After Distribution) — mirror of the Spring.

    A UTAD = a candle wicks ABOVE range_high by a meaningful margin (sweeping
    buy-side liquidity, not just noise) then closes back AT OR BELOW
    range_high. Precedes markdown in distribution.

    Equivalent to the SMC concept of a buy-side liquidity sweep with rejection.

    Returns None if no UTAD found.
    """
    if not trading_range:
        return None

    range_high = trading_range["range_high"]
    range_low = trading_range["range_low"]
    range_width = range_high - range_low
    recent = df.tail(SPRING_LOOKBACK)

    for i in range(len(recent)):
        row = recent.iloc[i]
        hi = float(row["high"])
        cl = float(row["close"])

        breach = hi - range_high
        if hi > range_high and breach >= MIN_BREACH_PCT * range_width and cl <= range_high:
            utad_high = hi

            tested = False
            for _, nxt in recent.iloc[i + 1: i + 3].iterrows():
                if range_high <= float(nxt["high"]) <= utad_high * 1.005:
                    tested = True
                    break

            return {
                "detected": True,
                "utad_high": round(utad_high, 5),
                "tested": tested,
                "strength": "STRONG" if cl < range_high else "WEAK",
            }

    return None


def get_wyckoff_context(
    df: pd.DataFrame,
    ob_at_price: Optional[dict] = None,
) -> dict:
    """
    Run full Wyckoff analysis and return a context dict for confluence scoring.

    Only Phase C (Spring / UTAD confirmed) is scored — Phase A/B are pre-
    actionable and would add noise. Phase D is detected via the existing CHoCH
    logic in the main confluence scorer.

    ob_at_price: the OB dict from get_price_at_ob() — used to detect Spring+OB
    or UTAD+OB confluence (highest conviction ICT+Wyckoff overlap).

    Return shape:
    {
        "phase":              "ACCUMULATION" | "DISTRIBUTION" | None,
        "sub_phase":          "C" | None,
        "key_event":          "SPRING" | "SPRING_TESTED" | "UTAD" | "UTAD_TESTED" | None,
        "confidence":         "HIGH" | "MEDIUM" | None,
        "bias":               "BULLISH" | "BEARISH" | None,
        "description":        str,
        "spring":             dict | None,
        "utad":               dict | None,
        "trading_range":      dict | None,
        "ob_confluence":      bool,
    }
    """
    empty: dict = {
        "phase": None, "sub_phase": None, "key_event": None,
        "confidence": None, "bias": None, "description": "",
        "spring": None, "utad": None, "trading_range": None,
        "ob_confluence": False,
    }

    # Strip the last SPRING_LOOKBACK candles when building the range so that
    # a spring wick doesn't redefine the range floor it's supposed to break.
    tr = identify_trading_range(df.iloc[:-SPRING_LOOKBACK] if len(df) > SPRING_LOOKBACK + 10 else df)
    if tr is None:
        return empty

    preceding = tr["preceding_trend"]
    spring = detect_spring(df, tr)
    utad = detect_utad(df, tr)

    # ── ACCUMULATION context (came from a downtrend) ──────────────────────
    if preceding == "BEARISH" and spring and spring["detected"]:
        key_event = "SPRING_TESTED" if spring["tested"] else "SPRING"
        confidence = "HIGH" if spring["tested"] else "MEDIUM"
        desc = (
            f"Accumulation Phase C — Spring {'tested' if spring['tested'] else 'confirmed'} "
            f"@ {spring['spring_low']}"
        )

        ob_confluence = False
        if ob_at_price and ob_at_price.get("direction") == "BULLISH":
            ob_lo = ob_at_price.get("ob_low", 0)
            ob_hi = ob_at_price.get("ob_high", 0)
            # Spring low must fall within or just below the Bullish OB
            if ob_lo * 0.997 <= spring["spring_low"] <= ob_hi * 1.003:
                ob_confluence = True
                desc += " at Bullish OB"

        return {
            **empty,
            "phase": "ACCUMULATION", "sub_phase": "C",
            "key_event": key_event, "confidence": confidence,
            "bias": "BULLISH", "description": desc,
            "spring": spring, "trading_range": tr,
            "ob_confluence": ob_confluence,
        }

    # ── DISTRIBUTION context (came from an uptrend) ───────────────────────
    if preceding == "BULLISH" and utad and utad["detected"]:
        key_event = "UTAD_TESTED" if utad["tested"] else "UTAD"
        confidence = "HIGH" if utad["tested"] else "MEDIUM"
        desc = (
            f"Distribution Phase C — UTAD {'tested' if utad['tested'] else 'confirmed'} "
            f"@ {utad['utad_high']}"
        )

        ob_confluence = False
        if ob_at_price and ob_at_price.get("direction") == "BEARISH":
            ob_lo = ob_at_price.get("ob_low", 0)
            ob_hi = ob_at_price.get("ob_high", 0)
            if ob_lo * 0.997 <= utad["utad_high"] <= ob_hi * 1.003:
                ob_confluence = True
                desc += " at Bearish OB"

        return {
            **empty,
            "phase": "DISTRIBUTION", "sub_phase": "C",
            "key_event": key_event, "confidence": confidence,
            "bias": "BEARISH", "description": desc,
            "utad": utad, "trading_range": tr,
            "ob_confluence": ob_confluence,
        }

    return empty
