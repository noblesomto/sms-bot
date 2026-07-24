# Signal Quality Program — Design Spec

**Date:** 2026-07-24
**Status:** Draft, pending user approval
**Owner:** noble
**Approach:** "C" — ship safe mechanical fixes now (Phase 1), then build a
backtester and route every strategy-tuning decision through it (Phase 2).

---

## 1. Background and motivation

Four weeks of live signals (31 resolved) show: 6 target hits, 14 stop-outs,
11 expired untouched, net −457 pips. Trade-taken win rate 30% against a
break-even requirement of ~33% at the enforced 2:1 R:R. The sample is
polluted by two now-fixed bugs (4-decimal pair rounding collapse, SL capped
inside the entry zone), so it understates the fixed bot — but three findings
are structural, not bug artifacts:

1. **Tracking doesn't model how the user trades.** The user enters at market
   the moment an alert arrives. The bot records entry as the zone midpoint
   and resolves TP/SL on candle *closes*, while the user's broker fills on
   wick touches. Tracked stats therefore diverge from the user's account.
2. **The confluence score is not predictive.** Score-8 signals performed
   worse than score-7. Factor-level records contradict several weights
   ("FVG present in zone": 9 wins / 12 losses; "Spring tested": 0/3;
   "Wyckoff Distribution Phase C": 5/2).
3. **There is no way to validate a strategy change except waiting.** At ~8
   signals/week, each tuning iteration costs weeks. The bot already fetches
   60 days of 15m history per pair every cycle and discards it.

**Goals:** keep ~8 signals/week; raise their quality; make tracked results
mean what the user's account experiences; make every future strategy change
evidence-based.

**Non-goals (out of scope):** automated order execution, paid data
providers, economic-news filtering, new pairs/timeframes. News filtering is
the most likely future addition and should not be precluded by the design.

---

## 2. Phase 1 — Honest tracking + safe quality fixes

Four independent changes, each deployable alone. None alters the confluence
scoring or filter thresholds.

### 2.1 Market-entry alignment

*Problem:* `_save_signal` stores `entry_price` = zone midpoint; the R:R
filter measures risk from the zone midpoint. The user fills at market
(e.g., signal #33: zone mid 4031.75 vs market 4036.70 — a 50-gold-pip gap
that silently degrades realized R:R).

*Change:*
- `scheduler.py _save_signal`: store `entry_price = current_price` (market
  price at alert time). Entry zone bounds remain stored and displayed.
- `scheduler.py _check_rr`: compute risk and reward from `current_price`,
  not the zone midpoint. This makes the 2:1 filter honest for market entry;
  marginal setups where the zone mid passed but market price does not will
  (correctly) be dropped.
- `alerts/formatter.py`: alert shows `Entry (market): <current_price>` as
  the primary entry line; the OB zone is shown as context beneath it.

*Data note:* rows created before the cutover keep zone-mid entries. No
migration; the cutover date (deploy date of Phase 1) is recorded at the
bottom of this spec and in assistant memory. Cross-era PnL comparisons are
invalid and stats should be filtered to post-cutover rows.

### 2.2 Wick-aware outcome tracking

*Problem:* `_check_signal_outcomes` resolves TP/SL on the latest candle
*close*. Brokers fill on touch. A wick through SL that closes back inside
records "still active" while the user is already stopped out.

*Change:* in `_check_signal_outcomes`, use the latest candle's `high`/`low`
for touch detection. Precedence within a single candle is conservative:
**SL before TP** (for ACTIVE longs: check `low <= SL` first, then
`high >= TP2`, then `high >= TP1`; mirrored for shorts; same principle in
the TP1_HIT state). Rationale: when both were touched inside one candle,
bar data cannot order the touches, and assuming the loss avoids inflating
results.

### 2.3 Expiry exit notification

*Problem:* expiry silently updates the DB (`continue` without any send).
The market-entered user holds an untracked drifting position.

