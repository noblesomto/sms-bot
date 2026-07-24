import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, Depends, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from db.database import get_db
from db.models import Signal, Scan
from api.stats import (
    get_overview, get_by_pair, get_by_timeframe,
    get_factor_performance, get_equity_curve, get_recent_signals,
)

logger = logging.getLogger(__name__)

router = APIRouter()
_ws_clients: list[WebSocket] = []
_update_queue: asyncio.Queue = asyncio.Queue()

_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


@router.get("/health")
async def health_check(db: Session = Depends(get_db)) -> Dict[str, Any]:
    """Return bot liveness status, last scan timestamp, and active signal count."""
    last_scan = db.query(Scan).order_by(Scan.scanned_at.desc()).first()
    active_count = db.query(Signal).filter(Signal.status == "ACTIVE").count()
    return {
        "status": "online",
        "last_scan_at": last_scan.scanned_at.isoformat() if last_scan else None,
        "active_signals": active_count,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/signals")
async def get_signals(db: Session = Depends(get_db)) -> Dict[str, Any]:
    """Return the 20 most recent signals across all pairs."""
    rows = db.query(Signal).order_by(Signal.created_at.desc()).limit(20).all()
    return {"signals": [_ser(s) for s in rows], "count": len(rows)}


@router.get("/signals/{pair}")
async def get_signals_by_pair(pair: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    """Return the 20 most recent signals for a specific pair."""
    rows = (
        db.query(Signal)
        .filter(Signal.pair == pair.upper())
        .order_by(Signal.created_at.desc())
        .limit(20)
        .all()
    )
    return {"pair": pair.upper(), "signals": [_ser(s) for s in rows], "count": len(rows)}


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Serve the visual performance dashboard."""
    return templates.TemplateResponse("dashboard.html", {"request": request})


@router.get("/api/stats")
async def stats() -> Dict[str, Any]:
    """Return all dashboard stats as JSON (used by the dashboard JS)."""
    return {
        "overview": get_overview(),
        "by_pair": get_by_pair(),
        "by_timeframe": get_by_timeframe(),
        "factors": get_factor_performance(),
        "equity_curve": get_equity_curve(),
        "recent_signals": get_recent_signals(50),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/scan/trigger")
async def trigger_scan() -> Dict[str, Any]:
    """Manually trigger an immediate full scan (fires and forgets)."""
    from scheduler import run_scan_now
    asyncio.create_task(run_scan_now())
    return {"status": "scan_triggered", "timestamp": datetime.now(timezone.utc).isoformat()}


@router.websocket("/ws/status")
async def ws_status(websocket: WebSocket) -> None:
    """Stream real-time scan update events to connected clients."""
    await websocket.accept()
    _ws_clients.append(websocket)
    try:
        while True:
            try:
                msg = await asyncio.wait_for(_update_queue.get(), timeout=30)
                await websocket.send_text(json.dumps(msg))
            except asyncio.TimeoutError:
                await websocket.send_text(json.dumps({"type": "ping"}))
    except WebSocketDisconnect:
        _ws_clients.remove(websocket)


async def broadcast_update(update: dict) -> None:
    """Push a scan event to the WebSocket broadcast queue."""
    await _update_queue.put(update)


def _ser(s: Signal) -> dict:
    """Serialise a Signal ORM row to a JSON-safe dict."""
    return {
        "id": s.id,
        "pair": s.pair,
        "timeframe": s.timeframe,
        "direction": s.direction,
        "confluence_score": s.confluence_score,
        "entry_price": s.entry_price,
        "entry_zone_low": s.entry_zone_low,
        "entry_zone_high": s.entry_zone_high,
        "target1": s.target1,
        "target2": s.target2,
        "invalidation": s.invalidation,
        "factors": json.loads(s.factors_json) if s.factors_json else [],
        "status": s.status,
        "hit_target": s.hit_target,
        "pnl_pips": s.pnl_pips,
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "alerted_at": s.alerted_at.isoformat() if s.alerted_at else None,
        "hit_at": s.hit_at.isoformat() if s.hit_at else None,
    }
