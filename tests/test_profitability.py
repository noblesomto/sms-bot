"""Tests for the 2026-08-24 profitability roadmap changes:

- SHORT-only evaluation gate (_direction_allowed / ENABLE_LONG)
- HTF regime tagging (_htf_regime_of)
- Spread-netted PnL and R-multiple bookkeeping (_net_pips / _pnl_r)
- Kill-switch decision rule (_should_kill_switch)
"""
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from config import _parse_enable_long, get_spread_pips, settings
from alerts.formatter import format_kill_switch_alert
from core.strategy import _direction_allowed, _htf_regime_of
from db.database import Base
from db.models import Signal, BotState
import scheduler
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


def test_parse_enable_long_accepts_known_true_values():
    for val in ("true", "True", " TRUE ", "1", "yes", "YES"):
        assert _parse_enable_long(val) is True


def test_parse_enable_long_fails_closed_on_unrecognized_values():
    # An unrecognized ENABLE_LONG value must disable LONG, not silently
    # leave it enabled — the whole point of this switch is to stop the
    # documented LONG bleed, so ambiguous input must fail toward the safer
    # (disabled) state rather than toward the bleed.
    for val in ("false", "0", "no", "off", "disabled", "", "  ", "banana"):
        assert _parse_enable_long(val) is False


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


# ── Phase 2½ fixes: persisted re-alert throttle + resolved-only window ──

def _memory_db():
    """A fresh in-memory SQLite session bound to the full app schema."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def test_killswitch_last_alert_persists_across_load_calls():
    # A plain module global resets on every process restart, defeating the
    # 7-day re-alert throttle — it must be readable back via a fresh query,
    # not just an in-memory variable.
    db = _memory_db()
    assert scheduler._load_killswitch_last_alert(db) is None

    now = datetime.now(timezone.utc)
    scheduler._save_killswitch_last_alert(db, now)
    assert scheduler._load_killswitch_last_alert(db) == now

    # A second save (re-alert cadence check running again) must update the
    # same row, not accumulate duplicates.
    later = now + timedelta(days=8)
    scheduler._save_killswitch_last_alert(db, later)
    assert scheduler._load_killswitch_last_alert(db) == later
    assert db.query(BotState).count() == 1
    db.close()


def _make_signal(status: str, pnl_r: float) -> Signal:
    return Signal(
        pair="EUR/USD", timeframe="1h", direction="SHORT", confluence_score=5,
        entry_zone_high=1.1, entry_zone_low=1.09, status=status, pnl_r=pnl_r,
    )


def test_resolved_r_values_excludes_still_open_tp1_hit():
    # TP1_HIT carries an interim pnl_r that can still change (blended on
    # TP2/SL_AFTER_TP1) — the kill switch must only average truly closed
    # outcomes, or a batch of still-open partial wins can mask a real drop
    # in expectancy.
    db = _memory_db()
    db.add_all([
        _make_signal("HIT", 1.0),
        _make_signal("INVALIDATED", -1.0),
        _make_signal("PARTIAL_WIN", 0.3),
        _make_signal("EXPIRED", -0.2),
        _make_signal("TP1_HIT", 0.5),   # still open — must be excluded
        _make_signal("ACTIVE", None),   # no pnl_r yet
    ])
    db.commit()

    r_values = scheduler._fetch_resolved_r_values(db, limit=30)
    assert sorted(r_values) == sorted([1.0, -1.0, 0.3, -0.2])
    db.close()
