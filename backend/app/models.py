from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base

LEDGER_KINDS = (
    "deposit",
    "withdraw",
    "trade",
    "funding",
    "other",
    "airdrop",
    "kol_reward",
    "transfer",
    # Phase-4: a same-tx asset swap inside a protocol is not an external flow.
    "defi_deposit",
    "defi_redeem",
    "swap",
)
PENDING_STATUSES = ("pending", "confirmed", "rejected")
EXTERNAL_FLOW_KINDS = ("deposit", "withdraw", "transfer")
INCOME_KINDS = ("airdrop", "kol_reward")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    # P18: disabled accounts cannot log in or use an already-issued token.
    # The built-in demo account is disabled by migration; its data is kept.
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    # P18: embedded in every JWT. Bumping it invalidates tokens issued before
    # the bump without touching any other user row.
    token_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # Phase-3·7: returns/TWR ignore snapshots before this date (Asia/Shanghai). NULL = whole history.
    returns_start_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    wallets: Mapped[list[UserWallet]] = relationship(back_populates="user")

    @property
    def is_demo(self) -> bool:
        """The built-in demo account is identified by username, never by id."""
        return bool(self.username and self.username.strip().lower() == "demo")


class UserWallet(Base):
    """链上地址按用户归属。没有钱包的用户（如 demo）不得读取他人地址。"""

    __tablename__ = "user_wallets"
    __table_args__ = (UniqueConstraint("user_id", "chain", "address", name="uq_user_wallet"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chain: Mapped[str] = mapped_column(String(16), nullable=False)  # evm | solana
    address: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    user: Mapped[User] = relationship(back_populates="wallets")


class ApiCredential(Base):
    __tablename__ = "api_credentials"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    exchange_name: Mapped[str] = mapped_column(String(64), nullable=False)
    encrypted_payload: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"
    __table_args__ = (
        CheckConstraint(
            "kind IN (" + ", ".join(f"'{kind}'" for kind in LEDGER_KINDS) + ")",
            name="ck_ledger_entries_kind",
        ),
        Index("ix_ledger_entries_user_ts", "user_id", "ts"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    amount_usdt: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)


SNAPSHOT_SOURCES = ("seed", "sync", "backfill")


class Snapshot(Base):
    __tablename__ = "snapshots"
    __table_args__ = (
        UniqueConstraint("user_id", "ts", name="uq_snapshots_user_ts"),
        Index("ix_snapshots_user_ts", "user_id", "ts"),
        CheckConstraint(
            "source IN ('seed', 'sync', 'backfill')",
            name="ck_snapshots_source",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    nav_usdt: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    # Phase-3: seed rows are the phase-1 demo curve. sync/backfill are real NAV.
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="seed", server_default="seed"
    )
    # P13: independent data provenance (demo / fixture / live). NULL on rows
    # written before this column existed; reads fall back to ``source`` and
    # nothing backfills history.
    provenance: Mapped[str | None] = mapped_column(String(16), nullable=True)
    live: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # JSON text: per-source asset amounts and USDT values. Optional.
    breakdown: Mapped[str | None] = mapped_column(Text, nullable=True)


class SyncRun(Base):
    """One scheduler / manual / backfill execution. Status lives here, not in logs."""

    __tablename__ = "sync_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'ok', 'partial', 'error')",
            name="ck_sync_runs_status",
        ),
        CheckConstraint(
            "trigger IN ('manual', 'scheduled', 'backfill')",
            name="ck_sync_runs_trigger",
        ),
        Index("ix_sync_runs_finished", "finished_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    trigger: Mapped[str] = mapped_column(String(16), nullable=False, default="manual")
    user_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)


class SyncReconcileLog(Base):
    """Read-only result of the NAV identity check after one sync snapshot.

    Written by the sync lane; never used to mutate the ledger, snapshots or
    positions. ``sync_run_id`` is nullable because the caller's ``sync_runs``
    row is persisted after the sync action returns; ``snapshot_id``/``ts``
    identify the checked snapshot instead.
    """

    __tablename__ = "sync_reconcile_log"
    __table_args__ = (
        CheckConstraint(
            "status IN ('ok', 'warn', 'fail')",
            name="ck_sync_reconcile_status",
        ),
        Index("ix_sync_reconcile_user_id", "user_id"),
        Index("ix_sync_reconcile_user_ts", "user_id", "ts"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    sync_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("sync_runs.id", ondelete="SET NULL"), nullable=True
    )
    snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("snapshots.id", ondelete="SET NULL"), nullable=True
    )
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    nav_usdt: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    flat_total_usdt: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    covered_total_usdt: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    positions_net_usdt: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    residual_usdt: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    tolerance_usdt: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    status: Mapped[str] = mapped_column(String(8), nullable=False)
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PendingConfirmation(Base):
    __tablename__ = "pending_confirmations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'confirmed', 'rejected')",
            name="ck_pending_confirmations_status",
        ),
        Index("ix_pending_confirmations_user_created", "user_id", "created_at"),
        # NULL source/external_id (phase-1 rows) do not collide: Postgres and
        # SQLite both treat NULLs as distinct in unique indexes.
        Index(
            "uq_pending_user_source_external",
            "user_id",
            "source",
            "external_id",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    detail: Mapped[str] = mapped_column(String(1000), nullable=False)
    amount_usdt: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Phase-2 attribution columns. Nullable so phase-1 rows stay valid.
    source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    direction: Mapped[str | None] = mapped_column(String(8), nullable=True)
    asset: Mapped[str | None] = mapped_column(String(32), nullable=True)
    amount_raw: Mapped[Decimal | None] = mapped_column(Numeric(36, 18), nullable=True)
    suggested_category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    suggested_confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3), nullable=True)
    suggested_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    final_category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ledger_entry_id: Mapped[int | None] = mapped_column(
        ForeignKey("ledger_entries.id", ondelete="SET NULL"), nullable=True
    )
    # Phase-4 Blockscout enrichment. Nullable: historical rows stay valid.
    # JSON keeps structured token legs; ``detail`` stays human-readable text.
    meta: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)

    @property
    def meta_data(self) -> dict:
        value = self.meta
        if isinstance(value, dict):
            return value
        if isinstance(value, str) and value.strip():
            try:
                parsed = json.loads(value)
                return parsed if isinstance(parsed, dict) else {}
            except (TypeError, ValueError):
                return {}
        return {}

    @property
    def counterparty_label(self) -> str | None:
        value = self.meta_data.get("counterparty_label")
        return str(value) if value else None

    @property
    def counterparty_kind(self) -> str | None:
        value = self.meta_data.get("counterparty_kind")
        return str(value) if value else None

    @property
    def counterparty_label_source(self) -> str | None:
        value = self.meta_data.get("counterparty_label_source")
        return str(value) if value else None

    @property
    def counterparty_chain(self) -> str | None:
        value = self.meta_data.get("chain")
        return str(value) if value else None

    @property
    def counterparty_address(self) -> str | None:
        value = self.meta_data.get("counterparty_address")
        return str(value) if value else None

    @property
    def protocol_deposit(self) -> bool:
        return bool(self.meta_data.get("protocol_deposit"))

    @property
    def protocol_deposit_asset(self) -> str | None:
        value = self.meta_data.get("protocol_deposit_asset")
        return str(value) if value else None

    @property
    def explain(self) -> str | None:
        value = self.meta_data.get("explain")
        return str(value) if value else None

    @property
    def received_tokens(self) -> list[dict]:
        value = self.meta_data.get("received_tokens")
        return value if isinstance(value, list) else []


