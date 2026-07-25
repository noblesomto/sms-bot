"""TDD tests for backtest/engine.py — the historical replay engine.

Tests fall into two groups:
  1. Direct tests of `simulate_outcome` (pure function: signal + future 15m
     bars -> resolved trade dict) covering SL-first wick precedence, the
     TP1 ladder + 50/50 blending, plain expiry, and TP1-then-expiry.
  2. Integration tests of `run_pair` (the walk-forward driver) with
     `core.strategy.evaluate` monkeypatched inside `backtest.engine` so
     signals fire deterministically at known bars — covering warm-up,
     the <200-bar timeframe skip, entry accounting, and the position-book
     Filters 6/7 mirror.

Price scale is kept at realistic forex magnitude (~1.10) so `scheduler._calc_pips`
(price_diff * 10000) produces clean, easy-to-check pip values.
"""
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from backtest import engine
from scheduler import _calc_pips

PAIR = "EUR/USD"
ENTRY_PRICE = 1.1000
SL = 1.0950     # -50 pips
TP1 = 1.1050    # +50 pips
TP2 = 1.1100    # +100 pips

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
STEP = timedelta(minutes=15)
WARMUP = 200


def make_base_df(n, price=ENTRY_PRICE, start=START):
    """n flat 15m bars: open=high=low=close=price."""
    times = [start + i * STEP for i in range(n)]
    return pd.DataFrame({
        "datetime": times,
        "open": [price] * n,
        "high": [price] * n,
        "low": [price] * n,
        "close": [price] * n,
        "volume": [1] * n,
    })


def _base_signal(**overrides):
    sig = {
        "direction": "LONG", "score": 5, "max_score": 8, "factors": ["ob", "fvg"],
        "entry_low": 1.0990, "entry_high": 1.1010,
        "target1": TP1, "target2": TP2, "invalidation": SL,
        "entry_price": ENTRY_PRICE, "kz_name": "LONDON_OPEN", "session": "LONDON",
    }
    sig.update(overrides)
    return sig


# ── simulate_outcome: direct unit tests ──────────────────────────────────────

def _signal_seed(entry_ts, direction="LONG", sl=SL, tp1=TP1, tp2=TP2):
    return {
        "pair": PAIR, "timeframe": "15min", "direction": direction,
        "score": 5, "factors": ["ob"],
        "entry_ts": entry_ts, "entry": ENTRY_PRICE,
        "sl": sl, "tp1": tp1, "tp2": tp2,
    }


def _bars(rows, start=START + WARMUP * STEP + STEP):
    """Build a 15m bars_15m DataFrame from a list of (high, low, close) tuples,
    starting one bar after the given entry timestamp."""
    times = [start + i * STEP for i in range(len(rows))]
    return pd.DataFrame({
        "datetime": times,
        "open": [r[2] for r in rows],
        "high": [r[0] for r in rows],
        "low": [r[1] for r in rows],
        "close": [r[2] for r in rows],
        "volume": [1] * len(rows),
    })


ENTRY_TS = START + WARMUP * STEP


def test_simulate_sl_first_same_candle():
    # One bar wicks through both SL and TP1 — SL-first precedence wins.
    bars = _bars([(1.1060, 1.0940, 1.1000)])
    trade = engine.simulate_outcome(_signal_seed(ENTRY_TS), bars)
    assert trade["outcome"] == "SL"
    assert trade["exit_price"] == SL
    assert trade["exit_ts"] == bars["datetime"].iloc[0]
    assert trade["pnl_pips"] == -50.0
    assert trade["expired"] is False


def test_simulate_tp1_then_tp2_blends_50_50():
    bars = _bars([
        (1.1055, 1.0980, 1.1040),   # TP1 hit
        (1.1105, 1.1010, 1.1090),   # TP2 hit
    ])
    trade = engine.simulate_outcome(_signal_seed(ENTRY_TS), bars)
    assert trade["outcome"] == "TP2"
    assert trade["exit_price"] == TP2
    tp1_leg = _calc_pips(PAIR, TP1 - ENTRY_PRICE)
    tp2_leg = _calc_pips(PAIR, TP2 - ENTRY_PRICE)
    assert trade["pnl_pips"] == round(0.5 * tp1_leg + 0.5 * tp2_leg, 1)
    assert trade["expired"] is False


def test_simulate_tp2_direct_without_tp1_is_unblended():
    bars = _bars([(1.1105, 1.1010, 1.1090)])   # jumps straight through TP2
    trade = engine.simulate_outcome(_signal_seed(ENTRY_TS), bars)
    assert trade["outcome"] == "TP2"
    assert trade["pnl_pips"] == _calc_pips(PAIR, TP2 - ENTRY_PRICE)


