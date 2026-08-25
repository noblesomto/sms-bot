"""CLI entry point for the backtest engine + report.

Usage:
    python -m backtest.run [--pairs A,B|all] [--timeframes 15min,1h]
                            [--days 60] [--csv out.csv]

Fetches base 15m candle history per pair via `core.data_feed.get_candles`,
runs `backtest.engine.run_pair` for each pair, concatenates the resulting
trade lists, and prints the aggregate `backtest.report` summary + factor-edge
tables. Progress is printed per pair (with an explicit flush) since a single
pair's engine run can take ~15-25 minutes.
"""
import argparse
import csv
import sys
import time
from datetime import timedelta

from config import settings
from core.data_feed import get_candles
from backtest.engine import run_pair
from backtest.report import summarize, factor_edge, render_text

CSV_FIELDNAMES = [
    "pair", "timeframe", "direction", "score", "factors", "entry_ts",
    "entry", "sl", "tp1", "tp2", "outcome", "exit_ts", "exit_price",
    "pnl_pips", "expired",
]


def _parse_pairs(arg: str) -> list:
    if arg is None or arg.strip().lower() == "all":
        return list(settings.PAIRS)
    return [p.strip() for p in arg.split(",") if p.strip()]


def _parse_timeframes(arg: str) -> tuple:
    return tuple(p.strip() for p in arg.split(",") if p.strip())


def _trim_to_days(base_df, days: int):
    """Keep only the trailing `days` days of history (most recent bar's
    timestamp minus `days`), mirroring/bounding the live feed's 60-day cap."""
    if base_df is None or len(base_df) == 0 or not days:
        return base_df
    cutoff = base_df["datetime"].iloc[-1] - timedelta(days=days)
    return base_df[base_df["datetime"] > cutoff].reset_index(drop=True)


def _write_csv(trades: list, path: str) -> None:
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()
        for t in trades:
            row = dict(t)
            row["factors"] = ";".join(row.get("factors") or [])
            writer.writerow(row)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m backtest.run",
        description="Run the SMC backtest engine across pairs and print a summary report.",
    )
    parser.add_argument(
        "--pairs", default="all",
        help="Comma-separated pair list, or 'all' (default) to use config.settings.PAIRS.",
    )
    parser.add_argument(
        "--timeframes", default="15min,1h",
        help="Comma-separated scan timeframes to backtest (default: 15min,1h).",
    )
    parser.add_argument(
        "--days", type=int, default=60,
        help="Trim fetched history to the trailing N days (default: 60, "
             "matching the live data feed's history cap).",
    )
    parser.add_argument(
        "--csv", default=None,
        help="Optional path to write the raw trade list as CSV.",
    )
    return parser


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)

    pairs = _parse_pairs(args.pairs)
    timeframes = _parse_timeframes(args.timeframes)

    print(f"Backtesting {len(pairs)} pair(s): {', '.join(pairs)} "
          f"| timeframes={list(timeframes)} | days={args.days} "
          f"| ENABLE_LONG={settings.ENABLE_LONG} (from env — LONG candidates "
          f"are silently dropped in evaluate() when False)", flush=True)

    all_trades = []
    for pair in pairs:
        print(f"[{pair}] fetching candles...", flush=True)
        base_df = get_candles(pair, "15min", 10000)
        if base_df is None or len(base_df) == 0:
            print(f"[{pair}] no data available, skipping", flush=True)
            continue

        base_df = _trim_to_days(base_df, args.days)
        print(f"[{pair}] running engine over {len(base_df)} 15m bars...", flush=True)

        t0 = time.time()
        trades = run_pair(pair, base_df, timeframes=timeframes)
        elapsed = time.time() - t0

        print(f"[{pair}] done: {len(trades)} trades in {elapsed:.1f}s", flush=True)
        all_trades.extend(trades)

    # Each pair's trades are chronological on their own, but concatenation
    # across pairs isn't — summarize()/factor_edge() require entry_ts order
    # for max_drawdown_pips to reflect a real historical equity curve.
    all_trades.sort(key=lambda t: t["entry_ts"])

    summary = summarize(all_trades)
    rows = factor_edge(all_trades)
    print(render_text(summary, rows))

    if args.csv:
        _write_csv(all_trades, args.csv)
        print(f"Wrote {len(all_trades)} trades to {args.csv}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
