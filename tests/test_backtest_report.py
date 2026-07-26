"""TDD tests for backtest/report.py — summarize(), factor_edge(), render_text().

All expected numbers below are hand-computed from a fixed 6-trade list (see
`TRADES`) so every assertion is an exact (or exact-fraction, via pytest.approx)
check rather than a re-implementation of the code under test.

Trade order in the list is entry_ts order (t0 < t1 < ... < t5), which is the
order `max_drawdown_pips` must use for its cumulative-pnl walk.

Wins: trades with pnl_pips > 0 (a 0.0 pnl_pips, as in trade 6, is NOT a win).
"""
from datetime import datetime, timedelta, timezone

import pytest

from backtest import report

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
STEP = timedelta(hours=1)


def _t(i, **overrides):
    base = {
        "pair": "EURUSD", "timeframe": "15min", "direction": "LONG",
        "score": 5, "factors": [], "entry_ts": T0 + i * STEP,
        "entry": 1.1000, "sl": 1.0950, "tp1": 1.1050, "tp2": 1.1100,
        "outcome": "TP2", "exit_ts": T0 + i * STEP + STEP,
        "exit_price": 1.1100, "pnl_pips": 0.0, "expired": False,
    }
    base.update(overrides)
    return base


TRADES = [
    _t(0, pair="EURUSD", timeframe="15min", direction="LONG", score=5,
       factors=["Price at Bullish OB (4022.5 - 4041.0)", "FVG"],
       outcome="TP2", pnl_pips=20.0),
    _t(1, pair="EURUSD", timeframe="15min", direction="LONG", score=5,
       factors=["FVG"], outcome="SL", pnl_pips=-10.0),
    _t(2, pair="EURUSD", timeframe="1h", direction="SHORT", score=3,
       factors=["Price at Bullish OB (3000 - 3100)"],
       outcome="TP1_ONLY_EXPIRED", pnl_pips=15.0),
    _t(3, pair="GBPUSD", timeframe="15min", direction="LONG", score=5,
       factors=["FVG", "Liquidity Sweep"], outcome="SL_AFTER_TP1", pnl_pips=-5.0),
    _t(4, pair="GBPUSD", timeframe="15min", direction="SHORT", score=4,
       factors=["Liquidity Sweep"], outcome="TP2", pnl_pips=30.0),
    _t(5, pair="GBPUSD", timeframe="15min", direction="SHORT", score=4,
       factors=[], outcome="EXPIRED", pnl_pips=0.0),
]


# ── summarize(): overall ─────────────────────────────────────────────────────

def test_summarize_overall_counts_and_win_rate():
    s = report.summarize(TRADES)
    overall = s["overall"]
    assert overall["n"] == 6
    assert overall["wins"] == 3
    assert overall["win_rate"] == pytest.approx(0.5)


def test_summarize_overall_expectancy():
    overall = report.summarize(TRADES)["overall"]
    assert overall["expectancy_pips"] == pytest.approx(50.0 / 6)


def test_summarize_overall_profit_factor():
    overall = report.summarize(TRADES)["overall"]
    # gross profit 65 (20+15+30), gross loss 15 (10+5) -> 65/15
    assert overall["profit_factor"] == pytest.approx(65.0 / 15.0)


def test_summarize_overall_max_drawdown():
    overall = report.summarize(TRADES)["overall"]
    # cum: 20, 10, 25, 20, 50, 50 -> peak 20,20,25,25,50,50 -> dd 0,10,0,5,0,0
    assert overall["max_drawdown_pips"] == pytest.approx(10.0)


def test_summarize_profit_factor_inf_when_no_losses():
    only_wins = [_t(0, pnl_pips=10.0), _t(1, pnl_pips=20.0)]
    overall = report.summarize(only_wins)["overall"]
    assert overall["profit_factor"] == float("inf")


def test_summarize_empty_trades():
    overall = report.summarize([])["overall"]
    assert overall["n"] == 0
    assert overall["wins"] == 0
    assert overall["win_rate"] == 0.0
    assert overall["expectancy_pips"] == 0.0
    assert overall["max_drawdown_pips"] == 0.0


# ── summarize(): slices ──────────────────────────────────────────────────────

def test_summarize_by_pair():
    by_pair = report.summarize(TRADES)["by_pair"]
    eur = by_pair["EURUSD"]
    assert eur["n"] == 3
    assert eur["wins"] == 2
    assert eur["win_rate"] == pytest.approx(2 / 3)
    assert eur["expectancy_pips"] == pytest.approx(25.0 / 3)
    assert eur["profit_factor"] == pytest.approx(35.0 / 10.0)
    assert eur["max_drawdown_pips"] == pytest.approx(10.0)

    gbp = by_pair["GBPUSD"]
    assert gbp["n"] == 3
    assert gbp["wins"] == 1
    assert gbp["win_rate"] == pytest.approx(1 / 3)
    assert gbp["expectancy_pips"] == pytest.approx(25.0 / 3)
    assert gbp["profit_factor"] == pytest.approx(30.0 / 5.0)
    assert gbp["max_drawdown_pips"] == pytest.approx(5.0)