def test_simulate_tp1_then_sl_after_tp1_blends_50_50():
    bars = _bars([
        (1.1055, 1.0980, 1.1040),   # TP1 hit
        (1.0990, 1.0940, 1.0950),   # SL hit after TP1
    ])
    trade = engine.simulate_outcome(_signal_seed(ENTRY_TS), bars)
    assert trade["outcome"] == "SL_AFTER_TP1"
    assert trade["exit_price"] == SL
    tp1_leg = _calc_pips(PAIR, TP1 - ENTRY_PRICE)
    sl_leg = _calc_pips(PAIR, SL - ENTRY_PRICE)
    assert trade["pnl_pips"] == round(0.5 * tp1_leg + 0.5 * sl_leg, 1)
    assert trade["expired"] is False


def test_simulate_plain_expiry_unblended():
    # 15min expiry = 12h = 48 bars; bar offset 48 (49th bar) is first past deadline.
    quiet = (1.1020, 1.0980, 1.1000)
    rows = [quiet] * 48 + [(1.1020, 1.0980, 1.1020)]
    bars = _bars(rows)
    trade = engine.simulate_outcome(_signal_seed(ENTRY_TS), bars)
    assert trade["outcome"] == "EXPIRED"
    assert trade["expired"] is True
    assert trade["exit_price"] == 1.1020
    assert trade["exit_ts"] == bars["datetime"].iloc[48]
    assert trade["pnl_pips"] == _calc_pips(PAIR, 1.1020 - ENTRY_PRICE)


def test_simulate_tp1_then_expiry_blends_50_50():
    tp1_bar = (1.1055, 1.0980, 1.1040)
    quiet = (1.1020, 1.0980, 1.1000)
    final = (1.1090, 1.1000, 1.1080)
    rows = [tp1_bar] + [quiet] * 47 + [final]
    bars = _bars(rows)
    trade = engine.simulate_outcome(_signal_seed(ENTRY_TS), bars)
    assert trade["outcome"] == "TP1_ONLY_EXPIRED"
    assert trade["expired"] is True
    assert trade["exit_price"] == 1.1080
    tp1_leg = _calc_pips(PAIR, TP1 - ENTRY_PRICE)
    expiry_leg = _calc_pips(PAIR, 1.1080 - ENTRY_PRICE)
    assert trade["pnl_pips"] == round(0.5 * tp1_leg + 0.5 * expiry_leg, 1)


def test_simulate_short_direction_mirrors():
    # SHORT: SL sits above entry, targets below. A single bar whose low wicks
    # straight through target2 (without an SL touch) resolves TP2 unblended.
    bars = _bars([(1.0960, 1.0900, 1.0910)])
    seed = _signal_seed(ENTRY_TS, direction="SHORT", sl=1.1050, tp1=1.0950, tp2=1.0900)
    trade = engine.simulate_outcome(seed, bars)
    assert trade["outcome"] == "TP2"
    assert trade["exit_price"] == 1.0900
    assert trade["pnl_pips"] == _calc_pips(PAIR, ENTRY_PRICE - 1.0900)


def test_simulate_unresolved_when_bars_run_out():
    bars = _bars([(1.1020, 1.0980, 1.1000)])   # quiet, single bar, no expiry reached
    trade = engine.simulate_outcome(_signal_seed(ENTRY_TS), bars)
    assert trade["outcome"] is None
    assert trade["exit_ts"] is None
    assert trade["pnl_pips"] is None


# ── run_pair: integration tests with evaluate monkeypatched ──────────────────

def _install_stub(monkeypatch, signals_by_call):
    """signals_by_call: dict call_number(1-based) -> signal dict or None.
    Calls beyond the max key return None. Returns the list `calls` recording
    (timeframe, now) for every invocation."""
    calls = []

    def stub(pair, tf, view, htf_view, itf_view, now):
        calls.append((tf, now))
        n = len(calls)
        sig = signals_by_call.get(n)
        return {"signal": sig, "analysis": {}}

    monkeypatch.setattr(engine, "evaluate", stub)
    return calls


def test_warmup_exactly_200_bars_no_calls(monkeypatch):
    calls = _install_stub(monkeypatch, {})
    base_df = make_base_df(WARMUP)
    trades = engine.run_pair(PAIR, base_df, timeframes=("15min",))
    assert trades == []
    assert calls == []


def test_warmup_one_bar_past_threshold_fires_once(monkeypatch):
    calls = _install_stub(monkeypatch, {})
    base_df = make_base_df(WARMUP + 1)
    engine.run_pair(PAIR, base_df, timeframes=("15min",))
    assert len(calls) == 1
    assert calls[0][0] == "15min"
    assert calls[0][1] == base_df["datetime"].iloc[WARMUP]


def test_1h_timeframe_skipped_until_enough_bars(monkeypatch):
    # 300 15m bars total -> at most 75 resampled 1h buckets, always < 200 -> never called.
    calls = _install_stub(monkeypatch, {})
    base_df = make_base_df(300)
    trades = engine.run_pair(PAIR, base_df, timeframes=("1h",))
    assert trades == []
    assert calls == []


