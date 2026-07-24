import logging
import pandas as pd

from core.precision import price_precision

logger = logging.getLogger(__name__)


def get_premium_discount_zones(df: pd.DataFrame, lookback: int = 50) -> dict:
    """
    Calculate premium/discount zone from the most recent major HTF swing range.

    Price above the 50% equilibrium (EQ) = Premium zone → look for shorts.
    Price below EQ = Discount zone → look for longs.
    Fibonacci levels (0.705 OTE, 0.79, 0.886) mark optimal entry windows.
    """
    window = df.tail(lookback)
    raw_high = float(window["high"].max())
    prec = price_precision(raw_high)
    major_high = round(raw_high, prec)
    major_low = round(float(window["low"].min()), prec)
    equilibrium = round((major_high + major_low) / 2, prec)
    current_price = round(float(df["close"].iloc[-1]), prec)

    if current_price > equilibrium:
        zone = "PREMIUM"
    elif current_price < equilibrium:
        zone = "DISCOUNT"
    else:
        zone = "EQUILIBRIUM"

    return {
        "zone": zone,
        "equilibrium": equilibrium,
        "swing_high": major_high,
        "swing_low": major_low,
        "current_price": current_price,
        "fibonacci_levels": get_fibonacci_levels(major_high, major_low, prec),
    }


def get_fibonacci_levels(swing_high: float, swing_low: float, precision: int = 2) -> dict:
    """
    Return key Fibonacci retracement levels measured from swing_low to swing_high.

    0.5   — equilibrium (EQ)
    0.705 — Optimal Trade Entry (OTE) zone
    0.79  — deep retracement entry
    0.886 — extreme retracement / last defence
    """
    diff = swing_high - swing_low
    return {
        "0.5": round(swing_high - 0.500 * diff, precision),
        "0.705": round(swing_high - 0.705 * diff, precision),
        "0.79": round(swing_high - 0.790 * diff, precision),
        "0.886": round(swing_high - 0.886 * diff, precision),
    }


def is_in_correct_pd_zone(pd_zone: dict, direction: str) -> bool:
    """
    Return True when price is on the correct side of equilibrium for the signal.

    Longs should originate from DISCOUNT; shorts from PREMIUM.
    """
    zone = pd_zone.get("zone", "EQUILIBRIUM")
    return (direction == "LONG" and zone == "DISCOUNT") or (
        direction == "SHORT" and zone == "PREMIUM"
    )


def test_premium_discount():
    """Standalone test with synthetic XAUUSD price data."""
    data = {
        "open": [2300, 2310, 2320, 2315, 2330, 2325, 2340, 2335, 2350, 2345],
        "high": [2315, 2325, 2330, 2335, 2345, 2340, 2355, 2350, 2360, 2355],
        "low": [2295, 2305, 2315, 2310, 2325, 2320, 2335, 2330, 2345, 2340],
        "close": [2310, 2320, 2315, 2330, 2325, 2340, 2335, 2350, 2345, 2352],
        "volume": [1000] * 10,
        "datetime": pd.date_range("2024-01-01", periods=10, freq="1h", tz="UTC"),
    }
    df = pd.DataFrame(data)
    result = get_premium_discount_zones(df)
    print(f"Zone: {result['zone']}")
    print(
        f"EQ: {result['equilibrium']}  "
        f"High: {result['swing_high']}  "
        f"Low: {result['swing_low']}"
    )
    print(f"Fibonacci: {result['fibonacci_levels']}")
    return result


if __name__ == "__main__":
    test_premium_discount()
