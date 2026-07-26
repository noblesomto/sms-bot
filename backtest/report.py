"""Backtest report: aggregate stats + factor edge analysis over a trade list
produced by `backtest.engine.run_pair`.

Two pure aggregation functions plus a text renderer:
  - `summarize(trades)` -> dict of overall + sliced stats (pair/timeframe/
    direction/score).
  - `factor_edge(trades)` -> per (normalized) confluence-factor win/edge stats.
  - `render_text(summary, factor_rows)` -> aligned plain-text report,
    including the header of documented backtest approximations.

Stat definitions (binding, per task spec):
  - wins: trades with pnl_pips > 0 (0.0 pnl is not a win, regardless of
    `outcome` — TP1_ONLY_EXPIRED/SL_AFTER_TP1 can land on either side of 0).
  - win_rate = wins / n.
  - expectancy_pips = mean(pnl_pips).
  - profit_factor = gross_profit / abs(gross_loss); gross_profit is the sum
    of positive pnl_pips, gross_loss the sum of negative pnl_pips. When
    gross_loss is 0 (no losing trades), profit_factor is float('inf')
    (rendered as the literal string "inf").
  - max_drawdown_pips: walk the cumulative pnl curve in trade order (the
    order the trades are given in, which callers are expected to supply in
    entry_ts order — see `_slice` which preserves input order per group),
    tracking the running peak (starting at 0.0) and the largest peak-minus-
    current gap seen.

Factor name normalization: a raw factor string like
"Price at Bullish OB (4022.5 - 4041.0)" is stripped of its parenthesised
suffix (and any trailing whitespace before it) down to "Price at Bullish OB".
Applied identically everywhere factor names are aggregated.
"""
import re
from collections import defaultdict

_PAREN_SUFFIX_RE = re.compile(r"\s*\([^)]*\)\s*$")


def _normalize_factor(name: str) -> str:
    return _PAREN_SUFFIX_RE.sub("", name).strip()


def _max_drawdown(trades: list) -> float:
    """Max drawdown on the cumulative pnl curve, walked in the given order."""
    peak = 0.0
    cum = 0.0
    max_dd = 0.0
    for t in trades:
        cum += t["pnl_pips"]
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd
    return max_dd


