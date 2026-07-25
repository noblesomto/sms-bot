import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from config import settings
from db.database import SessionLocal
from db.models import Signal, Scan
from core.data_feed import get_candles, data_source_note
from core.precision import price_precision
from core.sessions import is_in_kill_zone, get_current_session, is_weekend
from core.strategy import (
    evaluate,
    _check_rr,
    _calc_risk,
    _has_dol,
    _has_ob_rejection,
    _min_sl_buffer,
    _max_sl_distance,
    _OB_MAX_AGE,
)
from alerts.formatter import format_signal_alert, format_tp_hit_alert, format_expiry_alert
from alerts.telegram_bot import send_alert, send_tp_notification
from charts.plotter import generate_chart, cleanup_old_charts

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler(timezone="UTC")

# How long a signal stays ACTIVE before being auto-expired (no TP/SL hit)
TF_EXPIRY_HOURS = {"5min": 4, "15min": 12, "1h": 48}

# Candle duration per timeframe, used to determine when a bar has fully closed
_TF_DELTA = {"5min": timedelta(minutes=5), "15min": timedelta(minutes=15),
             "1h": timedelta(hours=1), "4h": timedelta(hours=4)}


async def scan_pair_timeframe(pair: str, timeframe: str) -> dict:
    """
    Execute a complete SMC analysis pass for one pair/timeframe combination.

    Pipeline:
      1. Fetch OHLCV candles (with retry/cache via data_feed)
      2. Detect market structure (BOS, CHoCH, bias)
      3. Find and validate order blocks
      4. Scan fair value gaps
      5. Map liquidity levels and sweeps
      6. Score signal confluence
      7. Save to DB, generate chart, send Telegram alert

    Returns a result dict for scan logging.
    """
    # 5min signals only fire inside kill zones — skip the API call entirely outside them
    # to protect the 800 credits/day budget on the Basic 8 plan.
    if timeframe == "5min":
        in_kz_pre, kz_name_pre = is_in_kill_zone()
        if not in_kz_pre:
            logger.info(f"[{pair}/{timeframe}] Outside kill zone — skipping (API budget)")
            return {"pair": pair, "timeframe": timeframe, "candles_fetched": 0,
                    "signals_found": 0, "error": None}

    logger.info(f"[{datetime.now(timezone.utc)}] Scanning {pair}/{timeframe}")
    result = {
        "pair": pair,
        "timeframe": timeframe,
        "candles_fetched": 0,
        "signals_found": 0,
        "error": None,
    }

    try:
        # Run blocking HTTP + retry logic in a thread to keep the event loop free
        df = await asyncio.to_thread(get_candles, pair, timeframe, 200)
        if df is None or df.empty:
            result["error"] = "Failed to fetch candles"
            return result

        result["candles_fetched"] = len(df)

        htf_df = await asyncio.to_thread(get_candles, pair, settings.HTF_TIMEFRAME, 200)

        # 1H intermediate candles for scalp TF entries (5min/15min must align with 1H)
        itf_df = None
        if timeframe in ("5min", "15min"):
            itf_df = await asyncio.to_thread(get_candles, pair, "1h", 200)

        now = datetime.now(timezone.utc)
        res = evaluate(pair, timeframe, df, htf_df, itf_df, now)
        analysis = res["analysis"]
        sig = res["signal"]

        if sig:
            direction = sig["direction"]

            # ── Filter 6: No conflicting active signal (opposite direction) ───
            if _has_conflicting_signal(pair, direction):
                logger.info(f"[{pair}/{timeframe}] Skipping {direction} — opposite direction "
                            f"signal already active for {pair}")

            # ── Filter 7: No duplicate active signal (same direction) ─────────
            elif _has_active_same_signal(pair, timeframe, direction):
                logger.info(f"[{pair}/{timeframe}] Skipping {direction} — identical signal "
                            f"already ACTIVE, waiting for it to resolve")

            else:
                signal_id = _save_signal(
                    pair=pair, timeframe=timeframe, direction=direction,
                    score=sig["score"], entry_low=sig["entry_low"], entry_high=sig["entry_high"],
                    target1=sig["target1"], target2=sig["target2"], invalidation=sig["invalidation"],
                    factors=sig["factors"], entry_price=sig["entry_price"],
                )

                message = format_signal_alert(
                    pair=pair, direction=direction, timeframe=timeframe,
                    session=sig["kz_name"] or sig["session"], confluence_score=sig["score"],
                    max_score=sig["max_score"],
                    factors=sig["factors"], entry_low=sig["entry_low"], entry_high=sig["entry_high"],
                    target1=sig["target1"], target2=sig["target2"], invalidation=sig["invalidation"],
                    wyckoff_context=analysis["wyckoff_ctx"], entry_price=sig["entry_price"],
                )

                # Re-fetch live candles for the chart — this guarantees the chart
                # shows live candles at alert time, rather than whatever was cached
                # when the scan pass started.
                chart_df = await asyncio.to_thread(get_candles, pair, timeframe, 200, True)
                if chart_df is None or chart_df.empty:
                    chart_df = analysis["df"]
                last_candle = chart_df["datetime"].iloc[-1].strftime("%Y-%m-%d %H:%M UTC")

                chart_path = generate_chart(
                    df=chart_df, pair=pair, obs=analysis["obs"], fvgs=analysis["fvgs"],
                    liquidity_levels=analysis["all_levels"],
                    bos_events=analysis["structure"]["bos_events"],
                    choch_events=analysis["structure"]["choch_events"],
                    signal={
                        "direction": direction,
                        "entry_low": sig["entry_low"], "entry_high": sig["entry_high"],
                        "target1": sig["target1"], "target2": sig["target2"],
                        "invalidation": sig["invalidation"],
                    },
                    wyckoff_context=analysis["wyckoff_ctx"],
                    data_note=f"last candle {last_candle} · data: {data_source_note(pair)}",
                )

                await send_alert(pair, {
                    "message": message, "signal_id": signal_id,
                    "timeframe": timeframe, "direction": direction,
                }, chart_path)
                result["signals_found"] += 1

    except Exception as e:
        logger.error(
            f"[{datetime.now(timezone.utc)}] Error scanning {pair}/{timeframe}: {e}",
            exc_info=True,
        )
        result["error"] = str(e)

    return result