def test_summarize_by_timeframe():
    by_tf = report.summarize(TRADES)["by_timeframe"]
    m15 = by_tf["15min"]
    assert m15["n"] == 5
    assert m15["wins"] == 2
    assert m15["expectancy_pips"] == pytest.approx(35.0 / 5)
    assert m15["profit_factor"] == pytest.approx(50.0 / 15.0)
    assert m15["max_drawdown_pips"] == pytest.approx(15.0)

    h1 = by_tf["1h"]
    assert h1["n"] == 1
    assert h1["wins"] == 1
    assert h1["win_rate"] == pytest.approx(1.0)
    assert h1["profit_factor"] == float("inf")
    assert h1["max_drawdown_pips"] == pytest.approx(0.0)


def test_summarize_by_direction():
    by_dir = report.summarize(TRADES)["by_direction"]
    long_ = by_dir["LONG"]
    assert long_["n"] == 3
    assert long_["wins"] == 1
    assert long_["profit_factor"] == pytest.approx(20.0 / 15.0)
    assert long_["max_drawdown_pips"] == pytest.approx(15.0)

    short_ = by_dir["SHORT"]
    assert short_["n"] == 3
    assert short_["wins"] == 2
    assert short_["profit_factor"] == float("inf")
    assert short_["max_drawdown_pips"] == pytest.approx(0.0)


def test_summarize_by_score():
    by_score = report.summarize(TRADES)["by_score"]
    assert by_score[5]["n"] == 3
    assert by_score[5]["wins"] == 1
    assert by_score[5]["profit_factor"] == pytest.approx(20.0 / 15.0)

    assert by_score[3]["n"] == 1
    assert by_score[3]["profit_factor"] == float("inf")

    assert by_score[4]["n"] == 2
    assert by_score[4]["wins"] == 1
    assert by_score[4]["expectancy_pips"] == pytest.approx(15.0)
    assert by_score[4]["profit_factor"] == float("inf")


# ── factor_edge() ─────────────────────────────────────────────────────────

def test_factor_edge_normalizes_parenthesised_suffix():
    rows = report.factor_edge(TRADES)
    names = {r["factor"] for r in rows}
    assert "Price at Bullish OB" in names
    # the raw un-normalized strings must not appear
    assert "Price at Bullish OB (4022.5 - 4041.0)" not in names
    assert "Price at Bullish OB (3000 - 3100)" not in names


def test_factor_edge_price_at_bullish_ob():
    rows = {r["factor"]: r for r in report.factor_edge(TRADES)}
    ob = rows["Price at Bullish OB"]
    assert ob["n_with"] == 2
    assert ob["win_rate_with"] == pytest.approx(1.0)
    assert ob["avg_pips_with"] == pytest.approx(17.5)
    assert ob["win_rate_without"] == pytest.approx(0.25)
    assert ob["avg_pips_without"] == pytest.approx(3.75)
    assert ob["edge_pips"] == pytest.approx(13.75)


def test_factor_edge_fvg():
    rows = {r["factor"]: r for r in report.factor_edge(TRADES)}
    fvg = rows["FVG"]
    assert fvg["n_with"] == 3
    assert fvg["win_rate_with"] == pytest.approx(1 / 3)
    assert fvg["avg_pips_with"] == pytest.approx(5.0 / 3)
    assert fvg["win_rate_without"] == pytest.approx(2 / 3)
    assert fvg["avg_pips_without"] == pytest.approx(15.0)
    assert fvg["edge_pips"] == pytest.approx(5.0 / 3 - 15.0)


def test_factor_edge_liquidity_sweep():
    rows = {r["factor"]: r for r in report.factor_edge(TRADES)}
    ls = rows["Liquidity Sweep"]
    assert ls["n_with"] == 2
    assert ls["win_rate_with"] == pytest.approx(0.5)
    assert ls["avg_pips_with"] == pytest.approx(12.5)
    assert ls["win_rate_without"] == pytest.approx(0.5)
    assert ls["avg_pips_without"] == pytest.approx(6.25)
    assert ls["edge_pips"] == pytest.approx(6.25)


def test_factor_edge_empty_trades():
    assert report.factor_edge([]) == []


# ── render_text() ─────────────────────────────────────────────────────────

def test_render_text_includes_approximation_notes():
    summary = report.summarize(TRADES)
    rows = report.factor_edge(TRADES)
    text = report.render_text(summary, rows)
    assert isinstance(text, str)
    assert "15m bar close" in text or "hour-close" in text
    assert "60" in text and "day" in text.lower()
    assert "spread" in text.lower() or "slippage" in text.lower()


def test_render_text_includes_key_numbers():
    summary = report.summarize(TRADES)
    rows = report.factor_edge(TRADES)
    text = report.render_text(summary, rows)
    assert "EURUSD" in text
    assert "GBPUSD" in text
    assert "FVG" in text
    assert "Liquidity Sweep" in text
    assert "Price at Bullish OB" in text


def test_render_text_renders_inf_profit_factor_as_inf():
    summary = report.summarize([_t(0, pnl_pips=10.0), _t(1, pnl_pips=20.0)])
    text = report.render_text(summary, [])
    assert "inf" in text
