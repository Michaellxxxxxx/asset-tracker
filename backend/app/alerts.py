"""Evaluate health-factor and option-expiry rules after a sync, then push WeChat.

Lane E. Called from the end of ``run_sync`` inside try/except so a failure here
never changes the sync result. No context_token means the notice stays queued.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from .integrations.wechat.client import WechatClient, get_wechat_client
from .integrations.wechat.health import send_blocked_for
from .integrations.wechat.queue import enqueue_alert
from .models import AlertEvent, AlertRule, Position, Snapshot, User, WechatContextToken

logger = logging.getLogger(__name__)

# Options inside this window fire an expiry notice. Not a health-factor rule.
OPTION_EXPIRY_DAYS = 7
_ERROR_LIMIT = 180

# p12a: when the send path is stale and the cached token has not been
# refreshed since the last failure, do not attempt an inline send. The notice
# is recorded as skipped:token_refresh_required and the next sync after a real
# token refresh can deliver a fresh alert. Set WECHAT_ALERT_BLOCK_ON_STALE=0 to
# restore the old "try inline every evaluation" behaviour.
ALERT_BLOCK_ON_STALE = os.getenv("WECHAT_ALERT_BLOCK_ON_STALE", "1").strip().casefold() not in (
    "0",
    "false",
    "no",
    "off",
)

# A4: a failed source or a carried-forward position is "stale data" only after
# it has persisted for this long. Re-notify at most once per cooldown; a
# terminally failed delivery never starts a cooldown (same rule as p7a).
DATA_STALE_AFTER = timedelta(hours=6)
DATA_STALE_COOLDOWN = timedelta(hours=6)

# A6: distance from the current collateral price to the liquidation price.
# A7: NAV drawdown from the rolling peak. Both thresholds can be overridden by
# the caller or by an environment variable; no schema change is needed, and the
# existing all-protocol AlertRule still owns enabled/cooldown.
LIQ_ALERT_DISTANCE_PCT = Decimal("10")
NAV_DRAWDOWN_ALERT_PCT = Decimal("8")
NAV_DRAWDOWN_LOOKBACK_DAYS = 7
# A daily snapshot may legitimately be ~24h old; 48h means two missed captures.
NAV_SNAPSHOT_STALE_AFTER = timedelta(hours=48)

# p9e: explicit kind on new alert_events rows. A6/A7 also keep the pre-p9e
# negative-hf marker (A6: position_id set, A7: position_id NULL) so code from
# before p9e still routes new rows; readers prefer ``event_kind`` and only fall
# back to that marker for legacy NULL rows. Data-source notices stay NULL.
EVENT_KIND_HF = "hf"
# p12b: debt exists but the adapter could not compute a health factor. The
# event level stays ``warn`` (the DB check constraint only allows
# warn/danger/recover); a distinct kind keeps its cooldown separate from a
# real HF warning and makes recovery detectable.
EVENT_KIND_HF_MISSING = "hf_missing"
# p38: a carried-forward (``details.stale``) row with debt keeps its old HF.
# A degraded notice may still fire, but as its own kind so the stale value can
# neither start nor suppress a real ``hf`` cooldown/recover/escalation.
EVENT_KIND_HF_STALE = "hf_stale"
EVENT_KIND_LIQ_DISTANCE = "liq_distance"
EVENT_KIND_DRAWDOWN = "drawdown"


@dataclass(frozen=True)
class AlertEvalResult:
    fired: int = 0
    suppressed: int = 0
    sent: int = 0
    queued: int = 0
    # A9: fired, but the user has no context_token. Not a delivery attempt and
    # not a queue failure; the notice is still persisted for a later flush.
    skipped: int = 0


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _health_factor(details: dict | None) -> Decimal | None:
    if not isinstance(details, dict):
        return None
    raw = details.get("health_factor")
    if raw is None or raw == "":
        return None
    try:
        return Decimal(str(raw))
    except Exception:  # noqa: BLE001 - a bad fixture must not abort evaluation
        return None


def _option_expiry(details: dict | None) -> datetime | None:
    if not isinstance(details, dict):
        return None
    raw = details.get("expiry")
    if not raw:
        return None
    return _parse_datetime(raw)


def _parse_datetime(raw: object) -> datetime | None:
    if isinstance(raw, datetime):
        return _aware(raw)
    if not raw:
        return None
    text = str(raw).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return _aware(parsed)


def _details(position: Position) -> dict | None:
    return position.details if isinstance(position.details, dict) else None


def _is_stale(position: Position) -> bool:
    """p7b marks a carried-forward row; its old HF must not be scored (O1)."""
    details = _details(position)
    return bool(details and details.get("stale") is True)


def _is_failed_result(channel_result: str | None) -> bool:
    """p7a: only a terminal ``failed:*`` delivery releases the cooldown."""
    return str(channel_result or "").startswith("failed")


def _source_protocol(source: str) -> str:
    """``lending:jupiter:perps`` -> ``jupiter``; ``binance:X`` -> ``binance``."""
    parts = str(source).split(":")
    if len(parts) >= 2 and parts[0] == "lending":
        return parts[1]
    return parts[0] if parts else str(source)


def _level_for(hf: Decimal, rule: AlertRule) -> str | None:
    danger = Decimal(str(rule.danger_hf))
    warn = Decimal(str(rule.warn_hf))
    if hf <= danger:
        return "danger"
    if hf <= warn:
        return "warn"
    return None


def _pick_rule(rules: list[AlertRule], protocol: str) -> AlertRule | None:
    """Protocol-specific row wins. The all-protocols row (protocol NULL) is the fallback."""
    specific = next((row for row in rules if row.protocol == protocol and row.enabled), None)
    if specific is not None:
        return specific
    catch_all = next((row for row in rules if row.protocol is None and row.enabled), None)
    return catch_all


def _ensure_default_rule(db: Session, user_id: int) -> list[AlertRule]:
    rows = list(db.scalars(select(AlertRule).where(AlertRule.user_id == user_id)).all())
    if any(row.protocol is None for row in rows):
        return rows
    default = AlertRule(user_id=user_id)
    db.add(default)
    db.flush()
    rows.append(default)
    return rows


def _event_kind_scope(kind: str):
    """p9e: ``event_kind`` wins; NULL rows use the legacy marker inference.

    New rows are written with both the explicit kind and the old marker, so
    pre-p9e code keeps working, while this scope ignores a negative ``hf`` on a
    row that explicitly says it is a health-factor event (and vice versa).
    """
    legacy = AlertEvent.event_kind.is_(None)
    if kind == EVENT_KIND_HF:
        return or_(
            AlertEvent.event_kind == kind,
            and_(legacy, AlertEvent.hf >= 0),
        )
    if kind in (EVENT_KIND_HF_MISSING, EVENT_KIND_HF_STALE):
        # Deliberately exact: a missing-HF or stale-HF notice must not share a
        # cooldown with a real warn/danger/recover health event.
        return AlertEvent.event_kind == kind
    if kind == EVENT_KIND_LIQ_DISTANCE:
        return or_(
            AlertEvent.event_kind == kind,
            and_(legacy, AlertEvent.hf < 0),
        )
    return or_(
        AlertEvent.event_kind == kind,
        and_(legacy, AlertEvent.position_id.is_(None), AlertEvent.hf < 0),
    )


def _in_cooldown(
    db: Session,
    user_id: int,
    position_id: int,
    level: str,
    cooldown: timedelta,
    now: datetime,
    *,
    kind: str | None = None,
) -> bool:
    """Same position + same level inside the window is a duplicate. recover uses the same window.

    The clock is ``created_at`` (when the alert fired), not ``sent_at``: a
    queued notice has not been delivered yet but still counts, while a
    terminally failed notice must never start cooldown. ``kind`` scopes the
    lookup to A6 (``liq_distance``), health factor (``hf``), A7
    (``drawdown``), or either (None).
    """
    stmt = (
        select(AlertEvent.created_at)
        .where(
            AlertEvent.user_id == user_id,
            AlertEvent.position_id == position_id,
            AlertEvent.level == level,
            or_(
                AlertEvent.channel_result.is_(None),
                AlertEvent.channel_result.not_like("failed%"),
            ),
        )
        .order_by(AlertEvent.created_at.desc(), AlertEvent.id.desc())
        .limit(1)
    )
    if kind is not None:
        stmt = stmt.where(_event_kind_scope(kind))
    last = db.scalar(stmt)
    if last is None:
        return False
    return _aware(last) + cooldown > now


def _last_event(db: Session, user_id: int, position_id: int) -> AlertEvent | None:
    return db.scalar(
        select(AlertEvent)
        .where(AlertEvent.user_id == user_id, AlertEvent.position_id == position_id)
        .order_by(AlertEvent.created_at.desc(), AlertEvent.id.desc())
        .limit(1)
    )


def _hf_history_scope():
    """Health-factor history, including p12b's ``hf_missing`` notices."""
    return or_(
        AlertEvent.event_kind.in_([EVENT_KIND_HF, EVENT_KIND_HF_MISSING]),
        and_(
            AlertEvent.event_kind.is_(None),
            AlertEvent.position_id.is_not(None),
            AlertEvent.hf >= 0,
        ),
    )


