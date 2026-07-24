# Signal Quality Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make signal tracking match the user's market-entry trading reality and prune weak order blocks, per spec §2 (`docs/superpowers/specs/2026-07-24-signal-quality-program-design.md`).

**Architecture:** Four independent changes to the existing scanner: (1) a pure outcome-resolution function using candle highs/lows wired into `_check_signal_outcomes`; (2) `entry_price` semantics switched to market-at-alert throughout save/R:R/alert; (3) a Telegram notification on signal expiry; (4) a displacement gate inside `find_order_blocks`. No confluence scoring or threshold changes.

**Tech Stack:** Python 3.12, pandas, SQLAlchemy, python-telegram-bot, pytest (new dev dep). Project venv: `./venv/bin/python3`. Production deploy: scp to root@13.140.186.169:/root/smc_bot + `systemctl restart smc_bot`.

## Global Constraints

- Working dir: `/home/www/scripts/smc`. Run everything with `./venv/bin/python3` / `./venv/bin/pytest`.
- Phase 1 must NOT change confluence scoring weights, `MIN_CONFLUENCE_SCORE`, kill-zone, or bias filters (spec §2).
- Same-candle TP/SL ambiguity always resolves to SL first (spec §2.2).
- New constants `DISPLACEMENT_ATR_MULT = 1.5`, `DISPLACEMENT_WINDOW = 3` must be env-overridable in `config.py` like `MIN_CONFLUENCE_SCORE` (spec §2.4).
- Commit after every task (repo initialized in Task 0). Commit messages end with:
  `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`

---

### Task 0: Repo + test scaffolding

**Files:**
- Create: git repo at `/home/www/scripts/smc` (`.gitignore` already exists)
- Create: `tests/__init__.py` (empty)
- Modify: `requirements.txt` (append `pytest`)

**Interfaces:**
- Produces: a git baseline commit; `./venv/bin/pytest` runnable.

- [ ] **Step 1:** Verify `.gitignore` covers `venv/`, `__pycache__/`, `.env`, `*.db` — append any that are missing.
- [ ] **Step 2:** `git init && git add -A && git commit -m "chore: baseline before signal-quality phase 1"` (add co-author trailer).
- [ ] **Step 3:** Append `pytest` to `requirements.txt`; run `./venv/bin/pip install pytest`; create empty `tests/__init__.py`.
- [ ] **Step 4:** Run `./venv/bin/pytest --version` — expect a version string.
- [ ] **Step 5:** Commit: `chore: add pytest scaffolding`.

---

### Task 1: Wick-aware outcome resolution

**Files:**
- Modify: `scheduler.py` (extract logic from `_check_signal_outcomes`, currently lines ~566-597)
- Test: `tests/test_outcomes.py`

**Interfaces:**
- Produces: `scheduler._resolve_outcome(direction: str, status: str, high: float, low: float, target1, target2, invalidation) -> Optional[tuple[str, float]]` returning e.g. `("TP1", 4088.75)`, `("SL", 4021.57)`, `("SL_AFTER_TP1", ...)`, `("TP2", ...)` or `None`. `status` is `"ACTIVE"` or `"TP1_HIT"`.

- [ ] **Step 1: Write the failing test** — `tests/test_outcomes.py`:

