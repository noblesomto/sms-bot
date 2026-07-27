# Backtest Root Cause Analysis — 2026-07-27

Companion to `2026-07-27-baseline.txt` / `.csv` (38 trades, 7 pairs, 15min+1h,
60-day lookback, generated via `python -m backtest.run`). This is the first
backtest run produced since the Phase 2 backtester (`backtest/engine.py`,
`backtest/report.py`) shipped — filed here per the Phase 2 plan's Task 4
convention (`docs/superpowers/plans/2026-07-25-signal-quality-phase2.md`).
The original run output also sits at `doc/backtest_others.csv` (ad-hoc
location, untracked); this is the canonical copy.

## Summary of baseline results

Overall: 38 trades, 23.7% win rate, expectancy **−4.15 pips**, profit factor
0.91, max drawdown 1028.7 pips.

- LONG: 16 trades, 6.2% win rate, PF 0.02, expectancy −53.44 pips.
- SHORT: 22 trades, 36.4% win rate, PF 1.81, expectancy +31.70 pips.
- 1h timeframe carries 994.8 of the 1028.7 pips of drawdown; 15min is PF 2.00.
- Score 7 (PF 0.03) underperforms score 6 (PF 1.45) and score 8 (PF 1.03).
- Full per-pair/per-timeframe/per-factor tables in `2026-07-27-baseline.txt`.

This reproduces the same shape of problem flagged from live data before
Phase 2 started (spec changelog: "31 resolved, net −457 pips... score 8 did
worse than 7... Spring 0W/3L... −506-pip LONG bleed").

## Finding 1 — LONG-side bleed: HTF trend_bias too permissive during RANGING (spec §3.3 priority 2)

**Root cause, confirmed with live data.** `core/structure.get_trend_bias()`
(lines 115-138) requires the *last two* confirmed swing highs to be higher
AND the last two confirmed swing lows to be higher to return `"BULLISH"`
(mirror for `"BEARISH"`); any pattern that doesn't cleanly satisfy both at
once — extremely common mid-trend, since real swings rarely alternate
perfectly — collapses to `"RANGING"`.

`core/strategy.evaluate()` Filter 2 (lines 141-147) only blocks a LONG
candidate when the 4H HTF bias is explicitly `"BEARISH"`; a `"RANGING"` read
is treated as no-conflict and lets the candidate through. So whenever the
HTF classifier fails to recognize an ongoing downtrend as `"BEARISH"` (i.e.
reads `"RANGING"` instead — shown below to be common), LONG entries pass
the gate unopposed.

**Compounding factor:** when no order block is at the current price, the
candidate *direction* itself comes from `structure["trend_bias"]` computed
on the **scan timeframe** (1h/15min), not the 4H HTF view — a much noisier,
shorter window. A local bounce on the 1h chart inside a larger 4H decline is
enough to fire a fallback LONG candidate, which the leaky Filter 2 then
fails to reject.

**Verification (fresh NAS100 data, fetched live, not from the backtest CSV):**
walked `analyze_structure()` on a trailing 200-bar 4H view exactly as
`backtest/engine.py`/`core/strategy.py` do, across 2026-07-13→07-26:

| Date range (4H) | NAS100 close | HTF trend_bias |
|---|---|---|
| 07-14 20:00 → 07-15 20:00 | 29786 → 29690 | BULLISH |
| 07-16 00:00 → 07-19 20:00 | 29737 → 28868 (real ~3% decline) | **RANGING** |
| 07-20 04:00 → 07-21 12:00 | 28911 → 29259 | BEARISH |
| 07-21 16:00 → 07-24 04:00 | 29310 → 28614 (real ~2.4% decline) | **RANGING** |
| 07-24 08:00 → 07-26 20:00 | 28680 → 28727 | BEARISH |

NAS100 fell ~29786 → ~28282 (−5%) over this window, but the 4H bias spent
most of the decline reading `RANGING`, not `BEARISH`.

**Effect on the backtest:** 15 of 16 LONG trades resolved as `SL`, landing
almost exactly at the fixed max-SL-distance cap (−50.0 NAS100, −80.0 US30 —
`core/strategy._max_sl_distance`), meaning price ran straight to the stop
without ever tagging TP1. Every NAS100/US30 LONG loss in the CSV falls
inside 2026-07-07→07-23 — the exact window verified above as a real decline
misclassified as `RANGING` on the 4H read.

## Finding 2 — Wyckoff Spring/Accumulation's negative edge is inherited, not a detector bug

`detect_spring()` and `detect_utad()` (`core/wyckoff.py`) are structurally
symmetric — compared line-by-line, no asymmetry in the bullish vs. bearish
detection logic. Every Spring-tagged trade is a LONG trade, and LONG
direction is gated by the same leaky Filter 2 from Finding 1 — so Spring's
poor factor-edge showing (−52 to −66 pips edge in the baseline report) is a
symptom of the LONG-side bleed, not a defect in the Spring pattern match
itself.

Secondary, lower-confidence observation: `identify_trading_range()`'s
`preceding_trend` classification (`core/wyckoff.py` lines 76-83, decides
ACCUMULATION vs. DISTRIBUTION context) is a single-point close-to-close
percent change over ~60 candles — the same class of "too coarse for choppy
trending markets" fragility as `get_trend_bias()`. Worth revisiting
alongside Finding 1's fix rather than separately.

## Finding 3 — "score 7 underperforms score 6" is small-sample noise, not a scoring defect

Broke down score 6 (n=24) and score 7 (n=10) trade-by-trade. Score 6's
positive expectancy is carried almost entirely by three large US30 TP2
trend trades (+569, +421, +300 pips) that happened to land in that bucket;
score 7 had no equivalent outlier (best trade +19 pips) despite similar
factor combinations otherwise. At n=10-24 per score bucket, one or two
fat-tailed TP2 outcomes on index pairs dominate the average — expected
variance at this sample size, not evidence that higher confluence makes a
signal worse.

**Do not tune the confluence scoring formula off this comparison** — needs
a materially larger sample (more days and/or more pairs) before per-score
expectancy differences are trustworthy.

## Recommendations (ranked per spec §3.3 priority order)

1. **(Spec priority 2, now evidenced concretely)** Fix the HTF gate before
   anything else: either require `get_trend_bias()` agreement (not just
   absence of explicit conflict) before allowing a counter-to-recent-price
   directional entry when HTF reads `RANGING`, or make `get_trend_bias()`
   itself less binary-fragile (e.g. don't require simultaneous HH+HL/LH+LL
   from only the last two swings — widen the swing window or add a
   slope/EMA tie-breaker for mixed patterns).
2. **(Spec priority 1)** Hold off on confluence factor reweighting
   (Spring down-weight, etc.) until Finding 1 is fixed and re-backtested —
   Spring's negative edge may resolve on its own once the LONG-direction
   leak closes; reweighting now risks masking the real defect.
3. **(Spec priorities 3-4)** 1h minimum score and `TF_EXPIRY_HOURS` tuning:
   hold. Current sample (10-25 trades per slice) is too small to tune
   confidently — revisit after a longer/larger backtest, ideally after
   Finding 1's fix changes the trade mix.
4. Do not act on the raw score-6-beats-score-7 ordering (Finding 3) — not
   statistically meaningful at this sample size.

## Process note

No tuning changes were made. Per spec §3.3, a strategy parameter change
ships only after a backtest shows improved expectancy without materially
worse drawdown versus the current configuration on the same data, and
requires sign-off before deploy.
