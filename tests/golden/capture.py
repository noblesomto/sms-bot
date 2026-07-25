"""Golden-parity capture harness for scheduler.scan_pair_timeframe.

Monkeypatches every IO/DB/nondeterministic dependency the scan pipeline
touches so it can be replayed byte-for-byte against recorded fixtures:

  - scheduler.get_candles       -> loads a recorded fixture DataFrame
  - scheduler.send_alert        -> async no-op, records call args
  - scheduler.generate_chart    -> no-op, returns None
  - scheduler.SessionLocal      -> sessionmaker bound to a fresh temp sqlite
                                    DB (tables created from db.models)
  - scheduler.is_in_kill_zone   -> fixed (True, "LONDON_OPEN")
  - scheduler.get_current_session -> fixed "LONDON"

Used twice:
  1. Against pre-refactor code, to produce the frozen tests/golden/expected.json.
  2. From tests/test_golden.py, against the refactored code, to prove the
     extraction of core/strategy.py:evaluate() changed nothing observable.
"""
import asyncio
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

import pandas as pd

import scheduler
from db.database import Base
from db.models import Signal

FIXTURES_DIR = Path(__file__).parent / "fixtures"

# Pairs x timeframes the golden harness exercises. 15min pulls in an extra
# 1H ITF fetch; both pull in the 4H HTF fetch — all three fixture files
# per pair must exist (see record_fixtures.py).
PAIRS = ["XAU/USD", "EUR/USD", "GBP/JPY", "US30"]
TIMEFRAMES = ["15min", "1h"]

PAIR_SAFE = {
    "XAU/USD": "XAU_USD",
    "EUR/USD": "EUR_USD",
    "GBP/JPY": "GBP_JPY",
    "US30": "US30",
}


def _load_fixture(pair: str, timeframe: str):
    safe = PAIR_SAFE.get(pair, pair.replace("/", "_"))
    path = FIXTURES_DIR / f"{safe}_{timeframe}.parquet"
    if not path.exists():
        return None
    return pd.read_parquet(path)


def _fake_get_candles(pair, timeframe, outputsize=200, force_fresh=False):
    df = _load_fixture(pair, timeframe)
    if df is None or df.empty:
        return None
    return df.tail(outputsize).reset_index(drop=True)


def _fake_generate_chart(*args, **kwargs):
    return None


def _fake_is_in_kill_zone(*args, **kwargs):
    return True, "LONDON_OPEN"


def _fake_get_current_session(*args, **kwargs):
    return "LONDON"


def _serialize_signal(sig: Signal) -> dict:
    """All Signal columns except id/created_at/alerted_at/hit_at (timestamps
    are inherently nondeterministic across capture runs)."""
    return {
        "pair": sig.pair,
        "timeframe": sig.timeframe,
        "direction": sig.direction,
        "confluence_score": sig.confluence_score,
        "entry_zone_high": sig.entry_zone_high,
        "entry_zone_low": sig.entry_zone_low,
        "target1": sig.target1,
        "target2": sig.target2,
        "invalidation": sig.invalidation,
        "factors_json": sig.factors_json,
        "status": sig.status,
        "entry_price": sig.entry_price,
        "hit_target": sig.hit_target,
        "pnl_pips": sig.pnl_pips,
    }


@contextmanager
def _patched_scheduler(db_path: str, alerts_sent: list):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    test_session_local = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    async def fake_send_alert(pair, payload, chart_path=None):
        alerts_sent.append({"pair": pair, "payload": payload, "chart_path": chart_path})

    originals = {
        "get_candles": scheduler.get_candles,
        "send_alert": scheduler.send_alert,
        "generate_chart": scheduler.generate_chart,
        "SessionLocal": scheduler.SessionLocal,
        "is_in_kill_zone": scheduler.is_in_kill_zone,
        "get_current_session": scheduler.get_current_session,
    }
    scheduler.get_candles = _fake_get_candles
    scheduler.send_alert = fake_send_alert
    scheduler.generate_chart = _fake_generate_chart
    scheduler.SessionLocal = test_session_local
    scheduler.is_in_kill_zone = _fake_is_in_kill_zone
    scheduler.get_current_session = _fake_get_current_session
    try:
        yield test_session_local
    finally:
        for name, value in originals.items():
            setattr(scheduler, name, value)
        engine.dispose()


def capture(output_path=None) -> dict:
    """Run the full golden scenario (4 pairs x 2 scan timeframes) against the
    patched scheduler and return {"runs": [...], "signals": [...]}.

    If output_path is given, also writes the result there as JSON.
    """
    alerts_sent: list = []
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "golden.db")
        with _patched_scheduler(db_path, alerts_sent) as test_session_local:
            runs = []
            for pair in PAIRS:
                for tf in TIMEFRAMES:
                    res = asyncio.run(scheduler.scan_pair_timeframe(pair, tf))
                    runs.append({"pair": pair, "timeframe": tf, "result": res})

            db = test_session_local()
            try:
                signals = db.query(Signal).order_by(Signal.id).all()
                signal_rows = [_serialize_signal(s) for s in signals]
            finally:
                db.close()

    output = {
        "runs": runs,
        "signals": signal_rows,
        "alerts_sent_count": len(alerts_sent),
    }
    if output_path:
        with open(output_path, "w") as f:
            json.dump(output, f, indent=2, sort_keys=False, default=str)
    return output


if __name__ == "__main__":
    out_path = Path(__file__).parent / "expected.json"
    result = capture(out_path)
    print(
        f"Captured {len(result['runs'])} runs, {len(result['signals'])} signal(s), "
        f"{result['alerts_sent_count']} alert(s) -> {out_path}"
    )
