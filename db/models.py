from sqlalchemy import Column, Integer, String, Float, DateTime, Text
from sqlalchemy.sql import func

from db.database import Base


class Signal(Base):
    __tablename__ = "signals"

    id = Column(Integer, primary_key=True, index=True)
    pair = Column(String(20), nullable=False)
    timeframe = Column(String(10), nullable=False)
    direction = Column(String(10), nullable=False)
    confluence_score = Column(Integer, nullable=False)
    entry_zone_high = Column(Float, nullable=False)
    entry_zone_low = Column(Float, nullable=False)
    target1 = Column(Float, nullable=True)
    target2 = Column(Float, nullable=True)
    invalidation = Column(Float, nullable=True)
    factors_json = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    alerted_at = Column(DateTime(timezone=True), nullable=True)
    status = Column(String(20), default="ACTIVE")
    # Outcome tracking
    entry_price = Column(Float, nullable=True)       # market price at alert time (rows before 2026-07-24 hold zone midpoints)
    hit_target = Column(String(10), nullable=True)   # TP1 | TP2 | SL
    hit_at = Column(DateTime(timezone=True), nullable=True)
    pnl_pips = Column(Float, nullable=True)          # positive = profit


class Scan(Base):
    __tablename__ = "scans"

    id = Column(Integer, primary_key=True, index=True)
    pair = Column(String(20), nullable=False)
    timeframe = Column(String(10), nullable=False)
    scanned_at = Column(DateTime(timezone=True), server_default=func.now())
    candles_fetched = Column(Integer, default=0)
    signals_found = Column(Integer, default=0)
    error = Column(Text, nullable=True)