```python
from scheduler import _resolve_outcome

T1, T2, SL = 4088.75, 4112.5, 4021.57

def test_long_sl_wick_close_inside():
    # wick pierces SL, closes back above — broker fills, close-based logic missed this
    assert _resolve_outcome("LONG", "ACTIVE", high=4035.0, low=4020.0,
                            target1=T1, target2=T2, invalidation=SL) == ("SL", SL)

def test_long_tp1_wick():
    assert _resolve_outcome("LONG", "ACTIVE", high=4090.0, low=4050.0,
                            target1=T1, target2=T2, invalidation=SL) == ("TP1", T1)

def test_long_tp2_beats_tp1():
    assert _resolve_outcome("LONG", "ACTIVE", high=4115.0, low=4050.0,
                            target1=T1, target2=T2, invalidation=SL) == ("TP2", T2)

def test_same_candle_sl_precedence():
    # both SL and TP touched in one bar → conservative SL
    assert _resolve_outcome("LONG", "ACTIVE", high=4115.0, low=4020.0,
                            target1=T1, target2=T2, invalidation=SL) == ("SL", SL)

def test_no_touch_returns_none():
    assert _resolve_outcome("LONG", "ACTIVE", high=4050.0, low=4030.0,
                            target1=T1, target2=T2, invalidation=SL) is None

def test_tp1_hit_state_sl_after_tp1():
    assert _resolve_outcome("LONG", "TP1_HIT", high=4095.0, low=4020.0,
                            target1=T1, target2=T2, invalidation=SL) == ("SL_AFTER_TP1", SL)

def test_short_mirror():
    # SHORT: SL above, targets below
    assert _resolve_outcome("SHORT", "ACTIVE", high=4044.0, low=4035.0,
                            target1=3993.25, target2=3990.4, invalidation=4043.7) == ("SL", 4043.7)
    assert _resolve_outcome("SHORT", "ACTIVE", high=4040.0, low=3990.0,
                            target1=3993.25, target2=3990.4, invalidation=4043.7) == ("TP2", 3990.4)

def test_none_targets_ignored():
    assert _resolve_outcome("LONG", "ACTIVE", high=4090.0, low=4050.0,
                            target1=None, target2=None, invalidation=SL) is None
```

- [ ] **Step 2:** Run `./venv/bin/pytest tests/test_outcomes.py -q` — expect ImportError (`_resolve_outcome` undefined).
- [ ] **Step 3: Implement** — add to `scheduler.py` above `_check_signal_outcomes`:

```python
def _resolve_outcome(direction: str, status: str, high: float, low: float,
                     target1, target2, invalidation):
    """Wick-touch TP/SL resolution for one candle. SL is checked first: when a
    single bar touches both stop and target, bar data cannot order the touches,
    so the conservative loss is assumed (spec 2026-07-24 §2.2)."""
    if direction == "LONG":
        sl_hit = invalidation is not None and low <= invalidation
        tp2_hit = target2 is not None and high >= target2
        tp1_hit = target1 is not None and high >= target1
    else:
        sl_hit = invalidation is not None and high >= invalidation
        tp2_hit = target2 is not None and low <= target2
        tp1_hit = target1 is not None and low <= target1

    if status == "ACTIVE":
        if sl_hit:
            return ("SL", invalidation)
        if tp2_hit:
            return ("TP2", target2)
        if tp1_hit:
            return ("TP1", target1)
    elif status == "TP1_HIT":
        if sl_hit:
            return ("SL_AFTER_TP1", invalidation)
        if tp2_hit:
            return ("TP2", target2)
    return None
```

- [ ] **Step 4:** Run `./venv/bin/pytest tests/test_outcomes.py -q` — expect all pass.
- [ ] **Step 5: Rewire `_check_signal_outcomes`.** Replace the block from `close = float(df["close"].iloc[-1])` through the end of the `elif sig.status == "TP1_HIT":` ladder with:

```python
            last = df.iloc[-1]
            high, low = float(last["high"]), float(last["low"])

            outcome = _resolve_outcome(
                sig.direction, sig.status, high, low,
                sig.target1, sig.target2, sig.invalidation,
            )
            if not outcome:
                continue
            hit_target, hit_price = outcome
```

  (The `if not hit_target: continue` line that followed the old ladder becomes redundant — delete it. Everything from `price_diff = ...` down is unchanged.)

- [ ] **Step 6:** `./venv/bin/python3 -m py_compile scheduler.py` then rerun the full test file — expect pass.
- [ ] **Step 7:** Commit: `fix: resolve TP/SL on wick touches with SL-first precedence`.

---

### Task 2: Market-entry alignment

**Files:**
- Modify: `scheduler.py` (`_check_rr` ~line 273, its call site ~line 201, `_save_signal` ~line 457, `format_signal_alert` call ~line 225)
- Modify: `alerts/formatter.py` (`format_signal_alert`)
- Test: `tests/test_market_entry.py`

**Interfaces:**
- Produces: `_check_rr(direction, entry_price: float, target1, invalidation, min_rr=1.5) -> bool` (zone args removed); `_save_signal(..., entry_price: float)` keyword arg; `format_signal_alert(..., entry_price: float = None)`.

