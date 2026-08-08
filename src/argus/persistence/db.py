"""Database engine setup — WAL mode, session factory, and table creation."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

from sqlmodel import Session, SQLModel, create_engine

from argus.config import get_settings

# Import all models so SQLModel metadata is populated
from argus.persistence import models as _models  # noqa: F401


def _build_engine(db_path: str | None = None):
    settings = get_settings()
    path = db_path or settings.db_path
    url = f"sqlite:///{path}"
    engine = create_engine(
        url,
        connect_args={"check_same_thread": False},
        echo=False,
    )
    # Enable WAL mode for concurrent readers
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA journal_mode=WAL")
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")
    return engine


# Module-level singleton engine (lazy)
_engine = None


def get_engine(db_path: str | None = None):
    global _engine
    if _engine is None or db_path is not None:
        _engine = _build_engine(db_path)
    return _engine


def init_db(db_path: str | None = None) -> None:
    """Create all tables if they don't exist."""
    engine = get_engine(db_path)
    SQLModel.metadata.create_all(engine)


@contextmanager
def get_session(db_path: str | None = None) -> Generator[Session, None, None]:
    engine = get_engine(db_path)
    with Session(engine) as session:
        yield session