ADDRESS_LABEL_KINDS = ("contract", "eoa", "token", "zero", "unknown")


class AddressLabel(Base):
    """Per-chain counterparty label cache.

    ``(chain, address)`` is the identity: the same 0x address can be a
    different actor on another chain, so it must never be shared across
    chains.  ``source='manual'`` is a human override and never expires;
    every other row carries an ``expires_at`` so a stale public lookup is
    refreshed by the background job.
    """

    __tablename__ = "address_labels"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('contract', 'eoa', 'token', 'zero', 'unknown')",
            name="ck_address_labels_kind",
        ),
        Index("ix_address_labels_expires_at", "expires_at"),
    )

    chain: Mapped[str] = mapped_column(String(32), primary_key=True)
    address: Mapped[str] = mapped_column(String(128), primary_key=True)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    is_scam: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    meta: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


WECHAT_QUEUE_STATUSES = ("queued", "sent", "failed")


class WechatPushQueue(Base):
    """Outbound ClawBot messages. The bot token itself is never stored here."""

    __tablename__ = "wechat_push_queue"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'sent', 'failed')",
            name="ck_wechat_push_queue_status",
        ),
        Index("ix_wechat_push_queue_status_user", "status", "user_id"),
        # One *active* retry row per alert. Historical failed rows (the six
        # pre-P7a pushes) are deliberately outside the predicate: they are
        # terminal and must never block or be reset by a later enqueue.
        Index(
            "uq_wechat_push_queue_alert_event_active",
            "alert_event_id",
            unique=True,
            sqlite_where=text("alert_event_id IS NOT NULL AND status <> 'failed'"),
            postgresql_where=text("alert_event_id IS NOT NULL AND status <> 'failed'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # A row is either a pending-confirmation notice or an alert retry. One of
    # the two references is set; neither is required.
    pending_id: Mapped[int | None] = mapped_column(
        ForeignKey("pending_confirmations.id", ondelete="CASCADE"), nullable=True, index=True
    )
    alert_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("alert_events.id", ondelete="CASCADE"), nullable=True, index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued", server_default="queued")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # P7a: exponential backoff gate. NULL means "due now". Set on a failed or
    # token-blocked attempt and cleared on success or after a token refresh.
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class WechatContextToken(Base):
    """Server-only cache of the latest inbound context_token. Never log the value."""

    __tablename__ = "wechat_context_tokens"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    context_token: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class WechatPollState(Base):
    """Single-row cursor store. key='get_updates_buf' holds the long-poll offset."""

    __tablename__ = "wechat_poll_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")


# JSON on SQLite (tests), JSONB on Postgres. Spec field name is `details`.
DetailsJSON = JSON().with_variant(JSONB, "postgresql")

POSITION_KINDS = ("option", "lending")
POSITION_PROTOCOLS = (
    "deribit",
    "binance",
    "aave",
    "morpho",
    "pendle",
    "kamino",
    "jupiter",
)
POSITION_STATUSES = ("open", "closed")
ALERT_LEVELS = ("warn", "danger", "recover")


class Position(Base):
    """One option or lending position. Net value is mark (collateral − debt for lending)."""

    __tablename__ = "positions"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "protocol",
            "account_ref",
            "market_ref",
            name="uq_positions_user_protocol_account_market",
        ),
        CheckConstraint(
            "kind IN ('option', 'lending')",
            name="ck_positions_kind",
        ),
        CheckConstraint(
            "protocol IN ('deribit', 'binance', 'aave', 'morpho', 'pendle', 'kamino', 'jupiter')",
            name="ck_positions_protocol",
        ),
        CheckConstraint(
            "status IN ('open', 'closed')",
            name="ck_positions_status",
        ),
        Index("ix_positions_user_kind", "user_id", "kind"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    protocol: Mapped[str] = mapped_column(String(16), nullable=False)
    chain: Mapped[str | None] = mapped_column(String(32), nullable=True)
    account_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    market_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    label: Mapped[str | None] = mapped_column(String(160), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="open", server_default="open"
    )
    net_value_usdt: Mapped[Decimal] = mapped_column(Numeric(38, 8), nullable=False, default=0)
    collateral_usdt: Mapped[Decimal] = mapped_column(Numeric(38, 8), nullable=False, default=0)
    debt_usdt: Mapped[Decimal] = mapped_column(Numeric(38, 8), nullable=False, default=0)
    details: Mapped[dict | None] = mapped_column(DetailsJSON, nullable=True)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    asof: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sync_id: Mapped[int | None] = mapped_column(
        ForeignKey("sync_runs.id", ondelete="SET NULL"), nullable=True
    )
    # Set only when a row is closed because its chain lost every wallet.
    # Normal closes (protocol read cleanly, position gone) leave this NULL.
    close_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # P13: independent data provenance (demo / fixture / live) and its live
    # flag. Nullable for rows written before the phase; no historical backfill.
    provenance: Mapped[str | None] = mapped_column(String(16), nullable=True)
    live: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    snapshots: Mapped[list[PositionSnapshot]] = relationship(
        back_populates="position", cascade="all, delete-orphan"
    )


class PositionSnapshot(Base):
    """Point-in-time row used later for per-position PnL and APR. P0 only writes it."""

    __tablename__ = "position_snapshots"
    __table_args__ = (Index("ix_position_snapshots_position_ts", "position_id", "ts"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    position_id: Mapped[int] = mapped_column(
        ForeignKey("positions.id", ondelete="CASCADE"), nullable=False
    )
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    net_value_usdt: Mapped[Decimal] = mapped_column(Numeric(38, 8), nullable=False)
    collateral_usdt: Mapped[Decimal] = mapped_column(Numeric(38, 8), nullable=False, default=0)
    debt_usdt: Mapped[Decimal] = mapped_column(Numeric(38, 8), nullable=False, default=0)
    health_factor: Mapped[Decimal | None] = mapped_column(Numeric(38, 8), nullable=True)
    apy_net: Mapped[Decimal | None] = mapped_column(Numeric(38, 8), nullable=True)
    details: Mapped[dict | None] = mapped_column(DetailsJSON, nullable=True)
    # Same sync run that wrote the parent position. NULL for historical rows.
    sync_id: Mapped[int | None] = mapped_column(
        ForeignKey("sync_runs.id", ondelete="SET NULL"), nullable=True
    )
    # P13: copied from the position on every write. Nullable for old rows.
    provenance: Mapped[str | None] = mapped_column(String(16), nullable=True)
    live: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    position: Mapped[Position] = relationship(back_populates="snapshots")


ASSET_CLASSES = ("cash", "crypto", "stock", "defi", "option", "other")
RISK_LEVELS = ("low", "medium", "high")


class AssetOverride(Base):
    """User-owned classification override. Sync jobs must never write this table.

    ``key`` is the stable key produced by ``portfolio_class.holding_key`` or
    ``position_key``. Both enum columns are nullable so a user may override only
    the class or only the risk; the missing field falls back to the default rule.
    """

    __tablename__ = "asset_overrides"
    __table_args__ = (
        UniqueConstraint("user_id", "key", name="uq_asset_overrides_user_key"),
        CheckConstraint(
            "asset_class IS NULL OR asset_class IN "
            "('cash', 'crypto', 'stock', 'defi', 'option', 'other')",
            name="ck_asset_overrides_class",
        ),
        CheckConstraint(
            "risk_level IS NULL OR risk_level IN ('low', 'medium', 'high')",
            name="ck_asset_overrides_risk",
        ),
        Index("ix_asset_overrides_user_id", "user_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    asset_class: Mapped[str | None] = mapped_column(String(16), nullable=True)
    risk_level: Mapped[str | None] = mapped_column(String(16), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        server_default=func.now(),
    )


class AlertRule(Base):
    """Health-factor thresholds. protocol NULL means the rule covers every protocol.

    One row per (user_id, protocol) including the all-protocols row (protocol NULL).
    SQLite and Postgres both treat NULLs as distinct in a unique constraint, so the
    GET path upserts the default row instead of relying on the constraint alone.
    """

    __tablename__ = "alert_rules"
    __table_args__ = (
        UniqueConstraint("user_id", "protocol", name="uq_alert_rules_user_protocol"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    protocol: Mapped[str | None] = mapped_column(String(16), nullable=True)
    warn_hf: Mapped[Decimal] = mapped_column(
        Numeric(38, 8), nullable=False, default=Decimal("1.5"), server_default="1.5"
    )
    danger_hf: Mapped[Decimal] = mapped_column(
        Numeric(38, 8), nullable=False, default=Decimal("1.2"), server_default="1.2"
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    cooldown_min: Mapped[int] = mapped_column(
        Integer, nullable=False, default=360, server_default="360"
    )


class AlertEvent(Base):
    """Record of a health-factor alert. Evaluation and WeChat send belong to lane E."""

    __tablename__ = "alert_events"
    __table_args__ = (
        CheckConstraint(
            "level IN ('warn', 'danger', 'recover')",
            name="ck_alert_events_level",
        ),
        Index("ix_alert_events_user_sent", "user_id", "sent_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    rule_id: Mapped[int | None] = mapped_column(
        ForeignKey("alert_rules.id", ondelete="SET NULL"), nullable=True
    )
    position_id: Mapped[int | None] = mapped_column(
        ForeignKey("positions.id", ondelete="SET NULL"), nullable=True
    )
    level: Mapped[str] = mapped_column(String(16), nullable=False)
    hf: Mapped[Decimal | None] = mapped_column(Numeric(38, 8), nullable=True)
    # p9e: explicit alert kind ("hf" / "liq_distance" / "drawdown"). NULL is
    # kept for rows written before p9e and for data-source notices that have no
    # synthetic metric; readers fall back to the legacy negative-hf inference.
    # A6/A7 still store their negative marker in ``hf`` so pre-p9e code keeps
    # routing new rows correctly.
    event_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # P7a: when the event fired. Cooldown/order use this because ``sent_at``
    # now means "actually delivered" and stays NULL while the notice is queued.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    channel_result: Mapped[str | None] = mapped_column(String(255), nullable=True)
