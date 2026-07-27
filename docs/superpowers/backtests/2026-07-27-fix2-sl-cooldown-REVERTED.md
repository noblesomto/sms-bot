# Fix 2 — SL cooldown (Filter 8): REVERTED 2026-07-27

**Status: reverted.** Implemented, validated on XAU/USD, then found to have
a real flaw once tested on a second pair (NAS100) — reverted same day
before deployment. Keeping this doc as a record of what was tried and why,
so a future attempt at "block repeat entries after a stop-loss" doesn't
rediscover the same failure mode from scratch.

## What it was

After Fix 2 (SL cooldown), XAU/USD was still net-negative on LONG. Digging
into why turned up a distinct issue: on 2026-07-13, four near-identical
Wyckoff-Spring-based LONG signals fired in a single day, each stopped out
right after the previous one resolved. Same pattern on 2026-07-16. Filter 8
blocked a new signal on the same (pair, timeframe, direction) for
`SL_COOLDOWN_BARS=4` bars after a full SL (not `SL_AFTER_TP1`).

Implemented in `scheduler._has_recent_sl` (live, DB-backed) and
`backtest.engine.run_pair`'s `recent_sl_exits` dict (backtest mirror), both
sharing `scheduler._sl_cooldown_hours`.

## XAU/USD validation (positive)

Frozen-data A/B, cooldown ON vs OFF, isolated from the other fixes:

| | Cooldown OFF | Cooldown ON |
|---|---|---|
| Trades | 13 | 8 |
| Expectancy | -7.19 pips | +38.31 pips |
| Profit factor | 0.89 | 1.63 |
| Max drawdown | 804 pips | 404 pips |

Blocked 6 clustered same-day repeat LONG entries, all -80 pip losers. Looked
clean.

## NAS100 validation (the problem)

Testing the combined fix set (all 3) on NAS100 as part of a broader
3-pair sweep (NAS100, GBP/USD, USD/JPY) turned up a serious side effect:
NAS100 went from 12 trades / 2 wins / PF 0.37 to 5 trades / **0 wins** /
PF 0.00. Total absolute loss improved slightly (-363 → -288 pips) but every
single winning trade was removed.

Traced both removals individually (reconstructing the exact filter/decision
path bar-by-bar, not just diffing trade lists):

- **+185.8 pip TP2 winner (2026-06-11) — caused by the SL cooldown.** A
  SHORT fired at 14:00, hit SL in 15 minutes (-50). The very next bar
  (14:30) had a *stronger* setup (score 8 vs. 7, one more confirming
  factor) that in the un-cooled-down run went on to hit TP2 for +185.8. The
  cooldown blocked it anyway. **This is the core flaw**: the cooldown
  cannot distinguish "revenge re-entry on the same failing thesis" (the
  XAU/USD pattern it was built for) from "quick stop-out followed by a
  genuinely improved setup minutes later" (what happened here). XAU/USD's
  repeated failures were all near-identical theses, so this blind spot
  never showed up there.
- **+26.1 pip partial winner (2026-07-14) — caused by Fix 3 (Spring/UTAD
  tightening), not the cooldown**, and legitimately so: the UTAD's
  confirming close stayed $15.25 above `range_high` and never recovered —
  the old 0.2% tolerance wrongly accepted it. Same category as XAU/USD's
  already-accepted trade-offs; not a reason to revert anything.

## Decision

Presented both findings and four options (score-based exception to the
cooldown, shorten the cooldown window, accept the trade-off, or drop it
entirely) to the user. **Chosen: drop the cooldown fix entirely.** Fix 1
(HTF momentum veto) and Fix 3 (Spring/UTAD tightening) don't show this
failure mode and were kept; re-validated together on XAU/USD and NAS100
without the cooldown — see `2026-07-27-root-cause-analysis.md` and
`2026-07-27-fix3-spring-tightening.md` for those, and whichever doc records
the post-revert 2-pair re-validation for the current final numbers.

**If a cooldown-style fix is revisited later:** the score-based exception
(cooldown only applies if the new candidate's score is not higher than the
one that just failed) was the recommended-but-not-taken option, and is
probably the right starting point — it directly targets the distinction
that broke here (repeat vs. improved setup) without needing the cooldown
gone entirely.

## Code state

Fully reverted: `scheduler.py` and `backtest/engine.py` restored via
`git checkout` (both files had no other changes mixed in — verified via
`git diff` before reverting). `tests/test_sl_cooldown.py` deleted; the 4
Filter-8 tests removed from `tests/test_backtest_engine.py`;
`test_position_freed_after_resolution_allows_new_signal` reverted to its
original SL-based resolution. Suite back to green (78 tests) with the
cooldown gone.
