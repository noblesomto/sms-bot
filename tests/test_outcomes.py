from scheduler import _resolve_outcome

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
