"""TDD tests for the Wyckoff Spring/UTAD confirmation tightening (Fix 3).

Root cause (2026-07-27 XAU/USD backtest, reconstructed against real fetched
1h candles — see docs/superpowers/backtests/2026-07-27-fix3-spring-
tightening.md): every failing Spring-based LONG on XAU/USD had one or both
of these defects:
  - the "recovery" close was allowed up to 0.2% BELOW range_low (the old
    `cl >= range_low * 0.998` tolerance) and still counted as "closed back
    inside the range" — one failing entry's close was $7 (0.17%) below
    range_low, well outside any reasonable rounding tolerance.
  - the wick breach below range_low was trivial (as little as $1.00-$1.70
    on a $60-70-wide range, 1.5-2.8% of range width) — noise, not a real
    stop-hunt.

Both defects apply symmetrically to detect_spring/detect_utad since they
share identical logic.
"""
import pandas as pd

from core.wyckoff import detect_spring, detect_utad


def _range(low, high):
    return {"range_high": high, "range_low": low, "range_mid": (low + high) / 2,
            "candle_count": 20, "preceding_trend": "BEARISH"}


def _df(rows):
    """rows: list of (low, close) tuples -> minimal OHLC df detect_spring/
    detect_utad need (only 'low'/'high' and 'close' are read)."""
    return pd.DataFrame({
        "open": [r[1] for r in rows],
        "high": [r[0] + 1 for r in rows],
        "low": [r[0] for r in rows],
        "close": [r[1] for r in rows],
    })


# ── Spring ────────────────────────────────────────────────────────────────

def test_spring_detects_genuine_breach_with_full_recovery():
    # range width 10 (100-110); breach 1.0 = 10% of width (>= 3% threshold);
    # close 100.5 fully recovers back inside the range.
    df = _df([(99.0, 100.5)])
    spring = detect_spring(df, _range(100.0, 110.0))
    assert spring is not None
    assert spring["detected"] is True


def test_spring_rejects_close_that_stays_below_range_low():
    # Close only reaches 99.85 — within the OLD 0.998 tolerance
    # (100 * 0.998 = 99.8) but still below range_low. Must now be rejected:
    # a real recovery closes back INSIDE the range, not just near it.
    df = _df([(99.0, 99.85)])
    spring = detect_spring(df, _range(100.0, 110.0))
    assert spring is None


def test_spring_rejects_trivial_breach_depth():
    # Breach of only 0.2 (2% of a 10-wide range, below the 3% floor) even
    # though the close fully recovers — a wick this shallow is noise, not
    # a stop-hunt.
    df = _df([(99.8, 100.5)])
    spring = detect_spring(df, _range(100.0, 110.0))
    assert spring is None


def test_spring_accepts_breach_just_above_threshold():
    # Breach of 0.35 (3.5% of a 10-wide range, clear of the 3% floor and of
    # float-rounding noise right at the boundary) with full recovery.
    df = _df([(99.65, 100.5)])
    spring = detect_spring(df, _range(100.0, 110.0))
    assert spring is not None
    assert spring["detected"] is True


def test_spring_no_wick_below_range_low_still_not_detected():
    df = _df([(100.5, 100.8)])
    spring = detect_spring(df, _range(100.0, 110.0))
    assert spring is None


# ── UTAD (mirror) ─────────────────────────────────────────────────────────

def _udf(rows):
    """rows: list of (high, close) tuples."""
    return pd.DataFrame({
        "open": [r[1] for r in rows],
        "high": [r[0] for r in rows],
        "low": [r[0] - 1 for r in rows],
        "close": [r[1] for r in rows],
    })


def test_utad_detects_genuine_breach_with_full_recovery():
    # range width 10 (90-100); breach 1.0 = 10% of width; close 99.5 fully
    # recovers back inside the range (below range_high).
    df = _udf([(101.0, 99.5)])
    utad = detect_utad(df, _range(90.0, 100.0))
    assert utad is not None
    assert utad["detected"] is True


def test_utad_rejects_close_that_stays_above_range_high():
    # Close only reaches 100.15 — within the OLD 1.002 tolerance
    # (100 * 1.002 = 100.2) but still above range_high.
    df = _udf([(101.0, 100.15)])
    utad = detect_utad(df, _range(90.0, 100.0))
    assert utad is None


def test_utad_rejects_trivial_breach_depth():
    # Breach of only 0.2 (2% of a 10-wide range, below the 3% floor) even
    # though the close fully recovers.
    df = _udf([(100.2, 99.5)])
    utad = detect_utad(df, _range(90.0, 100.0))
    assert utad is None