- [ ] **Step 1: Write the failing test** — `tests/test_market_entry.py`:

```python
from scheduler import _check_rr
from alerts.formatter import format_signal_alert

def test_rr_from_market_price():
    # market 4036.70, SL 4021.57 → risk 15.13; TP1 4088.75 → reward 52.05 → RR 3.44
    assert _check_rr("LONG", 4036.70, 4088.75, 4021.57, min_rr=2.0)

def test_rr_rejects_when_market_price_degrades_rr():
    # zone-mid RR would pass, market-price RR must fail:
    # market 4035, SL 4021 → risk 14; TP1 4055 → reward 20 → RR 1.43 < 2
    assert not _check_rr("LONG", 4035.0, 4055.0, 4021.0, min_rr=2.0)

def test_rr_short():
    assert _check_rr("SHORT", 4036.0, 3993.25, 4043.7, min_rr=2.0)

def test_rr_invalid_geometry():
    assert not _check_rr("LONG", 4030.0, 4020.0, 4040.0)  # target below entry

def test_alert_shows_market_entry():
    msg = format_signal_alert(
        pair="XAU/USD", direction="LONG", timeframe="1h", session="LONDON_OPEN",
        confluence_score=7, factors=["x"], entry_low=4022.5, entry_high=4041.0,
        target1=4088.75, target2=4112.5, invalidation=4021.57,
        entry_price=4036.70,
    )
    assert "Entry (market): 4036.70" in msg
    assert "4022.50 – 4041.00" in msg  # zone still shown as context
```

- [ ] **Step 2:** Run `./venv/bin/pytest tests/test_market_entry.py -q` — expect TypeError/AssertionError failures.
- [ ] **Step 3: Implement `_check_rr`** — replace the function with:

```python
def _check_rr(direction: str, entry_price: float,
              target1: float, invalidation: float, min_rr: float = 1.5) -> bool:
    """Return True only if TP1 distance is at least min_rr × SL distance,
    measured from the live market price — the user enters at market on alert,
    so zone-midpoint R:R overstated reality (spec 2026-07-24 §2.1)."""
    if direction == "LONG":
        reward = target1 - entry_price
        risk = entry_price - invalidation
    else:
        reward = entry_price - target1
        risk = invalidation - entry_price
    if risk <= 0 or reward <= 0:
        return False
    return (reward / risk) >= min_rr
```

  Update the call site in `scan()`:
  `if not _check_rr(direction, current_price, target1, invalidation, min_rr=2.0):`
  (log line keeps entry zone for context, add `market={current_price}`).

- [ ] **Step 4: `_save_signal` stores market price.** Add keyword param `entry_price: float` to `_save_signal`; delete the `entry_price = round((entry_low + entry_high) / 2, prec)` line; use `entry_price=round(entry_price, prec)` in the `Signal(...)` constructor. In `scan()`, pass `entry_price=current_price` in the `_save_signal(...)` call.
- [ ] **Step 5: Alert formatter.** In `format_signal_alert`, add param `entry_price: float = None` and replace the entry-zone line with:

```python
    entry_line = (
        f"📍 Entry (market): {_fmt(entry_price)}\n"
        f"   OB zone: {_fmt(entry_low)} – {_fmt(entry_high)}\n\n"
        if entry_price is not None
        else f"📍 Entry Zone: {_fmt(entry_low)} – {_fmt(entry_high)}\n\n"
    )
```

  and use `{entry_line}` in the returned f-string where the old zone line was. In `scan()`, pass `entry_price=current_price` to `format_signal_alert(...)`.
- [ ] **Step 6:** Run `./venv/bin/pytest tests/ -q` (all files) and `./venv/bin/python3 -m py_compile scheduler.py alerts/formatter.py` — expect pass.
- [ ] **Step 7:** Commit: `feat: align entry price, R:R filter, and alerts with market-entry trading`.

---

### Task 3: Expiry exit notification

**Files:**
- Modify: `alerts/formatter.py` (new `format_expiry_alert`)
- Modify: `scheduler.py` (expiry branch of `_check_signal_outcomes`, ~lines 547-558)
- Test: `tests/test_expiry_alert.py`

