from datetime import datetime, timezone

from core.sessions import market_hours_elapsed

def _utc(y, m, d, h=0, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=timezone.utc)

def test_same_day_elapsed_is_wall_clock():
    start = _utc(2026, 8, 12, 10, 0)  # Wednesday
    end = _utc(2026, 8, 12, 14, 0)
    assert market_hours_elapsed(start, end) == 4.0

def test_weekend_hours_excluded():
    # Friday 20:00 -> Sunday 20:00 is 48h wall-clock, but only the 4h of
    # Friday evening (20:00-24:00) are market-open; Sat/Sun don't count.
    start = _utc(2026, 8, 14, 20, 0)  # Friday
    end = _utc(2026, 8, 16, 20, 0)    # Sunday
    assert market_hours_elapsed(start, end) == 4.0

def test_friday_signal_does_not_expire_at_48h_wall_clock():
    # A 1h-timeframe signal (TF_EXPIRY_HOURS=48) created Friday afternoon
    # must NOT have hit its 48h budget by Sunday, even though 48 wall-clock
    # hours have passed -- most of that was weekend closure.
    created = _utc(2026, 8, 14, 14, 0)  # Friday
    checked_at = _utc(2026, 8, 16, 14, 0)  # Sunday, 48h wall-clock later
    elapsed = market_hours_elapsed(created, checked_at)
    assert elapsed < 48
    assert elapsed == 10.0  # 14:00-24:00 Friday only

def test_spans_full_weekend_into_monday():
    start = _utc(2026, 8, 14, 22, 0)  # Friday 22:00
    end = _utc(2026, 8, 17, 4, 0)     # Monday 04:00
    # 2h Friday + 4h Monday = 6h open; Sat/Sun (48h) excluded
    assert market_hours_elapsed(start, end) == 6.0

def test_no_weekend_in_range_matches_wall_clock():
    start = _utc(2026, 8, 10, 9, 0)  # Monday
    end = _utc(2026, 8, 14, 9, 0)    # Friday
    assert market_hours_elapsed(start, end) == 96.0

def test_end_before_start_returns_zero():
    start = _utc(2026, 8, 14, 9, 0)
    end = _utc(2026, 8, 14, 8, 0)
    assert market_hours_elapsed(start, end) == 0.0
