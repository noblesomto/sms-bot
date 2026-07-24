import logging
import pandas as pd

logger = logging.getLogger(__name__)

# Minimum close displacement above/below a swing before it counts as a BOS.
# Filters 1-2 pip micro-breaks that generate noise OBs on 5min candles.
# 0.03% ≈ 3 pips EUR/USD · ~$0.70 XAU/USD · ~4.6 pips USD/JPY
MIN_BOS_PCT = 0.0003


def identify_swings(df: pd.DataFrame, lookback: int = 5) -> pd.DataFrame:
    """
    Detect swing highs and lows using a rolling window of ±lookback candles.

    A swing high: the candle's high is strictly the highest in the full window.
    A swing low:  the candle's low is strictly the lowest in the full window.

    Adds boolean columns swing_high and swing_low to the returned DataFrame.
    """
    df = df.copy()
    df["swing_high"] = False
    df["swing_low"] = False

    n = len(df)
    for i in range(lookback, n - lookback):
        window = df.iloc[i - lookback: i + lookback + 1]
        if df["high"].iloc[i] == window["high"].max():
            df.at[df.index[i], "swing_high"] = True
        if df["low"].iloc[i] == window["low"].min():
            df.at[df.index[i], "swing_low"] = True

    return df


def detect_bos(df: pd.DataFrame) -> list:
    """
    Detect Break of Structure (BOS) events across the candlestick series.

    Bullish BOS: a close above the most recent confirmed swing high.
    Bearish BOS: a close below the most recent confirmed swing low.
    The broken swing level is reset after each BOS to track the next one.

    Returns a list of BOS event dicts (type, direction, price, candle_index,
    broken_index, datetime).
    """
    events = []
    last_sh = last_sl = None
    last_sh_idx = last_sl_idx = None

    for i in range(len(df)):
        row = df.iloc[i]

        if row.get("swing_high", False):
            last_sh = row["high"]
            last_sh_idx = i
        if row.get("swing_low", False):
            last_sl = row["low"]
            last_sl_idx = i

        if last_sh is not None and i > last_sh_idx:
            if row["close"] > last_sh * (1 + MIN_BOS_PCT):
                events.append({
                    "type": "BOS",
                    "direction": "BULLISH",
                    "price": last_sh,
                    "candle_index": i,
                    "broken_index": last_sh_idx,
                    "datetime": row.get("datetime"),
                })
                last_sh = None

        if last_sl is not None and i > last_sl_idx:
            if row["close"] < last_sl * (1 - MIN_BOS_PCT):
                events.append({
                    "type": "BOS",
                    "direction": "BEARISH",
                    "price": last_sl,
                    "candle_index": i,
                    "broken_index": last_sl_idx,
                    "datetime": row.get("datetime"),
                })
                last_sl = None

    return events


def detect_choch(bos_events: list) -> list:
    """
    Identify Change of Character (CHoCH) events from a BOS sequence.

    In an established downtrend the first bullish BOS is a CHoCH (potential reversal).
    In an established uptrend the first bearish BOS is a CHoCH.
    Subsequent BOS events in the same direction are continuation signals, not CHoCH.
    """
    choch_events = []
    trend = None

    for bos in bos_events:
        if trend is None:
            trend = bos["direction"]
            continue
        if trend == "BEARISH" and bos["direction"] == "BULLISH":
            choch_events.append({**bos, "type": "CHoCH"})
            trend = "BULLISH"
        elif trend == "BULLISH" and bos["direction"] == "BEARISH":
            choch_events.append({**bos, "type": "CHoCH"})
            trend = "BEARISH"
        else:
            trend = bos["direction"]

    return choch_events


def get_trend_bias(df: pd.DataFrame) -> str:
    """
    Determine market bias from the sequence of swing highs and lows.

    HH + HL pattern → BULLISH.
    LH + LL pattern → BEARISH.
    Mixed or insufficient swings → RANGING.
    """
    highs = df[df["swing_high"] == True]["high"].tolist()
    lows = df[df["swing_low"] == True]["low"].tolist()

    if len(highs) < 2 or len(lows) < 2:
        return "RANGING"

    hh = highs[-1] > highs[-2]
    hl = lows[-1] > lows[-2]
    lh = highs[-1] < highs[-2]
    ll = lows[-1] < lows[-2]

    if hh and hl:
        return "BULLISH"
    if lh and ll:
        return "BEARISH"
    return "RANGING"


def analyze_structure(df: pd.DataFrame, lookback: int = 5) -> dict:
    """Run the full structure pipeline and return a consolidated result dict."""
    df = identify_swings(df, lookback)
    bos_events = detect_bos(df)
    choch_events = detect_choch(bos_events)
    bias = get_trend_bias(df)

    return {
        "df": df,
        "bos_events": bos_events,
        "choch_events": choch_events,
        "trend_bias": bias,
        "last_choch": choch_events[-1] if choch_events else None,
        "last_bos": bos_events[-1] if bos_events else None,
    }


def test_structure():
    """Standalone test on synthetic trending data (30 candles, lookback=3)."""
    # Uptrend then pullback to produce BOS + CHoCH
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
    result = analyze_structure(df, lookback=3)
    print(f"Trend bias: {result['trend_bias']}")
    print(f"BOS events: {len(result['bos_events'])}")
    print(f"CHoCH events: {len(result['choch_events'])}")
    if result["last_bos"]:
        b = result["last_bos"]
        print(f"Last BOS: {b['direction']} @ {b['price']} on candle {b['candle_index']}")
    return result


if __name__ == "__main__":
    test_structure()