def _last_hf_level(db: Session, user_id: int, position_id: int) -> str | None:
    """Latest health-factor level, ignoring A6's negative-hf marker.

    ``hf_missing`` is included so the first successful HF read after a data
    gap emits a recover (or the real warn/danger) instead of staying silent.
    """
    event = db.scalar(
        select(AlertEvent)
        .where(
            AlertEvent.user_id == user_id,
            AlertEvent.position_id == position_id,
            _hf_history_scope(),
        )
        .order_by(AlertEvent.created_at.desc(), AlertEvent.id.desc())
        .limit(1)
    )
    return event.level if event is not None else None


def _last_level_hf_event(
    db: Session, user_id: int, position_id: int, level: str
) -> AlertEvent | None:
    """Latest delivered/non-failed real HF event at ``level`` for escalation."""
    return db.scalar(
        select(AlertEvent)
        .where(
            AlertEvent.user_id == user_id,
            AlertEvent.position_id == position_id,
            AlertEvent.level == level,
            AlertEvent.hf.is_not(None),
            _event_kind_scope(EVENT_KIND_HF),
            or_(
                AlertEvent.channel_result.is_(None),
                AlertEvent.channel_result.not_like("failed%"),
            ),
        )
        .order_by(AlertEvent.created_at.desc(), AlertEvent.id.desc())
        .limit(1)
    )


def _hf_escalated(
    db: Session, user_id: int, position_id: int, level: str, hf: Decimal
) -> bool:
    """True when HF fell enough since the last same-level event to bypass cooldown.

    p36: two independent criteria, OR-ed. A drop of at least
    ``ALERT_HF_ESCALATE_DELTA`` (absolute, default 0.10) or a drop of at least
    ``ALERT_HF_ESCALATE_PCT`` percent of the previous HF (relative, default 8)
    both count as materially worse. A threshold of 0 disables that criterion;
    when both are 0 a worse reading never bypasses the cooldown.
    """
    previous = _last_level_hf_event(db, user_id, position_id, level)
    if previous is None or previous.hf is None:
        return False
    previous_hf = Decimal(str(previous.hf))
    if hf >= previous_hf:
        return False
    abs_drop = previous_hf - hf
    delta = _env_decimal("ALERT_HF_ESCALATE_DELTA", Decimal("0.10"))
    pct = _env_decimal("ALERT_HF_ESCALATE_PCT", Decimal("8"))
    absolute_hit = delta > 0 and abs_drop >= delta
    relative_hit = (
        pct > 0
        and previous_hf > 0
        and abs_drop / previous_hf >= pct / Decimal("100")
    )
    return absolute_hit or relative_hit


def _decimal_value(raw: object) -> Decimal | None:
    if raw is None or raw == "":
        return None
    try:
        return Decimal(str(raw))
    except Exception:  # noqa: BLE001 - bad payload values are simply missing
        return None


def _env_decimal(name: str, default: Decimal) -> Decimal:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    value = _decimal_value(raw)
    if value is None or value < 0:
        logger.warning("ignoring invalid %s=%r", name, raw)
        return default
    return value


def _threshold_pct(
    override: object,
    env_name: str,
    default: Decimal,
) -> Decimal:
    value = _decimal_value(override) if override is not None else _env_decimal(env_name, default)
    if value is None or value < 0:
        return default
    return value


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(str(raw).strip())
    except ValueError:
        logger.warning("ignoring invalid %s=%r", name, raw)
        return default
    return value if value >= minimum else default


