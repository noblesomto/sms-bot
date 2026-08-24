import pandas as pd
import pytest

from core.strategy import (
    _htf_momentum_conflicts,
    _RANGING_MOMENTUM_LOOKBACK,
    _RANGING_MOMENTUM_PCT,
    _filter_obs_by_age,
    _OB_MAX_AGE,
    _htf_bearish_reversal,
    _max_sl_distance,
    _sl_distance_floor,
    _current_atr,
    _SL_ATR_MULT,
    _min_rr_for,
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


# ── OB age filter (root cause: 2026-07-27 GBP/USD + USD/JPY zero-signal ────
# backtest — see docs/superpowers/backtests/2026-07-27-ob-max-age.md) ───────

def _ob(candle_index):
    return {"direction": "BULLISH", "ob_low": 1.0, "ob_high": 1.1,
            "candle_index": candle_index, "mitigated": False, "is_breaker": False}


def test_filter_obs_by_age_keeps_ob_within_new_threshold():
    # Age 150 bars: beyond the OLD 15min cap (96) but within the current one
    # (190) — this is exactly the OB population the 2026-07-27 backtest
    # showed being discarded needlessly (still unmitigated, just old).
    view_len = 200
    obs = [_ob(candle_index=view_len - 1 - 150)]
    kept = _filter_obs_by_age(obs, view_len, _OB_MAX_AGE["15min"])
    assert kept == obs


def test_filter_obs_by_age_drops_ob_beyond_threshold():
    view_len = 200
    obs = [_ob(candle_index=view_len - 1 - (_OB_MAX_AGE["15min"] + 1))]
    assert _filter_obs_by_age(obs, view_len, _OB_MAX_AGE["15min"]) == []


def test_filter_obs_by_age_boundary_is_inclusive():
    view_len = 200
    obs = [_ob(candle_index=view_len - 1 - _OB_MAX_AGE["15min"])]
    assert _filter_obs_by_age(obs, view_len, _OB_MAX_AGE["15min"]) == obs


# ── LONG-only HTF reversal guard (root cause: 2026-07-28 150d NAS100+US30 ──
# backtest — see docs/superpowers/backtests/2026-07-28-long-side-reversal-
# trace.md) ──────────────────────────────────────────────────────────────

def _htf_ohlc_df(closes: list) -> pd.DataFrame:
    n = len(closes)
    return pd.DataFrame({
        "open": closes, "high": [c + 1 for c in closes], "low": [c - 1 for c in closes],
        "close": closes, "volume": [1000] * n,
        "datetime": pd.date_range("2024-01-01", periods=n, freq="4h", tz="UTC"),
    })


def test_htf_bearish_reversal_true_when_last_bos_is_bearish():
    # A confirmed swing low (valley shape, trough at index 5) followed by a
    # sharp break below it — the exact "fresh reversal after a rally"
    # pattern found in 8/9 losing LONG trades in the 2026-07-28 backtest.
    closes = [150, 140, 130, 115, 105, 100, 110, 120, 135, 145, 150, 90]
    assert _htf_bearish_reversal(_htf_ohlc_df(closes))


def test_htf_bearish_reversal_false_when_last_bos_is_bullish():
    # Mirror shape: a confirmed swing high (peak at index 5) followed by a
    # break ABOVE it — the last confirmed structural break is BULLISH.
    closes = [100, 110, 120, 135, 145, 150, 140, 130, 115, 105, 100, 160]
    assert not _htf_bearish_reversal(_htf_ohlc_df(closes))


def test_htf_bearish_reversal_false_when_no_bos_yet():
    closes = [100.0, 100.5, 99.8, 100.2]
    assert not _htf_bearish_reversal(_htf_ohlc_df(closes))


def test_htf_bearish_reversal_fails_open_on_none_or_empty_df():
    assert not _htf_bearish_reversal(None)
    assert not _htf_bearish_reversal(pd.DataFrame())


# ── ATR-scaled fallback SL distance (root cause: 2026-07-28 NAS100 SHORT ───
# trace — 3 of 4 recorded NAS100 SHORT losses hit the fixed 50-index-point
# fallback while ATR(14) was 76-149 points, i.e. the stop was tighter than a
# single average candle's range) ───────────────────────────────────────────

def _atr_df(true_range: float, n: int = 20, base: float = 100.0) -> pd.DataFrame:
    """n candles with a constant true range and no gaps (close never moves),
    so ATR(14) == true_range exactly."""
    half = true_range / 2
    return pd.DataFrame({
        "open": [base] * n, "high": [base + half] * n, "low": [base - half] * n,
        "close": [base] * n,
    })


def test_current_atr_computes_constant_true_range():
    df = _atr_df(true_range=10.0)
    assert _current_atr(df) == pytest.approx(10.0)


def test_current_atr_returns_zero_on_insufficient_history():
    assert _current_atr(_atr_df(true_range=10.0, n=1)) == 0.0


def test_max_sl_distance_uses_atr_when_wider_than_floor():
    # NAS100 floor is 50.0; ATR(14)=200 * 1.5 mult = 300, well past the floor.
    df = _atr_df(true_range=200.0)
    assert _max_sl_distance("NAS100", 30000.0, df) == pytest.approx(200.0 * _SL_ATR_MULT)


def test_max_sl_distance_falls_back_to_floor_when_atr_below_floor():
    # ATR(14)=1 * 1.5 mult = 1.5, well under NAS100's 50.0 floor.
    df = _atr_df(true_range=1.0)
    assert _max_sl_distance("NAS100", 30000.0, df) == 50.0


def test_max_sl_distance_scoped_to_index_pairs_only():
    # 2026-07-28: all-pairs ATR-scaling collapsed XAU/USD's signal volume via
    # the R:R gate too, for no evidenced benefit there (only NAS100/US30
    # showed the underlying too-tight-fixed-SL problem) — scope the ATR
    # scaling to indices; every other pair keeps the pure fixed floor
    # regardless of its own ATR.
    high_atr_df = _atr_df(true_range=200.0)
    assert _max_sl_distance("US30", 50000.0, high_atr_df) == pytest.approx(200.0 * _SL_ATR_MULT)
    assert _max_sl_distance("XAU/USD", 4000.0, high_atr_df) == 8.0  # unaffected, pure floor
    assert _max_sl_distance("EUR/USD", 1.1, high_atr_df) == 0.0020  # unaffected, pure floor


def test_max_sl_distance_falls_back_to_floor_when_df_missing_or_too_short():
    assert _max_sl_distance("NAS100", 30000.0, None) == 50.0
    assert _max_sl_distance("NAS100", 30000.0, _atr_df(true_range=200.0, n=1)) == 50.0


def test_sl_distance_floor_matches_original_per_instrument_values():
    assert _sl_distance_floor("NAS100") == 50.0
    assert _sl_distance_floor("US30") == 80.0
    assert _sl_distance_floor("XAU/USD") == 8.0
    assert _sl_distance_floor("XAG/USD") == 0.40
    assert _sl_distance_floor("EUR/JPY") == 0.25
    assert _sl_distance_floor("EUR/USD") == 0.0020


# ── Lower min R:R for indices, testing whether pairing it with the ATR-────
# scaled SL recovers signal volume Filter 5 otherwise collapses (2026-07-28,
# still under evaluation — see docs/superpowers/backtests/2026-07-28-
# nas100-short-atr-sl.md) ───────────────────────────────────────────────

def test_min_rr_for_indices_returns_lower_threshold():
    assert _min_rr_for("NAS100") == 1.5
    assert _min_rr_for("US30") == 1.5


def test_min_rr_for_other_pairs_returns_default():
    assert _min_rr_for("XAU/USD") == 2.0
    assert _min_rr_for("EUR/USD") == 2.0
