# Profitability Roadmap — 2026-08-24

**Status:** Phases 0–2 + kill-switch implemented this commit; Phase 3 is a
measurement period; Phase 4 deliberately deferred.
**Evidence base:** VPS production DB fetched 2026-08-24 08:22 UTC —
55 signals Jul 1 → Aug 21 (7.3 weeks).

---

## 1. Where the evidence says we are

| Slice | n | Net pips | Verdict |
|---|---|---|---|
| Whole ledger | 55 | −476.2 | Not profitable |
| LONG | 26 | **−920.4** (3 wins, 11.5% WR) | Documented failure mode in every root-cause doc |
| SHORT | 29 | **+444.2** (7 wins, 24.1% WR) | The only profitable leg |
| US30 | 11 | +475.8 | Fix5b working as backtested |
| NAS100 | 11 | −352.7 | Negative across every window/config tested |
| XAU/USD | 15 | −637.0 | Mixed eras; pre-fix losses dominate |
| Forex majors | 1–4 each | ±noise | Signal starvation, untunable |

Weekly bleed trajectory: **−114 pips/wk** (pre-07-24) → **−13 pips/wk**
(phase 2). The fix program works but hasn't crossed zero.

Honest caveats: samples are tiny (n=11 per index); no spread/slippage
modeled; "pips" mix incomparable units (index points / gold $0.10 / forex
pips); 25% of outcomes (14 EXPIRED) carry NULL PnL and are invisible to the
scoreboard.

## 2. The plan and what "done" means

Theme: **stop losing first, measure honestly second, tune third, size
fourth.** Success criterion for the whole program: ≥100 post-cutover
resolved signals with positive expectancy *net of spread*, max drawdown
contained, same sign across two regimes.

### Phase 0 — Reconcile repo ↔ production ✅ (this commit)
Committed `e108541`: Fix4 (LONG reversal guard), Fix5b (ATR SL + R:R 1.5
indices), OB-age widening — all already live on the VPS since 07-28.

### Phase 1 — Cut the documented bleed (implemented here)
1. **SHORT-only evaluation mode.** New `ENABLE_LONG` setting (env-driven,
   default `true` so behavior changes only when explicitly deployed).
   When false, `evaluate()` discards LONG candidates before scoring.
   Rationale: LONG = 3 wins / 26 trades / −920 pips live; every mechanical
   LONG guard shipped so far reduced but did not eliminate the bleed.
2. **NAS100 out of the default pair list** (`.env.example` + roadmap note:
   remove from VPS `PAIRS` at deploy). Negative in every backtest window
   and in live (−352.7 pips / 11 trades).

### Phase 2 — Make the scoreboard honest (implemented here)
3. **R-multiple tracking.** New `signals.pnl_r` column: pnl_pips ÷ risk
   distance (entry→SL, converted with the same pip conventions). Index
   points, gold dollars and forex pips become comparable; portfolio-level
   expectancy becomes meaningful.
4. **Expiry mark-to-close.** EXPIRED rows now record realized-at-expiry
   `pnl_pips`/`pnl_r` instead of NULL (matches backtester semantics; the
   old NULL convention hid 25% of outcomes). The Telegram expiry alert is
   unchanged.
5. **Spread deduction.** Per-pair spreads (`SPREADS_JSON` env-overridable,
   conservative defaults documented in config.py) subtracted from gross
   pnl at every resolution: TP1, TP2, SL, SL_AFTER_TP1, expiry. Until
   real fills prove otherwise, forex expectancies within ~1 pip of zero
   must be read as noise.
6. **HTF regime tag.** New `signals.htf_regime` column (UP/DOWN/FLAT over
   the last 20 HTF closes, ±2% threshold — same spirit as Fix1's veto).
   Lets future analysis answer "was the SHORT edge real or was July just
   bearish?" without re-deriving candles.

### Phase 2½ — Circuit breaker (implemented here)
7. **Kill-switch check** (hourly): with ≥20 resolved R-multiples on file,
   trailing mean ≤ **−0.5R** fires a Telegram alert (max once per 7 days)
   and logs critical. It does NOT auto-disable scanning — that stays a
   human decision — but it makes a slow re-bleed impossible to miss.

### Phase 3 — Measurement period (~8–12 weeks, no code)
Run SHORT-only. At ≥100 resolved post-cutover signals, revisit under spec
§3.3 discipline: confluence factor weights, `TF_EXPIRY_HOURS` (scheduler),
whether any forex pair earns compute, re-enabling LONG (only if its own
trailing ledger turns positive net-of-spread across ≥30 trades).

### Phase 4 — Position sizing (deferred by design)
Equal-risk sizing, concurrent-exposure caps, weekly loss limits. A signal
service becomes a trading system when sizing rules exist — but sizing on
top of unproven expectancy just industrializes variance. Gate it behind a
positive Phase 3 report.

## 3. Deploy checklist (VPS)

```
git pull && venv/bin/python -m pytest -q        # expect green
# .env changes:
#   ENABLE_LONG=false
#   PAIRS=US30,XAU/USD,XAG/USD,EURUSD,...   (drop NAS100)
scp core/strategy.py core/scheduler.py config.py db/models.py db/database.py alerts/formatter.py root@VPS:/root/smc_bot/...
systemctl restart smc_bot && curl -s localhost:8000/health
```
Migration runs automatically on startup (`_migrate_db`, additive columns
only — no data loss, old rows keep NULL pnl_r/regime).

## 4. Risks

| Risk | Mitigation |
|---|---|
| SHORT edge is regime luck (Jul–Aug chop/down tape) | htf_regime tag + Phase 3 regime slicing before declaring victory |
| Spread defaults wrong per broker | SPREADS_JSON env override; calibrate after first month vs user's account |
| Kill-switch threshold too tight/loose | −0.5R ≈ break-even minus buffer at historical 24% WR & ~2.2R avg win; review at Phase 3 |
| SHORT-only starves signal volume further | Volume was ~8/wk total; SHORT share ~53%; monitor weekly count ≥4 or revisit gates |

## 5. Changelog

- **2026-08-24** — Roadmap written; Phases 0–2½ implemented (commit series
  starting `e108541`). Next checkpoint: **+4 weeks after deploy**, compare
  SHORT-only ledger vs the +61 pips/wk projection.