def _env_hours(name: str, default: timedelta) -> timedelta:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        hours = float(str(raw).strip())
    except ValueError:
        logger.warning("ignoring invalid %s=%r", name, raw)
        return default
    return timedelta(hours=hours) if hours > 0 else default


def _format_price(value: Decimal) -> str:
    if abs(value) >= 1:
        return f"{value:.2f}"
    if abs(value) >= Decimal("0.01"):
        return f"{value:.6f}".rstrip("0").rstrip(".")
    return f"{value:.10f}".rstrip("0").rstrip(".")


def _price_from_collateral(details: dict | None) -> Decimal | None:
    """USD-weighted collateral price. Matches p8b's multi-collateral reference."""
    if not isinstance(details, dict):
        return None
    for key in ("current_price", "mark_price", "index_price", "underlying_price"):
        explicit = _decimal_value(details.get(key))
        if explicit is not None and explicit > 0:
            return explicit
    legs = details.get("collateral")
    if not isinstance(legs, list):
        raw_legs = details.get("legs")
        legs = [
            item
            for item in raw_legs
            if isinstance(item, dict) and item.get("side") == "collateral"
        ] if isinstance(raw_legs, list) else []
    total_usd = Decimal("0")
    weighted_price = Decimal("0")
    for item in legs:
        if not isinstance(item, dict):
            continue
        usd = _decimal_value(item.get("usd"))
        amount = _decimal_value(item.get("amount"))
        price = _decimal_value(item.get("price"))
        if price is None and amount is not None and amount > 0 and usd is not None and usd > 0:
            price = usd / amount
        if price is None or price <= 0 or usd is None or usd <= 0:
            continue
        total_usd += usd
        weighted_price += price * usd
    if total_usd <= 0:
        return None
    return weighted_price / total_usd


def _liq_metrics(
    position: Position,
    details: dict | None,
) -> tuple[Decimal, Decimal, Decimal, bool] | None:
    """Return ``(distance_pct, current_price, liq_price, estimated)``.

    Distance is how far the current price still is from liquidation, in
    percent of the current price. It turns negative once price crosses the
    liquidation level. Perps use the stored HF (long: mark/liq, short: liq/mark)
    because their mark price is not persisted; spot lending uses the
    collateral legs' USD/amount.
    """
    if not isinstance(details, dict):
        return None
    liq = _decimal_value(details.get("liq_price"))
    if liq is None or liq <= 0:
        return None
    side = str(details.get("side") or "").lower()
    hf = _health_factor(details)
    if side in ("long", "short"):
        # Perps keep equity (not notional) in debt_usdt, so a positive-equity
        # long has debt 0. HF is the authoritative mark/liq relation.
        if hf is None or hf <= 0:
            return None
        current = liq / hf if side == "short" else liq * hf
    else:
        debt = _decimal_value(position.debt_usdt)
        if debt is None or debt <= 0:
            return None
        current = _price_from_collateral(details)
    if current is None or current <= 0:
        return None
    if side == "short":
        distance = (liq - current) / current * Decimal("100")
    else:
        distance = (current - liq) / current * Decimal("100")
    return distance, current, liq, bool(details.get("liq_price_estimated"))


def _liq_hf(distance: Decimal) -> Decimal:
    """Negative marker: A6 events never collide with a real (>=0) health factor."""
    return -(abs(distance) + Decimal("1"))


def _nav_hf(drawdown: Decimal) -> Decimal:
    """Negative marker for A7 (position_id NULL); freshness/reconcile events use NULL."""
    return -(abs(drawdown) + Decimal("1"))


def _last_liq_event(db: Session, user_id: int, position_id: int) -> AlertEvent | None:
    return db.scalar(
        select(AlertEvent)
        .where(
            AlertEvent.user_id == user_id,
            AlertEvent.position_id == position_id,
            _event_kind_scope(EVENT_KIND_LIQ_DISTANCE),
        )
        .order_by(AlertEvent.created_at.desc(), AlertEvent.id.desc())
        .limit(1)
    )


def _last_nav_event(db: Session, user_id: int) -> AlertEvent | None:
    return db.scalar(
        select(AlertEvent)
        .where(
            AlertEvent.user_id == user_id,
            AlertEvent.position_id.is_(None),
            _event_kind_scope(EVENT_KIND_DRAWDOWN),
        )
        .order_by(AlertEvent.created_at.desc(), AlertEvent.id.desc())
        .limit(1)
    )


def _breakdown_has_stale(breakdown: str | None) -> bool:
    if not breakdown:
        return False
    try:
        payload = json.loads(breakdown)
    except (TypeError, ValueError):
        return False
    holdings = payload.get("holdings") if isinstance(payload, dict) else None
    if not isinstance(holdings, list):
        return False
    return any(
        isinstance(item, dict) and item.get("stale") is True for item in holdings
    )


def _snapshot_is_stale(row: Snapshot, moment: datetime, stale_after: timedelta) -> bool:
    if moment - _aware(row.ts) > stale_after:
        return True
    return _breakdown_has_stale(row.breakdown)


def _liq_message(
    position: Position,
    level: str,
    current: Decimal,
    liq: Decimal,
    distance: Decimal,
    threshold: Decimal,
    estimated: bool,
) -> str:
    name = (position.label or position.protocol or "仓位")[:40]
    estimate = "（估算）" if estimated else ""
    if level == "recover":
        return (
            f"清算价恢复 {name}，现价 {_format_price(current)}，"
            f"清算价 {_format_price(liq)}{estimate}，距离 {distance:.2f}%，"
            f"已回到警戒线 {threshold:.2f}% 以上。"
        )
    if level == "danger":
        return (
            f"清算价危险 {name}，现价 {_format_price(current)}，"
            f"清算价 {_format_price(liq)}{estimate}，"
            f"已跌破清算价 {abs(distance):.2f}%。"
        )
    return (
        f"清算价预警 {name}，现价 {_format_price(current)}，"
        f"清算价 {_format_price(liq)}{estimate}，距离 {distance:.2f}%，"
        f"低于警戒线 {threshold:.2f}%。"
    )


def _nav_message(
    level: str,
    current: Decimal,
    peak: Decimal,
    drawdown: Decimal,
    threshold: Decimal,
    lookback_days: int,
) -> str:
    if level == "recover":
        return (
            f"净值回撤恢复：当前 NAV {current:.2f}，近 {lookback_days} 日峰值 "
            f"{peak:.2f}，回撤 {drawdown:.2f}%，已回到阈值 {threshold:.2f}% 内。"
        )
    return (
        f"净值回撤预警：当前 NAV {current:.2f}，近 {lookback_days} 日峰值 "
        f"{peak:.2f}，回撤 {drawdown:.2f}%，超过阈值 {threshold:.2f}%。"
    )