**Interfaces:**
- Consumes: `_calc_pips(pair, price_diff)` (existing), `send_tp_notification(msg)` (existing).
- Produces: `format_expiry_alert(pair, direction, timeframe, entry, current_price, unrealized_pips, expiry_hours) -> str` (`current_price`/`unrealized_pips` may be None).

- [ ] **Step 1: Write the failing test** — `tests/test_expiry_alert.py`:

```python
from alerts.formatter import format_expiry_alert

def test_expiry_alert_contents():
    msg = format_expiry_alert(pair="XAU/USD", direction="LONG", timeframe="1h",
                              entry=4036.70, current_price=4030.00,
                              unrealized_pips=-67.0, expiry_hours=48)
    assert "EXPIRED" in msg and "XAU/USD" in msg
    assert "4036.70" in msg and "4030.00" in msg
    assert "-67.0" in msg and "48h" in msg
    assert "no longer tracking" in msg.lower()

def test_expiry_alert_without_price():
    msg = format_expiry_alert(pair="XAU/USD", direction="LONG", timeframe="1h",
                              entry=4036.70, current_price=None,
                              unrealized_pips=None, expiry_hours=48)
    assert "n/a" in msg.lower()
```

- [ ] **Step 2:** Run `./venv/bin/pytest tests/test_expiry_alert.py -q` — expect ImportError.
- [ ] **Step 3: Implement** — add to `alerts/formatter.py`:

```python
def format_expiry_alert(pair, direction, timeframe, entry,
                        current_price, unrealized_pips, expiry_hours) -> str:
    """Sent when a signal expires with neither TP nor SL hit. The user enters
    at market on alert, so an expired signal is an open position they are
    still holding — tell them the bot has stopped tracking it."""
    dir_emoji = "🟢" if direction == "LONG" else "🔴"
    if current_price is not None:
        sign = "+" if unrealized_pips >= 0 else ""
        price_line = (f"📉 Current: {_fmt(current_price)} "
                      f"({sign}{unrealized_pips} pips unrealized)")
    else:
        price_line = "📉 Current: n/a"
    return "\n".join([
        f"⌛ SIGNAL EXPIRED — {pair}",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        f"{dir_emoji} Direction: {direction} | {timeframe.upper()}",
        f"📍 Entry: {_fmt(entry)}",
        price_line,
        "",
        f"No TP/SL hit within {expiry_hours}h — the bot is no longer tracking",
        "this signal. If you entered, close or manage the position manually.",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
    ])
```

- [ ] **Step 4:** Run the test file — expect pass.
- [ ] **Step 5: Wire into scheduler.** Import `format_expiry_alert` alongside `format_tp_hit_alert`. Replace the expiry branch body (after `sig.hit_at = now`) so it fetches a price and notifies before `continue`:

```python
                        sig.status = "EXPIRED"
                        sig.hit_target = "EXPIRED"
                        sig.hit_at = now
                        resolved += 1
                        df_exp = await asyncio.to_thread(get_candles, sig.pair, sig.timeframe, 5)
                        cur = unrealized = None
                        if df_exp is not None and not df_exp.empty:
                            cur = float(df_exp["close"].iloc[-1])
                            diff = (cur - entry) if sig.direction == "LONG" else (entry - cur)
                            unrealized = _calc_pips(sig.pair, diff)
                        await send_tp_notification(format_expiry_alert(
                            pair=sig.pair, direction=sig.direction,
                            timeframe=sig.timeframe, entry=entry,
                            current_price=cur, unrealized_pips=unrealized,
                            expiry_hours=expiry_hours,
                        ))
                        logger.info(f"[{sig.pair}/{sig.timeframe}] expired after {expiry_hours}h — user notified")
                        continue
```

- [ ] **Step 6:** `./venv/bin/python3 -m py_compile scheduler.py` + full `./venv/bin/pytest tests/ -q` — expect pass.
- [ ] **Step 7:** Commit: `feat: notify on signal expiry with unrealized P&L`.

---

### Task 4: Displacement-validated order blocks

