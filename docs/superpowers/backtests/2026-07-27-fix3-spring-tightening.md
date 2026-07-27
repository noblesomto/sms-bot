# Fix 3 — Wyckoff Spring/UTAD confirmation tightening: validated on XAU/USD

Companion to `2026-07-27-fix2-sl-cooldown.md`. After Fix 2 (SL cooldown),
XAU/USD's LONG direction was still net-negative: 0% win rate over the 6
remaining trades, -80.67 pips expectancy. This investigates why the
Spring-based LONG entries themselves keep failing, not just why they
repeat.

## Investigation

Reconstructed the exact Wyckoff context `core.strategy.evaluate()` would
have built at each failing entry, using real fetched 1h/15min candles (not
the backtest CSV — actual price history). Two concrete defects in
`core/wyckoff.py`'s `detect_spring`/`detect_utad`:

1. **Recovery-close tolerance too loose.** The old check
   (`cl >= range_low * 0.998`) accepted a close up to 0.2% *below*
   `range_low` as "closed back inside the range." The 2026-07-10 13:15
   entry's confirming candle closed at 4095.90 vs. `range_low` 4103.00 —
   **$7.10 (0.17%) below**, yet still counted.
2. **Trivial wick breaches counted as stop-hunts.** The 2026-07-13 04:00
   entry: range $4076.80-$4144.60 (width $67.80), spring breached
   `range_low` by just **$1.00 (1.5% of range width)**. The 2026-07-16
   11:00 entry: range $4029.40-$4089.10 (width $59.70), breach **$1.70
   (2.8% of range width)**. Neither is a meaningful stop-hunt.

Both defects are symmetric between `detect_spring`/`detect_utad` since they
share identical logic.

## The fix

- Recovery close must be genuinely back inside the range: `cl >= range_low`
  (Spring) / `cl <= range_high` (UTAD) — no downward/upward slack.
- Minimum breach depth: `MIN_BREACH_PCT = 0.03` (3% of range width) —
  filters wicks that technically poke past the boundary but are noise.

8 new unit tests (`tests/test_wyckoff.py`). Full suite: 89/89 green.

## Validation — frozen-data A/B on XAU/USD

Same methodology as Fixes 1-2: fetch once, run twice against the identical
frozen 60-day DataFrame, Fix 1 + Fix 2 active in both variants (isolates
Fix 3). Full output: `2026-07-27-xauusd-ab-spring-tightening.txt`.

| | Loose (original) | Tight (current) |
|---|---|---|
| Trades | 8 | 5 |
| Win rate | 25.0% | 20.0% |
| Expectancy | 38.31 pips | **51.20 pips** |
| Profit factor | 1.63 | **1.79** |
| Max drawdown | 404 pips | **244 pips** |
| Total pnl (expectancy × n) | +306.5 pips | +256.0 pips |

Diff: tightening removed 3 trades — 2 correctly-identified bad LONG
Springs (both -80 pip SL: 2026-07-10, 2026-07-16) and 1 SHORT/UTAD winner
(2026-07-09, +210.5 pips via TP2). **Checked the removed winner
specifically**, since losing a winner needs scrutiny, not just acceptance:
its confirming candle wicked only **$0.50 above `range_high`** on a
$56.50-wide range (0.9% of width, well under the 3% floor) while its close
was already comfortably below `range_high` the entire time — not a
stop-hunt-and-reject pattern, just noise that happened to win. Losing it is
the expected, acceptable cost of removing a genuinely low-quality signal;
per-trade expectancy, profit factor, and drawdown all improved even though
total pnl over this one 60-day sample dipped slightly (a single lucky trade
is not a basis to keep a noisy detection rule).

**Residual, not fixed by this change:** the 4 remaining LONG/Spring trades
on XAU/USD are still all losses (0% win rate). Tightening removes
low-quality *signals*; it doesn't guarantee the remaining, genuinely-formed
Springs succeed — gold was net-declining over this window even amid chop,
which may simply make bottom-picking (accumulation) harder than top-picking
(distribution) here. Not enough sample to separate "Spring still needs
more work" from "this window was just unfavorable for LONGs" — flagging,
not concluding.

## Cumulative effect, XAU/USD, 60d

**Note (2026-07-27, after this doc was first written):** Fix 2 (SL
cooldown) was implemented, validated on XAU/USD, then reverted the same
day after broader testing on NAS100 exposed it blocking a legitimate
improved re-entry — see `2026-07-27-fix2-sl-cooldown-REVERTED.md`. The
numbers below are Fix 1 + Fix 3 only (current, final code state), not the
originally-reported 3-fix combo.

| | Original (no fixes) | Fix 1 + Fix 3 |
|---|---|---|
| Trades | 13 | 7 |
| Expectancy | -7.19 pips | **+13.71 pips** |
| Profit factor | 0.89 | **1.20** |
| Max drawdown | 804 pips | **404 pips** |
| Total pnl | -93.5 pips | **+95.97 pips** |

Also validated on NAS100 (see `2026-07-27-nas100-ab-postrevert.txt`):
trades 12→6, expectancy -30.27→-16.98 pips, PF 0.37→0.65, maxDD 412→238.
Every metric improved on both pairs with no winners inappropriately
removed. XAU/USD flips to net-profitable; NAS100 improves substantially but
stays net-negative in this window. GBP/USD and USD/JPY had zero signals
either way in this window (no evidence either direction).

Not deployed; per spec §3.3 needs sign-off before any of this ships.
