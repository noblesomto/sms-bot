"""Pure signal-decision logic for the SMC scanner.

`evaluate()` is the analysis-and-decision core of the scan pipeline: given
already-fetched candles for the scan timeframe (and, where applicable, the
4H HTF and 1H ITF), it runs structure/OB/FVG/liquidity/Wyckoff analysis,
builds a candidate direction, scores confluence, and applies filters 1-5
(kill zone, HTF bias, ITF bias, Draw on Liquidity, OB rejection) plus the
minimum risk/reward filter — returning a decision dict and everything the
caller needs for alerting/charting.

It performs no IO and touches no DB/session/alert/chart state — the caller
(scheduler.scan_pair_timeframe) is responsible for fetching candles, running
Filters 6/7 (duplicate/conflicting active-signal checks against the DB),
persisting the signal, sending the alert, and generating the chart.
"""
import logging

import pandas as pd

from config import settings
from core.precision import price_precision
from core.structure import analyze_structure
from core.order_blocks import find_order_blocks, validate_obs, get_price_at_ob
from core.fvg import scan_fvgs, update_fvg_status, get_fvg_at_price
from core.liquidity import find_equal_highs_lows, get_previous_day_week_levels, detect_liquidity_sweeps
from core.sessions import is_in_kill_zone, get_current_session
from core.premium_discount import get_premium_discount_zones
from core.confluence import score_signal
from core.wyckoff import get_wyckoff_context

logger = logging.getLogger(__name__)

# Discard OBs that are too old — stale OBs have likely been tested and weakened.
# Age = candles since the OB formed. ICT OBs remain valid much longer than
# previously set; the tight windows (5h on 15min, 15h on 1H) were wiping out
# all OBs and silently blocking every signal.
#
# Raised again 2026-07-27: the 96/72 caps (24h / 3d) were STILL discarding
# most live OBs for slower-moving forex majors. Backtest evidence (GBP/USD,
# USD/JPY, 60d): 62.6-89.6% of decision points had >=1 unmitigated OB, but
# only 21.1-29.7% had one young enough to survive the cap — a real,
# unmitigated OB just hadn't been retested yet, which per ICT theory doesn't
# invalidate it (mitigation, checked separately in validate_obs, already
# covers "tested and weakened"). 190 sits just under the ~199-bar natural
# ceiling the 200-bar analysis view already imposes (an OB whose formation
# candle scrolls out of that trailing window can never be found again
# regardless of this cap) — so this isn't "unlimited," it's "let the age cap
# stop double-gating on top of a limit that already exists structurally."
# 5min left unchanged — no evidence gathered for it in this investigation.
_OB_MAX_AGE = {"5min": 30, "15min": 190, "1h": 190}  # 5min=2.5h, 15min=~2d, 1h=~8d


def _filter_obs_by_age(obs: list, view_len: int, max_age: int) -> list:
    """Discard OBs older than max_age bars relative to the last bar in the
    analysis view (view_len). See _OB_MAX_AGE."""
    return [ob for ob in obs if (view_len - 1 - ob.get("candle_index", 0)) <= max_age]

# Secondary veto for Filter 2 when the 4H swing-based trend_bias reads
# RANGING. get_trend_bias() only looks at the last 2 confirmed swing highs/
# lows and needs both to agree, so a real multi-day trend with a mixed swing
# pattern (common) is misread as RANGING, letting counter-trend entries
# through Filter 2 unopposed — root cause confirmed in the 2026-07-27
# backtest (docs/superpowers/backtests/2026-07-27-root-cause-analysis.md).
# Net close-to-close change over a longer window catches what the swing
# classifier misses without touching its semantics elsewhere (it's still
# used unchanged for OB validation, scoring, ITF checks, etc.). Threshold is
# deliberately wide so a genuine basing/accumulation range (net move small)
# still reads as "no conflict" and doesn't block legitimate Wyckoff
# Spring/UTAD entries, which are supposed to fire during exactly that
# condition.
_RANGING_MOMENTUM_LOOKBACK = 20   # 4H candles (~3.3 days)
_RANGING_MOMENTUM_PCT = 0.02      # 2% net move counts as a real hidden trend

