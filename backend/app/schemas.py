from __future__ import annotations

import re
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

USERNAME_PATTERN = r"^[A-Za-z0-9_.-]+$"


class RegisterIn(BaseModel):
    username: str = Field(min_length=3, max_length=32, pattern=USERNAME_PATTERN)
    password: str = Field(min_length=6, max_length=72)

    @field_validator("username")
    @classmethod
    def clean_username(cls, value: str) -> str:
        value = value.strip()
        if not re.fullmatch(USERNAME_PATTERN, value):
            raise ValueError("用户名只能包含字母、数字、下划线、点或连字符")
        return value


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)

    @field_validator("username")
    @classmethod
    def clean_username(cls, value: str) -> str:
        return value.strip()


class ResetPasswordIn(BaseModel):
    """Admin reset. Same length bounds as RegisterIn.password."""

    password: str = Field(min_length=6, max_length=72)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    is_admin: bool
    is_demo: bool = False
    created_at: datetime


class TokenOut(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    user: UserOut


class SnapshotPoint(BaseModel):
    ts: datetime
    nav_usdt: float
    # P13: per-point data provenance, so a mixed history is not flattened to
    # the badge of the latest point. Null on rows written before P13.
    provenance: str | None = None
    live: bool | None = None
    # P19: read-time flag for a sync point written without the position fold
    # (half snapshot). Never stored.
    partial: bool = False


class MiscSummaryOut(BaseModel):
    """Assets that fold into 杂项 after same-symbol aggregation.

    ``count`` is the number of assets. ``total_usdt`` is the sum of their
    already-priced market value; unpriced dust contributes 0.
    """

    count: int = 0
    total_usdt: float = 0.0


class HoldingOut(BaseModel):
    """Flat row from the latest sync snapshot breakdown.holdings[]."""

    source: str
    asset: str
    amount: str | float
    price_usdt: str | float | None = None
    value_usdt: str | float
    priced: bool = True
    live: bool = True
    price_source: str | None = None
    chain: str | None = None
    contract: str | None = None
    icon_url: str | None = None
    stale: bool | None = None
    # Phase-4 P0: true when an open protocol position already prices this row.
    # None on older payloads that never ran the marker.
    covered_by_position: bool | None = None
    # Phase-5: read-only classification, computed on every request.
    asset_class: Literal["cash", "crypto", "stock", "defi", "option", "other"] | None = None
    risk_level: Literal["low", "medium", "high"] | None = None
    risk_reason: str | None = None
    overridden: bool = False


class OverviewOut(BaseModel):
    current_nav_usdt: float
    currency: Literal["USDT"] = "USDT"
    series: list[SnapshotPoint]
    # Phase-3: which snapshot family produced this NAV. Additive; old clients ignore it.
    # P13 adds demo/fixture/live while keeping the legacy values for old rows.
    nav_source: Literal["sync", "backfill", "seed", "demo", "fixture", "live"] = "seed"
    # P13: latest snapshot provenance and live flag. ``nav_source`` mirrors
    # ``provenance`` for P13 rows; both are exposed for old/new clients.
    provenance: Literal["demo", "fixture", "live"] | None = None
    live: bool | None = None
    latest_snapshot_ts: datetime | None = None
    # Phase-3·7, additive: the user's returns baseline date (YYYY-MM-DD) or null.
    returns_start: date | None = None
    # Phase-3·12: flat holdings from the latest sync snapshot only (no seed/backfill).
    holdings: list[HoldingOut] = []
    holdings_asof: datetime | None = None
    # Phase-4 P0: open positions' net value, already folded into current_nav_usdt.
    # Zero when the user has no open positions, so older NAV stays unchanged.
    positions_net_value_usdt: float = 0.0
    # 杂项: same-symbol aggregate below misc_threshold_usdt, plus unpriced assets.
    # Additive. The total stays inside current_nav_usdt; it is a display fold only.
    misc_threshold_usdt: float = 50.0
    misc: MiscSummaryOut = Field(default_factory=MiscSummaryOut)
    # Phase-6, additive: latest sync reconcile status for this user, or null
    # before the first reconciled sync. Old clients ignore it.
    reconcile_status: Literal["ok", "warn", "fail"] | None = None
    # P7a, additive: WeChat context-token freshness for this user. A missing
    # token is stale with a null age. Read-only; never carries the token.
    wechat_token_stale: bool = False
    wechat_token_age_hours: float | None = None
    # p15b: the W1 send-path alert consumer. Additive; no token or message
    # body is exposed, only the redacted last send error.
    wechat_send_stale: bool = False
    wechat_send_error: str | None = None
    wechat_send_since: datetime | None = None


class ReturnPoint(BaseModel):
    ts: datetime
    return_rate: float


class ReturnsOut(BaseModel):
    period: Literal["day", "week", "month", "year"]
    return_amount_usdt: float
    return_rate: float
    # Additive: the external flow the chain removed, signed (+ = inflow). cash
    # is the ledger (deposit/withdraw/transfer) component; coverage is the NAV
    # step from a source onboarding or position-fold switch. kind lets the UI
    # word the line without calling newly counted assets a deposit.
    excluded_flow_usdt: float = 0.0
    excluded_cash_flow_usdt: float = 0.0
    excluded_coverage_flow_usdt: float = 0.0
    excluded_flow_kind: Literal["none", "cash", "coverage", "mixed"] = "none"
    series: list[ReturnPoint]
    nav_source: Literal["sync", "backfill", "seed", "demo", "fixture", "live"] = "seed"
    provenance: Literal["demo", "fixture", "live"] | None = None
    live: bool | None = None
    latest_snapshot_ts: datetime | None = None
    # Phase-3·7, additive. returns_start: baseline date or null. window_clamped: the
    # requested period reaches before it, so the window starts at effective_start_ts.
    returns_start: date | None = None
    window_clamped: bool = False
    effective_start_ts: datetime | None = None


class BreakdownSegmentOut(BaseModel):
    """One bucket of the p26 segment table (month / ISO week / Shanghai day)."""

    key: str
    label: str
    start: datetime
    end: datetime
    intervals: int = 0
    amount_usdt: float | None = None
    rate: float | None = None
    # "no_snapshot" means the bucket has no snapshot interval at all. A null
    # amount/rate is never rewritten as zero and the bucket is never interpolated.
    missing: Literal["no_snapshot"] | None = None


class BreakdownSegmentsOut(BaseModel):
    month: list[BreakdownSegmentOut] = Field(default_factory=list)
    week: list[BreakdownSegmentOut] = Field(default_factory=list)
    day: list[BreakdownSegmentOut] = Field(default_factory=list)


class BreakdownAssetOut(BaseModel):
    asset: str
    asset_class: str
    asset_class_label: str
    amount_usdt: float
    # Price effect / the asset's first valuation inside the window. Null when
    # that first valuation is absent or zero; the amount still renders.
    rate: float | None = None
    base_value_usdt: float | None = None
    sources: list[str] = Field(default_factory=list)


class BreakdownSumUnitOut(BaseModel):
    unit: Literal["month", "week", "day"]
    sum_usdt: float
    total_usdt: float
    diff_usdt: float
    chain_rate: float
    total_rate: float
    rate_diff: float


class BreakdownSumCheckOut(BaseModel):
    total_usdt: float
    month: BreakdownSumUnitOut | None = None
    week: BreakdownSumUnitOut | None = None
    day: BreakdownSumUnitOut | None = None


class ReturnsBreakdownOut(BaseModel):
    """p26 additive readout: segment table + asset price-effect attribution."""

    period: Literal["day", "week", "month", "year"]
    return_amount_usdt: float
    return_rate: float
    nav0_usdt: float
    window_start: datetime | None = None
    window_end: datetime | None = None
    segments: BreakdownSegmentsOut
    assets: list[BreakdownAssetOut] = Field(default_factory=list)
    assets_missing: bool = False
    assets_missing_reason: str | None = None
    unattributed_usdt: float | None = None
    unattributed_reason: str = ""
    sum_check: BreakdownSumCheckOut
    nav_source: Literal["sync", "backfill", "seed", "demo", "fixture", "live"] = "seed"
    provenance: Literal["demo", "fixture", "live"] | None = None
    live: bool | None = None
    latest_snapshot_ts: datetime | None = None
    returns_start: date | None = None
    window_clamped: bool = False
    effective_start_ts: datetime | None = None


class ReceivedTokenOut(BaseModel):
    """One same-transaction token leg stored in pending meta."""

    symbol: str
    name: str | None = None
    amount: float
    direction: Literal["in", "out"] = "in"
    contract: str | None = None

    @field_validator("amount", mode="before")
    @classmethod
    def _coerce_amount(cls, value: object) -> float:
        return float(value or 0)


class PendingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    detail: str
    amount_usdt: float | None
    status: Literal["pending", "confirmed", "rejected"]
    created_at: datetime
    confirmed_at: datetime | None
    # Phase-2 fields. Omitted-as-null keeps phase-1 clients working.
    source: str | None = None
    external_id: str | None = None
    direction: str | None = None
    asset: str | None = None
    amount_raw: float | None = None
    suggested_category: str | None = None
    suggested_confidence: float | None = None
    suggested_reason: str | None = None
    final_category: str | None = None
    ts: datetime | None = None
    # Phase-4 Blockscout enrichment. Null on historical / un-enriched rows.
    counterparty_label: str | None = None
    # Phase-4g address labels. Backward-compatible optional additions.
    counterparty_kind: str | None = None
    counterparty_label_source: str | None = None
    counterparty_chain: str | None = None
    counterparty_address: str | None = None
    protocol_deposit: bool = False
    protocol_deposit_asset: str | None = None
    explain: str | None = None
    received_tokens: list[ReceivedTokenOut] = []
    # Phase-4 nature/safety. Read-only, computed per request; never a column.
    nature: Literal[
        "exchange_internal",
        "protocol_deposit",
        "protocol_redeem",
        "swap",
        "bridge",
        "self_transfer",
        "external_out",
        "dust",
        "test_data",
        "unknown",
    ] | None = None
    safety: Literal["safe", "review", "none"] | None = None

    @field_validator("amount_raw", "suggested_confidence", mode="before")
    @classmethod
    def _coerce_decimal(cls, value: object) -> float | None:
        if value is None or isinstance(value, float):
            return value
        return float(value)


class ConfirmIn(BaseModel):
    """Optional body. Missing body keeps the phase-1 confirm contract."""

    category: str | None = None


class SyncStats(BaseModel):
    fetched: int
    enqueued: int
    duplicates: int


class SyncAccepted(BaseModel):
    job_id: str
    status: Literal["queued"] = "queued"


class SyncJobStatus(BaseModel):
    job_id: str | None = None
    status: str
    result: SyncStats | None = None
    # Phase-3 scheduler bookkeeping. Present even when a job_id is queried.
    last_run_at: datetime | None = None
    last_status: str | None = None
    last_error: str | None = None
    last_trigger: Literal["manual", "scheduled", "backfill"] | None = None
    interval_seconds: int | None = None


class SyncReconcileItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ts: datetime
    snapshot_id: int | None = None
    nav_usdt: float
    flat_total_usdt: float
    covered_total_usdt: float
    positions_net_usdt: float
    residual_usdt: float
    tolerance_usdt: float
    status: Literal["ok", "warn", "fail"]
    detail: dict[str, object] | None = None


class SyncReconcileOut(BaseModel):
    status: Literal["ok", "warn", "fail"] | None = None
    latest: SyncReconcileItemOut | None = None
    items: list[SyncReconcileItemOut] = Field(default_factory=list)


class BackfillOut(BaseModel):
    created: int
    updated: int
    from_: datetime = Field(alias="from", serialization_alias="from")
    to: datetime

    model_config = ConfigDict(populate_by_name=True)


class WalletOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    chain: Literal["evm", "solana"]
    address: str
    created_at: datetime


_EVM_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_SOLANA_BASE58_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


class WalletIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chain: Literal["evm", "solana"]
    address: str

    @field_validator("address")
    @classmethod
    def _strip_address(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("地址不能为空")
        return cleaned

    def normalized(self) -> tuple[str, str]:
        """Return ``(chain, address)`` ready to store. EVM is lowercased."""
        if self.chain == "evm":
            if not _EVM_RE.fullmatch(self.address):
                raise ValueError("EVM 地址须为 0x 开头的 40 位十六进制")
            return self.chain, self.address.lower()
        if not _SOLANA_BASE58_RE.fullmatch(self.address):
            raise ValueError("Solana 地址须为 32～44 位 Base58 字符")
        return self.chain, self.address


class PositionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: Literal["option", "lending"]
    protocol: Literal["deribit", "binance", "aave", "morpho", "pendle", "kamino", "jupiter"]
    chain: str | None = None
    account_ref: str
    market_ref: str
    label: str | None = None
    status: Literal["open", "closed"]
    net_value_usdt: float
    collateral_usdt: float
    debt_usdt: float
    details: dict | None = None
    opened_at: datetime | None = None
    asof: datetime | None = None
    sync_id: int | None = None
    close_reason: str | None = None
    # P13: data provenance / live flag. Null on positions written before P13.
    provenance: Literal["demo", "fixture", "live"] | None = None
    live: bool | None = None
    # Phase-5: read-only classification, computed on every request.
    asset_class: Literal["cash", "crypto", "stock", "defi", "option", "other"] | None = None
    risk_level: Literal["low", "medium", "high"] | None = None
    risk_reason: str | None = None
    overridden: bool = False

    @field_validator("net_value_usdt", "collateral_usdt", "debt_usdt", mode="before")
    @classmethod
    def _money(cls, value: object) -> float:
        if isinstance(value, float):
            return value
        return float(value)  # type: ignore[arg-type]


class ClassBucketOut(BaseModel):
    """One asset-class bucket. Serialized with the contract key ``class``."""

    model_config = ConfigDict(populate_by_name=True)

    asset_class: str = Field(alias="class")
    label: str
    value_usdt: float
    pct: float
    items: int


class RiskBucketOut(BaseModel):
    risk: str
    value_usdt: float
    pct: float


class StructureOut(BaseModel):
    total_usdt: float
    by_class: list[ClassBucketOut]
    by_risk: list[RiskBucketOut]
    high_risk_pct: float


class PortfolioItemOut(BaseModel):
    key: str
    name: str
    source: str
    kind: Literal["holding", "position"]
    asset_class: str
    risk_level: str
    risk_reason: str
    overridden: bool = False
    value_usdt: float
    misc: bool = False
    chain: str | None = None
    contract: str | None = None
    position_id: int | None = None


class PortfolioItemsOut(BaseModel):
    total_usdt: float
    items: list[PortfolioItemOut]


class OverrideIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=255)
    asset_class: str | None = None
    risk_level: str | None = None
    reason: str = Field(default="", max_length=255)


class OverrideOut(BaseModel):
    key: str
    asset_class: str | None = None
    risk_level: str | None = None
    reason: str | None = None
    updated_at: datetime
    overridden: bool = True


class OverrideDeleteOut(BaseModel):
    deleted: bool
    key: str


class LendingSummaryOut(BaseModel):
    total_collateral_usdt: float
    total_debt_usdt: float
    net_value_usdt: float
    min_health_factor: float | None
    positions: list[PositionOut]


class OptionsSummaryOut(BaseModel):
    total_mark_value_usdt: float
    total_premium_usdt: float
    unrealized_pnl_usdt: float
    positions: list[PositionOut]


class AlertRuleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol: (
        Literal["deribit", "binance", "aave", "morpho", "pendle", "kamino", "jupiter"] | None
    ) = None
    warn_hf: float = 1.5
    danger_hf: float = 1.2
    enabled: bool = True
    cooldown_min: int = 360


class AlertRuleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    protocol: str | None = None
    warn_hf: float
    danger_hf: float
    enabled: bool
    cooldown_min: int

    @field_validator("warn_hf", "danger_hf", mode="before")
    @classmethod
    def _hf(cls, value: object) -> float:
        if isinstance(value, float):
            return value
        return float(value)  # type: ignore[arg-type]


class AlertEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    rule_id: int | None = None
    position_id: int | None = None
    level: Literal["warn", "danger", "recover"]
    hf: float | None = None
    # p9e, additive: "hf" / "liq_distance" / "drawdown", or NULL for data-source
    # notices. Rows written before p9e are inferred read-only by the router;
    # old frontends simply ignore the extra nullable field.
    event_kind: str | None = None
    # P7a: sent_at is delivery time and stays null while queued/failed.
    sent_at: datetime | None = None
    # P7a, additive: when the alert fired (also the cooldown clock).
    created_at: datetime | None = None
    channel_result: str | None = None
    # p8e2: A4 stores source identity in the reconcile log, not alert_events.
    # The API joins it back so a NULL-position row never falls back to a
    # generic 「数据源同步异常」. ``source_kind`` distinguishes stale carry-
    # forward data ("stale") from a provider/sync failure ("sync").
    source_name: str | None = None
    source_kind: Literal["stale", "sync"] | None = None

    @field_validator("hf", mode="before")
    @classmethod
    def _hf(cls, value: object) -> float | None:
        if value is None or isinstance(value, float):
            return value
        return float(value)  # type: ignore[arg-type]
