import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta

import pandas as pd

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from config import settings
from db.database import SessionLocal
from db.models import Signal, Scan
from core.data_feed import get_candles, data_source_note
from core.precision import price_precision
from core.structure import analyze_structure
from core.order_blocks import find_order_blocks, validate_obs, get_price_at_ob
from core.fvg import scan_fvgs, update_fvg_status, get_fvg_at_price
from core.liquidity import find_equal_highs_lows, get_previous_day_week_levels, detect_liquidity_sweeps
from core.sessions import is_in_kill_zone, get_current_session, is_weekend
from core.premium_discount import get_premium_discount_zones
from core.confluence import score_signal
from core.wyckoff import get_wyckoff_context
from alerts.formatter import format_signal_alert, format_tp_hit_alert, format_expiry_alert
from alerts.telegram_bot import send_alert, send_tp_notification
from charts.plotter import generate_chart, cleanup_old_charts

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler(timezone="UTC")

# How long a signal stays ACTIVE before being auto-expired (no TP/SL hit)
TF_EXPIRY_HOURS = {"5min": 4, "15min": 12, "1h": 48}


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

        structure = analyze_structure(df)
        df = structure["df"]

        htf_df = await asyncio.to_thread(get_candles, pair, settings.HTF_TIMEFRAME, 200)
        htf_bias = "RANGING"
        if htf_df is not None and not htf_df.empty:
            htf_bias = analyze_structure(htf_df)["trend_bias"]

        # 1H intermediate bias for scalp TF entries (5min/15min must align with 1H)
        itf_bias = "RANGING"
        if timeframe in ("5min", "15min"):
            itf_df = await asyncio.to_thread(get_candles, pair, "1h", 200)
            if itf_df is not None and not itf_df.empty:
                itf_bias = analyze_structure(itf_df)["trend_bias"]

        obs = validate_obs(df, find_order_blocks(df, structure["bos_events"]))
        # Discard OBs that are too old — stale OBs have likely been tested and weakened.
        # Age = candles since the OB formed. ICT OBs remain valid much longer than
        # previously set; the tight windows (5h on 15min, 15h on 1H) were wiping out
        # all OBs and silently blocking every signal.
        _OB_MAX_AGE = {"5min": 30, "15min": 96, "1h": 72}  # 5min=2.5h, 15min=24h, 1h=3d
        max_ob_age = _OB_MAX_AGE.get(timeframe, 72)
        obs = [ob for ob in obs if (len(df) - 1 - ob.get("candle_index", 0)) <= max_ob_age]

        fvgs = update_fvg_status(df, scan_fvgs(df))

        eq_levels = find_equal_highs_lows(df)
        pd_levels = get_previous_day_week_levels(df)
        all_levels = {**eq_levels, **pd_levels}
        sweeps = detect_liquidity_sweeps(df, all_levels)

        in_kz, kz_name = is_in_kill_zone()
        session = get_current_session()
        # Use HTF range for premium/discount — the 50% EQ on a 5min range is meaningless;
        # ICT premium/discount is defined by the HTF (4H) swing, not the scalp window.
        pd_zone = get_premium_discount_zones(
            htf_df if htf_df is not None and not htf_df.empty else df
        )

        current_price = float(df["close"].iloc[-1])
        ob_at_price = get_price_at_ob(obs, current_price)
        fvg_at_price = get_fvg_at_price(fvgs, current_price)
        last_choch = structure["last_choch"]
        wyckoff_ctx = get_wyckoff_context(df, ob_at_price)
        if wyckoff_ctx.get("key_event"):
            logger.info(
                f"[{pair}/{timeframe}] Wyckoff: {wyckoff_ctx['description']} "
                f"(confidence={wyckoff_ctx['confidence']})"
            )

        # Determine candidate signal directions
        candidates = []
        if ob_at_price:
            candidates.append("LONG" if ob_at_price["direction"] == "BULLISH" else "SHORT")
        elif structure["trend_bias"] == "BULLISH":
            candidates.append("LONG")
        elif structure["trend_bias"] == "BEARISH":
            candidates.append("SHORT")

        if not candidates:
            logger.debug(f"[{pair}/{timeframe}] No signal candidate — price not at any OB, "
                         f"trend={structure['trend_bias']}, OBs={len(obs)}")

        for direction in candidates:
            conf = score_signal(
                direction=direction,
                htf_bias=htf_bias,
                ob_at_price=ob_at_price,
                fvg_at_price=fvg_at_price,
                liquidity_sweeps=sweeps,
                in_kill_zone=in_kz,
                pd_zone=pd_zone,
                ltf_choch=last_choch,
                wyckoff_context=wyckoff_ctx,
            )

            if conf["score"] < settings.MIN_CONFLUENCE_SCORE:
                logger.info(f"[{pair}/{timeframe}] Score {conf['score']}/{conf['max_score']} "
                            f"below threshold {settings.MIN_CONFLUENCE_SCORE} — "
                            f"factors: {', '.join(conf['factors'])}")
                continue

            # ── Filter 1: Kill zone enforcement (5min/15min only) ────────────
            # 1H signals can fire any time; scalp TFs only during institutional windows.
            if timeframe in ("5min", "15min") and not in_kz:
                logger.info(f"[{pair}/{timeframe}] Skipping {direction} — outside kill zone")
                continue

            # ── Filter 2: HTF bias alignment (4H) ────────────────────────────
            if htf_bias == "BULLISH" and direction == "SHORT":
                logger.info(f"[{pair}/{timeframe}] Skipping SHORT — 4H bias is BULLISH")
                continue
            if htf_bias == "BEARISH" and direction == "LONG":
                logger.info(f"[{pair}/{timeframe}] Skipping LONG — 4H bias is BEARISH")
                continue

            # ── Filter 3: Intermediate TF alignment (1H for 5min/15min) ──────
            if timeframe in ("5min", "15min"):
                if itf_bias == "BULLISH" and direction == "SHORT":
                    logger.info(f"[{pair}/{timeframe}] Skipping SHORT — 1H bias is BULLISH")
                    continue
                if itf_bias == "BEARISH" and direction == "LONG":
                    logger.info(f"[{pair}/{timeframe}] Skipping LONG — 1H bias is BEARISH")
                    continue

            # ── Filter 4: Draw on Liquidity must exist in trade direction ─────
            if not _has_dol(direction, current_price, all_levels):
                logger.info(f"[{pair}/{timeframe}] Skipping {direction} — no clear Draw on Liquidity")
                continue

            # ── Filter 5: OB rejection confirmation ──────────────────────────
            # When price is inside an OB, require a wick or body rejection before
            # alerting — entering the moment price touches the OB risks the break.
            if ob_at_price and not _has_ob_rejection(df, direction, ob_at_price):
                logger.info(f"[{pair}/{timeframe}] Skipping {direction} — price in OB but no rejection yet")
                continue

            prec = price_precision(current_price)
            entry_low = ob_at_price["ob_low"] if ob_at_price else round(current_price * 0.999, prec)
            entry_high = ob_at_price["ob_high"] if ob_at_price else round(current_price * 1.001, prec)
            target1, target2, invalidation = _calc_risk(direction, current_price, ob_at_price, all_levels, pair)

            # ── Filter 5: Minimum 2:1 risk/reward ────────────────────────────
            if not _check_rr(direction, current_price, target1, invalidation, min_rr=2.0):
                logger.info(f"[{pair}/{timeframe}] Skipping {direction} — R:R below 2:1 "
                            f"(market={current_price} zone={entry_low}-{entry_high} t1={target1} sl={invalidation})")
                continue

            # ── Filter 6: No conflicting active signal (opposite direction) ───
            if _has_conflicting_signal(pair, direction):
                logger.info(f"[{pair}/{timeframe}] Skipping {direction} — opposite direction "
                            f"signal already active for {pair}")
                continue

            # ── Filter 7: No duplicate active signal (same direction) ─────────
            if _has_active_same_signal(pair, timeframe, direction):
                logger.info(f"[{pair}/{timeframe}] Skipping {direction} — identical signal "
                            f"already ACTIVE, waiting for it to resolve")
                continue

            signal_id = _save_signal(
                pair=pair, timeframe=timeframe, direction=direction,
                score=conf["score"], entry_low=entry_low, entry_high=entry_high,
                target1=target1, target2=target2, invalidation=invalidation,
                factors=conf["factors"], entry_price=current_price,
            )

            message = format_signal_alert(
                pair=pair, direction=direction, timeframe=timeframe,
                session=kz_name or session, confluence_score=conf["score"],
                max_score=conf["max_score"],
                factors=conf["factors"], entry_low=entry_low, entry_high=entry_high,
                target1=target1, target2=target2, invalidation=invalidation,
                wyckoff_context=wyckoff_ctx, entry_price=current_price,
            )

            # Re-fetch live candles for the chart — the scan df can be up to a
            # cache-TTL old (58 min on 1H), which made alert charts trail the
            # user's broker by up to a full bar.
            chart_df = await asyncio.to_thread(get_candles, pair, timeframe, 200, True)
            if chart_df is None or chart_df.empty:
                chart_df = df
            last_candle = chart_df["datetime"].iloc[-1].strftime("%Y-%m-%d %H:%M UTC")

            chart_path = generate_chart(
                df=chart_df, pair=pair, obs=obs, fvgs=fvgs,
                liquidity_levels=all_levels,
                bos_events=structure["bos_events"],
                choch_events=structure["choch_events"],
                signal={
                    "direction": direction,
                    "entry_low": entry_low, "entry_high": entry_high,
                    "target1": target1, "target2": target2,
                    "invalidation": invalidation,
                },
                wyckoff_context=wyckoff_ctx,
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


def _has_ob_rejection(df: pd.DataFrame, direction: str, ob: dict) -> bool:
    """Return True if the last 3 candles show a rejection wick or body at the OB boundary.

    For LONG (bullish OB):
      - Lower wick ≥ 30% of candle range, touching the OB zone, OR
      - Bullish candle that opened/touched below OB midpoint and closed above it.
    For SHORT (bearish OB): mirror of above.
    The check catches both wick rejections (pin bars) and body rejections (engulfing).
    """
    if not ob:
        return True  # No OB = fallback entry zone, skip rejection requirement

    ob_mid = ob["ob_mid"]
    ob_lo = ob["ob_low"]
    ob_hi = ob["ob_high"]
    recent = df.tail(3)

    for _, c in recent.iterrows():
        hi, lo = float(c["high"]), float(c["low"])
        op, cl = float(c["open"]), float(c["close"])
        total = hi - lo
        if total <= 0:
            continue

        if direction == "LONG":
            if lo > ob_hi or hi < ob_lo:
                continue          # candle not touching OB at all
            lower_wick = min(op, cl) - lo
            if lower_wick / total >= 0.30:
                return True       # wick rejection: price tried lower, was rejected
            if cl > op and lo <= ob_mid and cl >= ob_mid:
                return True       # body rejection: bullish close through OB midpoint
        else:
            if hi < ob_lo or lo > ob_hi:
                continue
            upper_wick = hi - max(op, cl)
            if upper_wick / total >= 0.30:
                return True
            if cl < op and hi >= ob_mid and cl <= ob_mid:
                return True

    return False


def _has_dol(direction: str, price: float, levels: dict) -> bool:
    """Return True if a clear Draw on Liquidity pool exists within 5% in the trade direction.

    ICT requires a known liquidity target (EQH/EQL cluster, PDH/PDL, PWH/PWL) before
    entering — without a target to draw to, the trade has no directional purpose.
    """
    if direction == "LONG":
        eqh = [l for l in (levels.get("eqh_levels") or []) if price < l <= price * 1.05]
        pdh = levels.get("pdh")
        pwh = levels.get("pwh")
        return (
            bool(eqh)
            or (pdh and price < pdh <= price * 1.05)
            or (pwh and price < pwh <= price * 1.05)
        )
    else:
        eql = [l for l in (levels.get("eql_levels") or []) if price * 0.95 <= l < price]
        pdl = levels.get("pdl")
        pwl = levels.get("pwl")
        return (
            bool(eql)
            or (pdl and price * 0.95 <= pdl < price)
            or (pwl and price * 0.95 <= pwl < price)
        )


def _min_sl_buffer(price: float) -> float:
    """Minimum SL buffer in price units to protect against stop hunts below the OB."""
    if price >= 10000:  # NAS100 (~30k), US30 (~52k): 10 points
        return 10.0
    if price >= 1000:   # XAU/USD: $0.50
        return 0.50
    if price >= 100:    # USD/JPY, XAG/USD: 5 pips
        return 0.05
    return 0.00050      # EUR/USD, GBP/USD: 5 pips


def _max_sl_distance(pair: str, price: float) -> float:
    """Hard cap on SL distance from entry — prevents oversized risk on wide OBs."""
    if pair == "NAS100":  return 50.0    # 50 index points
    if pair == "US30":    return 80.0    # 80 index points
    if "XAU" in pair:     return 8.0     # $8 = 80 gold pips
    if "XAG" in pair:     return 0.40    # $0.40 = 4 silver pips
    if "JPY" in pair:     return 0.25    # 25 JPY pips
    return 0.0020                        # 20 forex pips


def _calc_risk(direction: str, price: float, ob: dict, levels: dict, pair: str = "") -> tuple:
    """Derive Target 1, Target 2, and Invalidation from nearby liquidity levels.

    SL is placed just beyond the OB boundary (5% of OB range, min buffer per
    instrument). The stop is never pulled inside the OB to satisfy a risk cap —
    a stop inside the zone gets hit by normal mitigation of the very structure
    the entry is based on. When there is no OB, `_max_sl_distance` provides the
    fallback stop distance from the current price.
    """
    prec = price_precision(price)
    max_dist = _max_sl_distance(pair, price)

    if direction == "LONG":
        # Pool all upside liquidity targets, sort ascending (nearest first)
        eqh = [l for l in (levels.get("eqh_levels") or []) if l > price]
        pdh = levels.get("pdh")
        pwh = levels.get("pwh")
        candidates = sorted({l for l in eqh + [pdh, pwh] if l and l > price})
        t1 = candidates[0] if candidates else round(price * 1.010, prec)
        t2 = candidates[1] if len(candidates) > 1 else round(t1 * 1.010, prec)
        if ob:
            buf = max((ob["ob_high"] - ob["ob_low"]) * 0.05, _min_sl_buffer(price))
            inv = round(ob["ob_low"] - buf, prec)
        else:
            inv = round(price - max_dist, prec)
    else:
        # Pool all downside liquidity targets, sort descending (nearest first)
        eql = [l for l in (levels.get("eql_levels") or []) if l < price]
        pdl = levels.get("pdl")
        pwl = levels.get("pwl")
        candidates = sorted({l for l in eql + [pdl, pwl] if l and l < price}, reverse=True)
        t1 = candidates[0] if candidates else round(price * 0.990, prec)
        t2 = candidates[1] if len(candidates) > 1 else round(t1 * 0.990, prec)
        if ob:
            buf = max((ob["ob_high"] - ob["ob_low"]) * 0.05, _min_sl_buffer(price))
            inv = round(ob["ob_high"] + buf, prec)
        else:
            inv = round(price + max_dist, prec)

    return round(t1, prec), round(t2, prec), round(inv, prec)


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
                last = df.iloc[-1]
                high, low = float(last["high"]), float(last["low"])

                outcome = _resolve_outcome(
                    sig.direction, sig.status, high, low,
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