# LONG-only HTF reversal guard, added on top of Filter 2 (root cause:
# 2026-07-28 150d NAS100+US30 backtest, see docs/superpowers/backtests/
# 2026-07-28-long-side-reversal-trace.md). get_trend_bias() needs the last
# TWO confirmed swings to agree before flipping away from the prior trend,
# so it lags a real reversal by design; the RANGING-momentum veto above
# only catches a reversal once it's moved 2%+ over 20 candles — also
# lagging. A BOS (core.structure.detect_bos) only needs ONE swing broken,
# so it reacts sooner. Evidenced against every losing LONG in that
# backtest: 8 of 9 fired with the HTF's last confirmed BOS already
# BEARISH (0.3-4.5 days old) while trend_bias/the momentum veto still read
# BULLISH/RANGING at the same moment — i.e. the LONG entry pattern (OB
# retest / Spring / discount-zone bounce) fires early into what turns out
# to be a fresh downtrend, not a continuation dip. Checked the symmetric
# SHORT-side mirror (block SHORT when the last BOS is BULLISH) against the
# same dataset: it would have blocked the single best trade in it (+421.2
# pips) alongside some losers, netting out roughly flat — SHORT doesn't
# show this failure mode, so this stays LONG-only rather than a general
# symmetric filter.


def _htf_bearish_reversal(htf_df) -> bool:
    """Return True if the HTF's most recently confirmed structural break
    (BOS) is BEARISH. See the constant block above for the evidence this
    is built on and why it's LONG-only, not symmetric with SHORT.

    Fails open (returns False) when there isn't enough history to judge —
    same convention as _htf_momentum_conflicts.
    """
    if htf_df is None or htf_df.empty:
        return False
    last_bos = analyze_structure(htf_df)["last_bos"]
    return bool(last_bos and last_bos["direction"] == "BEARISH")


# ── Profitability roadmap Phase 1 (2026-08-24) ────────────────────────────
# Live ledger Jul 1–Aug 21 (VPS DB): LONG = 3 wins / 26 trades / −920 pips;
# SHORT = +444 pips. Every shipped mechanical LONG guard reduced but never
# eliminated the bleed, so the roadmap moves LONG behind an explicit opt-in
# switch for an evaluation period instead of stacking yet another filter.
def _direction_allowed(direction: str) -> bool:
    """Return False when direction is disabled by settings.ENABLE_LONG.

    SHORT-only evaluation mode: with ENABLE_LONG=false, LONG candidates are
    dropped before scoring so nothing downstream (confluence, filters,
    alerts) ever sees them.
    """
    if direction == "LONG" and not settings.ENABLE_LONG:
        return False
    return True


# Regime tag stored on every signal row (Phase 2 item 6): lets later
# analysis answer "was the SHORT edge real or just the Jul–Aug tape?"
# without re-deriving candles. Same ±2%-over-20-closes spirit as Fix1's
# momentum veto — deliberately coarse; this is a label, not a filter.
_REGIME_LOOKBACK = 20
_REGIME_THRESHOLD_PCT = 0.02


def _htf_regime_of(htf_df) -> str:
    """Classify the HTF view as UP/DOWN/FLAT over the last _REGIME_LOOKBACK
    closes (±_REGIME_THRESHOLD_PCT), or UNKNOWN without enough history."""
    if htf_df is None or len(htf_df) < 2:
        return "UNKNOWN"
    window = htf_df.tail(_REGIME_LOOKBACK + 1)
    start = float(window["close"].iloc[0])
    end = float(window["close"].iloc[-1])
    if start <= 0:
        return "UNKNOWN"
    pct = (end - start) / start
    if pct >= _REGIME_THRESHOLD_PCT:
        return "UP"
    if pct <= -_REGIME_THRESHOLD_PCT:
        return "DOWN"
    return "FLAT"


