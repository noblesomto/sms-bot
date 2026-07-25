# Signal Quality Phase 2 Implementation Plan — Backtester

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the replay backtester from spec §3 (`docs/superpowers/specs/2026-07-24-signal-quality-program-design.md`): extract a pure `evaluate()` shared by the live scanner and the backtester (zero live behavior change, golden-test gated), then a walk-forward engine + report that simulates the user's exact trading reality over 60 days of history.

**Architecture:** `core/strategy.evaluate()` becomes the single signal-decision function. `scheduler.scan()` keeps only IO (fetch, DB filters 6/7, persist, alert, chart). `backtest/` walks 15m history bar-by-bar per pair, calls `evaluate()` with resampled views and `now`=bar time, simulates fills against a position book, and reports expectancy/per-factor edge.

**Tech Stack:** Python 3.12, pandas, pytest (`./venv/bin/pytest`). Backtester runs locally only — never on the VPS.

## Global Constraints

- Task 1 must produce ZERO change in live signal behavior — gated by a golden test capturing pre-refactor outputs on frozen fixtures and asserting identity post-refactor.
- The engine must mirror the user's reality exactly as Phase 1 defined it: market entry at the alert bar's close; wick-touch TP/SL on subsequent **15m** bars with **SL-before-TP** same-bar precedence; TP1→TP1_HIT ladder with live's 50/50 pnl blending; `TF_EXPIRY_HOURS` expiry with the simulated trade closed at the expiry bar's close (no NULL outcomes in sim).
- Position-book rules mirror scheduler Filters 6/7: no opposite-direction open position for the same pair (any timeframe); no duplicate open position for the same pair+timeframe+direction.
- Documented approximations (print in report header): one evaluation per 15m bar close vs live's 5-min cadence; 60-day history cap; no spread/slippage in v1.
- No strategy-parameter changes in this phase (weights, thresholds, filters stay as-is) — tuning is a separate, backtest-gated step per spec §3.3.
- Commits end with: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`

---

### Task 1: Extract `evaluate()` with golden parity gate

**Files:**
- Create: `core/strategy.py`, `tests/golden/capture.py`, `tests/golden/fixtures/` (recorded candles), `tests/golden/expected.json`, `tests/test_golden.py`
- Modify: `scheduler.py` (`scan()` body)

**Interfaces:**
- Produces: `core.strategy.evaluate(pair: str, timeframe: str, df, htf_df, itf_df, now) -> dict` where `df` is the scan-TF candles (200 bars), `htf_df` the 4h candles (may be None), `itf_df` the 1h candles (only for 5min/15min scans, else None), `now` an aware UTC datetime used for every kill-zone/session decision (pass `dt=now` to `is_in_kill_zone`/`get_current_session` — they already accept it).
- Return dict: `{"signal": None | {direction, score, max_score, factors, entry_low, entry_high, target1, target2, invalidation, entry_price, kz_name, session}, "analysis": {obs, fvgs, all_levels, structure, wyckoff_ctx, df}}` — `analysis` carries everything `scan()`'s alert/chart code consumed; `entry_price` is `current_price` (last close of df); `df` inside analysis is the structure-annotated frame.

**Steps:**

- [ ] **Step 1: Record fixtures.** Script (one-off, then delete or keep in tests/golden/) that uses `core.data_feed.get_candles` to fetch and save candles for 4 pairs (`XAU/USD`, `EUR/USD`, `GBP/JPY`, `US30`) × timeframes (`15min`, `1h`, `4h`) as parquet files `tests/golden/fixtures/{PAIR_SAFE}_{TF}.parquet`. Commit the fixtures.
- [ ] **Step 2: Capture golden BEFORE refactor.** `tests/golden/capture.py`: monkeypatches `scheduler.get_candles` (loads fixture parquet by (pair, tf)), `scheduler.send_alert` (async no-op recording calls), `scheduler.generate_chart` (returns None), `scheduler.SessionLocal` (sessionmaker on a fresh temp sqlite with tables created from `db.models`), and `scheduler.is_in_kill_zone`/`scheduler.get_current_session` (fixed: `(True, "LONDON_OPEN")` / `"LONDON"` — determinism). Runs `asyncio.run(scan(pair, tf))` for all 4 pairs × (15min, 1h); dumps per-run: the result dict and every Signal row (all columns except timestamps/id) to `tests/golden/expected.json`. Run it on the CURRENT code; commit `expected.json`.
- [ ] **Step 3: Extract.** Move the analysis-and-decision block of `scan()` (everything from `structure = analyze_structure(df)` through the R:R filter, INCLUDING candidates/scoring/filters 1-5 and `_calc_risk`, EXCLUDING fetches, Filters 6/7, `_save_signal`, alert, chart) into `core.strategy.evaluate()` per the interface above. `_calc_risk`, `_check_rr`, `_min_sl_buffer`, `_max_sl_distance`, `_has_dol`, `_has_ob_rejection`, `_OB_MAX_AGE` move to `core/strategy.py` (they are pure); scheduler imports what it still references. `scan()` becomes: fetch dfs → `res = evaluate(...)` → if `res["signal"]`: Filters 6/7 → save/alert/chart using `res`. Keep log lines equivalent.
- [ ] **Step 4: Golden test.** `tests/test_golden.py` reruns the capture harness against the refactored code and asserts output identity with `expected.json`. Run full suite: `./venv/bin/pytest tests/ -q` — all green including golden.
- [ ] **Step 5:** Commit.

---

### Task 2: Replay engine + outcome simulation

**Files:**
- Create: `backtest/__init__.py`, `backtest/engine.py`
- Test: `tests/test_backtest_engine.py`

**Interfaces:**
- Consumes: `core.strategy.evaluate(...)` (Task 1), `core.data_feed._resample_ohlcv`, `core.data_feed.get_candles`, `scheduler.TF_EXPIRY_HOURS` (import or duplicate as constant with comment), `scheduler._resolve_outcome`-equivalent logic.
- Produces: `backtest.engine.run_pair(pair: str, base_df, timeframes=("15min","1h")) -> list[dict]` — walk-forward over a 15m base DataFrame; and `backtest.engine.simulate_outcome(signal: dict, bars_15m) -> dict`. Trade record dict: `{pair, timeframe, direction, score, factors, entry_ts, entry, sl, tp1, tp2, outcome, exit_ts, exit_price, pnl_pips, expired: bool}`. `outcome ∈ {"TP2","TP1_ONLY_EXPIRED","SL","SL_AFTER_TP1","EXPIRED"}`.

**Engine rules (binding):**
- Warm-up 200 15m bars; then at each subsequent 15m bar close `t`: for each scan timeframe, build views visible at `t` (15m slice `[:t]`; 1h/4h resampled from the slice via `_resample_ohlcv`, tail(200)); skip the timeframe if its view has <200 bars; call `evaluate(pair, tf, view, htf_view, itf_view, now=t)`.
- Position book gates BEFORE accepting a signal (Filters 6/7 mirror, per Global Constraints).
- On acceptance: entry = the 15m bar's close at `t`, SL/TP from the signal; simulate forward on subsequent 15m bars: SL-first wick precedence (reuse the same ladder semantics as `scheduler._resolve_outcome`, ACTIVE→TP1_HIT states); expiry after `TF_EXPIRY_HOURS[tf]` → close at that bar's close, outcome "EXPIRED", pnl = signed diff in pips (use `scheduler._calc_pips` or move it to a shared module). TP1-then-expiry closes at expiry bar close with 50/50 blend, outcome "TP1_ONLY_EXPIRED".
- Kill-zone note: `evaluate` receives `now=t` so 15min signals honor kill zones historically; weekends naturally absent from candle data.
- Determinism: same inputs → identical trade list.

**Steps:** TDD — tests first with synthetic 15m frames engineered to (a) produce a scripted signal via a stub `evaluate` (monkeypatched) and verify entry/SL-first/TP1-ladder/expiry accounting and position-book blocking; (b) verify warm-up and <200-bar skip; then implement; full suite green; commit.

---

### Task 3: Report + CLI

**Files:**
- Create: `backtest/report.py`, `backtest/run.py`
- Test: `tests/test_backtest_report.py`

**Interfaces:**
- Consumes: trade-record list from `backtest.engine.run_pair`.
- Produces: `backtest.report.summarize(trades: list[dict]) -> dict` with overall + sliced stats: n, wins (TP2/TP1_ONLY_EXPIRED/SL_AFTER_TP1 count as wins iff pnl_pips>0), win_rate, expectancy_pips, profit_factor, max_drawdown_pips (on cumulative pnl in trade order), sliced by pair / timeframe / direction / score; and `factor_edge(trades) -> list[dict]` per factor: n_with, win_rate_with, avg_pips_with, win_rate_without, avg_pips_without, edge_pips (avg_with − avg_without). `render_text(summary, factor_rows) -> str` (aligned tables; header lists the documented approximations). `run.py`: `python -m backtest.run [--pairs A,B|all] [--timeframes 15min,1h] [--days 60] [--csv out.csv]` — fetches base data via `get_candles(pair, "15min", 10000)`, runs engine per pair, prints report, optional CSV of raw trades.
- Factor names: normalize by stripping the parenthesised suffix (e.g. "Price at Bullish OB (4022.5 – 4041.0)" → "Price at Bullish OB"), same normalization for all aggregation.

**Steps:** TDD on `summarize`/`factor_edge` with a hand-built trade list (assert exact expectancy/PF/drawdown numbers); implement; wire CLI; full suite green; commit.

---

### Task 4 (inline, controller): Deploy refactor + first real backtest run

- [ ] Deploy the Task-1 refactor to the VPS (scheduler.py + core/strategy.py + any moved imports), restart, verify a clean scan cycle (zero live behavior change expected).
- [ ] Run `python -m backtest.run --pairs all` locally; save output to `docs/superpowers/backtests/2026-07-25-baseline.txt` + CSV; commit.
- [ ] Present results to the user with tuning recommendations ranked per spec §3.3 priorities. No tuning changes without their sign-off.

## Self-review notes
- Spec §3.1→Task 1 (sessions already take `dt` — no separate task needed), §3.2→Tasks 2-3, §3.3 workflow→Task 4 presentation; rollout §5.2-5.3 honored (golden gate before deploy; backtester never on VPS).
- `_calc_pips`/`_resolve_outcome` reuse: engine may import from `scheduler` (import is side-effect-light) or the implementer may move them to a shared `core/` module — either is acceptable; keep one definition, no duplication.