*Change:* when a signal transitions to EXPIRED, fetch the current price and
send a Telegram message via the existing notification path: pair,
direction, timeframe, entry price, current price, unrealized pips
(`_calc_pips`), and the instruction that the bot has stopped tracking —
close or self-manage. New `format_expiry_alert()` in `alerts/formatter.py`.
Expired signals keep `pnl_pips = NULL` (position outcome is the user's).

### 2.4 Displacement-validated order blocks

*Problem:* `find_order_blocks`/`validate_obs` accept any last-opposing
candle before a BOS. ICT doctrine requires *displacement*: the move leaving
the OB must be impulsive and leave an imbalance; without it the zone is
weak. Many of the 14 stop-outs sat at such weak zones.

*Change:* an OB is kept only if, within the `DISPLACEMENT_WINDOW = 3`
candles after the OB candle, at least one holds:
- cumulative price travel in the OB direction ≥ `DISPLACEMENT_ATR_MULT =
  1.5` × ATR(14) of the scan timeframe, **or**
- those candles create an FVG (reuse `core/fvg.py` detection on that
  3-candle window).

Both constants live in `config.py` (env-overridable like existing
settings). Implemented inside `core/order_blocks.py` so every consumer
(scanner, charts) sees the same filtered set.

*Volume risk:* this prunes zones, but the six 4-decimal forex pairs are
newly back online after the rounding fix, adding volume. Monitor the first
two weeks; if weekly signal count falls below ~5, lower
`DISPLACEMENT_ATR_MULT` to 1.2 before touching anything else.

---

## 3. Phase 2 — Backtester and evidence-based tuning

### 3.1 Shared evaluation core (refactor, no behavior change)

Extract the signal decision from `scheduler.scan()` into a pure function in
a new `core/strategy.py`:

```
evaluate(pair, timeframe, dfs, now) -> EvalResult
  dfs: {"scan": df, "htf": df_4h, "itf": df_1h | None}
  now: aware datetime (kill-zone/session checks use this, never wall clock)
  EvalResult: candidate signal (direction, score, factors, zone, targets,
              invalidation, market price) or the reason none fired
```

Everything currently between "analyze structure" and "Filter 7" moves in,
**except** stateful filters (duplicate/conflicting-signal checks hit the
DB) and side effects (persistence, alert, chart) — those stay in
`scheduler.scan()`. The backtester replicates the stateful filters against
its own simulated position book.

*Refactor safety:* a golden test captures signals produced on a frozen
candle fixture before the refactor and asserts identity after it.

### 3.2 Replay engine

New top-level `backtest/` package:

- `backtest/engine.py` — for each pair: take the 60-day 15m base (via
  `core/data_feed`, one fetch per pair per run), walk forward bar-by-bar
  after a 200-bar warm-up. At each 15m bar close, build the visible-history
  views (15m slice; 1h/4h resampled from the slice with the same
  `_resample_ohlcv` used live), call `evaluate()` for each configured scan
  timeframe with `now` = bar timestamp, and apply the simulated position
  book (no duplicate same-direction ACTIVE, no opposite-direction
  conflict — mirroring Filters 6/7).
- Outcome simulation mirrors Phase-1 live tracking exactly: market entry at
  the closing price of the alert bar; wick-touch TP/SL on subsequent 15m
  bars with SL-before-TP same-bar precedence; TP1 → TP1_HIT ladder;
  `TF_EXPIRY_HOURS` expiry. Expired simulated trades are additionally
  marked to close at expiry-bar close so expectancy includes them (unlike
  live, where outcome is the user's — the simulation must not have NULLs).
- `backtest/report.py` — aggregates: win rate, expectancy (pips/trade),
  profit factor, max drawdown (pips), sliced by pair / timeframe /
  direction / score; per-factor edge table (win rate and avg pips with vs
  without each factor). Text table to stdout + CSV of raw simulated trades.
- `backtest/run.py` — CLI: `python -m backtest.run [--pairs EUR/USD,...]
  [--timeframes 15min,1h] [--days 60]`. Runs locally (not on the VPS); no
  scheduler, DB, or Telegram involvement.

*Known approximations (documented in the report header):*
- Scan cadence is one evaluation per 15m bar close vs live's 5-minute
  cadence — intra-bar alert timing is not modeled.
- 60 days of 15m history is Yahoo's cap: enough for tuning, not an
  all-regime proof.
- No spread/slippage modeling in v1 (constant per-pair spread deduction is
  an acceptable v2 addition).

### 3.3 Tuning workflow (process, enforced by convention)

A strategy parameter changes in production only after a backtest run shows,
versus the current configuration on the same data: (a) expectancy improves,
and (b) max drawdown does not worsen materially (>20%). Each accepted
change is deployed, dated in this spec's changelog, and compared against
live results at +2 weeks.

Priority tuning candidates, in order:
1. Confluence factor reweighting (factors with negative live/backtest edge
   — bare FVG presence, Spring — down or out; UTAD/Distribution up).
2. HTF bias strictness for counter-trend entries (the −506-pip LONG bleed:
   likely `trend_bias` too permissive when 4H is RANGING).
3. 1h-timeframe minimum score (1h alone lost −460 pips).
4. `TF_EXPIRY_HOURS` per timeframe (11/31 expiries suggests windows and/or
   zone-return assumptions need data).

---

## 4. Testing

- **Unit (Phase 1):** wick-outcome resolution (SL-wick candle, TP-wick
  candle, both-in-one-bar → SL precedence, TP1→TP2 ladder); displacement
  rule (impulsive pass, drift fail, FVG-pass without ATR-pass); expiry
  notification formatting; R:R-from-market-price boundary cases.
- **Golden (Phase 2 refactor):** frozen multi-pair candle fixture →
  identical signal set before/after the `evaluate()` extraction.
- **Simulation sanity:** deterministic synthetic price paths with known
  outcomes; determinism across runs; a qualitative check that replaying the
  live period reproduces the character of logged signals (exact identity
  is not expected — live data snapshots differ).
- All tests are one-off scripts under `tests/` runnable with the project
  venv (no framework dependency is currently in `requirements.txt`; adding
  `pytest` is acceptable if convenient).

## 5. Rollout

1. Phase 1 items implemented and unit-tested locally → deployed together to
   the VPS (upload + `systemctl restart smc_bot`) → verified via bot.log,
   `/health`, and the next live alerts. Cutover date recorded below.
2. Phase 2 refactor deployed only after the golden test passes; it must
   produce zero change in live signal behavior.
3. Backtester runs locally on demand; it never runs on the VPS.
4. Tuning changes ship one at a time under the §3.3 rule.

## 6. Risks

| Risk | Mitigation |
|---|---|
| Displacement filter cuts signal volume below target | Constants in config; drop multiplier to 1.2; forex pairs returning adds volume |
| Refactor changes live behavior subtly | Golden parity test gates deployment |
| Backtest overfits 60 days of one regime | §3.3 requires drawdown guard; live +2-week comparison after each change; treat forex/metals/indices as separate cohorts in reports |
| Wick-based SL detection marks more losses than close-based did | Intended — it matches broker reality; expect reported win rate to drop while account fidelity rises |

## 7. Changelog / cutovers

- **2026-07-24 08:56 UTC — Phase 1 deployed to production.** Stats before
  this instant use zone-mid entries and close-based outcomes; rows after it
  use market-at-alert entries and wick-swept outcomes. Cross-era PnL
  comparisons are invalid.
