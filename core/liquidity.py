import logging
import pandas as pd

from core.precision import price_precision

logger = logging.getLogger(__name__)

EQH_EQL_TOLERANCE = 0.0005  # 0.05% tolerance for clustering equal highs/lows


def find_equal_highs_lows(df: pd.DataFrame) -> dict:
    """
    Locate Equal Highs (EQH) and Equal Lows (EQL) — clustered liquidity pools.

    EQH: 2+ swing highs within 0.05% of each other → buy-side liquidity resting above.
    EQL: 2+ swing lows  within 0.05% of each other → sell-side liquidity resting below.

    Requires swing_high / swing_low columns from identify_swings().
    """
    if "swing_high" not in df.columns:
        return {"eqh_levels": [], "eql_levels": []}

    highs = df[df["swing_high"] == True]["high"].tolist()
    lows = df[df["swing_low"] == True]["low"].tolist()

    return {
        "eqh_levels": _cluster_levels(highs),
        "eql_levels": _cluster_levels(lows),
    }


def _cluster_levels(prices: list) -> list:
    """Group price levels within EQH_EQL_TOLERANCE of each other; keep groups of ≥2."""
    if not prices:
        return []

    clusters = []
    used: set = set()

    for i, p in enumerate(prices):
        if i in used:
            continue
        group = [p]
        for j in range(i + 1, len(prices)):
            if j in used:
                continue
            if abs(p - prices[j]) / p <= EQH_EQL_TOLERANCE:
                group.append(prices[j])
                used.add(j)
        if len(group) >= 2:
            avg = sum(group) / len(group)
            clusters.append(round(avg, price_precision(avg)))
        used.add(i)

    return clusters


def get_previous_day_week_levels(df: pd.DataFrame) -> dict:
    """
    Extract Previous Day High/Low (PDH/PDL) and Previous Week High/Low (PWH/PWL).

    These are prime liquidity targets because institutional orders cluster there.
    Requires a datetime column with UTC-aware timestamps.
    """
    if df.empty or "datetime" not in df.columns:
        return {"pdh": None, "pdl": None, "pwh": None, "pwl": None}

    df = df.copy()
    df["_date"] = df["datetime"].dt.date
    iso = df["datetime"].dt.isocalendar()
    df["_year"] = iso.year.values
    df["_week"] = iso.week.values

    daily = df.groupby("_date").agg(high=("high", "max"), low=("low", "min"))
    weekly = df.groupby(["_year", "_week"]).agg(high=("high", "max"), low=("low", "min"))

    pdh = pdl = pwh = pwl = None

    if len(daily) >= 2:
        prev = daily.iloc[-2]
        prec = price_precision(float(prev["high"]))
        pdh, pdl = round(float(prev["high"]), prec), round(float(prev["low"]), prec)

    if len(weekly) >= 2:
        prev = weekly.iloc[-2]
        prec = price_precision(float(prev["high"]))
        pwh, pwl = round(float(prev["high"]), prec), round(float(prev["low"]), prec)

    return {"pdh": pdh, "pdl": pdl, "pwh": pwh, "pwl": pwl}


def detect_liquidity_sweeps(df: pd.DataFrame, levels: dict) -> list:
    """
    Detect liquidity sweeps in the last 10 candles.

    Buy-side sweep:  wick above EQH/PDH/PWH then close back below → bullish reversal signal.
    Sell-side sweep: wick below EQL/PDL/PWL then close back above → bearish reversal signal.

    A sweep followed by a reversal is a high-probability SMC entry trigger.
    """
    sweeps = []
    recent = df.iloc[-10:]

    buy_targets = (
        [(lv, "EQH") for lv in levels.get("eqh_levels", [])]
        + ([(levels["pdh"], "PDH")] if levels.get("pdh") else [])
        + ([(levels["pwh"], "PWH")] if levels.get("pwh") else [])
    )
    sell_targets = (
        [(lv, "EQL") for lv in levels.get("eql_levels", [])]
        + ([(levels["pdl"], "PDL")] if levels.get("pdl") else [])
        + ([(levels["pwl"], "PWL")] if levels.get("pwl") else [])
    )

    for _, row in recent.iterrows():
        hi = float(row["high"])
        lo = float(row["low"])
        cl = float(row["close"])

        for level, label in buy_targets:
            if hi > level and cl < level:
                # Buy-side (resistance) liquidity swept and rejected → bearish reversal expected
                sweeps.append({
                    "type": "BUY_SIDE_SWEPT",
                    "level": level,
                    "level_type": label,
                    "datetime": row.get("datetime"),
                    "direction": "BEARISH",
                })

        for level, label in sell_targets:
            if lo < level and cl > level:
                # Sell-side (support) liquidity swept and rejected → bullish reversal expected
                sweeps.append({
                    "type": "SELL_SIDE_SWEPT",
                    "level": level,
                    "level_type": label,
                    "datetime": row.get("datetime"),
                    "direction": "BULLISH",
                })

    return sweeps


def test_liquidity():
    """Standalone test — multi-day data ensures PDH/PDL and repeated swing levels for EQH/EQL."""
    # Build 3 days of 8 candles each; equal swing highs around 113 cluster as EQH
    highs  = [103,105,107,106,108,110,109,111,  113,112,113,111,109,113,112,110,  113,114,116,115,117,119,118,120]
    lows   = [99, 100,102,101,103,105,104,106,  108,107,108,106,104,108,107,105,  109,110,112,111,113,115,114,116]
    opens  = [100,102,104,103,105,107,106,108,  110,111,110,109,107,109,110,109,  111,112,114,113,115,117,116,118]
    closes = [102,104,103,105,107,106,108,110,  111,110,111,109,107,111,109,107,  112,113,115,114,116,118,117,119]
    n = len(closes)
    data = {
        "open": opens, "high": highs, "low": lows, "close": closes,
        "volume": [1000] * n,
        "datetime": pd.date_range("2024-01-15 00:00", periods=n, freq="3h", tz="UTC"),
    }
    df = pd.DataFrame(data)

    from core.structure import identify_swings
    df = identify_swings(df, lookback=3)

    eq = find_equal_highs_lows(df)
    pd_levels = get_previous_day_week_levels(df)
    all_levels = {**eq, **pd_levels}
    sweeps = detect_liquidity_sweeps(df, all_levels)

    print(f"EQH: {eq['eqh_levels']}  EQL: {eq['eql_levels']}")
    print(f"PDH: {pd_levels['pdh']}  PDL: {pd_levels['pdl']}")
    print(f"Liquidity sweeps: {len(sweeps)}")
    return all_levels, sweeps


if __name__ == "__main__":
    test_liquidity()
