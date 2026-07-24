import logging
from typing import Optional
import pandas as pd

from core.precision import price_precision

logger = logging.getLogger(__name__)

MIN_GAP_PCT = 0.001  # 0.1% of price — filters noise from structurally meaningful gaps


def scan_fvgs(df: pd.DataFrame) -> list:
    """
    Scan the candlestick series for Fair Value Gaps (FVGs / imbalances).

    Bullish FVG: candle[i-1].high < candle[i+1].low  (unfilled space above).
    Bearish FVG: candle[i-1].low  > candle[i+1].high (unfilled space below).
    Gaps smaller than MIN_GAP_PCT of mid price are ignored as noise.

    Returns a list of FVG dicts with fvg_low, fvg_high, fvg_mid, direction,
    candle_index, datetime, status, is_inverse.
    """
    fvgs = []

    for i in range(1, len(df) - 1):
        prev, curr, nxt = df.iloc[i - 1], df.iloc[i], df.iloc[i + 1]
        mid = float(curr["close"])

        # Bullish FVG
        if prev["high"] < nxt["low"]:
            gap = float(nxt["low"] - prev["high"])
            if gap / mid >= MIN_GAP_PCT:
                prec = price_precision(mid)
                fvgs.append({
                    "direction": "BULLISH",
                    "fvg_low": round(float(prev["high"]), prec),
                    "fvg_high": round(float(nxt["low"]), prec),
                    "fvg_mid": round((float(prev["high"]) + float(nxt["low"])) / 2, prec),
                    "candle_index": i,
                    "datetime": curr.get("datetime"),
                    "status": "FRESH",
                    "is_inverse": False,
                })

        # Bearish FVG
        elif prev["low"] > nxt["high"]:
            gap = float(prev["low"] - nxt["high"])
            if gap / mid >= MIN_GAP_PCT:
                prec = price_precision(mid)
                fvgs.append({
                    "direction": "BEARISH",
                    "fvg_high": round(float(prev["low"]), prec),
                    "fvg_low": round(float(nxt["high"]), prec),
                    "fvg_mid": round((float(prev["low"]) + float(nxt["high"])) / 2, prec),
                    "candle_index": i,
                    "datetime": curr.get("datetime"),
                    "status": "FRESH",
                    "is_inverse": False,
                })

    return fvgs


def update_fvg_status(df: pd.DataFrame, fvgs: list) -> list:
    """
    Determine each FVG's fill status by walking every candle from the gap's
    formation through to the latest candle — not just the most recent one.
    scan_fvgs() re-detects the same historical gaps on every scan with status
    reset to FRESH, so checking only the last candle (the previous approach)
    missed any fill that happened between formation and now: a gap violated
    and inverted days ago would still report FRESH today just because the
    current candle doesn't happen to touch it.

    Fresh       → price has never entered the gap.
    Partially   → price entered but never closed through.
    Filled      → price closed through at some point; the gap flips to an
                  Inverse FVG and now acts as resistance (was bullish) or
                  support (was bearish).
    """
    last_idx = len(df) - 1
    active = []

    for fvg in fvgs:
        lo, hi = fvg["fvg_low"], fvg["fvg_high"]
        status = "FRESH"

        for j in range(fvg["candle_index"] + 1, last_idx + 1):
            row = df.iloc[j]
            c, l, h = float(row["close"]), float(row["low"]), float(row["high"])

            if fvg["direction"] == "BULLISH":
                if c < lo:
                    status = "FILLED"
                    break
                elif l < hi and status == "FRESH":
                    status = "PARTIALLY_FILLED"
            else:
                if c > hi:
                    status = "FILLED"
                    break
                elif h > lo and status == "FRESH":
                    status = "PARTIALLY_FILLED"

        fvg["status"] = status
        fvg["is_inverse"] = (status == "FILLED")
        active.append(fvg)

    return active


def get_fvg_at_price(fvgs: list, price: float) -> Optional[dict]:
    """Return the first unfilled FVG whose range contains the given price."""
    for fvg in fvgs:
        if fvg["status"] == "FILLED":
            continue
        if fvg["fvg_low"] <= price <= fvg["fvg_high"]:
            return fvg
    return None


def test_fvg():
    """Standalone test using synthetic data with an obvious bullish gap."""
    data = {
        "open": [100, 102, 107, 106, 108],
        "high": [103, 106, 110, 109, 112],
        "low": [99, 101, 106, 105, 107],
        "close": [102, 105, 108, 107, 111],
        "volume": [1000] * 5,
        "datetime": pd.date_range("2024-01-01", periods=5, freq="1h", tz="UTC"),
    }
    df = pd.DataFrame(data)
    fvgs = scan_fvgs(df)
    fvgs = update_fvg_status(df, fvgs)
    print(f"Found {len(fvgs)} FVG(s)")
    for fvg in fvgs:
        print(f"  {fvg['direction']} FVG: {fvg['fvg_low']} – {fvg['fvg_high']}  [{fvg['status']}]")
    return fvgs


def test_fvg_stale_fill_regression():
    """
    Regression test for the stale-fill bug: a gap that was fully violated
    mid-history, with price now far away and not touching it on the latest
    candle, must still report FILLED — not FRESH just because the last
    candle alone doesn't overlap the gap.
    """
    data = {
        "open":  [100, 102, 110, 110, 112],
        "high":  [103, 106, 112, 111, 116],
        "low":   [99,  101, 110, 90,  111],
        "close": [102, 105, 111, 95,  114],
        "volume": [1000] * 5,
        "datetime": pd.date_range("2024-01-01", periods=5, freq="1h", tz="UTC"),
    }
    df = pd.DataFrame(data)
    fvgs = scan_fvgs(df)
    fvgs = update_fvg_status(df, fvgs)

    assert len(fvgs) == 1, f"expected 1 FVG, got {len(fvgs)}"
    fvg = fvgs[0]
    assert fvg["direction"] == "BULLISH"
    assert fvg["status"] == "FILLED", f"expected FILLED, got {fvg['status']}"
    assert fvg["is_inverse"] is True

    print("test_fvg_stale_fill_regression: PASS "
          f"(gap {fvg['fvg_low']}-{fvg['fvg_high']} correctly detected as FILLED "
          "despite last candle not touching it)")


if __name__ == "__main__":
    test_fvg()
    test_fvg_stale_fill_regression()
