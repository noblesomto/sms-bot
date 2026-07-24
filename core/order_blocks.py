import logging
from typing import Optional
import pandas as pd

from core.precision import price_precision

logger = logging.getLogger(__name__)

MAX_ACTIVE_OBS = 5  # Maximum unmitigated OBs to track per direction


def find_order_blocks(df: pd.DataFrame, bos_events: list) -> list:
    """
    Identify Order Blocks (OBs) from BOS impulses.

    Bullish OB: the last bearish (red) candle immediately before the bullish
                impulse that broke the swing high (BOS).
    Bearish OB: the last bullish (green) candle immediately before the bearish
                impulse that broke the swing low (BOS).

    Returns a list of OB dicts: direction, ob_high, ob_low, ob_mid,
    candle_index, datetime, mitigated, is_breaker.
    """
    obs = []

    for bos in bos_events:
        direction = bos["direction"]
        broken_idx = bos.get("broken_index", 0)
        search_start = max(0, broken_idx - 10)

        if direction == "BULLISH":
            for i in range(broken_idx, search_start - 1, -1):
                candle = df.iloc[i]
                if float(candle["close"]) < float(candle["open"]):  # bearish candle
                    obs.append(_make_ob(candle, i, "BULLISH"))
                    break

        elif direction == "BEARISH":
            for i in range(broken_idx, search_start - 1, -1):
                candle = df.iloc[i]
                if float(candle["close"]) > float(candle["open"]):  # bullish candle
                    obs.append(_make_ob(candle, i, "BEARISH"))
                    break

    return obs


def _make_ob(candle: pd.Series, idx: int, direction: str) -> dict:
    prec = price_precision(float(candle["high"]))
    high = round(float(candle["high"]), prec)
    low = round(float(candle["low"]), prec)
    return {
        "direction": direction,
        "ob_high": high,
        "ob_low": low,
        "ob_mid": round((high + low) / 2, prec),
        "candle_index": idx,
        "datetime": candle.get("datetime"),
        "mitigated": False,
        "is_breaker": False,
    }


def validate_obs(df: pd.DataFrame, obs: list) -> list:
    """
    Update OB status and enforce the MAX_ACTIVE_OBS limit per direction.

    Mitigation rule (ICT standard): an OB is fully mitigated when ANY close
    after formation goes OUTSIDE the zone entirely —
      Bullish OB: close < ob_low   (price left the zone through the bottom)
      Bearish OB: close > ob_high  (price left the zone through the top)

    Previous midpoint check was too aggressive — volatile candles that wick
    through the OB midpoint but close inside the zone are normal price action
    inside the OB, not a breach. Midpoint crossing wiped out every OB on all
    pairs because intrabar wicks routinely reach the 50% level.

    A truly mitigated OB becomes a Breaker Block (bias flips).
    Returns only unmitigated OBs (up to MAX_ACTIVE_OBS each) plus all breakers.
    """
    closes = df["close"].values  # numpy array for fast slice checks

    for ob in obs:
        if ob["mitigated"]:
            continue
        ob_lo = ob["ob_low"]
        ob_hi = ob["ob_high"]
        ob_idx = ob.get("candle_index", 0)
        # All closes strictly AFTER the OB candle
        closes_after = closes[ob_idx + 1:] if ob_idx + 1 < len(closes) else closes[-1:]

        if ob["direction"] == "BULLISH" and (closes_after < ob_lo).any():
            ob["mitigated"] = True
            ob["is_breaker"] = True
            ob["direction"] = "BEARISH"
        elif ob["direction"] == "BEARISH" and (closes_after > ob_hi).any():
            ob["mitigated"] = True
            ob["is_breaker"] = True
            ob["direction"] = "BULLISH"

    active_bull = [o for o in obs if not o["mitigated"] and o["direction"] == "BULLISH"]
    active_bear = [o for o in obs if not o["mitigated"] and o["direction"] == "BEARISH"]
    breakers = [o for o in obs if o["is_breaker"]]

    return active_bull[-MAX_ACTIVE_OBS:] + active_bear[-MAX_ACTIVE_OBS:] + breakers


def get_price_at_ob(obs: list, current_price: float) -> Optional[dict]:
    """Return the unmitigated OB whose range contains current_price, or None."""
    for ob in obs:
        if ob["mitigated"] or ob["is_breaker"]:
            continue
        if ob["ob_low"] <= current_price <= ob["ob_high"]:
            return ob
    return None


def test_order_blocks():
    """Standalone test — 30-candle uptrend produces a bullish BOS and OB."""
    highs = [103,104,106,107,109,111,112,114,115,118,116,114,115,117,119,121,120,118,119,122,121,119,120,123,125,124,122,123,126,128]
    lows  = [99, 100,100,103,102,106,104,108,106,110,108,106,107,109,111,113,111,109,110,113,112,110,111,114,116,114,112,113,116,118]
    opens = [100,102,101,105,103,108,106,110,107,112,114,112,109,111,113,115,118,116,113,115,118,118,115,116,119,122,121,118,119,122]
    closes= [102,101,105,103,108,106,110,107,112,116,113,110,112,114,116,118,117,115,116,119,118,116,117,120,122,121,119,120,123,126]
    n = len(closes)
    data = {
        "open": opens, "high": highs, "low": lows, "close": closes,
        "volume": [1000] * n,
        "datetime": pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC"),
    }
    df = pd.DataFrame(data)

    from core.structure import identify_swings, detect_bos
    df = identify_swings(df, lookback=3)
    bos_events = detect_bos(df)

    obs = find_order_blocks(df, bos_events)
    obs = validate_obs(df, obs)
    print(f"Found {len(obs)} order block(s)  (from {len(bos_events)} BOS event(s))")
    for ob in obs:
        tag = " [BREAKER]" if ob["is_breaker"] else ""
        print(f"  {ob['direction']} OB: {ob['ob_low']} – {ob['ob_high']}{tag}")
    return obs


if __name__ == "__main__":
    test_order_blocks()