def _has_conflicting_signal(pair: str, direction: str) -> bool:
    """Return True if an active signal in the opposite direction exists for this pair."""
    opposite = "SHORT" if direction == "LONG" else "LONG"
    db = SessionLocal()
    try:
        return db.query(Signal).filter(
            Signal.pair == pair,
            Signal.direction == opposite,
            Signal.status == "ACTIVE",
        ).count() > 0
    finally:
        db.close()


def _has_active_same_signal(pair: str, timeframe: str, direction: str) -> bool:
    """Return True if an ACTIVE signal for the same pair/timeframe/direction already exists.

    Prevents the scanner from stacking duplicate entries every 5 minutes when the
    same OB zone keeps being detected while an earlier signal is still alive.
    """
    db = SessionLocal()
    try:
        return db.query(Signal).filter(
            Signal.pair == pair,
            Signal.timeframe == timeframe,
            Signal.direction == direction,
            Signal.status == "ACTIVE",
        ).count() > 0
    finally:
        db.close()


def _calc_pips(pair: str, price_diff: float) -> float:
    """Convert signed price difference to pips/points. Positive = profit, negative = loss."""
    if pair in ("NAS100", "US30"):
        return round(price_diff, 1)         # indices: 1 point = 1 pip
    if "JPY" in pair:
        return round(price_diff * 100, 1)
    if "XAU" in pair or "XAG" in pair:
        return round(price_diff * 10, 1)    # metals: $0.10 = 1 pip
    return round(price_diff * 10000, 1)     # forex: 0.0001 = 1 pip


def _save_signal(
    pair, timeframe, direction, score,
    entry_low, entry_high, target1, target2, invalidation, factors,
    entry_price: float,
) -> int:
    """Persist a new signal row and return its primary key."""
    prec = price_precision(entry_low)
    db = SessionLocal()
    try:
        sig = Signal(
            pair=pair, timeframe=timeframe, direction=direction,
            confluence_score=score,
            entry_zone_low=round(entry_low, prec),
            entry_zone_high=round(entry_high, prec),
            target1=round(target1, prec) if target1 is not None else None,
            target2=round(target2, prec) if target2 is not None else None,
            invalidation=round(invalidation, prec) if invalidation is not None else None,
            factors_json=json.dumps(factors),
            status="ACTIVE",
            entry_price=round(entry_price, prec),
        )
        db.add(sig)
        db.commit()
        db.refresh(sig)
        return sig.id
    finally:
        db.close()


def _log_scan(pair, timeframe, candles_fetched, signals_found, error=None):
    db = SessionLocal()
    try:
        db.add(Scan(
            pair=pair, timeframe=timeframe,
            candles_fetched=candles_fetched,
            signals_found=signals_found,
            error=error,
        ))
        db.commit()
    finally:
        db.close()


