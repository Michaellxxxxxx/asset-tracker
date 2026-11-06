"""Idempotent migrations for phase-2 and phase-3 columns and check constraints.

``create_all`` does not add columns to tables that already exist. On Postgres we
ALTER TABLE; on SQLite (tests) create_all already builds the full model, so this
is a no-op unless a column is somehow missing.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

from sqlalchemy import bindparam, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import NoSuchTableError, OperationalError
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Startup migrations must never hold one transaction across many tables. Each
# statement below gets its own short transaction, a 5s lock wait, and a 30s
# statement cap; a blocked migration is postponed instead of stalling the app.
PENDING_LOCK_TIMEOUT_MS = 5_000
PENDING_STATEMENT_TIMEOUT_MS = 30_000
MIGRATION_LOCK_ATTEMPTS = 3
MIGRATION_RETRY_DELAYS = (1.0, 2.0)
PENDING_BATCH_SIZE = 500

PENDING_COLUMNS: tuple[tuple[str, str], ...] = (
    ("source", "VARCHAR(64)"),
    ("external_id", "VARCHAR(255)"),
    ("direction", "VARCHAR(8)"),
    ("asset", "VARCHAR(32)"),
    ("amount_raw", "NUMERIC(36, 18)"),
    ("suggested_category", "VARCHAR(32)"),
    ("suggested_confidence", "NUMERIC(4, 3)"),
    ("suggested_reason", "VARCHAR(255)"),
    ("final_category", "VARCHAR(32)"),
    ("ts", "TIMESTAMP WITH TIME ZONE"),
    ("ledger_entry_id", "INTEGER"),
    # Phase-4: Blockscout enrichment (JSON text / JSONB-compatible).
    ("meta", "JSON"),
)

SNAPSHOT_COLUMNS: tuple[tuple[str, str, str | None], ...] = (
    # name, type, optional server default applied only when the column is new
    ("source", "VARCHAR(16)", "'seed'"),
    # P13 independent data provenance. Nullable: old rows stay NULL and are
    # interpreted from ``source`` by the read path; no historical backfill.
    ("provenance", "VARCHAR(16)", None),
    ("live", "BOOLEAN", None),
    ("breakdown", "TEXT", None),
)

LEDGER_KINDS = (
    "deposit",
    "withdraw",
    "trade",
    "funding",
    "other",
    "airdrop",
    "kol_reward",
    "transfer",
    "defi_deposit",
    "defi_redeem",
    "swap",
)
PENDING_STATUSES = ("pending", "confirmed", "rejected")


class _MigrationLockTimeout(RuntimeError):
    """A migration statement could not obtain its lock within the retry budget."""


class _MigrationResult:
    """Rows fetched before the short transaction committed."""

    def __init__(self, rows: list, rowcount: int) -> None:
        self._rows = rows
        self.rowcount = rowcount

    def all(self) -> list:
        return self._rows

    def scalars(self) -> "_ScalarRows":
        return _ScalarRows([row[0] for row in self._rows])


class _ScalarRows:
    def __init__(self, values: list) -> None:
        self._values = values

    def all(self) -> list:
        return self._values


def _is_lock_conflict(exc: BaseException) -> bool:
    """True for lock_timeout, deadlock, serialization failure and cancellation."""
    if not isinstance(exc, OperationalError):
        return False
    for candidate in (getattr(exc, "orig", None), exc):
        if candidate is None:
            continue
        code = getattr(candidate, "pgcode", None) or getattr(candidate, "sqlstate", None)
        if code:
            return str(code) in {"55P03", "40P01", "40001", "57014"}
    text_value = f"{type(getattr(exc, 'orig', exc)).__name__}: {getattr(exc, 'orig', exc)}".lower()
    return any(
        marker in text_value
        for marker in ("lock timeout", "deadlock", "serialization failure", "statement timeout")
    )


class _MigrationRunner:
    """One short transaction per DDL/DML statement, with bounded lock waits."""

    def __init__(
        self,
        engine: Engine,
        *,
        lock_timeout_ms: int = PENDING_LOCK_TIMEOUT_MS,
        statement_timeout_ms: int = PENDING_STATEMENT_TIMEOUT_MS,
        attempts: int = MIGRATION_LOCK_ATTEMPTS,
        sleep: Callable[[float], None] | None = None,
        batch_size: int = PENDING_BATCH_SIZE,
    ) -> None:
        self.engine = engine
        self.dialect = engine.dialect.name
        self.lock_timeout_ms = int(lock_timeout_ms)
        self.statement_timeout_ms = int(statement_timeout_ms)
        self.attempts = max(int(attempts), 1)
        self.sleep = sleep if sleep is not None else time.sleep
        self.batch_size = max(int(batch_size), 1)
        self._table_cache: dict[str, bool] = {}
        self._columns_cache: dict[str, dict[str, dict]] = {}
        self._index_cache: dict[str, set[str]] = {}
        self._check_cache: dict[str, set[str]] = {}

    def _configure(self, conn) -> None:
        if self.dialect != "postgresql":
            return
        # Literal integers are safe: they come from this module's constants, not
        # request data. SET LOCAL is scoped to this short transaction.
        conn.execute(text(f"SET LOCAL lock_timeout = {self.lock_timeout_ms}"))
        conn.execute(text(f"SET LOCAL statement_timeout = {self.statement_timeout_ms}"))

    def _execute_once(self, statement, params: dict | None) -> _MigrationResult:
        with self.engine.begin() as conn:
            self._configure(conn)
            result = conn.execute(statement, params or {})
            rowcount = int(getattr(result, "rowcount", -1))
            rows = list(result.fetchall()) if result.returns_rows else []
        return _MigrationResult(rows, rowcount)

    def execute(self, statement, params: dict | None = None) -> _MigrationResult:
        for attempt in range(self.attempts):
            try:
                return self._execute_once(statement, params)
            except OperationalError as exc:
                if not _is_lock_conflict(exc):
                    raise
                if attempt + 1 >= self.attempts:
                    summary = " ".join(str(statement).split())[:120]
                    logger.warning(
                        "migration statement postponed after %s lock-wait attempt(s): %s",
                        self.attempts,
                        summary,
                    )
                    raise _MigrationLockTimeout(summary) from exc
                delay = MIGRATION_RETRY_DELAYS[min(attempt, len(MIGRATION_RETRY_DELAYS) - 1)]
                summary = " ".join(str(statement).split())[:120]
                logger.warning(
                    "migration statement hit a lock conflict; retrying in %ss (attempt %s/%s): %s",
                    delay,
                    attempt + 2,
                    self.attempts,
                    summary,
                )
                self.sleep(delay)
        raise _MigrationLockTimeout("migration retry budget exhausted")  # pragma: no cover

    def _reflect(self, getter, what: str):
        """Run one inspection on a short connection with the same lock budget.

        Postgres reflection takes an AccessShare lock on the inspected relation
        (its ``pg_attribute`` query joins the table's lock), so an unrelated
        ``ALTER TABLE`` can block a naive ``inspect(engine)`` forever. Bound it
        like DDL: 5s wait, bounded retries, then let startup continue.
        """
        for attempt in range(self.attempts):
            try:
                with self.engine.connect() as conn:
                    if self.dialect == "postgresql":
                        conn.execute(text(f"SET LOCAL lock_timeout = {self.lock_timeout_ms}"))
                        conn.execute(
                            text(f"SET LOCAL statement_timeout = {self.statement_timeout_ms}")
                        )
                    return getter(inspect(conn))
            except OperationalError as exc:
                if not _is_lock_conflict(exc):
                    raise
                if attempt + 1 >= self.attempts:
                    logger.warning(
                        "migration schema inspection postponed after %s lock-wait "
                        "attempt(s): %s",
                        self.attempts,
                        what,
                    )
                    raise _MigrationLockTimeout(what) from exc
                delay = MIGRATION_RETRY_DELAYS[min(attempt, len(MIGRATION_RETRY_DELAYS) - 1)]
                logger.warning(
                    "migration schema inspection hit a lock conflict; retrying in "
                    "%ss (attempt %s/%s): %s",
                    delay,
                    attempt + 2,
                    self.attempts,
                    what,
                )
                self.sleep(delay)
        raise _MigrationLockTimeout(what)  # pragma: no cover

    def _columns(self, table: str) -> dict[str, dict]:
        if table not in self._columns_cache:
            try:
                rows = self._reflect(
                    lambda inspector: inspector.get_columns(table), f"columns of {table}"
                )
                self._columns_cache[table] = {col["name"]: col for col in rows}
            except NoSuchTableError:
                self._columns_cache[table] = {}
        return self._columns_cache[table]

    def column(self, table: str, name: str) -> dict | None:
        return self._columns(table).get(name)

    def has_column(self, table: str, name: str) -> bool:
        return name in self._columns(table)

    def has_table(self, table: str) -> bool:
        if table not in self._table_cache:
            try:
                self._table_cache[table] = bool(
                    self._reflect(lambda inspector: inspector.has_table(table), f"table {table}")
                )
            except NoSuchTableError:
                self._table_cache[table] = False
        return self._table_cache[table]

    def has_index(self, table: str, name: str) -> bool:
        if table not in self._index_cache:
            try:
                self._index_cache[table] = {
                    idx["name"]
                    for idx in self._reflect(
                        lambda inspector: inspector.get_indexes(table), f"indexes of {table}"
                    )
                }
            except NoSuchTableError:
                self._index_cache[table] = set()
        return name in self._index_cache[table]

    def has_check(self, table: str, name: str) -> bool:
        if table not in self._check_cache:
            try:
                self._check_cache[table] = {
                    constraint["name"]
                    for constraint in self._reflect(
                        lambda inspector: inspector.get_check_constraints(table),
                        f"checks of {table}",
                    )
                }
            except NoSuchTableError:
                self._check_cache[table] = set()
        return name in self._check_cache[table]

    def ensure_table(self, table: str, create_sql: str) -> bool:
        if self.has_table(table):
            return False
        self.execute(text(create_sql))
        self._table_cache[table] = True
        self._columns_cache.pop(table, None)
        self._index_cache.pop(table, None)
        self._check_cache.pop(table, None)
        return True

    def ensure_column(self, table: str, name: str, ddl: str) -> bool:
        if self.has_column(table, name):
            return False
        self.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {ddl}"))
        self._columns_cache.pop(table, None)
        return True

    def ensure_index(self, table: str, name: str, create_sql: str) -> bool:
        if self.has_index(table, name):
            return False
        self.execute(text(create_sql))
        self._index_cache.pop(table, None)
        return True

    def ensure_check(self, table: str, name: str, expression: str) -> bool:
        # Add-only: dropping and recreating a check on every boot would take
        # ACCESS EXCLUSIVE locks for no schema change.
        if self.has_check(table, name):
            return False
        self.execute(text(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({expression})"))
        self._check_cache.pop(table, None)
        return True

    def batch_update_ids(self, select_sql: str, update_sql, *, batch_size: int | None = None) -> int:
        """Update rows in ascending-PK batches, committing after each batch.

        ``select_sql`` must filter ``id > :last_id`` and order by ``id ASC`` so a
        batch that changes no rows still advances the keyset instead of looping.
        """
        size = max(int(batch_size or self.batch_size), 1)
        last_id = 0
        total = 0
        while True:
            ids = (
                self.execute(text(select_sql), {"last_id": last_id, "limit": size})
                .scalars()
                .all()
            )
            if not ids:
                break
            self.execute(update_sql, {"ids": ids})
            last_id = int(ids[-1])
            total += len(ids)
            if len(ids) < size:
                break
        return total



def apply_phase2_migrations(engine: Engine) -> None:
    """Phase-2 plus phase-3. The name is kept so existing callers stay valid."""
    dialect = engine.dialect.name
    if dialect == "postgresql":
        _migrate_postgres(engine)
    elif dialect == "sqlite":
        _migrate_sqlite(engine)
    else:  # pragma: no cover
        logger.info("Skipping migrations for dialect %s", dialect)


def _migrate_pending_postgres(mig: _MigrationRunner) -> None:
    """Pending schema only, one short transaction per missing object.

    The old code opened with ``ALTER TABLE pending_confirmations`` and kept its
    ACCESS EXCLUSIVE lock until the very end of startup. Pre-checking makes an
    already-migrated database a true no-op, and each commit releases the lock
    before the next table is touched.
    """
    for name, ddl in PENDING_COLUMNS:
        mig.ensure_column("pending_confirmations", name, ddl)
    mig.ensure_index(
        "pending_confirmations",
        "uq_pending_user_source_external",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_pending_user_source_external "
        "ON pending_confirmations (user_id, source, external_id)",
    )
    mig.ensure_check(
        "pending_confirmations",
        "ck_pending_confirmations_status",
        "status IN ('pending', 'confirmed', 'rejected')",
    )
    # A small, idempotent data normalisation: rows from before ``ts`` existed
    # fall back to created_at everywhere, so materialise that same value in
    # ascending-PK batches. Each batch commits and releases its row locks;
    # no single statement scans or locks the whole table.
    mig.batch_update_ids(
        "SELECT id FROM pending_confirmations "
        "WHERE ts IS NULL AND id > :last_id "
        "ORDER BY id ASC LIMIT :limit",
        text(
            "UPDATE pending_confirmations SET ts = created_at "
            "WHERE id IN :ids AND ts IS NULL"
        ).bindparams(bindparam("ids", expanding=True)),
    )


def _migrate_postgres(engine: Engine) -> None:
    mig = _MigrationRunner(engine)
    try:
        # Fixed order: pending first (short transactions), then everything else.
        # No transaction spans two tables, so a worker holding ``users`` can no
        # longer deadlock with startup waiting for ``pending_confirmations``.
        _migrate_pending_postgres(mig)
        kind_list = ", ".join(f"'{kind}'" for kind in LEDGER_KINDS)
        mig.ensure_check(
            "ledger_entries",
            "ck_ledger_entries_kind",
            f"kind IN ({kind_list})",
        )
        _migrate_snapshots_postgres(mig)
        mig.ensure_check(
            "sync_runs",
            "ck_sync_runs_status",
            "status IN ('running', 'ok', 'partial', 'error')",
        )
        mig.ensure_table(
            "user_wallets",
            "CREATE TABLE IF NOT EXISTS user_wallets ("
            "id SERIAL PRIMARY KEY, "
            "user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
            "chain VARCHAR(16) NOT NULL, "
            "address VARCHAR(128) NOT NULL, "
            "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), "
            "CONSTRAINT uq_user_wallet UNIQUE (user_id, chain, address)"
            ")",
        )
        mig.ensure_index(
            "user_wallets",
            "ix_user_wallets_user_id",
            "CREATE INDEX IF NOT EXISTS ix_user_wallets_user_id ON user_wallets (user_id)",
        )
        _migrate_address_labels_postgres(mig)
        _migrate_wechat_postgres(mig)
        _migrate_positions_postgres(mig)
        _migrate_asset_overrides_postgres(mig)
        _migrate_sync_reconcile_postgres(mig)
        mig.ensure_column("users", "returns_start_date", "DATE")
        _migrate_user_auth_postgres(mig)
        _seed_returns_start(mig)
    except _MigrationLockTimeout as exc:
        # A busy worker must not take the API down. The schema is unchanged for
        # the blocked statement; the next app start retries it.
        logger.warning("Postgres migrations postponed; app startup continues: %s", exc)
        return
    logger.info("Phase-2/3 Postgres migrations applied")


def _migrate_address_labels_postgres(mig: _MigrationRunner) -> None:
    """p11g: per-chain counterparty label cache, additive and idempotent."""
    mig.ensure_table(
        "address_labels",
        "CREATE TABLE IF NOT EXISTS address_labels ("
        "chain VARCHAR(32) NOT NULL, "
        "address VARCHAR(128) NOT NULL, "
        "label VARCHAR(255) NOT NULL, "
        "kind VARCHAR(16) NOT NULL, "
        "source VARCHAR(64) NOT NULL, "
        "is_scam BOOLEAN NOT NULL DEFAULT FALSE, "
        "meta JSONB, "
        "fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), "
        "expires_at TIMESTAMPTZ, "
        "PRIMARY KEY (chain, address), "
        "CONSTRAINT ck_address_labels_kind CHECK "
        "(kind IN ('contract', 'eoa', 'token', 'zero', 'unknown'))"
        ")",
    )
    mig.ensure_index(
        "address_labels",
        "ix_address_labels_expires_at",
        "CREATE INDEX IF NOT EXISTS ix_address_labels_expires_at "
        "ON address_labels (expires_at)",
    )


def _ensure_wechat_alert_queue_identity(conn) -> None:
    """P8a: one active queue row per alert event; failed history is immutable.

    Older databases may already contain duplicate active rows for one event.
    We read the duplicate groups first (read-only SELECT), then keep the safest
    row per group -- a sent row if one exists, else the oldest queued row --
    and delete only the *extra active* duplicates. Rows whose status is
    ``failed`` (including the six pre-P7a pushes) are never selected, never
    deleted, and never reset. The partial unique index therefore covers every
    row except terminal failures.
    """
    if hasattr(conn, "has_index") and conn.has_index(
        "wechat_push_queue", "uq_wechat_push_queue_alert_event_active"
    ):
        # A present index proves the cleanup already ran, and now prevents new
        # duplicates. Skip the GROUP BY and per-row deletes on every boot.
        return
    duplicates = conn.execute(
        text(
            "SELECT alert_event_id, COUNT(*) AS n FROM wechat_push_queue "
            "WHERE alert_event_id IS NOT NULL AND status <> 'failed' "
            "GROUP BY alert_event_id HAVING COUNT(*) > 1"
        )
    ).all()
    removed = 0
    for event_id, _count in duplicates:
        rows = conn.execute(
            text(
                "SELECT id FROM wechat_push_queue "
                "WHERE alert_event_id = :event_id AND status <> 'failed' "
                "ORDER BY CASE status WHEN 'sent' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END, "
                "id ASC"
            ),
            {"event_id": event_id},
        ).scalars().all()
        for row_id in rows[1:]:
            conn.execute(
                text("DELETE FROM wechat_push_queue WHERE id = :row_id"),
                {"row_id": row_id},
            )
            removed += 1
    if removed:
        logger.warning(
            "wechat queue migration removed %s duplicate active alert row(s)", removed
        )
    conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_wechat_push_queue_alert_event_active "
            "ON wechat_push_queue (alert_event_id) "
            "WHERE alert_event_id IS NOT NULL AND status <> 'failed'"
        )
    )


def _wechat_postgres_current(mig: _MigrationRunner) -> bool:
    """True when every WeChat schema object already exists.

    Startup used to run ``ALTER TABLE wechat_push_queue`` unconditionally. Even
    an ``IF NOT EXISTS`` no-op takes an ACCESS EXCLUSIVE lock, so a busy queue
    could delay every boot. Inspect first and skip the DDL entirely.
    """
    if not all(
        mig.has_table(table)
        for table in ("wechat_push_queue", "wechat_context_tokens", "wechat_poll_state")
    ):
        return False
    pending_id = mig.column("wechat_push_queue", "pending_id")
    if pending_id is None or not pending_id.get("nullable", True):
        return False
    for name in ("next_attempt_at", "alert_event_id"):
        if not mig.has_column("wechat_push_queue", name):
            return False
    return all(
        mig.has_index("wechat_push_queue", name)
        for name in (
            "ix_wechat_push_queue_pending_id",
            "ix_wechat_push_queue_alert_event_id",
            "ix_wechat_push_queue_status_user",
        )
    )


def _migrate_wechat_postgres(conn) -> None:
    """Insurance for databases created before the WeChat tables existed.

    ``create_all`` already builds them on a fresh database. These statements
    only fill a gap; they do not store the bot token.
    """
    if isinstance(conn, _MigrationRunner) and _wechat_postgres_current(conn):
        _ensure_wechat_alert_queue_identity(conn)
        return
    conn.execute(
        text(
            "CREATE TABLE IF NOT EXISTS wechat_push_queue ("
            "id SERIAL PRIMARY KEY, "
            "pending_id INTEGER REFERENCES pending_confirmations(id) ON DELETE CASCADE, "
            "alert_event_id INTEGER REFERENCES alert_events(id) ON DELETE CASCADE, "
            "user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
            "text TEXT NOT NULL, "
            "status VARCHAR(16) NOT NULL DEFAULT 'queued', "
            "attempts INTEGER NOT NULL DEFAULT 0, "
            "last_error VARCHAR(255), "
            "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), "
            "sent_at TIMESTAMPTZ, "
            "next_attempt_at TIMESTAMPTZ, "
            "CONSTRAINT ck_wechat_push_queue_status CHECK (status IN ('queued', 'sent', 'failed'))"
            ")"
        )
    )
    # P7a: backoff gate. Additive only; existing rows read as "due now".
    conn.execute(
        text(
            "ALTER TABLE wechat_push_queue "
            "ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMPTZ"
        )
    )
    # Older queues required pending_id. Alerts have no pending row, so make the
    # column nullable and add the alert reference. Both statements are no-ops
    # on a queue created by the block above.
    conn.execute(
        text("ALTER TABLE wechat_push_queue ALTER COLUMN pending_id DROP NOT NULL")
    )
    conn.execute(
        text(
            "ALTER TABLE wechat_push_queue "
            "ADD COLUMN IF NOT EXISTS alert_event_id INTEGER "
            "REFERENCES alert_events(id) ON DELETE CASCADE"
        )
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_wechat_push_queue_pending_id "
            "ON wechat_push_queue (pending_id)"
        )
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_wechat_push_queue_alert_event_id "
            "ON wechat_push_queue (alert_event_id)"
        )
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_wechat_push_queue_status_user "
            "ON wechat_push_queue (status, user_id)"
        )
    )
    conn.execute(
        text(
            "CREATE TABLE IF NOT EXISTS wechat_context_tokens ("
            "user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE, "
            "context_token TEXT NOT NULL, "
            "updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()"
            ")"
        )
    )
    conn.execute(
        text(
            "CREATE TABLE IF NOT EXISTS wechat_poll_state ("
            "key VARCHAR(64) PRIMARY KEY, "
            "value TEXT NOT NULL DEFAULT ''"
            ")"
        )
    )
    _ensure_wechat_alert_queue_identity(conn)


def _positions_postgres_current(mig: _MigrationRunner) -> bool:
    """True when the phase-4/7/8/9 position schema is already complete."""
    if not all(
        mig.has_table(table)
        for table in ("positions", "position_snapshots", "alert_rules", "alert_events")
    ):
        return False
    for table, names in (
        ("positions", ("sync_id", "close_reason", "provenance", "live")),
        ("position_snapshots", ("sync_id", "provenance", "live")),
        ("alert_events", ("created_at", "event_kind")),
    ):
        if not all(mig.has_column(table, name) for name in names):
            return False
    created_at = mig.column("alert_events", "created_at")
    sent_at = mig.column("alert_events", "sent_at")
    if created_at is None or created_at.get("nullable", True):
        return False
    # A legacy NOT NULL/default on sent_at means the normalisation still has to
    # run; a nullable column without a server default is the finished shape.
    if sent_at is None or not sent_at.get("nullable", False) or sent_at.get("default"):
        return False
    return all(
        mig.has_index(table, name)
        for table, name in (
            ("positions", "ix_positions_user_id"),
            ("positions", "ix_positions_user_kind"),
            ("position_snapshots", "ix_position_snapshots_position_id"),
            ("position_snapshots", "ix_position_snapshots_position_ts"),
            ("alert_rules", "ix_alert_rules_user_id"),
            ("alert_events", "ix_alert_events_user_created"),
            ("alert_events", "ix_alert_events_user_id"),
            ("alert_events", "ix_alert_events_user_sent"),
        )
    )


def _migrate_positions_postgres(conn) -> None:
    """Insurance CREATE TABLE for positions / snapshots / alert rules and events.

    ``create_all`` already builds these on a fresh database. Repeating this
    function is a no-op (IF NOT EXISTS on tables and indexes).
    """
    if isinstance(conn, _MigrationRunner) and _positions_postgres_current(conn):
        return
    conn.execute(
        text(
            "CREATE TABLE IF NOT EXISTS positions ("
            "id SERIAL PRIMARY KEY, "
            "user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
            "kind VARCHAR(16) NOT NULL, "
            "protocol VARCHAR(16) NOT NULL, "
            "chain VARCHAR(32), "
            "account_ref VARCHAR(128) NOT NULL, "
            "market_ref VARCHAR(255) NOT NULL, "
            "label VARCHAR(160), "
            "status VARCHAR(16) NOT NULL DEFAULT 'open', "
            "net_value_usdt NUMERIC(38, 8) NOT NULL DEFAULT 0, "
            "collateral_usdt NUMERIC(38, 8) NOT NULL DEFAULT 0, "
            "debt_usdt NUMERIC(38, 8) NOT NULL DEFAULT 0, "
            "details JSONB, "
            "opened_at TIMESTAMPTZ, "
            "asof TIMESTAMPTZ, "
            "sync_id INTEGER REFERENCES sync_runs(id) ON DELETE SET NULL, "
            "close_reason VARCHAR(32), "
            "provenance VARCHAR(16), "
            "live BOOLEAN, "
            "CONSTRAINT uq_positions_user_protocol_account_market "
            "UNIQUE (user_id, protocol, account_ref, market_ref), "
            "CONSTRAINT ck_positions_kind CHECK (kind IN ('option', 'lending')), "
            "CONSTRAINT ck_positions_protocol CHECK ("
            "protocol IN ('deribit', 'binance', 'aave', 'morpho', 'pendle', 'kamino', 'jupiter')"
            "), "
            "CONSTRAINT ck_positions_status CHECK (status IN ('open', 'closed'))"
            ")"
        )
    )
    conn.execute(
        text("CREATE INDEX IF NOT EXISTS ix_positions_user_id ON positions (user_id)")
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_positions_user_kind ON positions (user_id, kind)"
        )
    )
    conn.execute(
        text(
            "CREATE TABLE IF NOT EXISTS position_snapshots ("
            "id SERIAL PRIMARY KEY, "
            "position_id INTEGER NOT NULL REFERENCES positions(id) ON DELETE CASCADE, "
            "ts TIMESTAMPTZ NOT NULL, "
            "net_value_usdt NUMERIC(38, 8) NOT NULL, "
            "collateral_usdt NUMERIC(38, 8) NOT NULL DEFAULT 0, "
            "debt_usdt NUMERIC(38, 8) NOT NULL DEFAULT 0, "
            "health_factor NUMERIC(38, 8), "
            "apy_net NUMERIC(38, 8), "
            "details JSONB, "
            "sync_id INTEGER REFERENCES sync_runs(id) ON DELETE SET NULL, "
            "provenance VARCHAR(16), "
            "live BOOLEAN"
            ")"
        )
    )
    # P8d additive columns for databases created before sync provenance.
    # Existing rows stay NULL; no backfill.
    conn.execute(
        text(
            "ALTER TABLE positions ADD COLUMN IF NOT EXISTS "
            "sync_id INTEGER REFERENCES sync_runs(id) ON DELETE SET NULL"
        )
    )
    conn.execute(
        text("ALTER TABLE positions ADD COLUMN IF NOT EXISTS close_reason VARCHAR(32)")
    )
    conn.execute(
        text("ALTER TABLE positions ADD COLUMN IF NOT EXISTS provenance VARCHAR(16)")
    )
    conn.execute(text("ALTER TABLE positions ADD COLUMN IF NOT EXISTS live BOOLEAN"))
    conn.execute(
        text(
            "ALTER TABLE position_snapshots ADD COLUMN IF NOT EXISTS "
            "sync_id INTEGER REFERENCES sync_runs(id) ON DELETE SET NULL"
        )
    )
    conn.execute(
        text(
            "ALTER TABLE position_snapshots ADD COLUMN IF NOT EXISTS provenance VARCHAR(16)"
        )
    )
    conn.execute(
        text("ALTER TABLE position_snapshots ADD COLUMN IF NOT EXISTS live BOOLEAN")
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_position_snapshots_position_id "
            "ON position_snapshots (position_id)"
        )
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_position_snapshots_position_ts "
            "ON position_snapshots (position_id, ts)"
        )
    )
    conn.execute(
        text(
            "CREATE TABLE IF NOT EXISTS alert_rules ("
            "id SERIAL PRIMARY KEY, "
            "user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
            "protocol VARCHAR(16), "
            "warn_hf NUMERIC(38, 8) NOT NULL DEFAULT 1.5, "
            "danger_hf NUMERIC(38, 8) NOT NULL DEFAULT 1.2, "
            "enabled BOOLEAN NOT NULL DEFAULT TRUE, "
            "cooldown_min INTEGER NOT NULL DEFAULT 360, "
            "CONSTRAINT uq_alert_rules_user_protocol UNIQUE (user_id, protocol)"
            ")"
        )
    )
    conn.execute(
        text("CREATE INDEX IF NOT EXISTS ix_alert_rules_user_id ON alert_rules (user_id)")
    )
    conn.execute(
        text(
            "CREATE TABLE IF NOT EXISTS alert_events ("
            "id SERIAL PRIMARY KEY, "
            "user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
            "rule_id INTEGER REFERENCES alert_rules(id) ON DELETE SET NULL, "
            "position_id INTEGER REFERENCES positions(id) ON DELETE SET NULL, "
            "level VARCHAR(16) NOT NULL, "
            "hf NUMERIC(38, 8), "
            "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), "
            "sent_at TIMESTAMPTZ, "
            "channel_result VARCHAR(255), "
            "CONSTRAINT ck_alert_events_level CHECK (level IN ('warn', 'danger', 'recover'))"
            ")"
        )
    )
    # P7a: ``sent_at`` means delivered, so queued events leave it NULL. An
    # additive ``created_at`` keeps fire time for cooldown and ordering.
    conn.execute(
        text("ALTER TABLE alert_events ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ")
    )
    # Set the default before the backfill so a concurrent alert insert after
    # the last batch cannot append a NULL and break SET NOT NULL below.
    conn.execute(
        text("ALTER TABLE alert_events ALTER COLUMN created_at SET DEFAULT NOW()")
    )
    if isinstance(conn, _MigrationRunner):
        # Keyset pagination in ascending primary-key order: one short
        # transaction per 500 rows instead of one large table-wide UPDATE.
        conn.batch_update_ids(
            "SELECT id FROM alert_events "
            "WHERE created_at IS NULL AND id > :last_id "
            "ORDER BY id ASC LIMIT :limit",
            text(
                "UPDATE alert_events SET created_at = COALESCE(sent_at, NOW()) "
                "WHERE id IN :ids AND created_at IS NULL"
            ).bindparams(bindparam("ids", expanding=True)),
        )
    else:  # pragma: no cover - only Postgres uses this path
        conn.execute(
            text(
                "UPDATE alert_events SET created_at = COALESCE(sent_at, NOW()) "
                "WHERE created_at IS NULL"
            )
        )
    conn.execute(
        text("ALTER TABLE alert_events ALTER COLUMN created_at SET NOT NULL")
    )
    conn.execute(
        text("ALTER TABLE alert_events ALTER COLUMN sent_at DROP NOT NULL")
    )
    # A legacy server default would stamp queued notices as delivered. The ORM
    # always writes sent_at explicitly (NULL until sent); drop the old default.
    conn.execute(
        text("ALTER TABLE alert_events ALTER COLUMN sent_at DROP DEFAULT")
    )
    # p9e: additive, nullable, no default and deliberately no backfill. Rows
    # written before p9e (and data-source notices) stay NULL; readers fall back
    # to the legacy negative-hf heuristic for them.
    conn.execute(
        text("ALTER TABLE alert_events ADD COLUMN IF NOT EXISTS event_kind VARCHAR(32)")
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_alert_events_user_created "
            "ON alert_events (user_id, created_at)"
        )
    )
    conn.execute(
        text("CREATE INDEX IF NOT EXISTS ix_alert_events_user_id ON alert_events (user_id)")
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_alert_events_user_sent "
            "ON alert_events (user_id, sent_at)"
        )
    )


def _migrate_asset_overrides_postgres(conn) -> None:
    """Idempotent table for phase-5 manual classification overrides."""
    if (
        isinstance(conn, _MigrationRunner)
        and conn.has_table("asset_overrides")
        and conn.has_index("asset_overrides", "ix_asset_overrides_user_id")
    ):
        return
    conn.execute(
        text(
            "CREATE TABLE IF NOT EXISTS asset_overrides ("
            "id SERIAL PRIMARY KEY, "
            "user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
            "key VARCHAR(255) NOT NULL, "
            "asset_class VARCHAR(16), "
            "risk_level VARCHAR(16), "
            "reason VARCHAR(255), "
            "updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), "
            "CONSTRAINT uq_asset_overrides_user_key UNIQUE (user_id, key), "
            "CONSTRAINT ck_asset_overrides_class CHECK ("
            "asset_class IS NULL OR asset_class IN "
            "('cash', 'crypto', 'stock', 'defi', 'option', 'other')), "
            "CONSTRAINT ck_asset_overrides_risk CHECK ("
            "risk_level IS NULL OR risk_level IN ('low', 'medium', 'high'))"
            ")"
        )
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_asset_overrides_user_id "
            "ON asset_overrides (user_id)"
        )
    )


def _migrate_sync_reconcile_postgres(conn) -> None:
    """Phase-6 read-only reconcile log. Additive; CREATE IF NOT EXISTS is idempotent."""
    if (
        isinstance(conn, _MigrationRunner)
        and conn.has_table("sync_reconcile_log")
        and conn.has_index("sync_reconcile_log", "ix_sync_reconcile_user_id")
        and conn.has_index("sync_reconcile_log", "ix_sync_reconcile_user_ts")
    ):
        return
    conn.execute(
        text(
            "CREATE TABLE IF NOT EXISTS sync_reconcile_log ("
            "id SERIAL PRIMARY KEY, "
            "user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
            "sync_run_id INTEGER REFERENCES sync_runs(id) ON DELETE SET NULL, "
            "snapshot_id INTEGER REFERENCES snapshots(id) ON DELETE SET NULL, "
            "ts TIMESTAMPTZ NOT NULL, "
            "nav_usdt NUMERIC(18, 2) NOT NULL, "
            "flat_total_usdt NUMERIC(18, 2) NOT NULL, "
            "covered_total_usdt NUMERIC(18, 2) NOT NULL, "
            "positions_net_usdt NUMERIC(18, 2) NOT NULL, "
            "residual_usdt NUMERIC(18, 2) NOT NULL, "
            "tolerance_usdt NUMERIC(18, 2) NOT NULL, "
            "status VARCHAR(8) NOT NULL, "
            "detail JSONB, "
            "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), "
            "CONSTRAINT ck_sync_reconcile_status CHECK (status IN ('ok', 'warn', 'fail'))"
            ")"
        )
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_sync_reconcile_user_id "
            "ON sync_reconcile_log (user_id)"
        )
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_sync_reconcile_user_ts "
            "ON sync_reconcile_log (user_id, ts)"
        )
    )


def _migrate_sync_reconcile_sqlite(conn, inspector) -> None:
    if inspector.has_table("sync_reconcile_log"):
        return
    conn.execute(
        text(
            "CREATE TABLE sync_reconcile_log ("
            "id INTEGER PRIMARY KEY, "
            "user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
            "sync_run_id INTEGER REFERENCES sync_runs(id) ON DELETE SET NULL, "
            "snapshot_id INTEGER REFERENCES snapshots(id) ON DELETE SET NULL, "
            "ts TIMESTAMP NOT NULL, "
            "nav_usdt NUMERIC(18, 2) NOT NULL, "
            "flat_total_usdt NUMERIC(18, 2) NOT NULL, "
            "covered_total_usdt NUMERIC(18, 2) NOT NULL, "
            "positions_net_usdt NUMERIC(18, 2) NOT NULL, "
            "residual_usdt NUMERIC(18, 2) NOT NULL, "
            "tolerance_usdt NUMERIC(18, 2) NOT NULL, "
            "status VARCHAR(8) NOT NULL, "
            "detail JSON, "
            "created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "CONSTRAINT ck_sync_reconcile_status CHECK (status IN ('ok', 'warn', 'fail'))"
            ")"
        )
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_sync_reconcile_user_id "
            "ON sync_reconcile_log (user_id)"
        )
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_sync_reconcile_user_ts "
            "ON sync_reconcile_log (user_id, ts)"
        )
    )


def _migrate_snapshots_postgres(conn) -> None:
    # Old seed rows have no source column. Adding it with a default of 'seed'
    # backfills them without a second UPDATE.
    for name, ddl, default in SNAPSHOT_COLUMNS:
        if default is None:
            conn.ensure_column("snapshots", name, ddl)
        else:
            conn.ensure_column("snapshots", name, f"{ddl} NOT NULL DEFAULT {default}")
    conn.ensure_check(
        "snapshots",
        "ck_snapshots_source",
        "source IN ('seed', 'sync', 'backfill')",
    )


ADMIN_RETURNS_START = "2026-10-23"

P18_DEMO_USERNAME = "demo"


def _disable_demo_login(conn) -> None:
    """P18: keep the demo row and its data, but close its login for good.

    The first run flips ``is_active`` and bumps ``token_version`` so tokens
    issued before this deploy are rejected. The predicate makes later runs a
    genuine no-op: a disabled demo row is not touched again.
    """
    conn.execute(
        text(
            "UPDATE users "
            "SET is_active = FALSE, token_version = token_version + 1 "
            "WHERE lower(username) = :demo AND is_active = TRUE"
        ),
        {"demo": P18_DEMO_USERNAME},
    )


def _migrate_user_auth_postgres(mig: _MigrationRunner) -> None:
    """P18: additive account-state columns plus the one-time demo shutdown."""
    mig.ensure_column("users", "is_active", "BOOLEAN NOT NULL DEFAULT TRUE")
    mig.ensure_column("users", "token_version", "INTEGER NOT NULL DEFAULT 0")
    _disable_demo_login(mig)


def _seed_returns_start(conn) -> None:
    """admin starts counting returns on 2026-10-23. Never overwrites a value; demo stays NULL."""
    conn.execute(
        text(
            "UPDATE users SET returns_start_date = :d "
            "WHERE username = 'admin' AND returns_start_date IS NULL"
        ),
        {"d": ADMIN_RETURNS_START},
    )


def _migrate_sqlite(engine: Engine) -> None:
    """SQLite tests create the full schema via create_all. Only patch older files."""
    inspector = inspect(engine)
    with engine.begin() as conn:
        if inspector.has_table("users"):
            user_cols = {col["name"] for col in inspector.get_columns("users")}
            if "returns_start_date" not in user_cols:
                conn.execute(text("ALTER TABLE users ADD COLUMN returns_start_date DATE"))
            # P18 additive auth columns; old file DBs default to active/v0.
            if "is_active" not in user_cols:
                conn.execute(
                    text("ALTER TABLE users ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT TRUE")
                )
            if "token_version" not in user_cols:
                conn.execute(
                    text("ALTER TABLE users ADD COLUMN token_version INTEGER NOT NULL DEFAULT 0")
                )
            _disable_demo_login(conn)
        if inspector.has_table("pending_confirmations"):
            existing = {col["name"] for col in inspector.get_columns("pending_confirmations")}
            for name, ddl in PENDING_COLUMNS:
                if name not in existing:
                    # SQLite accepts TIMESTAMP / NUMERIC as affinities.
                    conn.execute(text(f"ALTER TABLE pending_confirmations ADD COLUMN {name} {ddl}"))
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_pending_user_source_external "
                    "ON pending_confirmations (user_id, source, external_id)"
                )
            )
        if inspector.has_table("snapshots"):
            existing_snap = {col["name"] for col in inspector.get_columns("snapshots")}
            for name, ddl, default in SNAPSHOT_COLUMNS:
                if name in existing_snap:
                    continue
                if default is None:
                    conn.execute(text(f"ALTER TABLE snapshots ADD COLUMN {name} {ddl}"))
                else:
                    conn.execute(
                        text(
                            f"ALTER TABLE snapshots ADD COLUMN {name} {ddl} "
                            f"NOT NULL DEFAULT {default}"
                        )
                    )
        if not inspector.has_table("user_wallets"):
            # create_all covers fresh SQLite test databases. This covers a file DB
            # created before user_wallets existed.
            conn.execute(
                text(
                    "CREATE TABLE user_wallets ("
                    "id INTEGER PRIMARY KEY, "
                    "user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
                    "chain VARCHAR(16) NOT NULL, "
                    "address VARCHAR(128) NOT NULL, "
                    "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
                    "CONSTRAINT uq_user_wallet UNIQUE (user_id, chain, address)"
                    ")"
                )
            )
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_user_wallets_user_id ON user_wallets (user_id)"))
        if inspector.has_table("wechat_push_queue"):
            queue_cols = {col["name"] for col in inspector.get_columns("wechat_push_queue")}
            if "next_attempt_at" not in queue_cols:
                conn.execute(
                    text("ALTER TABLE wechat_push_queue ADD COLUMN next_attempt_at TIMESTAMP")
                )
            if "alert_event_id" in queue_cols:
                _ensure_wechat_alert_queue_identity(conn)
        if not inspector.has_table("address_labels"):
            conn.execute(
                text(
                    "CREATE TABLE address_labels ("
                    "chain VARCHAR(32) NOT NULL, "
                    "address VARCHAR(128) NOT NULL, "
                    "label VARCHAR(255) NOT NULL, "
                    "kind VARCHAR(16) NOT NULL, "
                    "source VARCHAR(64) NOT NULL, "
                    "is_scam BOOLEAN NOT NULL DEFAULT 0, "
                    "meta JSON, "
                    "fetched_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                    "expires_at TIMESTAMP, "
                    "PRIMARY KEY (chain, address), "
                    "CONSTRAINT ck_address_labels_kind CHECK "
                    "(kind IN ('contract', 'eoa', 'token', 'zero', 'unknown'))"
                    ")"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_address_labels_expires_at "
                    "ON address_labels (expires_at)"
                )
            )
        _migrate_positions_sqlite(conn, inspector)
        _migrate_asset_overrides_sqlite(conn, inspector)
        _migrate_sync_reconcile_sqlite(conn, inspector)


def _migrate_asset_overrides_sqlite(conn, inspector) -> None:
    if inspector.has_table("asset_overrides"):
        return
    conn.execute(
        text(
            "CREATE TABLE asset_overrides ("
            "id INTEGER PRIMARY KEY, "
            "user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
            "key VARCHAR(255) NOT NULL, "
            "asset_class VARCHAR(16), "
            "risk_level VARCHAR(16), "
            "reason VARCHAR(255), "
            "updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "CONSTRAINT uq_asset_overrides_user_key UNIQUE (user_id, key), "
            "CONSTRAINT ck_asset_overrides_class CHECK ("
            "asset_class IS NULL OR asset_class IN "
            "('cash', 'crypto', 'stock', 'defi', 'option', 'other')), "
            "CONSTRAINT ck_asset_overrides_risk CHECK ("
            "risk_level IS NULL OR risk_level IN ('low', 'medium', 'high'))"
            ")"
        )
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_asset_overrides_user_id "
            "ON asset_overrides (user_id)"
        )
    )


def _migrate_positions_sqlite(conn, inspector) -> None:
    """File DBs created before phase-4. Fresh test DBs already have the tables."""
    if not inspector.has_table("positions"):
        conn.execute(
            text(
                "CREATE TABLE positions ("
                "id INTEGER PRIMARY KEY, "
                "user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
                "kind VARCHAR(16) NOT NULL, "
                "protocol VARCHAR(16) NOT NULL, "
                "chain VARCHAR(32), "
                "account_ref VARCHAR(128) NOT NULL, "
                "market_ref VARCHAR(255) NOT NULL, "
                "label VARCHAR(160), "
                "status VARCHAR(16) NOT NULL DEFAULT 'open', "
                "net_value_usdt NUMERIC(38, 8) NOT NULL DEFAULT 0, "
                "collateral_usdt NUMERIC(38, 8) NOT NULL DEFAULT 0, "
                "debt_usdt NUMERIC(38, 8) NOT NULL DEFAULT 0, "
                "details JSON, "
                "opened_at TIMESTAMP, "
                "asof TIMESTAMP, "
                "sync_id INTEGER REFERENCES sync_runs(id) ON DELETE SET NULL, "
                "close_reason VARCHAR(32), "
                "provenance VARCHAR(16), "
                "live BOOLEAN, "
                "CONSTRAINT uq_positions_user_protocol_account_market "
                "UNIQUE (user_id, protocol, account_ref, market_ref), "
                "CONSTRAINT ck_positions_kind CHECK (kind IN ('option', 'lending')), "
                "CONSTRAINT ck_positions_protocol CHECK ("
                "protocol IN ('deribit', 'binance', 'aave', 'morpho', 'pendle', 'kamino', 'jupiter')"
                "), "
                "CONSTRAINT ck_positions_status CHECK (status IN ('open', 'closed'))"
                ")"
            )
        )
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_positions_user_id ON positions (user_id)"))
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_positions_user_kind ON positions (user_id, kind)")
        )
    else:
        # Additive P8d columns; old rows keep NULL. SQLite has no
        # ADD COLUMN IF NOT EXISTS, so inspect first.
        columns = {col["name"] for col in inspector.get_columns("positions")}
        if "sync_id" not in columns:
            conn.execute(
                text(
                    "ALTER TABLE positions ADD COLUMN sync_id INTEGER "
                    "REFERENCES sync_runs(id) ON DELETE SET NULL"
                )
            )
        if "close_reason" not in columns:
            conn.execute(text("ALTER TABLE positions ADD COLUMN close_reason VARCHAR(32)"))
        if "provenance" not in columns:
            conn.execute(text("ALTER TABLE positions ADD COLUMN provenance VARCHAR(16)"))
        if "live" not in columns:
            conn.execute(text("ALTER TABLE positions ADD COLUMN live BOOLEAN"))
    if not inspector.has_table("position_snapshots"):
        conn.execute(
            text(
                "CREATE TABLE position_snapshots ("
                "id INTEGER PRIMARY KEY, "
                "position_id INTEGER NOT NULL REFERENCES positions(id) ON DELETE CASCADE, "
                "ts TIMESTAMP NOT NULL, "
                "net_value_usdt NUMERIC(38, 8) NOT NULL, "
                "collateral_usdt NUMERIC(38, 8) NOT NULL DEFAULT 0, "
                "debt_usdt NUMERIC(38, 8) NOT NULL DEFAULT 0, "
                "health_factor NUMERIC(38, 8), "
                "apy_net NUMERIC(38, 8), "
                "details JSON, "
                "sync_id INTEGER REFERENCES sync_runs(id) ON DELETE SET NULL, "
                "provenance VARCHAR(16), "
                "live BOOLEAN"
                ")"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_position_snapshots_position_id "
                "ON position_snapshots (position_id)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_position_snapshots_position_ts "
                "ON position_snapshots (position_id, ts)"
            )
        )
    else:
        snapshot_columns = {
            col["name"] for col in inspector.get_columns("position_snapshots")
        }
        if "sync_id" not in snapshot_columns:
            conn.execute(
                text(
                    "ALTER TABLE position_snapshots ADD COLUMN sync_id INTEGER "
                    "REFERENCES sync_runs(id) ON DELETE SET NULL"
                )
            )
        if "provenance" not in snapshot_columns:
            conn.execute(
                text("ALTER TABLE position_snapshots ADD COLUMN provenance VARCHAR(16)")
            )
        if "live" not in snapshot_columns:
            conn.execute(text("ALTER TABLE position_snapshots ADD COLUMN live BOOLEAN"))
    if not inspector.has_table("alert_rules"):
        conn.execute(
            text(
                "CREATE TABLE alert_rules ("
                "id INTEGER PRIMARY KEY, "
                "user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
                "protocol VARCHAR(16), "
                "warn_hf NUMERIC(38, 8) NOT NULL DEFAULT 1.5, "
                "danger_hf NUMERIC(38, 8) NOT NULL DEFAULT 1.2, "
                "enabled BOOLEAN NOT NULL DEFAULT 1, "
                "cooldown_min INTEGER NOT NULL DEFAULT 360, "
                "CONSTRAINT uq_alert_rules_user_protocol UNIQUE (user_id, protocol)"
                ")"
            )
        )
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_alert_rules_user_id ON alert_rules (user_id)")
        )
    if not inspector.has_table("alert_events"):
        conn.execute(
            text(
                "CREATE TABLE alert_events ("
                "id INTEGER PRIMARY KEY, "
                "user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
                "rule_id INTEGER REFERENCES alert_rules(id) ON DELETE SET NULL, "
                "position_id INTEGER REFERENCES positions(id) ON DELETE SET NULL, "
                "level VARCHAR(16) NOT NULL, "
                "hf NUMERIC(38, 8), "
                "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
                "sent_at TIMESTAMP, "
                "channel_result VARCHAR(255), "
                "CONSTRAINT ck_alert_events_level CHECK (level IN ('warn', 'danger', 'recover'))"
                ")"
            )
        )
    else:
        _migrate_alert_events_sqlite(conn, inspector)
    conn.execute(
        text("CREATE INDEX IF NOT EXISTS ix_alert_events_user_id ON alert_events (user_id)")
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_alert_events_user_sent "
            "ON alert_events (user_id, sent_at)"
        )
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_alert_events_user_created "
            "ON alert_events (user_id, created_at)"
        )
    )


def _migrate_alert_events_sqlite(conn, inspector) -> None:
    """Old file DBs: add created_at and relax sent_at to nullable.

    SQLite cannot drop NOT NULL in place, so the one case that needs it rebuilds
    the table and copies every row (ids and timestamps preserved).
    """
    cols = {col["name"]: col for col in inspector.get_columns("alert_events")}
    if "created_at" not in cols:
        conn.execute(text("ALTER TABLE alert_events ADD COLUMN created_at TIMESTAMP"))
        conn.execute(
            text(
                "UPDATE alert_events SET created_at = "
                "COALESCE(sent_at, CURRENT_TIMESTAMP) WHERE created_at IS NULL"
            )
        )
    if cols.get("sent_at", {}).get("nullable") is not False:
        return
    conn.execute(
        text(
            "CREATE TABLE alert_events_p7a ("
            "id INTEGER PRIMARY KEY, "
            "user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
            "rule_id INTEGER REFERENCES alert_rules(id) ON DELETE SET NULL, "
            "position_id INTEGER REFERENCES positions(id) ON DELETE SET NULL, "
            "level VARCHAR(16) NOT NULL, "
            "hf NUMERIC(38, 8), "
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
            "sent_at TIMESTAMP, "
            "channel_result VARCHAR(255), "
            "CONSTRAINT ck_alert_events_level CHECK (level IN ('warn', 'danger', 'recover'))"
            ")"
        )
    )
    conn.execute(
        text(
            "INSERT INTO alert_events_p7a "
            "(id, user_id, rule_id, position_id, level, hf, created_at, sent_at, channel_result) "
            "SELECT id, user_id, rule_id, position_id, level, hf, "
            "COALESCE(created_at, sent_at, CURRENT_TIMESTAMP), sent_at, channel_result "
            "FROM alert_events"
        )
    )
    conn.execute(text("DROP TABLE alert_events"))
    conn.execute(text("ALTER TABLE alert_events_p7a RENAME TO alert_events"))


def seed_wallet_owner(db: Session) -> None:
    """把 env 地址幂等挂到 WALLET_OWNER_USERNAME（默认 admin）。不覆盖已有归属。"""
    from .config import settings
    from .models import User, UserWallet

    username = (settings.wallet_owner_username or "admin").strip() or "admin"
    owner = db.query(User).filter(User.username == username).one_or_none()
    if owner is None:
        return
    rows = [("evm", address) for address in settings.evm_addresses]
    rows += [("solana", address) for address in settings.solana_addresses]
    for chain, address in rows:
        address = (address or "").strip()
        if not address:
            continue
        if chain == "evm":
            address = address.lower()
        exists = (
            db.query(UserWallet)
            .filter(
                UserWallet.user_id == owner.id,
                UserWallet.chain == chain,
                UserWallet.address == address,
            )
            .one_or_none()
        )
        if exists is None and chain == "evm":
            # 兼容历史大小写重复：同址不同大小写视为已存在
            for row in (
                db.query(UserWallet)
                .filter(UserWallet.user_id == owner.id, UserWallet.chain == chain)
                .all()
            ):
                if (row.address or "").lower() == address:
                    exists = row
                    break
        if exists is None:
            db.add(UserWallet(user_id=owner.id, chain=chain, address=address))
    db.commit()
