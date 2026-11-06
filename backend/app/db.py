from __future__ import annotations

import logging
import time
from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings

logger = logging.getLogger(__name__)

def _connect_args(url: str) -> dict:
    # psycopg accepts connect_timeout; sqlite (used by the test suite) does not.
    # busy_timeout is in milliseconds. Default sqlite waits 5s and then raises;
    # a shared StaticPool connection (the test engine) can instead sit forever
    # when two sessions write, so bound the wait here too.
    if url.startswith("sqlite"):
        return {"check_same_thread": False, "timeout": 5}
    return {"connect_timeout": 5}


engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    future=True,
    connect_args=_connect_args(settings.database_url),
)

def install_fork_safety(target) -> None:
    """p30: forget inherited pooled connections in a forked child.

    RQ runs every job in a forked work-horse. Without this, the child checks
    out a psycopg connection the parent's scheduler/WeChat threads also use
    (INTRANS at pre-ping), and closing it in the child kills the parent's
    socket. ``dispose(close=False)`` drops the child's references without
    closing the parent's connections (SQLAlchemy's documented fork recipe).
    """
    import os

    if getattr(target, "_p30_fork_safe", False) or not hasattr(os, "register_at_fork"):
        return
    os.register_at_fork(after_in_child=lambda: target.dispose(close=False))
    target._p30_fork_safe = True


install_fork_safety(engine)

SessionLocal = sessionmaker(
    bind=engine,
    class_=Session,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def release_failed_session(db: Session, *, label: str = "db") -> None:
    """Roll back a failed unit of work and keep its connection out of the pool.

    The worker process shares one Engine pool between RQ sync jobs and the
    WeChat poll/flush threads.  A connection returned while still INTRANS (or
    already invalidated) makes the next borrower fail inside ``pool_pre_ping``
    with "can't change autocommit" / ``PendingRollbackError``.  Always attempt
    the rollback; retire the connection when the rollback itself fails or when
    SQLAlchemy already marked it invalid.
    """
    connection = None
    try:
        connection = db.connection()
    except Exception:  # noqa: BLE001 - a broken checkout must not hide the error
        connection = None
    try:
        db.rollback()
    except Exception:  # noqa: BLE001 - the original exception is what matters
        logger.warning("%s rollback failed; invalidating connection", label, exc_info=True)
        _invalidate_session(db, label)
        return
    if getattr(connection, "invalidated", False):
        _invalidate_session(db, label)


def _invalidate_session(db: Session, label: str) -> None:
    try:
        db.invalidate()
    except Exception:  # noqa: BLE001 - close() by the caller still runs
        logger.warning("%s invalidate failed", label, exc_info=True)


def wait_for_db(max_attempts: int = 30, delay_seconds: float = 1.0) -> None:
    """Wait for Postgres during container startup, then fail loudly."""
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            with engine.connect():
                logger.info("Database is reachable")
                return
        except OperationalError as exc:
            last_error = exc
            logger.warning(
                "Database not ready (attempt %s/%s); retrying in %.1fs",
                attempt,
                max_attempts,
                delay_seconds,
            )
            time.sleep(delay_seconds)
        except Exception as exc:  # pragma: no cover - defensive startup path
            last_error = exc
            logger.warning("Database connection error: %s", exc)
            time.sleep(delay_seconds)

    raise RuntimeError("Could not connect to the database after retries") from last_error
