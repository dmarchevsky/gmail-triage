"""Database engine/session setup (SQLite WAL) and Alembic-on-startup."""

from collections.abc import Generator
from pathlib import Path

from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.config import get_config

_engine = None
_SessionLocal: sessionmaker | None = None

BACKEND_DIR = Path(__file__).resolve().parent.parent


def get_engine():
    global _engine, _SessionLocal
    if _engine is None:
        cfg = get_config()
        url = cfg.sqlalchemy_url
        if url.startswith("sqlite"):
            cfg.data_dir.mkdir(parents=True, exist_ok=True)
        # timeout = SQLite busy timeout: writers wait instead of failing with
        # "database is locked" when another writer briefly holds the lock.
        _engine = create_engine(url, connect_args={"check_same_thread": False,
                                                   "timeout": 30}
                                if url.startswith("sqlite") else {})
        if url.startswith("sqlite"):
            @event.listens_for(_engine, "connect")
            def _set_sqlite_pragma(dbapi_conn, _record):
                cursor = dbapi_conn.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def get_sessionmaker() -> sessionmaker:
    get_engine()
    assert _SessionLocal is not None
    return _SessionLocal


def get_session() -> Generator[Session, None, None]:
    """FastAPI dependency."""
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def run_migrations() -> None:
    alembic_cfg = AlembicConfig(str(BACKEND_DIR / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    alembic_cfg.set_main_option("sqlalchemy.url", get_config().sqlalchemy_url)
    command.upgrade(alembic_cfg, "head")


def reset_engine_for_tests() -> None:
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None