def test_entry_accounting(monkeypatch):
    # Fire a signal on the very first decision bar; give enough quiet bars after
    # so the trade resolves via plain SL (no ladder complexity) — the focus here
    # is verifying entry price/ts/sl/tp1/tp2 bookkeeping, not the resolution math.
    sig = _base_signal()
    calls = _install_stub(monkeypatch, {1: sig})
    n_future = 5
    base_df = make_base_df(WARMUP + 1 + n_future)
    entry_idx = WARMUP
    # bar right after entry hits SL cleanly
    base_df.loc[entry_idx + 1, ["high", "low", "close"]] = [1.0960, 1.0940, 1.0945]

    trades = engine.run_pair(PAIR, base_df, timeframes=("15min",))
    assert len(trades) == 1
    t = trades[0]
    assert t["pair"] == PAIR
    assert t["timeframe"] == "15min"
    assert t["direction"] == "LONG"
    assert t["score"] == sig["score"]
    assert t["factors"] == sig["factors"]
    assert t["entry"] == base_df["close"].iloc[entry_idx] == ENTRY_PRICE
    assert t["entry_ts"] == base_df["datetime"].iloc[entry_idx]
    assert t["sl"] == SL
    assert t["tp1"] == TP1
    assert t["tp2"] == TP2
    assert t["outcome"] == "SL"
    assert t["exit_price"] == SL
    assert t["expired"] is False


def test_position_book_filter6_blocks_opposite_direction(monkeypatch):
    long_sig = _base_signal(direction="LONG")
    short_sig = _base_signal(direction="SHORT", invalidation=1.1050, target1=1.0950, target2=1.0900)
    calls = _install_stub(monkeypatch, {1: long_sig, 2: short_sig})

    base_df = make_base_df(WARMUP + 5)
    entry_idx = WARMUP           # LONG opened here
    # keep bar at entry_idx+1 (2nd decision bar) quiet so the LONG stays open
    # when the SHORT candidate is evaluated
    base_df.loc[entry_idx + 1, ["high", "low", "close"]] = [1.1020, 1.0980, 1.1000]
    # resolve LONG via TP2 two bars later
    base_df.loc[entry_idx + 2, ["high", "low", "close"]] = [1.1105, 1.1010, 1.1090]

    trades = engine.run_pair(PAIR, base_df, timeframes=("15min",))
    assert len(calls) >= 2   # short candidate was evaluated...
    assert len(trades) == 1  # ...but rejected by the position book
    assert trades[0]["direction"] == "LONG"


def test_position_book_filter7_blocks_same_tf_same_direction(monkeypatch):
    long_sig1 = _base_signal(direction="LONG")
    long_sig2 = _base_signal(direction="LONG")
    calls = _install_stub(monkeypatch, {1: long_sig1, 2: long_sig2})

    base_df = make_base_df(WARMUP + 5)
    entry_idx = WARMUP
    base_df.loc[entry_idx + 1, ["high", "low", "close"]] = [1.1020, 1.0980, 1.1000]
    base_df.loc[entry_idx + 2, ["high", "low", "close"]] = [1.1105, 1.1010, 1.1090]

    trades = engine.run_pair(PAIR, base_df, timeframes=("15min",))
    assert len(trades) == 1


def test_position_freed_after_resolution_allows_new_signal(monkeypatch):
    # LONG resolves immediately (SL on the very next bar); a second LONG signal
    # fired afterwards must NOT be blocked since the book is now empty.
    long_sig1 = _base_signal(direction="LONG")
    long_sig2 = _base_signal(direction="LONG")
    calls = _install_stub(monkeypatch, {1: long_sig1, 3: long_sig2})

    base_df = make_base_df(WARMUP + 6)
    entry_idx = WARMUP
    base_df.loc[entry_idx + 1, ["high", "low", "close"]] = [1.0960, 1.0940, 1.0945]  # SL -> closes here
    # decision bar 2 (entry_idx+1) returns None (not in signals_by_call), decision
    # bar 3 (entry_idx+2) fires long_sig2 — by now the book should be empty.
    base_df.loc[entry_idx + 3, ["high", "low", "close"]] = [1.1105, 1.1010, 1.1090]

    trades = engine.run_pair(PAIR, base_df, timeframes=("15min",))
    assert len(trades) == 2


def test_determinism(monkeypatch):
    def build_and_run():
        sig = _base_signal()
        calls = []

        def stub(pair, tf, view, htf_view, itf_view, now):
            calls.append(1)
            if len(calls) == 1:
                return {"signal": sig, "analysis": {}}
            return {"signal": None, "analysis": {}}

        monkeypatch.setattr(engine, "evaluate", stub)
        base_df = make_base_df(WARMUP + 6)
        base_df.loc[WARMUP + 1, ["high", "low", "close"]] = [1.0960, 1.0940, 1.0945]
        return engine.run_pair(PAIR, base_df, timeframes=("15min",))

    trades_a = build_and_run()
    trades_b = build_and_run()
    assert trades_a == trades_b
    assert len(trades_a) == 1
