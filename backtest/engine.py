"""Historical replay engine for the SMC scanner.

Walks a 15m base DataFrame bar-by-bar, calling the exact same decision
function the live scanner uses (`core.strategy.evaluate`) at every closed
15m bar, and simulates each accepted signal forward against subsequent
15m wicks using the same SL-first ladder semantics as
`scheduler._resolve_outcome` / `scheduler.check_signal_status`.

HTF note: live scans use `settings.HTF_TIMEFRAME` (configurable, currently
"1day"/"4h" depending on env) for the 4H/HTF bias input. This engine always
resamples the 15m base slice to "4h" for its HTF view — that is the
engine's fixed HTF definition, documented here rather than driven by
`settings.HTF_TIMEFRAME`, so a backtest run is reproducible independent of
whatever the live `.env` currently has configured.

Two public entry points:
  - `run_pair(pair, base_df, timeframes=("15min", "1h"))` — the walk-forward
    driver. Builds the views `evaluate()` would have seen at each bar close,
    applies the position-book gate (Filters 6/7 mirror), and for every
    accepted signal calls `simulate_outcome` to resolve it against the rest
    of the historical data.
  - `simulate_outcome(signal, bars_15m)` — pure function: given an accepted
    signal (with pair/timeframe/direction/entry/sl/tp1/tp2/entry_ts) and the
    15m bars strictly after entry, walks them oldest-to-newest applying
    `scheduler._resolve_outcome`'s SL-first wick ladder plus time-based
    expiry, and returns the resolved trade record. If the bars run out
    before the trade resolves (only possible for the last few trades near
    the end of the historical window), `outcome` is None — `run_pair` drops
    these from its output list since their fate is unknown, but the engine
    correctly leaves them occupying the position book until their (unknown)
    resolution, which means they still block conflicting future signals for
    the remainder of the walk-forward for that pair.

Performance: `evaluate()` runs full structure/OB/FVG/liquidity/Wyckoff
analysis, so calling it at every 15m bar for every scan timeframe is the
dominant cost. Two mitigations are applied:

  1. Resampling 15m -> 1h/4h is bounded to a small trailing window of raw
     bars per call (see `_bounded_resample`) rather than re-resampling the
     entire growing history each time — this keeps that part of the walk
     O(n) instead of O(n^2) without changing results at all: with
     `origin="epoch"` resample bucket boundaries are anchored to absolute
     time, so resampling a sufficiently large trailing slice produces
     byte-identical tail(200) buckets to resampling the full history from
     bar zero.
  2. Documented approximation (per task spec: "if pathologically slow,
     evaluate every 15m bar for the 15min timeframe but only on hour-close
     bars for the 1h timeframe"): a full brute-force run (evaluate() called
     for both "15min" and "1h" at every 15m bar) measured at >10 minutes and
     still climbing for a single pair on ~5,600 real 15m bars, projecting
     well past the 15-min/pair budget. `run_pair` therefore only calls
     `evaluate(pair, "1h", ...)` on 15m bars whose close lands on the hour
     boundary (bar open-minute == 45, i.e. the 4th 15m bar of the hour, the
     point at which the resampled 1h bucket has just closed) — every other
     15m bar still gets a full "15min" timeframe evaluation. This is a real
     approximation, not a free optimization: live re-evaluates the 1h
     timeframe on the same 15-min scan cadence as everything else, so a
     signal whose conditions were only briefly true on a partially-formed
     (in-progress) 1h candle at :00/:15/:30 and no longer true once that
     candle closed at :45 would be caught live but missed here. Trading off
     that narrow slice of intra-hour-candle signals for a >10x reduction in
     "1h" timeframe evaluations (from every 15m bar to 1-in-4) is the choice
     the task spec explicitly allows when brute force is pathologically
     slow — reported in the task report rather than silently applied.
"""
from datetime import timedelta

import pandas as pd

from core.strategy import evaluate
from core.data_feed import _resample_ohlcv
from scheduler import TF_EXPIRY_HOURS, _resolve_outcome, _calc_pips

# 15m bars of warm-up required before the first decision bar.
WARMUP_BARS = 200

# View length evaluate() expects (mirrors live's outputsize=200 fetch).
VIEW_BARS = 200

# 15m bars per resampled bucket, used to bound how much raw history each
# resample call needs to touch (see module docstring: Performance).
_BARS_PER_BUCKET = {"1h": 4, "4h": 16}
_RESAMPLE_BUFFER_BUCKETS = 8   # extra buckets so the oldest kept bucket in
                               # the tail(200) is never a truncation artifact

# This engine's fixed HTF definition — see module docstring "HTF note".
_ENGINE_HTF_RULE = "4h"

# Documented performance approximation (see module docstring "Performance"):
# when True, the "1h" scan timeframe is only evaluated on 15m bars whose
# close lands on the hour boundary (open-minute == 45) instead of every
# 15m bar. Exposed as a module flag so tests can force brute-force mode.
HOUR_CLOSE_ONLY_FOR_1H = True


