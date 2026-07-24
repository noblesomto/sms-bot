from datetime import datetime, timedelta, timezone

import pandas as pd

from scheduler import _resolve_outcome, _sweep_outcome, _sweep_window

T1, T2, SL = 4088.75, 4112.5, 4021.57

def test_long_sl_wick_close_inside():
    # wick pierces SL, closes back above — broker fills, close-based logic missed this
    assert _resolve_outcome("LONG", "ACTIVE", high=4035.0, low=4020.0,
                            target1=T1, target2=T2, invalidation=SL) == ("SL", SL)

def test_long_tp1_wick():
    assert _resolve_outcome("LONG", "ACTIVE", high=4090.0, low=4050.0,
                            target1=T1, target2=T2, invalidation=SL) == ("TP1", T1)

def test_long_tp2_beats_tp1():
    assert _resolve_outcome("LONG", "ACTIVE", high=4115.0, low=4050.0,
                            target1=T1, target2=T2, invalidation=SL) == ("TP2", T2)

def test_same_candle_sl_precedence():
    # both SL and TP touched in one bar → conservative SL
    assert _resolve_outcome("LONG", "ACTIVE", high=4115.0, low=4020.0,
                            target1=T1, target2=T2, invalidation=SL) == ("SL", SL)

def test_no_touch_returns_none():
    assert _resolve_outcome("LONG", "ACTIVE", high=4050.0, low=4030.0,
                            target1=T1, target2=T2, invalidation=SL) is None

def test_tp1_hit_state_sl_after_tp1():
    assert _resolve_outcome("LONG", "TP1_HIT", high=4095.0, low=4020.0,
                            target1=T1, target2=T2, invalidation=SL) == ("SL_AFTER_TP1", SL)

def test_short_mirror():
    # SHORT: SL above, targets below
    assert _resolve_outcome("SHORT", "ACTIVE", high=4044.0, low=4035.0,
                            target1=3993.25, target2=3990.4, invalidation=4043.7) == ("SL", 4043.7)
    assert _resolve_outcome("SHORT", "ACTIVE", high=4040.0, low=3990.0,
                            target1=3993.25, target2=3990.4, invalidation=4043.7) == ("TP2", 3990.4)

def test_none_targets_ignored():
    assert _resolve_outcome("LONG", "ACTIVE", high=4090.0, low=4050.0,
                            target1=None, target2=None, invalidation=SL) is None


def test_sweep_catches_older_candle_sl_when_latest_is_quiet():
    # Candle 1 wicks through SL; candles 2-3 are quiet — checking only the
    # last candle (the old behavior) would have missed the SL entirely.
    candles = [
        (4035.0, 4020.0),   # SL wick here
        (4050.0, 4045.0),   # quiet
        (4055.0, 4048.0),   # quiet (this is "the last candle")
    ]
    assert _sweep_outcome("LONG", "ACTIVE", candles,
                          target1=T1, target2=T2, invalidation=SL) == ("SL", SL)


def test_sweep_first_outcome_wins_sl_before_later_tp():
    # SL wicked in candle 2 (index 1); candle 4 (index 3) later touches TP1.
    # Oldest-to-newest scanning must stop at the SL and never reach the TP.
    candles = [
        (4050.0, 4045.0),   # candle 1: quiet
        (4035.0, 4020.0),   # candle 2: SL hit
        (4055.0, 4048.0),   # candle 3: quiet
        (4090.0, 4060.0),   # candle 4: would be TP1 if reached
    ]
    assert _sweep_outcome("LONG", "ACTIVE", candles,
                          target1=T1, target2=T2, invalidation=SL) == ("SL", SL)


def test_sweep_no_touch_returns_none():
    candles = [(4050.0, 4045.0), (4055.0, 4048.0)]
    assert _sweep_outcome("LONG", "ACTIVE", candles,
                          target1=T1, target2=T2, invalidation=SL) is None


def test_sweep_empty_candles_returns_none():
    assert _sweep_outcome("LONG", "ACTIVE", [], target1=T1, target2=T2, invalidation=SL) is None


def _make_1h_df():
    # Candle opens on the hour: N-2, N-1, N (still forming), each 1h wide.
    opens = [
        datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc),
        datetime(2026, 7, 24, 11, 0, tzinfo=timezone.utc),
        datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc),
    ]
    return pd.DataFrame({"datetime": opens, "high": [1, 2, 3], "low": [1, 2, 3]})


def test_sweep_window_includes_straddling_candle():
    # start_ts is 20 minutes into candle N (opened 12:00) — that candle's
    # close (13:00) is still after start_ts, so it must be included even
    # though its open (12:00) is before start_ts.
    df = _make_1h_df()
    start_ts = datetime(2026, 7, 24, 12, 20, tzinfo=timezone.utc)
    window = _sweep_window(df, start_ts, "1h")
    assert list(window["datetime"]) == [datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)]


def test_sweep_window_excludes_fully_closed_candles():
    # Candles N-2 (10:00) and N-1 (11:00) both closed (11:00, 12:00) before
    # start_ts (12:20) and must be excluded; only candle N remains.
    df = _make_1h_df()
    start_ts = datetime(2026, 7, 24, 12, 20, tzinfo=timezone.utc)
    window = _sweep_window(df, start_ts, "1h")
    assert datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc) not in list(window["datetime"])
    assert datetime(2026, 7, 24, 11, 0, tzinfo=timezone.utc) not in list(window["datetime"])


def test_sweep_window_none_start_ts_returns_full_df():
    df = _make_1h_df()
    window = _sweep_window(df, None, "1h")
    assert len(window) == len(df)