def evaluate(pair: str, timeframe: str, df: pd.DataFrame, htf_df, itf_df, now) -> dict:
    """Run structure/confluence analysis and the signal-decision filters.

    Args:
        pair: e.g. "XAU/USD"
        timeframe: scan timeframe, e.g. "15min"
        df: scan-TF candles (200 bars)
        htf_df: 4H candles, or None
        itf_df: 1H candles (only meaningful for 5min/15min scans), or None
        now: aware UTC datetime used for every kill-zone/session decision

    Returns:
        {"signal": None | {direction, score, max_score, factors, entry_low,
         entry_high, target1, target2, invalidation, entry_price, kz_name,
         session}, "analysis": {obs, fvgs, all_levels, structure, wyckoff_ctx, df}}
    """
    structure = analyze_structure(df)
    df = structure["df"]

    htf_bias = "RANGING"
    if htf_df is not None and not htf_df.empty:
        htf_bias = analyze_structure(htf_df)["trend_bias"]

    # 1H intermediate bias for scalp TF entries (5min/15min must align with 1H)
    itf_bias = "RANGING"
    if timeframe in ("5min", "15min") and itf_df is not None and not itf_df.empty:
        itf_bias = analyze_structure(itf_df)["trend_bias"]

    obs = validate_obs(df, find_order_blocks(df, structure["bos_events"]))
    max_ob_age = _OB_MAX_AGE.get(timeframe, 190)
    obs = _filter_obs_by_age(obs, len(df), max_ob_age)

    fvgs = update_fvg_status(df, scan_fvgs(df))

    eq_levels = find_equal_highs_lows(df)
    pd_levels = get_previous_day_week_levels(df)
    all_levels = {**eq_levels, **pd_levels}
    sweeps = detect_liquidity_sweeps(df, all_levels)

    in_kz, kz_name = is_in_kill_zone(dt=now)
    session = get_current_session(dt=now)
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

    analysis = {
        "obs": obs, "fvgs": fvgs, "all_levels": all_levels,
        "structure": structure, "wyckoff_ctx": wyckoff_ctx, "df": df,
        "htf_regime": _htf_regime_of(htf_df),
    }

    # Determine candidate signal directions
    candidates = []
    if ob_at_price:
        candidates.append("LONG" if ob_at_price["direction"] == "BULLISH" else "SHORT")
    elif structure["trend_bias"] == "BULLISH":
        candidates.append("LONG")
    elif structure["trend_bias"] == "BEARISH":
        candidates.append("SHORT")

    # Phase 1 roadmap gate: drop disabled directions before any scoring so
    # neither the confluence log nor downstream filters see them.
    pre_gate_candidates = candidates
    candidates = [d for d in candidates if _direction_allowed(d)]

    if not candidates:
        if pre_gate_candidates:
            logger.debug(f"[{pair}/{timeframe}] Candidate(s) {pre_gate_candidates} dropped "
                         f"by ENABLE_LONG gate")
        else:
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
        if htf_bias == "RANGING" and _htf_momentum_conflicts(direction, htf_df):
            logger.info(f"[{pair}/{timeframe}] Skipping {direction} — 4H bias reads RANGING "
                        f"but net momentum conflicts (missed hidden trend)")
            continue
        if direction == "LONG" and _htf_bearish_reversal(htf_df):
            logger.info(f"[{pair}/{timeframe}] Skipping LONG — HTF's most recent structural "
                        f"break (BOS) is BEARISH (likely a fresh reversal, not a "
                        f"continuation dip)")
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
        target1, target2, invalidation = _calc_risk(direction, current_price, ob_at_price, all_levels, pair, df)

        # ── Filter 5: Minimum 2:1 risk/reward ────────────────────────────
        if not _check_rr(direction, current_price, target1, invalidation, min_rr=_min_rr_for(pair)):
            logger.info(f"[{pair}/{timeframe}] Skipping {direction} — R:R below 2:1 "
                        f"(market={current_price} zone={entry_low}-{entry_high} t1={target1} sl={invalidation})")
            continue

        return {
            "signal": {
                "direction": direction,
                "score": conf["score"],
                "max_score": conf["max_score"],
                "factors": conf["factors"],
                "entry_low": entry_low,
                "entry_high": entry_high,
                "target1": target1,
                "target2": target2,
                "invalidation": invalidation,
                "entry_price": current_price,
                "kz_name": kz_name,
                "session": session,
            },
            "analysis": analysis,
        }

    return {"signal": None, "analysis": analysis}


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


def _htf_momentum_conflicts(direction: str, htf_df) -> bool:
    """Return True if raw net price change over `_RANGING_MOMENTUM_LOOKBACK`
    HTF candles clearly opposes `direction`, by at least `_RANGING_MOMENTUM_PCT`.

    Only meaningful (and only called) when `get_trend_bias()` already read
    RANGING for this HTF view — see the constants' docstring above for why.
    Fails open (returns False, i.e. no conflict) when there isn't enough
    history to judge, so it never blocks a signal it can't evaluate.
    """
    if htf_df is None or len(htf_df) < _RANGING_MOMENTUM_LOOKBACK + 1:
        return False

    window = htf_df.tail(_RANGING_MOMENTUM_LOOKBACK + 1)
    start = float(window["close"].iloc[0])
    end = float(window["close"].iloc[-1])
    if start <= 0:
        return False

    pct = (end - start) / start
    if direction == "LONG" and pct <= -_RANGING_MOMENTUM_PCT:
        return True
    if direction == "SHORT" and pct >= _RANGING_MOMENTUM_PCT:
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


