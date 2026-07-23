"""Database engine, session factory and declarative base.

SQLite is used for local/dev so the whole thing runs offline with no external
services (per the brief's guardrails). Override with RECOVERIQ_DB_URL if needed.
"""
from __future__ import annotations

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DATABASE_URL = os.environ.get("RECOVERIQ_DB_URL", "sqlite:///./recoveriq.db")

_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=_connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    pass


def get_db():
    """FastAPI dependency: yields a session and always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create all tables. Import models first so they are registered."""
    from app import models  # noqa: F401  (registers mappers on Base)

    Base.metadata.create_all(bind=engine)
