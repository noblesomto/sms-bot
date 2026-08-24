"""Tests for the 2026-08-24 profitability roadmap changes:

- SHORT-only evaluation gate (_direction_allowed / ENABLE_LONG)
- HTF regime tagging (_htf_regime_of)
- Spread-netted PnL and R-multiple bookkeeping (_net_pips / _pnl_r)
- Kill-switch decision rule (_should_kill_switch)
"""
import pandas as pd
import pytest

from config import get_spread_pips, settings
from alerts.formatter import format_kill_switch_alert
from core.strategy import _direction_allowed, _htf_regime_of
from scheduler import _net_pips, _pnl_r, _should_kill_switch


# ── Phase 1: SHORT-only evaluation mode ──────────────────────────────────

def test_direction_allowed_blocks_long_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_LONG", False)
    assert not _direction_allowed("LONG")


def test_direction_allowed_keeps_short_when_long_disabled(monkeypatch):
    # SHORT-only mode must not touch SHORT candidates — that's the whole point.
    monkeypatch.setattr(settings, "ENABLE_LONG", False)
    assert _direction_allowed("SHORT")


def test_direction_allowed_keeps_both_when_enabled(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_LONG", True)
    assert _direction_allowed("LONG")
    assert _direction_allowed("SHORT")


# ── Phase 2 item 6: HTF regime tag ───────────────────────────────────────

def _closes_df(closes: list) -> pd.DataFrame:
    return pd.DataFrame({"close": closes})


def test_regime_up_when_net_move_past_threshold():
    closes = [100.0] * 20 + [103.0]   # +3% over the window, threshold 2%
    assert _htf_regime_of(_closes_df(closes)) == "UP"


def test_regime_down_when_net_move_past_threshold():
    closes = [100.0] * 20 + [97.0]    # −3%
    assert _htf_regime_of(_closes_df(closes)) == "DOWN"


def test_regime_flat_within_threshold():
    # ±2% band is inclusive: exactly +2% still counts as UP (>=), so sit inside it
    closes = [100.0] * 20 + [101.0]   # +1%
    assert _htf_regime_of(_closes_df(closes)) == "FLAT"


def test_regime_uses_only_trailing_window():
    # A big old move outside the 21-bar window must not leak into the label:
    # early bars triple, then the trailing window is dead flat.
    closes = [100.0, 300.0] + [300.0] * 25
    assert _htf_regime_of(_closes_df(closes)) == "FLAT"


def test_regime_unknown_without_history():
    assert _htf_regime_of(None) == "UNKNOWN"
    assert _htf_regime_of(pd.DataFrame()) == "UNKNOWN"
    assert _htf_regime_of(_closes_df([100.0])) == "UNKNOWN"


# ── Phase 2 items 4–5: spread netting + R-multiples ──────────────────────

def test_spread_defaults_are_positive_and_known_for_core_pairs():
    for pair in ("EUR/USD", "XAU/USD", "US30", "NAS100"):
        assert get_spread_pips(pair) > 0


def test_net_pips_deducts_spread():
    gross = 10.0
    net = _net_pips("XAU/USD", gross)
    assert net == pytest.approx(round(gross - get_spread_pips("XAU/USD"), 1))
    assert net < gross


def test_pnl_r_divides_by_risk_distance_in_pair_units():
    # EUR/USD LONG: entry 1.10000, SL 1.09800 → risk = 20 forex pips.
    # A 40-pip win is exactly +2.0R.
    assert _pnl_r("EUR/USD", 40.0, 1.10000, 1.09800) == pytest.approx(2.0)


def test_pnl_r_negative_loss_gives_negative_r():
    # XAU/USD SHORT: entry 4000.0, SL above at 4010.0 → risk = 100 gold pips.
    # A full stop-out (−100 pips) is −1.0R regardless of sign conventions.
    assert _pnl_r("XAU/USD", -100.0, 4000.0, 4010.0) == pytest.approx(-1.0)


def test_pnl_r_returns_none_without_usable_stop():
    # Pre-cutover rows and fallback entries without invalidation must stay
    # NULL rather than divide by zero or invent a denominator.
    assert _pnl_r("EUR/USD", 10.0, None, 1.09800) is None
    assert _pnl_r("EUR/USD", 10.0, 1.10000, None) is None
    assert _pnl_r("EUR/USD", None, 1.10000, 1.09800) is None
    assert _pnl_r("EUR/USD", 10.0, 1.10000, 1.10000) is None  # zero-risk guard


# ── Phase 2½: kill-switch decision rule ──────────────────────────────────

def test_kill_switch_never_trips_below_minimum_sample():
    # One catastrophic trade among three must not fire a circuit breaker.
    assert not _should_kill_switch([-3.0, -0.5, 0.5], min_n=20, threshold=-0.5)


def test_kill_switch_fires_when_mean_at_threshold():
    r_values = [-1.0] * 10 + [0.5] * 10          # mean exactly −0.25R… adjust to boundary
    r_values = [-1.0] * 15 + [0.5] * 5           # mean = −0.5R exactly
    assert _should_kill_switch(r_values, min_n=20, threshold=-0.5)


def test_kill_switch_fires_when_mean_below_threshold():
    r_values = [-1.0] * 18 + [1.0] * 2           # mean = −0.7R
    assert _should_kill_switch(r_values, min_n=20, threshold=-0.5)


def test_kill_switch_stays_silent_when_expectancy_positive():
    r_values = [-1.0] * 5 + [1.0] * 15           # mean = +0.5R
    assert not _should_kill_switch(r_values, min_n=20, threshold=-0.5)


def test_kill_switch_alert_message_contains_numbers():
    msg = format_kill_switch_alert(n=30, mean_r=-0.62, threshold=-0.5)
    assert "-0.62" in msg and "30" in msg