**Files:**
- Modify: `config.py` (two settings), `core/order_blocks.py`
- Test: `tests/test_displacement.py`

**Interfaces:**
- Produces: `core.order_blocks._has_displacement(df, ob_idx: int, direction: str) -> bool`; `find_order_blocks` silently drops OBs failing it. Config: `settings.DISPLACEMENT_ATR_MULT` (float, default 1.5), `settings.DISPLACEMENT_WINDOW` (int, default 3).

- [ ] **Step 1: Write the failing test** — `tests/test_displacement.py`:

```python
import pandas as pd
from core.order_blocks import _has_displacement

def _df(rows):
    n = len(rows)
    return pd.DataFrame(
        [dict(zip(("open", "high", "low", "close"), r)) for r in rows]
    ).assign(volume=0.0,
             datetime=pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC"))

# 15 flat candles (range 1.0) to seed ATR ≈ 1.0, OB candle at idx 15
FLAT = [(100, 100.5, 99.5, 100)] * 15

def test_impulsive_move_passes():
    # bearish OB candle then 3 strong up candles: travel 100→105 = 5 ≥ 1.5×ATR
    rows = FLAT + [(100.5, 100.6, 99.6, 99.8),
                   (99.8, 102.0, 99.8, 101.9), (101.9, 103.5, 101.8, 103.4),
                   (103.4, 105.0, 103.3, 104.9)]
    assert _has_displacement(_df(rows), 15, "BULLISH")

def test_drift_fails():
    # weak follow-through: 3 candles crawl 0.3 total < 1.5×ATR, no gap
    rows = FLAT + [(100.5, 100.6, 99.6, 99.8),
                   (99.8, 100.0, 99.7, 99.9), (99.9, 100.1, 99.8, 100.0),
                   (100.0, 100.2, 99.9, 100.1)]
    assert not _has_displacement(_df(rows), 15, "BULLISH")

def test_fvg_gap_passes_without_atr_pass():
    # small travel but candle 17 low (100.9) > candle 15 high → bullish FVG
    rows = FLAT + [(100.5, 100.6, 99.6, 99.8),
                   (99.8, 100.7, 99.8, 100.6), (100.9, 101.2, 100.9, 101.1),
                   (101.1, 101.3, 101.0, 101.2)]
    assert _has_displacement(_df(rows), 15, "BULLISH")

def test_bearish_mirror():
    rows = FLAT + [(99.5, 100.4, 99.4, 100.2),
                   (100.2, 100.2, 98.0, 98.1), (98.1, 98.2, 96.5, 96.6),
                   (96.6, 96.7, 95.0, 95.1)]
    assert _has_displacement(_df(rows), 15, "BEARISH")

def test_ob_at_end_of_data_fails_open():
    # no candles after OB yet → no displacement evidence → reject
    rows = FLAT + [(100.5, 100.6, 99.6, 99.8)]
    assert not _has_displacement(_df(rows), 15, "BULLISH")
```

- [ ] **Step 2:** Run `./venv/bin/pytest tests/test_displacement.py -q` — expect ImportError.
- [ ] **Step 3: Config.** In `config.py`, after the `MIN_CONFLUENCE_SCORE` line, following the same pattern:

```python
    DISPLACEMENT_ATR_MULT: float = float(os.getenv("DISPLACEMENT_ATR_MULT", "1.5"))
    DISPLACEMENT_WINDOW: int = int(os.getenv("DISPLACEMENT_WINDOW", "3"))
```

- [ ] **Step 4: Implement** — in `core/order_blocks.py` add `from config import settings` and:

