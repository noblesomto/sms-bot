from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase

from config import settings

engine = create_engine(
    settings.DB_URL,
    connect_args={"check_same_thread": False} if "sqlite" in settings.DB_URL else {}
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    from db import models  # noqa: F401 — import to register models
    Base.metadata.create_all(bind=engine)
    _migrate_db()


def _migrate_db():
    """Add new columns to existing tables without dropping data (SQLite safe)."""
    new_cols = [
        ("signals", "entry_price", "FLOAT"),
        ("signals", "hit_target", "VARCHAR(10)"),
        ("signals", "hit_at", "DATETIME"),
        ("signals", "pnl_pips", "FLOAT"),
    ]
    with engine.connect() as conn:
        for table, col, col_type in new_cols:
            rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
            existing = {r[1] for r in rows}
            if col not in existing:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}"))
        conn.commit()
