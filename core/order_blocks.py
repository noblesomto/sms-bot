import logging
from typing import Optional
import pandas as pd

try:
    from config import settings
    from core.precision import price_precision
except ImportError:
    # Allow direct script invocation (./venv/bin/python3 core/order_blocks.py):
    # the script's own directory is on sys.path, not the project root, so the
    # top-level `config` and `core` packages aren't importable without this.
    import sys
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
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

    Fixture design (realistic continuous OHLC — each candle opens at/near the
    prior close, wicks contained, no unrealistic open/close gaps):
      idx 0-13:  a quiet base establishing ATR(14) baseline (~0.2-0.3 range/candle)
      idx 14:    a clear bearish candle — the Order Block candidate
      idx 15-17: a genuinely impulsive rally (~4-5x the base ATR per candle)
                 that both demonstrates displacement off the OB and forms the
                 swing high (idx 17)
      idx 18-21: a minor consolidation with lower highs than idx 17, needed so
                 identify_swings(lookback=3) confirms idx 17 as a swing high
      idx 22:    breakout candle that closes back above the idx-17 swing high,
                 triggering the BULLISH BOS
      idx 23-25: trailing candles for context
    """
    rows = [
        # Quiet base (indices 0-13): modest, continuous candles
        (100.0, 100.1, 99.9, 100.0),
        (100.0, 100.25, 99.9, 100.15),
        (100.15, 100.25, 99.95, 100.05),
        (100.05, 100.3, 99.95, 100.2),
        (100.2, 100.3, 100.0, 100.1),
        (100.1, 100.35, 100.0, 100.25),
        (100.25, 100.35, 100.05, 100.15),
        (100.15, 100.4, 100.05, 100.3),
        (100.3, 100.4, 100.1, 100.2),
        (100.2, 100.45, 100.1, 100.35),
        (100.35, 100.45, 100.15, 100.25),
        (100.25, 100.5, 100.15, 100.4),
        (100.4, 100.5, 100.2, 100.3),
        (100.3, 100.55, 100.2, 100.45),
        # idx 14: Order Block candidate — clear bearish candle
        (100.45, 100.5, 100.1, 100.15),
        # idx 15-17: impulsive breakout rally (creates the swing high at idx 17)
        (100.15, 101.25, 100.05, 101.15),
        (101.15, 102.35, 101.05, 102.25),
        (102.25, 103.25, 102.15, 103.15),
        # idx 18-21: minor consolidation, lower highs than idx 17
        (103.15, 103.18, 102.92, 102.95),
        (102.95, 103.08, 102.92, 103.05),
        (103.05, 103.08, 102.87, 102.9),
        (102.9, 102.98, 102.87, 102.95),
        # idx 22: breakout candle — closes above the idx-17 swing high -> BOS
        (102.95, 104.06, 102.85, 103.96),
        # idx 23-25: trailing context
        (103.96, 104.36, 103.86, 104.26),
        (104.26, 104.56, 104.16, 104.46),
        (104.46, 104.56, 104.26, 104.36),
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