```python
def _has_displacement(df: pd.DataFrame, ob_idx: int, direction: str) -> bool:
    """ICT displacement gate: an OB is only tradeable if the move leaving it
    was impulsive — within DISPLACEMENT_WINDOW candles after the OB, price
    travels ≥ DISPLACEMENT_ATR_MULT × ATR(14 at formation), or the follow-
    through leaves an FVG. Weak zones without displacement are discarded
    (spec 2026-07-24 §2.4)."""
    window = settings.DISPLACEMENT_WINDOW
    seg = df.iloc[ob_idx + 1: ob_idx + 1 + window]
    if seg.empty:
        return False

    hist = df.iloc[max(0, ob_idx - 14): ob_idx + 1]
    prev_close = hist["close"].shift(1)
    tr = pd.concat([hist["high"] - hist["low"],
                    (hist["high"] - prev_close).abs(),
                    (hist["low"] - prev_close).abs()], axis=1).max(axis=1)
    atr = float(tr.mean())
    ob_close = float(df.iloc[ob_idx]["close"])

    if direction == "BULLISH":
        travel = float(seg["high"].max()) - ob_close
    else:
        travel = ob_close - float(seg["low"].min())
    if atr > 0 and travel >= settings.DISPLACEMENT_ATR_MULT * atr:
        return True

    # FVG in the follow-through: 3-bar imbalance among candles ob_idx..ob_idx+window
    for j in range(ob_idx + 1, min(ob_idx + window, len(df) - 1)):
        if direction == "BULLISH" and float(df.iloc[j + 1]["low"]) > float(df.iloc[j - 1]["high"]):
            return True
        if direction == "BEARISH" and float(df.iloc[j + 1]["high"]) < float(df.iloc[j - 1]["low"]):
            return True
    return False
```

  In `find_order_blocks`, gate both appends:
  `if _has_displacement(df, i, "BULLISH"): obs.append(_make_ob(candle, i, "BULLISH"))`
  then `break` regardless (the last opposing candle is the only OB candidate for that BOS — if it lacks displacement the BOS yields no OB). Mirror for BEARISH.

- [ ] **Step 5:** Run `./venv/bin/pytest tests/ -q` and `./venv/bin/python3 core/order_blocks.py` (its self-test must still find ≥1 OB — the fixture's BOS impulse is strong).
- [ ] **Step 6:** Commit: `feat: require ICT displacement (ATR impulse or FVG) to validate OBs`.

---

### Task 5: Deploy + verify + cutover bookkeeping

**Files:**
- Deploy: `scheduler.py`, `alerts/formatter.py`, `core/order_blocks.py`, `config.py` → `root@13.140.186.169:/root/smc_bot/`
- Modify: spec changelog (`docs/superpowers/specs/2026-07-24-signal-quality-program-design.md` §7)

**Interfaces:** none (operational task).

- [ ] **Step 1:** Full local gate: `./venv/bin/pytest tests/ -q` all green; `py_compile` on all four files.
- [ ] **Step 2:** scp the four files to the VPS, `py_compile` there, `systemctl restart smc_bot`, confirm `systemctl is-active` → `active`.
- [ ] **Step 3:** Wait one scan cycle (poll `/health` `last_scan_at`); check `bot.log` since restart for `ERROR`; confirm scans complete and OB counts are nonzero across pairs (displacement shouldn't wipe out all OBs — if `No signal candidate — ... OBs=0` appears for *every* pair/TF, that's a rollback signal).
- [ ] **Step 4:** Fill spec §7 changelog: Phase 1 cutover date/time UTC. Update assistant memory (known-bugs / deployment notes): entry_price semantics changed on this date; pre-cutover rows use zone-mid entry and close-based outcomes.
- [ ] **Step 5:** Commit: `chore: record phase 1 cutover`.
- [ ] **Step 6:** Report to user: what shipped, what to expect (win-rate reporting drops but matches broker; expiry messages now arrive; possibly slightly fewer but cleaner OB signals; weekly volume watch — if <5 signals/week over 2 weeks, lower DISPLACEMENT_ATR_MULT to 1.2).

---

## Self-review notes

- Spec §2.1→Task 2, §2.2→Task 1, §2.3→Task 3, §2.4→Task 4, rollout §5→Task 5. Phase 2 (§3) deliberately deferred to its own plan after Phase 1 ships.
- Deviation from spec §2.4 wording: FVG check is a self-contained 3-bar imbalance test inside `order_blocks.py` rather than importing `core/fvg.py` — avoids index-offset coupling; the rule tested is identical.
- Type check: `_resolve_outcome` consumed only inside `scheduler.py`; `format_expiry_alert`/`format_signal_alert` signatures consistent between Tasks 2/3 and call sites; `_check_rr` new arity used at its single call site.