@dataclass(frozen=True)
class NavDrawdownResult:
    """Outcome of one A7 evaluation. ``reason`` explains a non-alerting skip."""

    evaluated: bool = False
    reason: str | None = None
    current_nav: Decimal | None = None
    peak_nav: Decimal | None = None
    drawdown_pct: Decimal | None = None
    level: str | None = None
    event_id: int | None = None
    fired: int = 0
    suppressed: int = 0
    sent: int = 0
    queued: int = 0
    skipped: int = 0



def _format_hf(hf: Decimal) -> str:
    return f"{hf:.2f}"


def _message(
    position: Position,
    level: str,
    hf: Decimal | None,
    *,
    days_left: int | None = None,
    escalated: bool = False,
) -> str:
    """Short Chinese notice. No address, key, or context token."""
    name = (position.label or position.protocol or "仓位")[:40]
    if level == "recover":
        shown = _format_hf(hf) if hf is not None else "—"
        return f"健康度恢复 {name}，当前 {shown}，已回到安全线以上。"
    if days_left is not None:
        return f"期权临近到期 {name}，剩余 {days_left} 天。"
    shown = _format_hf(hf) if hf is not None else "—"
    decline = "，继续下降" if escalated else ""
    if level == "danger":
        return f"健康度危险 {name}，当前 {shown}{decline}，已低于危险线。"
    return f"健康度预警 {name}，当前 {shown}{decline}，已低于警戒线。"


def _hf_missing_message(position: Position) -> str:
    """Risk cannot be scored: debt exists but liquidation inputs are missing."""
    name = (position.label or position.protocol or "仓位")[:40]
    return (
        f"健康度数据缺失 {name}，有借款但暂时无法计算健康度；"
        "请检查抵押/清算阈值数据。"
    )


def _hf_stale_message(position: Position, level: str, hf: Decimal | None) -> str:
    """p38: degraded notice for a carried-forward row whose old HF is risky."""
    name = (position.label or position.protocol or "仓位")[:40]
    if hf is None:
        return (
            f"健康度数据过期 {name}，有借款但数据源暂不可用，"
            "无法确认健康度，请尽快核对。"
        )
    shown = _format_hf(hf)
    if level == "danger":
        return (
            f"健康度危险（数据过期）{name}，沿用旧值 {shown}，"
            "数据源暂不可用，请尽快核对。"
        )
    return (
        f"健康度预警（数据过期）{name}，沿用旧值 {shown}，"
        "数据源暂不可用，请尽快核对。"
    )


def _closed_message(position: Position) -> str:
    """A8 recover notice for a position that is no longer at risk."""
    name = (position.label or position.protocol or "仓位")[:40]
    return f"健康度恢复 {name}，仓位已关闭或已还清，风险已解除。"


def _push(db: Session, user_id: int, text: str, client: WechatClient) -> str:
    """Send now, queue on a transient failure, or skip when there is no token.

    Never raises. Return value is stored on ``alert_events.channel_result``.
    A missing context_token is not a queue failure: the event is recorded and
    the notice is still enqueued, but the channel honestly says it was skipped.
    """
    token_row = db.get(WechatContextToken, user_id)
    context = token_row.context_token if token_row is not None else ""
    if not context:
        logger.info("alert skipped user=%s reason=no_token", user_id)
        return "skipped:no_token"
    if ALERT_BLOCK_ON_STALE and send_blocked_for(db, user_id):
        logger.info("alert skipped user=%s reason=token_refresh_required", user_id)
        return "skipped:token_refresh_required"
    try:
        result = client.send_message(context, text)
    except Exception as exc:  # noqa: BLE001 - a push failure must not fail the sync
        logger.info("alert queued user=%s reason=send_error", user_id)
        return f"queued:{type(exc).__name__}"[:_ERROR_LIMIT]
    if result.ok:
        mock = type(client).__name__ == "MockWechatClient"
        return "mock" if mock else "sent"
    error = (result.error or "send_failed")[:80]
    logger.info("alert queued user=%s reason=%s", user_id, error)
    return f"queued:{error}"[:_ERROR_LIMIT]


def enqueue_reconcile_failure(
    db: Session,
    *,
    user_id: int,
    nav_usdt: Decimal,
    residual_usdt: Decimal,
    tolerance_usdt: Decimal,
    now: datetime | None = None,
) -> AlertEvent | None:
    """Queue one WeChat notice for a failed NAV reconcile.

    Reuses ``alert_events`` (position_id NULL, level danger) and
    ``enqueue_alert`` so the notice survives without a context_token and is
    sent by the queue flusher, never inline. Same-class failures inside the
    all-protocol rule's ``cooldown_min`` return ``None`` (no event, no push).
    Warn-level reconcile rows never call this.
    """
    moment = _aware(now or datetime.now(timezone.utc))
    rules = _ensure_default_rule(db, user_id)
    rule = next((row for row in rules if row.protocol is None), None)
    cooldown = timedelta(minutes=max(int(rule.cooldown_min), 0) if rule is not None else 360)
    cutoff = moment - cooldown
    recent = db.scalar(
        select(AlertEvent.id)
        .where(
            AlertEvent.user_id == user_id,
            AlertEvent.position_id.is_(None),
            AlertEvent.level == "danger",
            AlertEvent.channel_result.like("reconcile%"),
            AlertEvent.channel_result.not_like("%failed%"),
            AlertEvent.created_at >= cutoff,
        )
        .order_by(AlertEvent.id.desc())
        .limit(1)
    )
    if recent is not None:
        logger.info("reconcile alert suppressed user=%s reason=cooldown", user_id)
        return None

    threshold = (tolerance_usdt * Decimal("5")).quantize(Decimal("0.01"))
    text = (
        f"对账失败：NAV {nav_usdt:.2f}，持仓与仓位差额 {residual_usdt:+.2f}，"
        f"超过失败阈值 {threshold:.2f}。已写入对账日志，请核查。"
    )
    event = AlertEvent(
        user_id=user_id,
        rule_id=rule.id if rule is not None else None,
        position_id=None,
        level="danger",
        hf=None,
        created_at=moment,
        sent_at=None,
        channel_result="reconcile_fail:queued",
    )
    db.add(event)
    db.flush()
    enqueue_alert(
        db,
        alert_event_id=event.id,
        user_id=user_id,
        text=text,
    )
    logger.info("reconcile alert queued user=%s event=%s", user_id, event.id)
    return event