async def run_full_scan():
    """Scan all configured PAIRS × TIMEFRAMES — pauses automatically on weekends."""
    if is_weekend():
        logger.info(f"[{datetime.now(timezone.utc)}] Weekend — forex market closed, scan paused")
        return

    n = len(settings.PAIRS) * len(settings.TIMEFRAMES)
    logger.info(f"[{datetime.now(timezone.utc)}] Full scan started: {n} pair/timeframe combinations")

    for pair in settings.PAIRS:
        for timeframe in settings.TIMEFRAMES:
            res = await scan_pair_timeframe(pair, timeframe)
            _log_scan(pair, timeframe, res["candles_fetched"], res["signals_found"], res["error"])


async def run_scan_now():
    """Trigger an immediate scan (called from the /scan/trigger API route)."""
    await run_full_scan()


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


def _sweep_outcome(direction: str, status: str, candles, target1, target2, invalidation):
    """Scan candles oldest→newest and return the first outcome found.

    A wick that hit SL/TP several candles ago must not be missed just because
    price has since drifted away and the latest bar alone is quiet — checking
    only the last candle (the previous approach) silently dropped resolutions
    that happened between the previous check and now.

    `status` is evaluated against every candle unchanged (it does not switch
    to TP1_HIT mid-sweep even once a TP1 candle is found): this keeps the
    simpler stop-at-first-outcome contract — a TP1 hit ends the sweep just
    like a terminal SL/TP2 would, and the next hourly check resumes scanning
    from `hit_at` onward in TP1_HIT state (spec 2026-07-24 §2.5, "choose the
    simpler stop-at-first-outcome approach").

    `candles` is an iterable of (high, low) pairs, oldest first.
    """
    for high, low in candles:
        outcome = _resolve_outcome(direction, status, high, low, target1, target2, invalidation)
        if outcome:
            return outcome
    return None


def _sweep_window(df, start_ts, timeframe):
    """Candles still open at or opened after start_ts. Compares candle CLOSE
    time (open + timeframe) so the bar forming when the signal/TP1 fired is
    included — its remaining wicks are the likeliest to hit the stop."""
    if start_ts is None:
        return df
    delta = _TF_DELTA.get(timeframe, timedelta(hours=1))
    return df[df["datetime"] + delta > start_ts]


