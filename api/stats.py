import json
from collections import defaultdict

from sqlalchemy import func

from db.database import SessionLocal
from db.models import Signal


def get_overview() -> dict:
    db = SessionLocal()
    try:
        total = db.query(Signal).count()
        active = db.query(Signal).filter(Signal.status == "ACTIVE").count()
        wins = db.query(Signal).filter(Signal.status == "HIT").count()
        partial_wins = db.query(Signal).filter(Signal.status == "PARTIAL_WIN").count()
        losses = db.query(Signal).filter(Signal.status == "INVALIDATED").count()
        completed = wins + partial_wins + losses
        # Partial wins (TP1 hit, then stopped out) get half credit — they were
        # right on direction but didn't run to TP2.
        win_rate = round((wins + 0.5 * partial_wins) / completed * 100, 1) if completed > 0 else 0.0
        tp1_hits = db.query(Signal).filter(Signal.hit_target == "TP1").count()
        tp2_hits = db.query(Signal).filter(Signal.hit_target == "TP2").count()
        total_pips = db.query(func.sum(Signal.pnl_pips)).filter(
            Signal.pnl_pips.isnot(None)
        ).scalar() or 0.0
        return {
            "total": total, "active": active, "wins": wins, "partial_wins": partial_wins,
            "losses": losses, "completed": completed, "win_rate": win_rate,
            "tp1_hits": tp1_hits, "tp2_hits": tp2_hits,
            "total_pips": round(total_pips, 1),
        }
    finally:
        db.close()


def get_by_pair() -> list:
    db = SessionLocal()
    try:
        pairs = db.query(Signal.pair).distinct().all()
        result = []
        for (pair,) in pairs:
            wins = db.query(Signal).filter(Signal.pair == pair, Signal.status == "HIT").count()
            partial_wins = db.query(Signal).filter(
                Signal.pair == pair, Signal.status == "PARTIAL_WIN"
            ).count()
            losses = db.query(Signal).filter(Signal.pair == pair, Signal.status == "INVALIDATED").count()
            total = wins + partial_wins + losses
            pips = db.query(func.sum(Signal.pnl_pips)).filter(
                Signal.pair == pair, Signal.pnl_pips.isnot(None)
            ).scalar() or 0.0
            result.append({
                "pair": pair, "wins": wins, "partial_wins": partial_wins, "losses": losses,
                "total": total,
                "win_rate": round((wins + 0.5 * partial_wins) / total * 100, 1) if total > 0 else 0.0,
                "pips": round(pips, 1),
            })
        return sorted(result, key=lambda x: x["pair"])
    finally:
        db.close()


def get_by_timeframe() -> list:
    db = SessionLocal()
    try:
        timeframes = db.query(Signal.timeframe).distinct().all()
        result = []
        for (tf,) in timeframes:
            wins = db.query(Signal).filter(Signal.timeframe == tf, Signal.status == "HIT").count()
            partial_wins = db.query(Signal).filter(
                Signal.timeframe == tf, Signal.status == "PARTIAL_WIN"
            ).count()
            losses = db.query(Signal).filter(Signal.timeframe == tf, Signal.status == "INVALIDATED").count()
            total = wins + partial_wins + losses
            pips = db.query(func.sum(Signal.pnl_pips)).filter(
                Signal.timeframe == tf, Signal.pnl_pips.isnot(None)
            ).scalar() or 0.0
            result.append({
                "timeframe": tf, "wins": wins, "partial_wins": partial_wins, "losses": losses,
                "total": total,
                "win_rate": round((wins + 0.5 * partial_wins) / total * 100, 1) if total > 0 else 0.0,
                "pips": round(pips, 1),
            })
        tf_order = {"5min": 0, "15min": 1, "1h": 2, "4h": 3, "1day": 4}
        return sorted(result, key=lambda x: tf_order.get(x["timeframe"], 99))
    finally:
        db.close()


def get_factor_performance() -> list:
    db = SessionLocal()
    try:
        resolved = db.query(Signal).filter(
            Signal.status.in_(["HIT", "INVALIDATED", "PARTIAL_WIN"]),
            Signal.factors_json.isnot(None),
        ).all()

        factor_wins: dict = defaultdict(float)
        factor_losses: dict = defaultdict(float)

        for sig in resolved:
            try:
                factors = json.loads(sig.factors_json)
            except (json.JSONDecodeError, TypeError):
                continue
            for raw in factors:
                # Normalise to the keyword before the first colon/parenthesis
                key = raw.split(":")[0].split("(")[0].strip()
                if sig.status == "HIT":
                    factor_wins[key] += 1
                elif sig.status == "PARTIAL_WIN":
                    # TP1 hit then stopped out — half credit each way.
                    factor_wins[key] += 0.5
                    factor_losses[key] += 0.5
                else:
                    factor_losses[key] += 1

        all_keys = set(factor_wins) | set(factor_losses)
        result = []
        for key in all_keys:
            w = factor_wins[key]
            l = factor_losses[key]
            total = w + l
            result.append({
                "factor": key, "wins": w, "losses": l, "total": total,
                "win_rate": round(w / total * 100, 1) if total > 0 else 0.0,
            })
        return sorted(result, key=lambda x: -x["total"])
    finally:
        db.close()


def get_equity_curve() -> list:
    db = SessionLocal()
    try:
        resolved = db.query(Signal).filter(
            Signal.pnl_pips.isnot(None),
            Signal.hit_at.isnot(None),
        ).order_by(Signal.hit_at).all()

        cumulative = 0.0
        points = []
        for sig in resolved:
            cumulative += sig.pnl_pips
            points.append({
                "date": sig.hit_at.isoformat(),
                "pips": sig.pnl_pips,
                "cumulative": round(cumulative, 1),
                "pair": sig.pair,
                "direction": sig.direction,
                "hit_target": sig.hit_target,
            })
        return points
    finally:
        db.close()


def get_recent_signals(limit: int = 50) -> list:
    db = SessionLocal()
    try:
        rows = db.query(Signal).order_by(Signal.created_at.desc()).limit(limit).all()
        result = []
        for s in rows:
            result.append({
                "id": s.id,
                "pair": s.pair,
                "timeframe": s.timeframe,
                "direction": s.direction,
                "score": s.confluence_score,
                "entry_price": s.entry_price,
                "entry_low": s.entry_zone_low,
                "entry_high": s.entry_zone_high,
                "target1": s.target1,
                "target2": s.target2,
                "invalidation": s.invalidation,
                "status": s.status,
                "hit_target": s.hit_target,
                "pnl_pips": s.pnl_pips,
                "created_at": s.created_at.isoformat() if s.created_at else None,
                "hit_at": s.hit_at.isoformat() if s.hit_at else None,
                "factors": json.loads(s.factors_json) if s.factors_json else [],
            })
        return result
    finally:
        db.close()