def _stats(trades: list) -> dict:
    n = len(trades)
    if n == 0:
        return {
            "n": 0, "wins": 0, "win_rate": 0.0, "expectancy_pips": 0.0,
            "profit_factor": 0.0, "max_drawdown_pips": 0.0,
        }

    pnls = [t["pnl_pips"] for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss = sum(p for p in pnls if p < 0)   # negative or 0

    profit_factor = (gross_profit / abs(gross_loss)) if gross_loss != 0 else float("inf")

    return {
        "n": n,
        "wins": wins,
        "win_rate": wins / n,
        "expectancy_pips": sum(pnls) / n,
        "profit_factor": profit_factor,
        "max_drawdown_pips": _max_drawdown(trades),
    }


def _slice(trades: list, key) -> dict:
    groups = defaultdict(list)
    for t in trades:
        groups[key(t)].append(t)
    return {k: _stats(v) for k, v in groups.items()}


def summarize(trades: list) -> dict:
    """Aggregate `trades` into overall + sliced stats.

    Returns {"overall": {...}, "by_pair": {pair: {...}}, "by_timeframe":
    {tf: {...}}, "by_direction": {dir: {...}}, "by_score": {score: {...}}}.
    Each stats dict has keys: n, wins, win_rate, expectancy_pips,
    profit_factor, max_drawdown_pips.
    """
    return {
        "overall": _stats(trades),
        "by_pair": _slice(trades, lambda t: t["pair"]),
        "by_timeframe": _slice(trades, lambda t: t["timeframe"]),
        "by_direction": _slice(trades, lambda t: t["direction"]),
        "by_score": _slice(trades, lambda t: t["score"]),
    }


def factor_edge(trades: list) -> list:
    """Per (normalized) confluence-factor win/edge stats.

    For every distinct normalized factor name seen across `trades`, splits
    trades into "with this factor present" vs "without", and reports each
    side's count/win_rate/avg pips plus edge_pips = avg_pips_with -
    avg_pips_without. Sorted by edge_pips descending.
    """
    all_factors = set()
    normalized_cache = []   # per-trade: set of normalized factor names
    for t in trades:
        norm = {_normalize_factor(f) for f in (t.get("factors") or [])}
        normalized_cache.append(norm)
        all_factors |= norm

    rows = []
    for factor in all_factors:
        with_trades = [t for t, norm in zip(trades, normalized_cache) if factor in norm]
        without_trades = [t for t, norm in zip(trades, normalized_cache) if factor not in norm]

        n_with = len(with_trades)
        n_without = len(without_trades)

        win_rate_with = (sum(1 for t in with_trades if t["pnl_pips"] > 0) / n_with
                          if n_with else 0.0)
        avg_pips_with = (sum(t["pnl_pips"] for t in with_trades) / n_with
                         if n_with else 0.0)
        win_rate_without = (sum(1 for t in without_trades if t["pnl_pips"] > 0) / n_without
                            if n_without else 0.0)
        avg_pips_without = (sum(t["pnl_pips"] for t in without_trades) / n_without
                           if n_without else 0.0)

        rows.append({
            "factor": factor,
            "n_with": n_with,
            "win_rate_with": win_rate_with,
            "avg_pips_with": avg_pips_with,
            "win_rate_without": win_rate_without,
            "avg_pips_without": avg_pips_without,
            "edge_pips": avg_pips_with - avg_pips_without,
        })

    rows.sort(key=lambda r: r["edge_pips"], reverse=True)
    return rows


# ── text rendering ───────────────────────────────────────────────────────

APPROXIMATION_NOTES = """\
Documented backtest approximations (vs. live):
  - One evaluate() call per closed 15m bar for the "15min" scan timeframe;
    the "1h" scan timeframe is only evaluated on hour-close bars (every 4th
    15m bar), not on live's 5-minute scan cadence.
  - History capped at 60 days of 15m bars per pair.
  - No spread or slippage modeled; entries/exits use raw candle prices.
"""


def _fmt_pf(pf: float) -> str:
    if pf == float("inf"):
        return "inf"
    return f"{pf:.2f}"


def _fmt_stats_row(label, s: dict) -> str:
    return (f"{label:<14} {s['n']:>5} {s['wins']:>5} "
            f"{s['win_rate']*100:>7.1f}% {s['expectancy_pips']:>10.2f} "
            f"{_fmt_pf(s['profit_factor']):>8} {s['max_drawdown_pips']:>10.2f}")


def _stats_table(title: str, rows: dict) -> str:
    header = (f"{'':<14} {'n':>5} {'wins':>5} {'win%':>8} "
              f"{'exp(pips)':>10} {'PF':>8} {'maxDD':>10}")
    lines = [title, header, "-" * len(header)]
    # Sort numerically when every key is numeric (e.g. score slices), else
    # fall back to a stable string sort (pair/timeframe/direction labels).
    keys = list(rows.keys())
    if all(isinstance(k, (int, float)) for k in keys):
        sort_key = lambda k: k
    else:
        sort_key = lambda k: str(k)
    for label in sorted(keys, key=sort_key):
        lines.append(_fmt_stats_row(str(label), rows[label]))
    return "\n".join(lines)


def render_text(summary: dict, factor_rows: list) -> str:
    """Render `summary` (from `summarize`) and `factor_rows` (from
    `factor_edge`) as an aligned plain-text report."""
    lines = []
    lines.append("=" * 70)
    lines.append("SMC BACKTEST REPORT")
    lines.append("=" * 70)
    lines.append(APPROXIMATION_NOTES)

    overall = summary["overall"]
    lines.append("Overall")
    lines.append("-" * 40)
    lines.append(f"  trades:          {overall['n']}")
    lines.append(f"  wins:            {overall['wins']}")
    lines.append(f"  win rate:        {overall['win_rate']*100:.1f}%")
    lines.append(f"  expectancy:      {overall['expectancy_pips']:.2f} pips")
    lines.append(f"  profit factor:   {_fmt_pf(overall['profit_factor'])}")
    lines.append(f"  max drawdown:    {overall['max_drawdown_pips']:.2f} pips")
    lines.append("")

    lines.append(_stats_table("By pair", summary["by_pair"]))
    lines.append("")
    lines.append(_stats_table("By timeframe", summary["by_timeframe"]))
    lines.append("")
    lines.append(_stats_table("By direction", summary["by_direction"]))
    lines.append("")
    lines.append(_stats_table("By score", summary["by_score"]))
    lines.append("")

    lines.append("Factor edge")
    fheader = (f"{'factor':<28} {'n_with':>7} {'win% w':>8} {'avg w':>8} "
              f"{'win% wo':>8} {'avg wo':>8} {'edge':>8}")
    lines.append(fheader)
    lines.append("-" * len(fheader))
    for r in factor_rows:
        lines.append(
            f"{r['factor']:<28} {r['n_with']:>7} "
            f"{r['win_rate_with']*100:>7.1f}% {r['avg_pips_with']:>8.2f} "
            f"{r['win_rate_without']*100:>7.1f}% {r['avg_pips_without']:>8.2f} "
            f"{r['edge_pips']:>8.2f}"
        )

    return "\n".join(lines)