async def check_signal_status():
    """Hourly job: resolve active signals against latest price and record outcome.

    Two-stage TP flow:
      ACTIVE   → TP1 hit → TP1_HIT  (counts as WIN, Telegram alert, keep watching)
      TP1_HIT  → TP2 hit → HIT/TP2  (full hit, second Telegram alert)
      TP1_HIT  → SL  hit → INVALIDATED/SL_AFTER_TP1 (partial win already banked)
      ACTIVE   → SL  hit → INVALIDATED
    """
    db = SessionLocal()
    try:
        watchlist = db.query(Signal).filter(
            Signal.status.in_(["ACTIVE", "TP1_HIT"])
        ).all()
        now = datetime.now(timezone.utc)
        resolved = 0

        for sig in watchlist:
            try:
                entry = sig.entry_price or ((sig.entry_zone_low + sig.entry_zone_high) / 2)

                # ── Expiry check (only ACTIVE signals expire; TP1_HIT never expires) ──
                if sig.status == "ACTIVE":
                    expiry_hours = TF_EXPIRY_HOURS.get(sig.timeframe, 48)
                    if sig.created_at:
                        created = sig.created_at if sig.created_at.tzinfo else sig.created_at.replace(tzinfo=timezone.utc)
                        if (now - created) > timedelta(hours=expiry_hours):
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

                df = await asyncio.to_thread(get_candles, sig.pair, sig.timeframe, 50)
                if df is None or df.empty:
                    continue

                # Sweep every candle since the signal reached its current state —
                # not just the latest bar — so a wick that hit SL/TP between the
                # previous check and now isn't missed just because price has since
                # moved away. ACTIVE sweeps from created_at; TP1_HIT sweeps from
                # hit_at so pre-TP1 candles aren't re-evaluated against TP1_HIT
                # rules. Both timestamps may come back naive from the DB — treat
                # naive as UTC, mirroring the expiry check above.
                start_ts = sig.created_at if sig.status == "ACTIVE" else sig.hit_at
                if start_ts is not None and start_ts.tzinfo is None:
                    start_ts = start_ts.replace(tzinfo=timezone.utc)

                window_df = _sweep_window(df, start_ts, sig.timeframe)
                if window_df.empty:
                    # Signal created only seconds ago — no candle qualifies yet;
                    # fall back to the latest bar as before.
                    window_df = df.tail(1)

                candles = list(zip(
                    window_df["high"].astype(float), window_df["low"].astype(float)
                ))
                outcome = _sweep_outcome(
                    sig.direction, sig.status, candles,
                    sig.target1, sig.target2, sig.invalidation,
                )
                if not outcome:
                    continue
                hit_target, hit_price = outcome

                price_diff = (hit_price - entry) if sig.direction == "LONG" else (entry - hit_price)
                pnl = _calc_pips(sig.pair, price_diff)
                resolved += 1

                if hit_target == "TP1":
                    # Intermediate win — keep watching for TP2
                    sig.status = "TP1_HIT"
                    sig.hit_target = "TP1"
                    sig.hit_at = now
                    sig.pnl_pips = pnl
                    msg = format_tp_hit_alert(
                        pair=sig.pair, direction=sig.direction, timeframe=sig.timeframe,
                        tp_level="TP1", hit_price=hit_price, entry=entry, pnl_pips=pnl,
                        target2=sig.target2,
                    )
                    await send_tp_notification(msg)
                    logger.info(f"[{sig.pair}/{sig.timeframe}] TP1 hit @ {hit_price} — watching for TP2")

                elif hit_target == "TP2":
                    # If TP1 was already banked, blend both legs 50/50 instead of
                    # discarding the TP1 profit in favor of the TP2-leg pnl alone.
                    came_via_tp1 = sig.status == "TP1_HIT"
                    sig.status = "HIT"
                    sig.hit_target = "TP2"
                    sig.hit_at = now
                    sig.pnl_pips = round(0.5 * sig.pnl_pips + 0.5 * pnl, 1) if came_via_tp1 else pnl
                    msg = format_tp_hit_alert(
                        pair=sig.pair, direction=sig.direction, timeframe=sig.timeframe,
                        tp_level="TP2", hit_price=hit_price, entry=entry, pnl_pips=pnl,
                    )
                    await send_tp_notification(msg)
                    logger.info(f"[{sig.pair}/{sig.timeframe}] TP2 hit @ {hit_price} — full target reached")

                elif hit_target == "SL":
                    sig.status = "INVALIDATED"
                    sig.hit_target = "SL"
                    sig.hit_at = now
                    sig.pnl_pips = pnl
                    logger.info(f"[{sig.pair}/{sig.timeframe}] SL hit @ {hit_price}")

                elif hit_target == "SL_AFTER_TP1":
                    # TP1 was already banked — blend the banked TP1 leg with the
                    # SL leg 50/50 and record it as a distinct partial-win outcome
                    # instead of overwriting pnl_pips with the SL loss alone and
                    # counting it as a full INVALIDATED loss.
                    sig.status = "PARTIAL_WIN"
                    sig.hit_target = "SL_AFTER_TP1"
                    sig.hit_at = now
                    sig.pnl_pips = round(0.5 * sig.pnl_pips + 0.5 * pnl, 1)
                    logger.info(f"[{sig.pair}/{sig.timeframe}] SL hit after TP1 — partial win banked")

            except Exception as e:
                logger.error(
                    f"[{sig.pair}/{sig.timeframe}] Error tracking signal id={sig.id}: {e}",
                    exc_info=True,
                )
                continue

        db.commit()
        logger.info(
            f"[{datetime.now(timezone.utc)}] Signal status check: "
            f"{len(watchlist)} reviewed, {resolved} resolved"
        )
    finally:
        db.close()


async def daily_cleanup():
    """Midnight UTC job: remove old chart images from /tmp."""
    deleted = cleanup_old_charts()
    logger.info(f"[{datetime.now(timezone.utc)}] Daily cleanup: {deleted} stale chart file(s) removed")


def setup_scheduler() -> AsyncIOScheduler:
    """Register all jobs and return the configured scheduler (not yet started)."""
    scheduler.add_job(
        run_full_scan,
        trigger=IntervalTrigger(minutes=settings.SCAN_INTERVAL_MINUTES),
        id="full_scan",
        replace_existing=True,
        next_run_time=datetime.now(timezone.utc),
    )
    scheduler.add_job(
        check_signal_status,
        trigger=IntervalTrigger(hours=1),
        id="signal_status_check",
        replace_existing=True,
    )
    scheduler.add_job(
        daily_cleanup,
        trigger=CronTrigger(hour=0, minute=0, timezone="UTC"),
        id="daily_cleanup",
        replace_existing=True,
    )
    return scheduler