# ATR multiple for the no-OB fallback SL distance (root cause: 2026-07-28
# NAS100 SHORT trace — 3 of NAS100's 4 recorded SHORT losses hit the old
# fixed 50-index-point fallback while ATR(14) was 76-149 points at entry,
# i.e. the stop was tighter (33-66% of ATR) than a single average candle's
# range, so ordinary volatility — not a wrong directional call — stopped
# the trade before the thesis could play out. 1.5x matches trend_bot's own
# (independently designed) SL-vs-ATR floor for the same reason. Docs:
# docs/superpowers/backtests/2026-07-28-nas100-short-atr-sl.md.
_SL_ATR_MULT = 1.5


def _current_atr(df: pd.DataFrame, period: int = 14) -> float:
    """Current ATR(period) as of the last row in df — a tail-slice + mean,
    same pattern as core.order_blocks._has_displacement's inline ATR (only
    the latest value is ever needed here, not a full rolling series).
    Returns 0.0 if there isn't enough history to compute a true range."""
    hist = df.tail(period + 1)
    if len(hist) < 2:
        return 0.0
    prev_close = hist["close"].shift(1)
    tr = pd.concat([
        hist["high"] - hist["low"],
        (hist["high"] - prev_close).abs(),
        (hist["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return float(tr.mean())


def _sl_distance_floor(pair: str) -> float:
    """Per-instrument floor for the no-OB fallback SL distance — a safety
    net for anomalously quiet/glitchy data, not the primary driver anymore
    (see _max_sl_distance)."""
    if pair == "NAS100":  return 50.0    # 50 index points
    if pair == "US30":    return 80.0    # 80 index points
    if "XAU" in pair:     return 8.0     # $8 = 80 gold pips
    if "XAG" in pair:     return 0.40    # $0.40 = 4 silver pips
    if "JPY" in pair:     return 0.25    # 25 JPY pips
    return 0.0020                        # 20 forex pips


# Scoped to index instruments only. Testing this on all pairs (2026-07-28)
# showed the same ATR-scaling that fixed NAS100's too-tight fallback also
# collapsed XAU/USD's signal volume via the R:R gate (see _min_rr_for) for
# no evidenced benefit there — only NAS100/US30 showed the underlying
# fixed-SL-tighter-than-typical-ATR problem in the first place.
_ATR_SCALED_PAIRS = {"NAS100", "US30"}


def _max_sl_distance(pair: str, price: float, df: pd.DataFrame = None) -> float:
    """No-OB fallback SL distance: max(per-instrument floor, ATR(14) * 1.5)
    for index pairs (_ATR_SCALED_PAIRS); the pure fixed floor for everyone
    else.

    ATR-scaling keeps the stop proportional to the instrument's *current*
    volatility instead of a flat number that can end up tighter than a
    single average candle's range (see _SL_ATR_MULT). Falls back to the
    fixed floor alone when df is missing/too short to compute ATR, or the
    pair isn't ATR-scaled at all.
    """
    floor = _sl_distance_floor(pair)
    if pair in _ATR_SCALED_PAIRS and df is not None and len(df) >= 2:
        atr = _current_atr(df)
        if atr > 0:
            return max(floor, atr * _SL_ATR_MULT)
    return floor


# Lower min R:R for the same index pairs, paired with the ATR-scaled SL
# above. Widening the SL to match real ATR also widens the risk side of
# Filter 5's R:R check while targets (from independent liquidity levels)
# don't move — collapsing signal volume for setups whose *nominal* R:R was
# only ever >= 2:1 because the old SL was artificially tight. Still under
# evaluation (2026-07-28) whether 1.5 recovers acceptable volume without
# taking on undue risk — see docs/superpowers/backtests/2026-07-28-nas100-
# short-atr-sl.md. Not deployed; needs backtest sign-off like any other
# strategy parameter (spec §3.3).
_MIN_RR_OVERRIDE = {"NAS100": 1.5, "US30": 1.5}


def _min_rr_for(pair: str) -> float:
    return _MIN_RR_OVERRIDE.get(pair, 2.0)


def _calc_risk(direction: str, price: float, ob: dict, levels: dict, pair: str = "",
               df: pd.DataFrame = None) -> tuple:
    """Derive Target 1, Target 2, and Invalidation from nearby liquidity levels.

    SL is placed just beyond the OB boundary (5% of OB range, min buffer per
    instrument). The stop is never pulled inside the OB to satisfy a risk cap —
    a stop inside the zone gets hit by normal mitigation of the very structure
    the entry is based on. When there is no OB, `_max_sl_distance` provides the
    fallback stop distance from the current price.
    """
    prec = price_precision(price)
    max_dist = _max_sl_distance(pair, price, df)

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