def _record(
    db: Session,
    *,
    user_id: int,
    rule: AlertRule | None,
    position: Position | None,
    level: str,
    hf: Decimal | None,
    now: datetime,
    channel_result: str,
    event_kind: str | None = None,
) -> AlertEvent:
    event = AlertEvent(
        user_id=user_id,
        rule_id=rule.id if rule is not None else None,
        position_id=position.id if position is not None else None,
        level=level,
        hf=hf,
        event_kind=event_kind,
        created_at=now,
        # ``sent_at`` now means actually delivered. A queued notice leaves it
        # NULL; the queue flusher fills it in on success.
        sent_at=now if channel_result in ("sent", "mock") else None,
        channel_result=channel_result[:255],
    )
    db.add(event)
    db.flush()
    return event


def _stale_message(source: str, stale_count: int, first_seen: datetime) -> str:
    hours = int(DATA_STALE_AFTER.total_seconds() // 3600)
    since = first_seen.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    suffix = f"，涉及 {stale_count} 个仓位（沿用旧值）" if stale_count else ""
    return (
        f"数据过期：{source} 自 {since} 起超过 {hours} 小时未成功更新{suffix}。"
        "请检查数据源或同步任务。"
    )


def _recovered_message(source: str) -> str:
    return f"数据恢复：{source} 已重新同步成功，数据恢复更新。"


def _queue_alert(db: Session, event: AlertEvent, user_id: int, text: str) -> None:
    """Persist a queued notice without letting queue bookkeeping fail the alert."""
    try:
        with db.begin_nested():
            enqueue_alert(db, alert_event_id=event.id, user_id=user_id, text=text)
    except Exception:  # noqa: BLE001 - retry bookkeeping must not fail sync
        logger.exception("alert queue enqueue failed event=%s", event.id)


def _should_queue(channel: str) -> bool:
    """Transient failures and a missing token both need a retry row."""
    return channel.startswith("queued") or channel == "skipped:no_token"


def evaluate_data_freshness(
    db: Session,
    user_id: int,
    *,
    failed_sources: Iterable[str] = (),
    stale_positions: Iterable[Mapping] = (),
    previous_state: Mapping | None = None,
    now: datetime | None = None,
    client: WechatClient | None = None,
) -> dict:
    """A4: one stale-data alert per failing source, plus one recover on return.

    A source is alerted only after it has been failing (or its positions have
    been carried forward) for ``DATA_STALE_AFTER``. The same source is then
    re-notified at most every ``DATA_STALE_COOLDOWN``; a terminal ``failed:*``
    delivery is ignored for cooldown purposes, so the next sync retries.

    Alert identity is kept in the reconcile log detail (``tracked_sources``)
    rather than in ``alert_events.channel_result``, which the queue rewrites on
    delivery. ``alert_events`` keeps position_id NULL and level warn/recover,
    so the existing table/constraints are unchanged.
    """
    moment = _aware(now or datetime.now(timezone.utc))

    prior: Mapping = {}
    if isinstance(previous_state, Mapping):
        raw_prior = previous_state.get("tracked_sources")
        if isinstance(raw_prior, Mapping):
            prior = raw_prior

    current: dict[str, dict] = {}
    for raw in failed_sources:
        source = str(raw or "").strip()
        if not source:
            continue
        current.setdefault(source, {"first_seen": moment, "stale_count": 0})
    for item in stale_positions:
        if not isinstance(item, Mapping):
            continue
        protocol = str(item.get("protocol") or "").strip()
        key = f"stale:{protocol}" if protocol else "stale:unknown"
        since = _parse_datetime(item.get("stale_since")) or moment
        represented = next(
            (source for source in current if _source_protocol(source) == protocol),
            None,
        )
        target = represented if represented is not None else key
        entry = current.setdefault(target, {"first_seen": since, "stale_count": 0})
        entry["first_seen"] = min(entry["first_seen"], since)
        entry["stale_count"] += 1

    if not current and not prior:
        return {
            "threshold_hours": int(DATA_STALE_AFTER.total_seconds() // 3600),
            "cooldown_hours": int(DATA_STALE_COOLDOWN.total_seconds() // 3600),
            "tracked_sources": {},
            "fired": [],
            "recovered": [],
            "suppressed": [],
        }

    sender = client or get_wechat_client()
    rules = _ensure_default_rule(db, user_id)
    rule = next((row for row in rules if row.protocol is None), None)

    tracked: dict[str, dict] = {}
    fired: list[dict] = []
    recovered: list[dict] = []
    suppressed: list[dict] = []

    for source in sorted(current):
        entry = current[source]
        first_seen = entry["first_seen"]
        raw_prior = prior.get(source)
        prior_event: AlertEvent | None = None
        if isinstance(raw_prior, Mapping):
            prior_first = _parse_datetime(raw_prior.get("first_seen"))
            if prior_first is not None and prior_first < first_seen:
                first_seen = prior_first
        raw_event_id = raw_prior.get("event_id") if isinstance(raw_prior, Mapping) else None
        if raw_event_id is not None:
            try:
                prior_event = db.get(AlertEvent, int(raw_event_id))
            except (TypeError, ValueError):
                prior_event = None
            if prior_event is not None and (
                prior_event.user_id != user_id or prior_event.level == "recover"
            ):
                prior_event = None
            # p7a: a terminal failure never starts a cooldown.
            if prior_event is not None and _is_failed_result(prior_event.channel_result):
                prior_event = None

        tracked[source] = {
            "first_seen": first_seen.isoformat(),
            "event_id": prior_event.id if prior_event is not None else None,
            "stale_count": int(entry["stale_count"]),
        }

        if rule is None or not rule.enabled:
            suppressed.append({"source": source, "reason": "disabled"})
            continue

        if prior_event is not None:
            age = moment - _aware(prior_event.created_at)
            if age < DATA_STALE_COOLDOWN:
                suppressed.append({"source": source, "reason": "cooldown"})
                continue
            # Cooldown elapsed and the source is still stale: notify again.
        elif moment - first_seen < DATA_STALE_AFTER:
            suppressed.append({"source": source, "reason": "waiting_threshold"})
            continue

        text = _stale_message(source, int(entry["stale_count"]), first_seen)
        channel = _push(db, user_id, text, sender)
        event = _record(
            db,
            user_id=user_id,
            rule=rule,
            position=None,
            level="warn",
            hf=None,
            now=moment,
            channel_result=channel,
        )
        tracked[source]["event_id"] = event.id
        fired.append(
            {"source": source, "event_id": event.id, "stale_count": int(entry["stale_count"])}
        )
        if _should_queue(channel):
            _queue_alert(db, event, user_id, text)

    for source in sorted(prior):
        if source in current:
            continue
        raw_prior = prior.get(source)
        if not isinstance(raw_prior, Mapping):
            continue
        raw_event_id = raw_prior.get("event_id")
        if raw_event_id is None:
            continue
        try:
            prior_event = db.get(AlertEvent, int(raw_event_id))
        except (TypeError, ValueError):
            continue
        if prior_event is None or prior_event.user_id != user_id:
            continue
        if prior_event.level == "recover":
            continue
        if rule is None or not rule.enabled:
            continue
        text = _recovered_message(source)
        channel = _push(db, user_id, text, sender)
        event = _record(
            db,
            user_id=user_id,
            rule=rule,
            position=None,
            level="recover",
            hf=None,
            now=moment,
            channel_result=channel,
        )
        recovered.append(
            {"source": source, "event_id": event.id, "from_event_id": prior_event.id}
        )
        if _should_queue(channel):
            _queue_alert(db, event, user_id, text)

    return {
        "threshold_hours": int(DATA_STALE_AFTER.total_seconds() // 3600),
        "cooldown_hours": int(DATA_STALE_COOLDOWN.total_seconds() // 3600),
        "tracked_sources": tracked,
        "fired": fired,
        "recovered": recovered,
        "suppressed": suppressed,
    }


def evaluate_liq_distance_alerts(
    db: Session,
    user_id: int,
    *,
    now: datetime | None = None,
    client: WechatClient | None = None,
    threshold_pct: object | None = None,
) -> AlertEvalResult:
    """A6: warn when price is within ``threshold_pct`` of the liquidation price.

    Only open lending rows with a debt and a usable ``liq_price`` are scored.
    The current price comes from the collateral legs (or the perp HF), so no
    extra network call is made. A6 shares the p7d cooldown/recover mechanics:
    same position + same level inside ``cooldown_min`` is suppressed, and a
    move back above the threshold emits one recover. ``details.stale`` rows
    are skipped exactly like health alerts.
    """
    moment = _aware(now or datetime.now(timezone.utc))
    threshold = _threshold_pct(
        threshold_pct, "LIQ_ALERT_DISTANCE_PCT", LIQ_ALERT_DISTANCE_PCT
    )
    sender = client or get_wechat_client()
    rules = _ensure_default_rule(db, user_id)
    positions = list(
        db.scalars(
            select(Position).where(
                Position.user_id == user_id,
                Position.status == "open",
                Position.kind == "lending",
            )
        ).all()
    )
    fired = 0
    suppressed = 0
    sent = 0
    queued = 0
    skipped = 0

    for position in positions:
        rule = _pick_rule(rules, position.protocol)
        if rule is None:
            continue
        cooldown = timedelta(minutes=max(int(rule.cooldown_min), 0))
        if _is_stale(position):
            continue
        details = _details(position)
        metrics = _liq_metrics(position, details)
        if metrics is None:
            continue
        distance, current, liq, estimated = metrics
        last = _last_liq_event(db, user_id, position.id)

        if distance >= threshold:
            if last is None:
                continue
            if last.level == "recover":
                if _in_cooldown(
                    db, user_id, position.id, "recover", cooldown, moment, kind=EVENT_KIND_LIQ_DISTANCE
                ):
                    suppressed += 1
                continue
            level = "recover"
            if _in_cooldown(
                db, user_id, position.id, level, cooldown, moment, kind=EVENT_KIND_LIQ_DISTANCE
            ):
                suppressed += 1
                continue
        else:
            level = "danger" if distance <= 0 else "warn"
            if _in_cooldown(
                db, user_id, position.id, level, cooldown, moment, kind=EVENT_KIND_LIQ_DISTANCE
            ):
                suppressed += 1
                continue

        text = _liq_message(position, level, current, liq, distance, threshold, estimated)
        channel = _push(db, user_id, text, sender)
        event = _record(
            db,
            user_id=user_id,
            rule=rule,
            position=position,
            level=level,
            hf=_liq_hf(distance),
            event_kind=EVENT_KIND_LIQ_DISTANCE,
            now=moment,
            channel_result=channel,
        )
        fired += 1
        if channel.startswith("queued"):
            queued += 1
            _queue_alert(db, event, user_id, text)
        elif channel.startswith("skipped:"):
            skipped += 1
            if channel == "skipped:no_token":
                _queue_alert(db, event, user_id, text)
        else:
            sent += 1

    if fired or suppressed:
        db.commit()
    return AlertEvalResult(
        fired=fired,
        suppressed=suppressed,
        sent=sent,
        queued=queued,
        skipped=skipped,
    )


def evaluate_nav_drawdown(
    db: Session,
    user_id: int,
    *,
    now: datetime | None = None,
    client: WechatClient | None = None,
    threshold_pct: object | None = None,
    lookback_days: int | None = None,
    stale_after: timedelta | None = None,
    admin_only: bool = True,
) -> NavDrawdownResult:
    """A7: one alert per drawdown episode, plus one recover on return inside.

    Reads (never writes) non-seed snapshots. The latest snapshot must exist,
    be fresh, and not carry stale venue quantities. The rolling peak is the
    max NAV in the last ``lookback_days``. A drawdown strictly above the
    threshold fires once; while it remains above, repeated evaluations only
    count a suppression. A move back to the threshold or inside emits one
    recover, after which a new breach can alert again.
    """
    moment = _aware(now or datetime.now(timezone.utc))
    user = db.get(User, user_id)
    if user is None:
        return NavDrawdownResult(reason="no_user")
    if admin_only and not user.is_admin:
        return NavDrawdownResult(reason="not_admin")

    latest = db.scalar(
        select(Snapshot)
        .where(
            Snapshot.user_id == user_id,
            Snapshot.source != "seed",
            Snapshot.ts <= moment,
        )
        .order_by(Snapshot.ts.desc())
        .limit(1)
    )
    if latest is None:
        return NavDrawdownResult(reason="no_snapshot")

    stale_window = stale_after or _env_hours(
        "NAV_SNAPSHOT_STALE_HOURS", NAV_SNAPSHOT_STALE_AFTER
    )
    current = _decimal_value(latest.nav_usdt)
    if _snapshot_is_stale(latest, moment, stale_window):
        return NavDrawdownResult(
            evaluated=True,
            reason="stale_snapshot",
            current_nav=current,
        )
    if current is None or current < 0:
        return NavDrawdownResult(evaluated=True, reason="invalid_nav")

    days = lookback_days if lookback_days is not None else _env_int(
        "NAV_DRAWDOWN_LOOKBACK_DAYS", NAV_DRAWDOWN_LOOKBACK_DAYS
    )
    start = moment - timedelta(days=max(int(days), 1))
    rows = list(
        db.scalars(
            select(Snapshot)
            .where(
                Snapshot.user_id == user_id,
                Snapshot.source != "seed",
                Snapshot.ts >= start,
                Snapshot.ts <= moment,
            )
            .order_by(Snapshot.ts.asc())
        ).all()
    )
    if not rows:
        return NavDrawdownResult(
            evaluated=True,
            reason="no_snapshot",
            current_nav=current,
        )
    peak = max(
        (value for row in rows if (value := _decimal_value(row.nav_usdt)) is not None),
        default=None,
    )
    if peak is None or peak <= 0:
        return NavDrawdownResult(
            evaluated=True,
            reason="invalid_nav",
            current_nav=current,
        )
    drawdown = (peak - current) / peak * Decimal("100")
    threshold = _threshold_pct(
        threshold_pct, "NAV_DRAWDOWN_ALERT_PCT", NAV_DRAWDOWN_ALERT_PCT
    )

    rules = _ensure_default_rule(db, user_id)
    rule = next((row for row in rules if row.protocol is None), None)
    if rule is None or not rule.enabled:
        return NavDrawdownResult(
            evaluated=True,
            reason="disabled",
            current_nav=current,
            peak_nav=peak,
            drawdown_pct=drawdown,
        )

    sender = client or get_wechat_client()
    last = _last_nav_event(db, user_id)
    if drawdown > threshold:
        if (
            last is not None
            and last.level != "recover"
            and not _is_failed_result(last.channel_result)
        ):
            return NavDrawdownResult(
                evaluated=True,
                reason="already_alerted",
                current_nav=current,
                peak_nav=peak,
                drawdown_pct=drawdown,
                level=last.level,
                event_id=last.id,
                suppressed=1,
            )
        level = "warn"
    else:
        if last is None or last.level == "recover":
            return NavDrawdownResult(
                evaluated=True,
                reason="within_threshold",
                current_nav=current,
                peak_nav=peak,
                drawdown_pct=drawdown,
            )
        level = "recover"

    text = _nav_message(level, current, peak, drawdown, threshold, max(int(days), 1))
    channel = _push(db, user_id, text, sender)
    event = _record(
        db,
        user_id=user_id,
        rule=rule,
        position=None,
        level=level,
        hf=_nav_hf(drawdown),
        event_kind=EVENT_KIND_DRAWDOWN,
        now=moment,
        channel_result=channel,
    )
    sent = queued = skipped = 0
    if channel.startswith("queued"):
        queued = 1
        _queue_alert(db, event, user_id, text)
    elif channel.startswith("skipped:"):
        skipped = 1
        if channel == "skipped:no_token":
            _queue_alert(db, event, user_id, text)
    else:
        sent = 1
    db.commit()
    return NavDrawdownResult(
        evaluated=True,
        reason="fired",
        current_nav=current,
        peak_nav=peak,
        drawdown_pct=drawdown,
        level=level,
        event_id=event.id,
        fired=1,
        sent=sent,
        queued=queued,
        skipped=skipped,
    )


def evaluate_alerts(
    db: Session,
    user_id: int,
    *,
    now: datetime | None = None,
    client: WechatClient | None = None,
    option_expiry_days: int = OPTION_EXPIRY_DAYS,
    liq_alert_distance_pct: object | None = None,
    nav_drawdown_pct: object | None = None,
) -> AlertEvalResult:
    """Score open positions and write alert_events. Caller commits.

    Health: hf <= danger_hf → danger; hf <= warn_hf → warn; a later reading
    above warn_hf emits one recover. Same position + same level inside
    cooldown_min is dropped. An option expiring within ``option_expiry_days``
    emits one warn (cooldown applies; no recover).

    A position carried forward from a failed source (``details.stale``) is
    never scored as a real ``hf`` event: its old HF must neither trigger,
    suppress, nor recover a health alert (A4 owns the stale-data notice). If
    such a lending row still carries debt (or ``hf_unavailable``) and its old
    HF is warn/danger, p38 emits a degraded ``hf_stale`` reminder instead; it
    has its own cooldown and is excluded from the real HF history and p36
    escalation, so it cannot block a later genuine warn/danger/recover. An old
    HF in the safe zone, or a row without debt, stays silent. A position that
    had a warn/danger and is now ``closed`` gets one recover saying it was
    closed/repaid (A8).

    A6 (liquidation-price distance) and A7 (NAV drawdown) run afterwards with
    the same sender/clock; their counts are folded into this result.
    """
    moment = _aware(now or datetime.now(timezone.utc))
    sender = client or get_wechat_client()
    rules = _ensure_default_rule(db, user_id)
    positions = list(
        db.scalars(
            select(Position).where(Position.user_id == user_id, Position.status == "open")
        ).all()
    )
    fired = 0
    suppressed = 0
    sent = 0
    queued = 0
    skipped = 0

    for position in positions:
        rule = _pick_rule(rules, position.protocol)
        if rule is None:
            continue
        cooldown = timedelta(minutes=max(int(rule.cooldown_min), 0))
        details = position.details if isinstance(position.details, dict) else None

        # O1/p38: a carried-forward row keeps an old HF. It must not drive a
        # real health alert (trigger, suppress, or recover). With debt it may
        # still emit a degraded ``hf_stale`` notice, isolated from ``hf``.
        event_kind = None
        if _is_stale(position):
            if position.kind != "lending":
                continue
            hf = _health_factor(details)
            debt = _decimal_value(position.debt_usdt) or Decimal("0")
            hf_unavailable = bool(details and details.get("hf_unavailable") is True)
            if debt <= 0 and not hf_unavailable:
                # Nothing to liquidate; the A4 stale-data notice owns this row.
                continue
            if hf is not None:
                level = _level_for(hf, rule)
                if level is None:
                    # The old value looks safe: no stale reminder (and no
                    # recover) instead of re-notifying on every sync failure.
                    continue
            else:
                # Debt exists but the adapter could not carry an old HF.
                level = "warn"
            event_kind = EVENT_KIND_HF_STALE
            if _in_cooldown(
                db,
                user_id,
                position.id,
                level,
                cooldown,
                moment,
                kind=EVENT_KIND_HF_STALE,
            ):
                suppressed += 1
                continue
            text = _hf_stale_message(position, level, hf)
            channel = _push(db, user_id, text, sender)
        elif position.kind == "lending":
            event_kind = EVENT_KIND_HF
            hf = _health_factor(details)
            debt = _decimal_value(position.debt_usdt) or Decimal("0")
            hf_unavailable = bool(details and details.get("hf_unavailable") is True)
            if hf is None and (debt > 0 or hf_unavailable):
                # p12b: a debt position whose liquidation threshold/price is
                # missing used to be skipped entirely. Warn that risk cannot
                # be scored; its own kind avoids colliding with a real warn.
                level = "warn"
                event_kind = EVENT_KIND_HF_MISSING
                if _in_cooldown(
                    db,
                    user_id,
                    position.id,
                    level,
                    cooldown,
                    moment,
                    kind=EVENT_KIND_HF_MISSING,
                ):
                    suppressed += 1
                    continue
                text = _hf_missing_message(position)
                channel = _push(db, user_id, text, sender)
            else:
                if hf is None:
                    continue
                level = _level_for(hf, rule)
                if level is None:
                    previous = _last_hf_level(db, user_id, position.id)
                    if previous is None:
                        continue
                    if previous == "recover":
                        # Still safe. Inside the recover cooldown this is a duplicate.
                        if _in_cooldown(
                            db, user_id, position.id, "recover", cooldown, moment, kind=EVENT_KIND_HF
                        ):
                            suppressed += 1
                        continue
                    level = "recover"
                escalated = False
                if _in_cooldown(
                    db, user_id, position.id, level, cooldown, moment, kind=EVENT_KIND_HF
                ):
                    # A materially worse reading at the same level is new risk,
                    # not a duplicate: let it through the cooldown once.
                    if level in ("warn", "danger") and _hf_escalated(
                        db, user_id, position.id, level, hf
                    ):
                        escalated = True
                    else:
                        suppressed += 1
                        continue
                text = _message(position, level, hf, escalated=escalated)
                channel = _push(db, user_id, text, sender)
        elif position.kind == "option":
            expiry = _option_expiry(details)
            if expiry is None:
                continue
            remaining = expiry - moment
            if remaining > timedelta(days=option_expiry_days) or remaining.total_seconds() < 0:
                continue
            level = "warn"
            hf = None
            if _in_cooldown(db, user_id, position.id, level, cooldown, moment):
                suppressed += 1
                continue
            days_left = max(int(remaining.total_seconds() // 86400), 0)
            text = _message(position, level, None, days_left=days_left)
            channel = _push(db, user_id, text, sender)
        else:
            continue

        event = _record(
            db,
            user_id=user_id,
            rule=rule,
            position=position,
            level=level,
            hf=hf,
            event_kind=event_kind,
            now=moment,
            channel_result=channel,
        )
        fired += 1
        if channel.startswith("queued"):
            queued += 1
            # Persist the notice for the queue flusher. The event is already
            # flushed, so a queue outage cannot lose the fact that it fired.
            _queue_alert(db, event, user_id, text)
        elif channel.startswith("skipped:"):
            skipped += 1
            # A9: only a missing token is persisted for a later flush. A
            # stale token is deliberately not queued: it would just create
            # another guaranteed failure before the next inbound refresh.
            if channel == "skipped:no_token":
                _queue_alert(db, event, user_id, text)
        else:
            sent += 1

    # A8: a warn/danger position that is now closed (or gone from the open
    # list) gets exactly one recover. A second evaluation sees the recover as
    # the latest event and only counts a duplicate suppression.
    closed = list(
        db.scalars(
            select(Position).where(Position.user_id == user_id, Position.status == "closed")
        ).all()
    )
    for position in closed:
        rule = _pick_rule(rules, position.protocol)
        if rule is None:
            continue
        latest = _last_event(db, user_id, position.id)
        if latest is None:
            continue
        if latest.level == "recover":
            # A terminal failure does not start a cooldown; retry next run.
            if not _is_failed_result(latest.channel_result):
                suppressed += 1
                continue
        text = _closed_message(position)
        channel = _push(db, user_id, text, sender)
        event = _record(
            db,
            user_id=user_id,
            rule=rule,
            position=position,
            level="recover",
            hf=None,
            now=moment,
            channel_result=channel,
        )
        fired += 1
        if channel.startswith("queued"):
            queued += 1
            _queue_alert(db, event, user_id, text)
        elif channel.startswith("skipped:"):
            skipped += 1
            if channel == "skipped:no_token":
                _queue_alert(db, event, user_id, text)
        else:
            sent += 1

    # A6: liquidation-price distance for lending rows whose adapter published
    # a liq_price (including p8b's estimated multi-collateral value).
    liq_result = evaluate_liq_distance_alerts(
        db,
        user_id,
        now=moment,
        client=sender,
        threshold_pct=liq_alert_distance_pct,
    )
    fired += liq_result.fired
    suppressed += liq_result.suppressed
    sent += liq_result.sent
    queued += liq_result.queued
    skipped += liq_result.skipped

    # A7: NAV drawdown, admin-only and read-only over snapshots. Non-admin
    # users and users without a fresh non-seed snapshot are skipped inside.
    nav_result = evaluate_nav_drawdown(
        db,
        user_id,
        now=moment,
        client=sender,
        threshold_pct=nav_drawdown_pct,
    )
    fired += nav_result.fired
    suppressed += nav_result.suppressed
    sent += nav_result.sent
    queued += nav_result.queued
    skipped += nav_result.skipped

    if fired or suppressed:
        db.commit()
    return AlertEvalResult(
        fired=fired,
        suppressed=suppressed,
        sent=sent,
        queued=queued,
        skipped=skipped,
    )