def _bounded_resample(base_df: pd.DataFrame, i: int, rule: str) -> pd.DataFrame:
    """Resample base_df[:i+1] to `rule`, keeping only the trailing VIEW_BARS
    buckets, without re-resampling the full (growing) history each call."""
    bars_per_bucket = _BARS_PER_BUCKET[rule]
    needed = (VIEW_BARS + _RESAMPLE_BUFFER_BUCKETS) * bars_per_bucket
    start = max(0, i + 1 - needed)
    chunk = base_df.iloc[start:i + 1]
    return _resample_ohlcv(chunk, rule).tail(VIEW_BARS).reset_index(drop=True)


def _raw15_view(base_df: pd.DataFrame, i: int) -> pd.DataFrame:
    start = max(0, i + 1 - VIEW_BARS)
    return base_df.iloc[start:i + 1].reset_index(drop=True)


def _build_views(base_df: pd.DataFrame, i: int, tf: str,
                  resampled_1h: pd.DataFrame, resampled_4h: pd.DataFrame):
    """Return (scan_view, htf_view, itf_view) visible at bar i for timeframe tf,
    or None for scan_view if the timeframe must be skipped (<200-bar view)."""
    htf_view = resampled_4h
    if tf == "15min":
        scan_view = _raw15_view(base_df, i)
        itf_view = resampled_1h
    elif tf == "1h":
        scan_view = resampled_1h
        itf_view = None
    else:
        raise ValueError(f"unsupported timeframe for backtest engine: {tf!r}")

    if len(scan_view) < VIEW_BARS:
        return None, None, None
    return scan_view, htf_view, itf_view


def run_pair(pair: str, base_df: pd.DataFrame, timeframes=("15min", "1h")) -> list:
    """Walk base_df (15m OHLCV, oldest-first) bar by bar and return the list
    of resolved trade records produced by calling `evaluate()` exactly as the
    live scanner would at every closed 15m bar, subject to the position-book
    gate (Filters 6/7 mirror, binding per the task spec).

    Determinism: pure function of (pair, base_df, timeframes) — no wall-clock
    or random state is consulted anywhere in the walk.
    """
    base_df = base_df.reset_index(drop=True)
    n = len(base_df)
    trades = []
    # Each entry: {"timeframe", "direction", "entry_ts", "exit_ts"} — exit_ts
    # is None for a trade that never resolved within the dataset, which means
    # it keeps blocking the book for the rest of the walk (see module docstring).
    open_positions = []

    for i in range(WARMUP_BARS, n):
        t_ts = base_df["datetime"].iloc[i]
        # df["datetime"] is the bar OPEN time; the decision/entry moment is the
        # bar CLOSE (open + 15m). Kill-zone checks and the expiry deadline are
        # anchored to the close so they match live semantics (created_at is set
        # at alert time ≈ bar close). Book bookkeeping stays on the uniform
        # open-time convention (entry/exit comparisons are self-consistent).
        t_close = t_ts + timedelta(minutes=15)

        # Free positions that have already resolved by this bar's timestamp.
        open_positions = [
            p for p in open_positions
            if p["exit_ts"] is None or p["exit_ts"] > t_ts
        ]

        resampled_1h = _bounded_resample(base_df, i, "1h")
        resampled_4h = _bounded_resample(base_df, i, _ENGINE_HTF_RULE)

        for tf in timeframes:
            # Documented performance approximation (see module docstring):
            # only evaluate "1h" on the 15m bar whose close lands on the
            # hour boundary — brute-forcing every 15m bar for both
            # timeframes measured well past the 15-min/pair budget.
            if tf == "1h" and HOUR_CLOSE_ONLY_FOR_1H and t_ts.minute != 45:
                continue

            scan_view, htf_view, itf_view = _build_views(
                base_df, i, tf, resampled_1h, resampled_4h
            )
            if scan_view is None:
                continue   # <200-bar view for this timeframe — skip (binding rule)

            result = evaluate(pair, tf, scan_view, htf_view, itf_view, now=t_close)
            sig = result["signal"]
            if not sig:
                continue

            direction = sig["direction"]
            opposite = "SHORT" if direction == "LONG" else "LONG"

            # Filter 6 mirror: reject if an opposite-direction position is open
            # for this pair (any timeframe).
            if any(p["direction"] == opposite for p in open_positions):
                continue
            # Filter 7 mirror: reject if a same-timeframe+same-direction
            # position is already open for this pair.
            if any(p["timeframe"] == tf and p["direction"] == direction
                   for p in open_positions):
                continue

            entry_price = float(base_df["close"].iloc[i])
            trade_seed = {
                "pair": pair, "timeframe": tf, "direction": direction,
                "score": sig["score"], "factors": sig["factors"],
                "entry_ts": t_close, "entry": entry_price,
                "sl": sig["invalidation"], "tp1": sig["target1"], "tp2": sig["target2"],
            }
            future_bars = base_df.iloc[i + 1:]
            trade = simulate_outcome(trade_seed, future_bars)

            open_positions.append({
                "timeframe": tf, "direction": direction,
                "entry_ts": t_ts, "exit_ts": trade["exit_ts"],
            })
            if trade["outcome"] is not None:
                trades.append(trade)
            # else: ran off the end of the dataset unresolved — dropped from
            # the output list, but still occupies the book above.

    return trades


