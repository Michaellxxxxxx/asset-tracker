"""Persisted WeChat send health, shared by the queue and ``/api/health``.

The context token can look fresh while every send fails with ``ret=-2`` (the
token was invalidated server-side, or iLink is rejecting the bot).  Token age
alone therefore cannot answer "is the send path stale?".  The flush loop writes
the outcome of the last real send here; a ret=-2/token-class failure marks the
path stale until a later send succeeds.  No token or message body is stored.

State lives in the existing ``wechat_poll_state`` key/value table so this needs
no migration.  ``alerted`` is the per-failure-window dedupe flag: the first
transition into stale logs one ERROR, later failures in the same window do not,
and a successful send clears the window.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import AlertEvent, User, WechatContextToken, WechatPollState
from .client import redact_send_text

logger = logging.getLogger(__name__)

SEND_HEALTH_KEY = "send_health"
_ERROR_LIMIT = 200

# p15b: the W1 send-path alert used to end at logger.error().  It now also
# creates one durable AlertEvent for the operator and queues exactly one
# WeChat notice.  A held row is parked far in the future so the queue never
# retries a doomed send; token refresh (reset_retry_backoff) or a real send
# success releases it.
HEALTH_ALERT_EVENT_KIND = "wechat_send_health"
HEALTH_ALERT_HOLD = "health_alert:await_token"
HEALTH_ALERT_HOLD_UNTIL = datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc)


@dataclass(frozen=True)
class SendHealth:
    """Read-only view for health endpoints and tests."""

    stale: bool
    last_send_error: str | None
    last_success_at: datetime | None
    last_failure_at: datetime | None
    failure_since: datetime | None
    alerted: bool


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _parse(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return _aware(datetime.fromisoformat(value))
    except ValueError:
        return None


def _load(db: Session) -> dict:
    row = db.get(WechatPollState, SEND_HEALTH_KEY)
    if row is None or not row.value:
        return {}
    try:
        data = json.loads(row.value)
    except (TypeError, ValueError):
        logger.warning("wechat send health state unreadable; resetting")
        return {}
    return data if isinstance(data, dict) else {}


def _save(db: Session, data: dict) -> None:
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    row = db.get(WechatPollState, SEND_HEALTH_KEY)
    if row is None:
        db.add(WechatPollState(key=SEND_HEALTH_KEY, value=payload))
    else:
        row.value = payload


def record_send_success(db: Session, *, at: datetime | None = None) -> None:
    """Record a real delivery success and clear the stale/alert window."""
    moment = _aware(at) or datetime.now(timezone.utc)
    data = _load(db)
    recovered = bool(data.get("stale")) or bool(data.get("alerted"))
    data.update(
        {
            "last_error": None,
            "last_success_at": moment.isoformat(),
            "stale": False,
            "alerted": False,
            "failure_since": None,
        }
    )
    _save(db, data)
    if recovered:
        # A real send proves the channel is usable again.  Release only the
        # queued health notices parked by the stale window; failed rows stay
        # terminal and are never selected.  Local import avoids health<->queue
        # import cycles (queue imports this module at import time).
        try:
            from .queue import release_health_alerts

            release_health_alerts(db)
        except Exception:  # noqa: BLE001 - health bookkeeping must not break a send
            logger.exception("wechat send health: failed to release held alerts")
        logger.info("wechat send path recovered by success at %s", moment.isoformat())


def _health_alert_owner_id(db: Session, failing_user_id: int) -> int | None:
    """Prefer the admin/operator, so a dead user token cannot hide the alert."""
    admin_id = db.scalar(
        select(User.id)
        .where(User.is_admin.is_(True))
        .order_by(User.id.asc())
        .limit(1)
    )
    return admin_id or failing_user_id


def _queue_health_alert(
    db: Session,
    *,
    failing_user_id: int,
    safe_error: str,
    failure_since: datetime,
    moment: datetime,
) -> AlertEvent | None:
    """Persist W1 as an alert event + one queued WeChat notice.

    If the owner's cached token predates this failure, the row is parked at
    ``HEALTH_ALERT_HOLD_UNTIL`` with no attempt spent.  It is released by a
    token refresh or by a later real send success; until then the queue must
    not retry a send that is guaranteed to fail.
    """
    owner_id = _health_alert_owner_id(db, failing_user_id)
    if owner_id is None:
        return None

    # Local imports keep the queue -> health import one-way at module load.
    from .queue import enqueue_alert

    event = AlertEvent(
        user_id=owner_id,
        rule_id=None,
        position_id=None,
        level="danger",
        hf=None,
        event_kind=HEALTH_ALERT_EVENT_KIND,
        created_at=moment,
        sent_at=None,
        channel_result="queued:health_alert",
    )
    db.add(event)
    db.flush()

    since = failure_since.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    text = (
        "系统告警：微信推送通道异常。\n"
        f"自 {since} 起发送持续失败。\n"
        f"最近错误：{safe_error}\n"
        "站点已显示醒目提示；token 刷新或下一次发送成功后自动恢复。"
    )
    row = enqueue_alert(
        db,
        alert_event_id=event.id,
        user_id=owner_id,
        text=text,
    )
    # enqueue_alert never revives a historical failed row.  A brand-new event
    # id cannot collide with one, but keep this guard explicit: only a queued
    # row belonging to this event may be parked.
    if row.status == "queued" and row.alert_event_id == event.id:
        token = db.get(WechatContextToken, owner_id)
        token_old = (
            token is None
            or token.updated_at is None
            or _aware(token.updated_at) <= moment
        )
        if token_old:
            row.last_error = HEALTH_ALERT_HOLD
            row.next_attempt_at = HEALTH_ALERT_HOLD_UNTIL
            db.flush()
    return event


def record_send_failure(
    db: Session,
    error: str | None,
    *,
    ret: int | None = None,
    token_error: bool = False,
    at: datetime | None = None,
    user_id: int | None = None,
) -> str:
    """Record one failed real send; return the redacted error that was stored.

    ``ret == -2`` is always stale, including the p11e "unverified" shape where
    the HTTP call succeeded but iLink answered ret=-2.  ``token_error`` covers
    textual token markers for callers that do not have a structured ret.
    A non-token failure updates the error text but never pretends an earlier
    stale window recovered; only a success does that.
    """
    moment = _aware(at) or datetime.now(timezone.utc)
    safe = redact_send_text(
        error or (f"ret={ret}" if ret is not None else "send_failed"),
        _ERROR_LIMIT,
    )
    data = _load(db)
    data["last_error"] = safe
    data["last_failure_at"] = moment.isoformat()

    stale = ret == -2 or bool(token_error)
    new_window = False
    if stale:
        if not data.get("stale"):
            data["stale"] = True
            data["failure_since"] = moment.isoformat()
            # New failure window: allow exactly one alert for it.
            data["alerted"] = False
            new_window = True
        if not data.get("alerted"):
            data["alerted"] = True
            logger.error(
                "wechat send health ALERT: send path stale since %s (ret=%s): %s",
                data.get("failure_since"),
                ret,
                safe,
            )
    _save(db, data)
    if new_window and user_id is not None:
        # W1's consumer: one durable event + one queued notice per window.
        # A failure to queue the notice must never undo the send-health state
        # that /api/health and the site banner read.
        try:
            with db.begin_nested():
                _queue_health_alert(
                    db,
                    failing_user_id=user_id,
                    safe_error=safe,
                    failure_since=moment,
                    moment=moment,
                )
        except Exception:  # noqa: BLE001
            logger.exception("wechat send health: failed to queue W1 alert")
    return safe


def send_blocked_for(db: Session, user_id: int) -> bool:
    """True when this user's cached token predates the last failed send (p12a).

    ``ret=-2 prepare failed`` invalidates the context token until an inbound
    message refreshes it. While the cached token is older than the latest
    failure, retrying is guaranteed to fail again, so alert evaluation must not
    launch another inline send or enqueue a fresh retry row. A token refresh
    (``updated_at`` after the failure) unblocks immediately.
    """
    data = _load(db)
    if not data.get("stale"):
        return False
    last_failure = _parse(data.get("last_failure_at")) or _parse(data.get("failure_since"))
    if last_failure is None:
        return False
    token = db.get(WechatContextToken, user_id)
    if token is None or token.updated_at is None:
        return True
    return _aware(token.updated_at) <= last_failure


def send_health_status(db: Session) -> SendHealth:
    data = _load(db)
    return SendHealth(
        stale=bool(data.get("stale")),
        last_send_error=data.get("last_error") or None,
        last_success_at=_parse(data.get("last_success_at")),
        last_failure_at=_parse(data.get("last_failure_at")),
        failure_since=_parse(data.get("failure_since")),
        alerted=bool(data.get("alerted")),
    )
