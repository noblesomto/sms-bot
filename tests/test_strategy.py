import pandas as pd

from core.strategy import (
    _htf_momentum_conflicts,
    _RANGING_MOMENTUM_LOOKBACK,
    _RANGING_MOMENTUM_PCT,
)


def _htf_df(closes: list) -> pd.DataFrame:
    return pd.DataFrame({"close": closes})


def test_momentum_conflicts_blocks_long_in_hidden_downtrend():
    # Net move well past the threshold, missed by the 2-swing bias classifier
    # (which read RANGING) — the exact failure mode from the 2026-07-27
    # NAS100 backtest finding.
    start = 100.0
    end = start * (1 - _RANGING_MOMENTUM_PCT - 0.01)
    closes = [start] + [start] * (_RANGING_MOMENTUM_LOOKBACK - 1) + [end]
    assert _htf_momentum_conflicts("LONG", _htf_df(closes))


def test_momentum_conflicts_blocks_short_in_hidden_uptrend():
    start = 100.0
    end = start * (1 + _RANGING_MOMENTUM_PCT + 0.01)
    closes = [start] + [start] * (_RANGING_MOMENTUM_LOOKBACK - 1) + [end]
    assert _htf_momentum_conflicts("SHORT", _htf_df(closes))


def test_momentum_allows_long_when_move_within_threshold():
    # A genuine basing/accumulation range should not trip the veto —
    # Wyckoff Springs are supposed to fire inside exactly this condition.
    start = 100.0
    end = start * (1 - _RANGING_MOMENTUM_PCT + 0.005)
    closes = [start] + [start] * (_RANGING_MOMENTUM_LOOKBACK - 1) + [end]
    assert not _htf_momentum_conflicts("LONG", _htf_df(closes))


def test_momentum_allows_long_when_trend_agrees():
    # Net move exceeds the threshold but in LONG's favor — not a conflict.
    start = 100.0
    end = start * (1 + _RANGING_MOMENTUM_PCT + 0.01)
    closes = [start] + [start] * (_RANGING_MOMENTUM_LOOKBACK - 1) + [end]
    assert not _htf_momentum_conflicts("LONG", _htf_df(closes))


def test_momentum_fails_open_on_insufficient_history():
    closes = [100.0, 99.0]
    assert not _htf_momentum_conflicts("LONG", _htf_df(closes))


def test_momentum_fails_open_on_none_df():
    assert not _htf_momentum_conflicts("LONG", None)