def simulate_outcome(signal: dict, bars_15m: pd.DataFrame) -> dict:
    """Resolve one accepted signal against the 15m bars that follow entry.

    `signal` must provide: pair, timeframe, direction, score, factors,
    entry_ts, entry, sl, tp1, tp2.
    `bars_15m`: 15m OHLCV bars strictly after entry_ts, oldest-first.

    Returns the full trade record: {pair, timeframe, direction, score,
    factors, entry_ts, entry, sl, tp1, tp2, outcome, exit_ts, exit_price,
    pnl_pips, expired}. `outcome` is None (exit_ts/exit_price/pnl_pips also
    None) if bars_15m is exhausted before the trade resolves.

    Ladder/blend rules (mirrors scheduler.check_signal_status exactly, plus
    the backtest-only TP1_ONLY_EXPIRED terminal state — see task spec):
      - SL-first wick precedence per bar (scheduler._resolve_outcome).
      - TP1 leg pnl = pips(entry -> tp1).
      - TP2 after TP1: 0.5*tp1_leg + 0.5*pips(entry -> tp2).
      - SL after TP1 ("SL_AFTER_TP1"): 0.5*tp1_leg + 0.5*pips(entry -> sl).
      - Plain expiry (never hit TP1): pips(entry -> expiry bar close).
      - TP1 then expiry ("TP1_ONLY_EXPIRED"): 0.5*tp1_leg + 0.5*pips(entry -> expiry bar close).
      - Blended pnl is rounded to 1 decimal, matching live.
    """
    pair = signal["pair"]
    tf = signal["timeframe"]
    direction = signal["direction"]
    entry = signal["entry"]
    sl = signal["sl"]
    tp1 = signal["tp1"]
    tp2 = signal["tp2"]
    entry_ts = signal["entry_ts"]

    trade = {
        "pair": pair, "timeframe": tf, "direction": direction,
        "score": signal.get("score"), "factors": signal.get("factors"),
        "entry_ts": entry_ts, "entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2,
        "outcome": None, "exit_ts": None, "exit_price": None,
        "pnl_pips": None, "expired": False,
    }

    expiry_hours = TF_EXPIRY_HOURS.get(tf, 48)
    expiry_deadline = entry_ts + timedelta(hours=expiry_hours)

    status = "ACTIVE"
    tp1_pnl = None

    def pips(price):
        diff = (price - entry) if direction == "LONG" else (entry - price)
        return _calc_pips(pair, diff)

    for _, bar in bars_15m.iterrows():
        ts = bar["datetime"]
        hi, lo, close = float(bar["high"]), float(bar["low"]), float(bar["close"])

        result = _resolve_outcome(direction, status, hi, lo, tp1, tp2, sl)
        if result:
            kind, price = result
            if kind == "TP1":
                status = "TP1_HIT"
                tp1_pnl = pips(price)
                # Not terminal — fall through to the expiry check below in
                # case this same bar also crosses the deadline.
            elif kind == "TP2":
                tp2_leg = pips(price)
                pnl = round(0.5 * tp1_pnl + 0.5 * tp2_leg, 1) if tp1_pnl is not None else tp2_leg
                trade.update(outcome="TP2", exit_ts=ts, exit_price=price,
                             pnl_pips=pnl, expired=False)
                return trade
            elif kind == "SL":
                trade.update(outcome="SL", exit_ts=ts, exit_price=price,
                             pnl_pips=pips(price), expired=False)
                return trade
            elif kind == "SL_AFTER_TP1":
                sl_leg = pips(price)
                pnl = round(0.5 * tp1_pnl + 0.5 * sl_leg, 1)
                trade.update(outcome="SL_AFTER_TP1", exit_ts=ts, exit_price=price,
                             pnl_pips=pnl, expired=False)
                return trade

        if ts > expiry_deadline:
            if status == "TP1_HIT":
                expiry_leg = pips(close)
                pnl = round(0.5 * tp1_pnl + 0.5 * expiry_leg, 1)
                trade.update(outcome="TP1_ONLY_EXPIRED", exit_ts=ts, exit_price=close,
                             pnl_pips=pnl, expired=True)
            else:
                trade.update(outcome="EXPIRED", exit_ts=ts, exit_price=close,
                             pnl_pips=pips(close), expired=True)
            return trade

    return trade   # ran out of bars before resolving — outcome stays None
