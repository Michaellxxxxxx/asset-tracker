"""p10d: startup migrations must not deadlock a worker's pending write.

The old startup path opened one transaction, took ACCESS EXCLUSIVE on
``pending_confirmations`` and only later asked for ACCESS EXCLUSIVE on
``users``.  A worker doing the reverse (SELECT users, then INSERT pending)
could therefore get 40P01 from PostgreSQL.  These tests pin the two halves of
the fix:

* migrations use one short transaction per statement, a bounded lock wait and
  ascending-PK batches;
* a user sync retries 40P01/40001 after rolling back, and the scheduler ends
  its ``users`` read transaction before the network-bound loop.

The PostgreSQL tests create and drop a disposable database on the stack from
``.env`` (127.0.0.1:5433).  They skip when that database is unavailable; the
scheduler tests only need the in-memory SQLite fixture.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from app import migrate
from app.auth import hash_password
from app.db import Base
from app.models import PendingConfirmation, SyncRun, User
from app.sync.scheduler import (
    _is_retryable_txn_error,
    _run_user_transaction,
    record_run,
    run_for_all_users,
)

PG_TEST_URL = os.environ.get(
    "P10D_TEST_DATABASE_URL",
    "postgresql+psycopg://asset:asset_pass@127.0.0.1:5433/asset_tracker_p10d_test",
)
_PG_URL = make_url(PG_TEST_URL)
PG_DB_NAME = _PG_URL.database or ""
assert PG_DB_NAME.replace("_", "").isalnum(), "test database name must be a simple identifier"
PG_ADMIN_URL = _PG_URL.set(database="postgres")


# ---------------------------------------------------------------------------
# PostgreSQL fixture: a throw-away database, never the live asset_tracker DB.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def pg_engine():
    admin = None
    engine = None
    try:
        admin = create_engine(
            PG_ADMIN_URL,
            isolation_level="AUTOCOMMIT",
            connect_args={"connect_timeout": 2},
        )
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{PG_DB_NAME}" WITH (FORCE)'))
            conn.execute(text(f'CREATE DATABASE "{PG_DB_NAME}"'))
        engine = create_engine(PG_TEST_URL, connect_args={"connect_timeout": 5})
        Base.metadata.create_all(engine)
    except Exception as exc:  # pragma: no cover - depends on the local stack
        if engine is not None:
            engine.dispose()
        if admin is not None:
            admin.dispose()
        pytest.skip(f"PostgreSQL test database unavailable: {exc}")

    try:
        yield engine
    finally:
        assert engine is not None and admin is not None
        engine.dispose()
        try:
            with admin.connect() as conn:
                conn.execute(text(f'DROP DATABASE IF EXISTS "{PG_DB_NAME}" WITH (FORCE)'))
        except Exception:  # pragma: no cover - best-effort teardown
            pass
        admin.dispose()


@pytest.fixture
def pg(pg_engine):
    """Empty, migrated-compatible tables for one test."""
    with pg_engine.begin() as conn:
        conn.execute(text("TRUNCATE TABLE users, sync_runs RESTART IDENTITY CASCADE"))
    return pg_engine


def _seed_user(conn, username: str = "p10d") -> int:
    return conn.execute(
        text(
            "INSERT INTO users (username, hashed_password, is_admin) "
            "VALUES (:username, :password, false) RETURNING id"
        ),
        {"username": username, "password": "x"},
    ).scalar_one()


def _deadlock(message: str = "deadlock detected") -> OperationalError:
    class _PgError(Exception):
        pgcode = "40P01"

    return OperationalError("SELECT 1", {}, _PgError(message))


@dataclass
class _Events:
    """Ordered trace of rollbacks, sleeps and attempts for the retry tests."""

    items: list = field(default_factory=list)

    def rollback(self) -> None:
        self.items.append("rollback")

    def sleep(self, delay: float) -> None:
        self.items.append(("sleep", delay))


# ---------------------------------------------------------------------------
# Migration transaction shape and pending batches
# ---------------------------------------------------------------------------


def test_runner_commits_each_statement_separately(pg):
    runner = migrate._MigrationRunner(pg, attempts=1)
    runner.execute(text("CREATE TABLE p10d_tx_probe (id INTEGER)"))
    # The first statement is already visible on a different connection; a
    # failure in the next statement must not roll it back.
    with pytest.raises(ProgrammingError):
        runner.execute(text("CREATE TABLE p10d_tx_probe (id INTEGER)"))
    with pg.connect() as conn:
        assert conn.execute(
            text("SELECT to_regclass('public.p10d_tx_probe')")
        ).scalar_one() is not None
    runner.execute(text("DROP TABLE p10d_tx_probe"))


def test_pending_backfill_is_ascending_and_batched(pg):
    with pg.begin() as conn:
        user_id = _seed_user(conn)
        conn.execute(
            text(
                "INSERT INTO pending_confirmations (user_id, title, detail, status, ts) "
                "SELECT :uid, 't', 'd', 'pending', NULL "
                "FROM generate_series(1, 1201)"
            ),
            {"uid": user_id},
        )

    runner = migrate._MigrationRunner(pg, batch_size=500, attempts=1)
    batches: list[list[int]] = []
    original = runner.execute

    def recording(statement, params=None):
        normalized = " ".join(str(statement).split())
        if "UPDATE pending_confirmations SET ts" in normalized:
            batches.append(list(params["ids"]))
        return original(statement, params)

    runner.execute = recording  # type: ignore[method-assign]
    migrate._migrate_pending_postgres(runner)

    assert [len(batch) for batch in batches] == [500, 500, 201]
    assert batches[0][0] < batches[1][0] < batches[2][0]
    for batch in batches:
        assert batch == sorted(batch)
    with pg.connect() as conn:
        assert conn.execute(
            text("SELECT count(*) FROM pending_confirmations WHERE ts IS NULL")
        ).scalar_one() == 0
        assert conn.execute(
            text("SELECT count(*) FROM pending_confirmations WHERE ts <> created_at")
        ).scalar_one() == 0


def test_second_migration_run_emits_no_ddl(pg):
    migrate.apply_phase2_migrations(pg)
    statements: list[str] = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(pg, "before_cursor_execute", capture)
    try:
        migrate.apply_phase2_migrations(pg)
    finally:
        event.remove(pg, "before_cursor_execute", capture)

    ddl = [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith(("ALTER", "CREATE", "DROP"))
    ]
    assert ddl == []


def test_startup_continues_after_bounded_lock_timeout(pg, monkeypatch, caplog):
    """A worker holding pending must delay startup by <2s, not hang or crash."""
    original_init = migrate._MigrationRunner.__init__

    def fast_init(
        self,
        engine,
        *,
        lock_timeout_ms=100,
        statement_timeout_ms=1_000,
        attempts=2,
        sleep=lambda _delay: None,
        batch_size=500,
    ):
        original_init(
            self,
            engine,
            lock_timeout_ms=lock_timeout_ms,
            statement_timeout_ms=statement_timeout_ms,
            attempts=attempts,
            sleep=sleep,
            batch_size=batch_size,
        )

    monkeypatch.setattr(migrate._MigrationRunner, "__init__", fast_init)
    with pg.connect() as blocker:
        transaction = blocker.begin()
        blocker.execute(text("LOCK TABLE pending_confirmations IN ACCESS EXCLUSIVE MODE"))
        started = time.monotonic()
        try:
            # Must return (not raise) and log the postponed statement.
            migrate.apply_phase2_migrations(pg)
        finally:
            elapsed = time.monotonic() - started
            transaction.rollback()

    assert elapsed < 2.5
    assert "retrying" in caplog.text
    assert "Postgres migrations postponed" in caplog.text


def test_missing_pending_column_is_added_without_a_long_transaction(pg):
    with pg.begin() as conn:
        conn.execute(text("ALTER TABLE pending_confirmations DROP COLUMN suggested_reason"))
    migrate.apply_phase2_migrations(pg)
    with pg.connect() as conn:
        columns = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'pending_confirmations'"
                )
            )
        }
    assert "suggested_reason" in columns


# ---------------------------------------------------------------------------
# Retry policy: only 40P01/40001, rollback first, 1s/2s/4s
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", ["40P01", "40001"])
def test_run_user_transaction_retries_abort_codes(code):
    class _PgError(Exception):
        pgcode = code

    events = _Events()
    attempts = {"n": 0}

    def action():
        attempts["n"] += 1
        if attempts["n"] <= 3:
            raise OperationalError("SELECT 1", {}, _PgError("conflict"))
        return "ok"

    result = _run_user_transaction(
        events, action, user_id=7, sleep=events.sleep
    )

    assert result == "ok"
    assert attempts["n"] == 4
    assert events.items == [
        "rollback",
        ("sleep", 1.0),
        "rollback",
        ("sleep", 2.0),
        "rollback",
        ("sleep", 4.0),
    ]


def test_run_user_transaction_gives_up_after_three_retries():
    events = _Events()
    attempts = {"n": 0}

    def action():
        attempts["n"] += 1
        raise _deadlock()

    with pytest.raises(OperationalError):
        _run_user_transaction(events, action, user_id=1, sleep=events.sleep)

    assert attempts["n"] == 4
    assert [item for item in events.items if item != "rollback"] == [
        ("sleep", 1.0),
        ("sleep", 2.0),
        ("sleep", 4.0),
    ]
    assert events.items.count("rollback") == 3


def test_lock_timeout_is_not_retried():
    class _PgError(Exception):
        pgcode = "55P03"

    events = _Events()
    attempts = {"n": 0}

    def action():
        attempts["n"] += 1
        raise OperationalError("ALTER TABLE", {}, _PgError("lock timeout"))

    with pytest.raises(OperationalError):
        _run_user_transaction(events, action, user_id=1, sleep=events.sleep)

    assert attempts["n"] == 1
    assert events.items == []
    assert _is_retryable_txn_error(
        OperationalError("SELECT 1", {}, _PgError("lock timeout"))
    ) is False


def test_retry_rolls_back_the_previous_attempt(db, demo_user: User):
    events = _Events()
    attempts = {"n": 0}

    def action():
        attempts["n"] += 1
        db.add(
            PendingConfirmation(
                user_id=demo_user.id,
                title="p10d",
                detail="retry me",
                status="pending",
                source="p10d",
                external_id="only-once",
            )
        )
        if attempts["n"] == 1:
            raise _deadlock()
        db.commit()
        return "ok"

    assert _run_user_transaction(db, action, user_id=demo_user.id, sleep=events.sleep) == "ok"
    assert attempts["n"] == 2
    assert db.query(PendingConfirmation).count() == 1


def test_record_run_persists_error_and_masks_secrets(db):
    def action():
        raise _deadlock("password=hunter2")

    with pytest.raises(OperationalError):
        record_run(db, trigger="manual", action=action, sleep=lambda _delay: None)

    row = db.query(SyncRun).one()
    assert row.status == "error"
    assert "hunter2" not in (row.error or "")
    assert "password=***" in (row.error or "")


# ---------------------------------------------------------------------------
# Scheduler: release the users read transaction before syncing
# ---------------------------------------------------------------------------


def test_run_for_all_users_releases_the_users_read_transaction(db, demo_user: User):
    seen: list[bool] = []

    def sync(user_id: int, mode: str | None, *, db: Session) -> None:
        # The read transaction that selected user ids must already be closed.
        seen.append(db.in_transaction())

    result = run_for_all_users(db, trigger="scheduled", sync=sync, sleep=lambda _delay: None)

    assert result.status == "ok"
    assert seen == [False]
    assert db.query(SyncRun).one().status == "ok"


def test_run_for_all_users_retries_one_user_and_continues(db, demo_user: User):
    other = User(username="other", hashed_password=hash_password("x"), is_admin=False)
    db.add(other)
    db.commit()
    db.refresh(other)
    events = _Events()
    attempts = {"n": 0}

    def sync(user_id: int, mode: str | None, *, db: Session) -> None:
        if user_id == demo_user.id:
            attempts["n"] += 1
            if attempts["n"] <= 2:
                raise _deadlock()

    result = run_for_all_users(
        db, trigger="scheduled", sync=sync, sleep=events.sleep
    )

    assert result.status == "ok"
    assert result.user_count == 2
    assert attempts["n"] == 3
    assert events.items == [("sleep", 1.0), ("sleep", 2.0)]
    assert db.query(SyncRun).one().status == "ok"
