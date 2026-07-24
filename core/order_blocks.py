import logging
from typing import Optional
import pandas as pd

from config import settings
from core.precision import price_precision

logger = logging.getLogger(__name__)

MAX_ACTIVE_OBS = 5  # Maximum unmitigated OBs to track per direction


def _has_displacement(df: pd.DataFrame, ob_idx: int, direction: str) -> bool:
    """ICT displacement gate: an OB is only tradeable if the move leaving it
    was impulsive — within DISPLACEMENT_WINDOW candles after the OB, price
    travels ≥ DISPLACEMENT_ATR_MULT × ATR(14 at formation), or the follow-
    through leaves an FVG. Weak zones without displacement are discarded
    (spec 2026-07-24 §2.4)."""
    window = settings.DISPLACEMENT_WINDOW
    seg = df.iloc[ob_idx + 1: ob_idx + 1 + window]
    if seg.empty:
        return False

    hist = df.iloc[max(0, ob_idx - 14): ob_idx + 1]
    prev_close = hist["close"].shift(1)
    tr = pd.concat([hist["high"] - hist["low"],
                    (hist["high"] - prev_close).abs(),
                    (hist["low"] - prev_close).abs()], axis=1).max(axis=1)
    atr = float(tr.mean())
    ob_close = float(df.iloc[ob_idx]["close"])

    if direction == "BULLISH":
        travel = float(seg["high"].max()) - ob_close
    else:
        travel = ob_close - float(seg["low"].min())
    if atr > 0 and travel >= settings.DISPLACEMENT_ATR_MULT * atr:
        return True

    # FVG in the follow-through: 3-bar imbalance among candles ob_idx..ob_idx+window
    for j in range(ob_idx + 1, min(ob_idx + window, len(df) - 1)):
        if direction == "BULLISH" and float(df.iloc[j + 1]["low"]) > float(df.iloc[j - 1]["high"]):
            return True
        if direction == "BEARISH" and float(df.iloc[j + 1]["high"]) < float(df.iloc[j - 1]["low"]):
            return True
    return False


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
                    if _has_displacement(df, i, "BULLISH"):
                        obs.append(_make_ob(candle, i, "BULLISH"))
                    break

        elif direction == "BEARISH":
            for i in range(broken_idx, search_start - 1, -1):
                candle = df.iloc[i]
                if float(candle["close"]) > float(candle["open"]):  # bullish candle
                    if _has_displacement(df, i, "BEARISH"):
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
    """Standalone test — simplified OB + displacement demonstration.
    Note: test_displacement.py provides comprehensive gate testing.
    This self-test verifies basic OB structure still works with the gate.
    """
    # Pattern: Uptrend → Bearish OB → Strong impulsive recovery → Trend
    # The impulsive recovery after the OB demonstrates displacement.
    # The detailed gate logic is tested in test_displacement.py (5 test cases).
    rows = [
        # Rising trend with pullback to create OB candidate (indices 0-8)
        (100, 102, 100, 101),        # idx 0: up
        (101, 103, 101, 102),        # idx 1: up (swing high potential)
        (102, 104, 101, 102.5),      # idx 2: up
        (102.5, 104.5, 102, 103),    # idx 3: up (swing high potential)
        (103, 105, 102.5, 103.5),    # idx 4: up
        (103.5, 105.5, 103, 104),    # idx 5: up (swing high potential)
        (104, 106, 103.5, 104.5),    # idx 6: up
        (104.5, 106.5, 104, 105),    # idx 7: up (swing high = 106.5)
        (105, 105, 103, 104),        # idx 8: pullback, bullish doji
        # Bearish OB and impulsive recovery (indices 9-11)
        (104, 104.5, 102.5, 102.8),  # idx 9: bearish (open 104 > close 102.8)
        (102.8, 107, 102.8, 106.5),  # idx 10: strong impulse
        (106.5, 109, 106.5, 108.5),  # idx 11: strong impulse
        # More data for structure detection and BOS confirmation (indices 12-29)
        (108.5, 110, 108, 109),
        (109, 111, 108.5, 110),
        (110, 112, 109.5, 111),
        (111, 113, 110.5, 112),
        (112, 114, 111.5, 113),
        (113, 115, 112.5, 114),
        (114, 116, 113.5, 115),
        (115, 117, 114.5, 116),
        (116, 118, 115.5, 117),
        (117, 119, 116.5, 118),
        (118, 120, 117.5, 119),
        (119, 121, 118.5, 120),
        (120, 122, 119.5, 121),
        (121, 123, 120.5, 122),
        (122, 124, 121.5, 123),
        (123, 125, 122.5, 124),
    ]
    highs = [r[1] for r in rows]
    lows = [r[2] for r in rows]
    opens = [r[0] for r in rows]
    closes = [r[3] for r in rows]
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
