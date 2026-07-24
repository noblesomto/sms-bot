import pandas as pd
from core.order_blocks import _has_displacement

def _df(rows):
    n = len(rows)
    return pd.DataFrame(
        [dict(zip(("open", "high", "low", "close"), r)) for r in rows]
    ).assign(volume=0.0,
             datetime=pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC"))

# 15 flat candles (range 1.0) to seed ATR ≈ 1.0, OB candle at idx 15
FLAT = [(100, 100.5, 99.5, 100)] * 15

def test_impulsive_move_passes():
    # bearish OB candle then 3 strong up candles: travel 100→105 = 5 ≥ 1.5×ATR
    rows = FLAT + [(100.5, 100.6, 99.6, 99.8),
                   (99.8, 102.0, 99.8, 101.9), (101.9, 103.5, 101.8, 103.4),
                   (103.4, 105.0, 103.3, 104.9)]
    assert _has_displacement(_df(rows), 15, "BULLISH")

def test_drift_fails():
    # weak follow-through: 3 candles crawl 0.3 total < 1.5×ATR, no gap
    rows = FLAT + [(100.5, 100.6, 99.6, 99.8),
                   (99.8, 100.0, 99.7, 99.9), (99.9, 100.1, 99.8, 100.0),
                   (100.0, 100.2, 99.9, 100.1)]
    assert not _has_displacement(_df(rows), 15, "BULLISH")

def test_fvg_gap_passes_without_atr_pass():
    # small travel but candle 17 low (100.9) > candle 15 high → bullish FVG
    rows = FLAT + [(100.5, 100.6, 99.6, 99.8),
                   (99.8, 100.7, 99.8, 100.6), (100.9, 101.2, 100.9, 101.1),
                   (101.1, 101.3, 101.0, 101.2)]
    assert _has_displacement(_df(rows), 15, "BULLISH")

def test_bearish_mirror():
    rows = FLAT + [(99.5, 100.4, 99.4, 100.2),
                   (100.2, 100.2, 98.0, 98.1), (98.1, 98.2, 96.5, 96.6),
                   (96.6, 96.7, 95.0, 95.1)]
    assert _has_displacement(_df(rows), 15, "BEARISH")

def test_ob_at_end_of_data_fails_open():
    # no candles after OB yet → no displacement evidence → reject
    rows = FLAT + [(100.5, 100.6, 99.6, 99.8)]
    assert not _has_displacement(_df(rows), 15, "BULLISH")

def test_full_pipeline_finds_displaced_ob():
    # Integration guard: the module self-test's realistic fixture must clear
    # the full pipeline (identify_swings -> detect_bos -> find_order_blocks ->
    # validate_obs) and yield at least one displacement-gated OB. This pins
    # down regressions where the fixture's ATR/impulse balance drifts back
    # to "0 OBs found" (the bug this task fixed).
    from core.order_blocks import test_order_blocks
    obs = test_order_blocks()
    assert len(obs) >= 1
    assert all(ob["direction"] in {"BULLISH", "BEARISH"} for ob in obs)
