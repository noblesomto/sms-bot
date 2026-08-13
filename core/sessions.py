from datetime import datetime, timedelta, timezone, time as dtime
from typing import Optional

# All times in UTC
# Entry kill zones only — windows of high institutional order flow where it's
# valid to trigger a trade. Asian Range is deliberately excluded: it's used
# for range definition (marking the day's initial balance), never for entry
# — thin, low-liquidity, chop-prone session that shouldn't gate scalp entries.
KILL_ZONES: dict = {
    "LONDON_OPEN": (dtime(7, 0), dtime(9, 0)),
    "LONDON_CLOSE": (dtime(11, 0), dtime(12, 0)),
    "NEW_YORK_OPEN": (dtime(13, 0), dtime(15, 0)),
}

SESSIONS: dict = {
    "ASIAN": (dtime(0, 0), dtime(7, 0)),
    "LONDON": (dtime(7, 0), dtime(12, 0)),
    "NEW_YORK": (dtime(13, 0), dtime(22, 0)),
}


def _utc_time(dt: Optional[datetime] = None) -> dtime:
    if dt is None:
        dt = datetime.now(timezone.utc)
    return dt.time()


def is_weekend(dt: Optional[datetime] = None) -> bool:
    """Return True on Saturday or Sunday UTC (forex market closed)."""
    if dt is None:
        dt = datetime.now(timezone.utc)
    return dt.weekday() >= 5


def market_hours_elapsed(start: datetime, end: datetime) -> float:
    """Hours between start and end, excluding weekend (Sat/Sun UTC) closure —
    mirrors is_weekend()'s whole-day definition so a signal created before a
    weekend doesn't have Sat/Sun counted against its expiry budget."""
    if end <= start:
        return 0.0
    total = 0.0
    cursor = start
    while cursor < end:
        day_end = datetime(cursor.year, cursor.month, cursor.day, tzinfo=cursor.tzinfo) + timedelta(days=1)
        segment_end = min(day_end, end)
        if not is_weekend(cursor):
            total += (segment_end - cursor).total_seconds() / 3600
        cursor = segment_end
    return total


def is_in_kill_zone(dt: Optional[datetime] = None) -> tuple:
    """
    Check whether the current UTC time falls inside an ICT entry kill zone.

    Kill zones are windows of high institutional order flow:
      London Open 07-09, London Close 11-12, New York Open 13-15.
    Asian Range (00-05) is intentionally not a kill zone — see KILL_ZONES.

    Returns (bool, zone_name | None).
    Signals generated outside kill zones receive a lower confluence score.
    """
    current = _utc_time(dt)
    for name, (start, end) in KILL_ZONES.items():
        if start <= current <= end:
            return True, name
    return False, None


def get_current_session(dt: Optional[datetime] = None) -> str:
    """
    Determine the active trading session based on UTC time.

    Returns one of: ASIAN, LONDON, NEW_YORK, OFF.
    """
    current = _utc_time(dt)
    for name, (start, end) in SESSIONS.items():
        if start <= current <= end:
            return name
    return "OFF"


def test_sessions():
    """Standalone test across representative UTC timestamps."""
    test_times = [
        datetime(2024, 1, 15, 7, 30, tzinfo=timezone.utc),   # London Open KZ
        datetime(2024, 1, 15, 13, 30, tzinfo=timezone.utc),  # NY Open KZ
        datetime(2024, 1, 15, 20, 0, tzinfo=timezone.utc),   # Off-session
        datetime(2024, 1, 20, 10, 0, tzinfo=timezone.utc),   # Saturday
    ]
    for dt in test_times:
        in_kz, zone = is_in_kill_zone(dt)
        session = get_current_session(dt)
        weekend = is_weekend(dt)
        print(
            f"{dt.strftime('%Y-%m-%d %H:%M UTC')}: "
            f"session={session}, kill_zone={zone or 'None'}, weekend={weekend}"
        )


if __name__ == "__main__":
    test_sessions()
