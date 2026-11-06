"""p15b: W1 send-health alert has real consumers.

The send path is exercised with fake senders only; no iLink calls.  These
tests lock three promises:

* a stale send path creates one durable AlertEvent and one WeChat notice;
* an expired owner token parks that notice without spending retries until a
  token refresh / real success releases it;
* historical ``failed`` rows are never revived or resent by the health path.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.auth import hash_password
from app.integrations.wechat import queue as wechat_queue
from app.integrations.wechat.client import SendResult
from app.integrations.wechat.health import (
    HEALTH_ALERT_EVENT_KIND,
    HEALTH_ALERT_HOLD,
    HEALTH_ALERT_HOLD_UNTIL,
    record_send_failure,
    send_health_status,
)
from app.integrations.wechat.queue import (
    flush_all_queued,
    flush_user_queue,
    reset_retry_backoff,
)
from app.models import AlertEvent, User, WechatContextToken, WechatPushQueue

NOW = datetime(2026, 10, 28, 16, 0, 25, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _fast_health_tests(monkeypatch):
    monkeypatch.setattr(wechat_queue, "FLUSH_MIN_INTERVAL_SECONDS", 0.0)


def _user(db, name: str = "p15b-user", *, admin: bool = False) -> User:
    user = User(
        username=name,
        hashed_password=hash_password("demo123"),
        is_admin=admin,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _context(db, user: User, *, at: datetime, token: str = "ctx-p15b") -> None:
    db.add(WechatContextToken(user_id=user.id, context_token=token, updated_at=at))
    db.commit()


def _queue_row(db, user: User, text: str = "p15b payload") -> WechatPushQueue:
    row = WechatPushQueue(
        user_id=user.id,
        pending_id=None,
        alert_event_id=None,
        text=text,
        status="queued",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _failed_row(db, user: User) -> WechatPushQueue:
    row = WechatPushQueue(
        user_id=user.id,
        pending_id=None,
        alert_event_id=None,
        text="old failed payload - must never resend",
        status="failed",
        attempts=7,
        next_attempt_at=None,
        last_error="failed:historical",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


class _Sender:
    def __init__(self, result: SendResult) -> None:
        self.result = result
        self.calls: list[tuple[str, str]] = []

    def send_message(self, context_token: str, text: str) -> SendResult:
        self.calls.append((context_token, text))
        return self.result


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _health_events(db) -> list[AlertEvent]:
    return list(
        db.scalars(
            select(AlertEvent)
            .where(AlertEvent.event_kind == HEALTH_ALERT_EVENT_KIND)
            .order_by(AlertEvent.id.asc())
        ).all()
    )


def _event_queue_row(db, event: AlertEvent) -> WechatPushQueue:
    return db.scalar(
        select(WechatPushQueue).where(WechatPushQueue.alert_event_id == event.id)
    )


def test_send_failure_creates_event_and_pushes_when_owner_token_is_fresh(db, client):
    admin = _user(db, "p15b-admin", admin=True)
    failing = _user(db, "p15b-failing")
    # The admin's token is newer than the failure: the operator notice can go.
    _context(db, admin, at=NOW + timedelta(minutes=5), token="ctx-admin")
    _context(db, failing, at=NOW - timedelta(hours=2), token="ctx-failing")
    _queue_row(db, failing)

    stats = flush_user_queue(
        db,
        failing.id,
        client=_Sender(SendResult(ok=False, ret=-2, error="ret=-2 prepare failed")),
        now=NOW,
    )
    assert stats.failed == 1

    health = send_health_status(db)
    assert health.stale is True
    assert client.get("/api/health").json()["status"] == "degraded"

    events = _health_events(db)
    assert len(events) == 1
    event = events[0]
    assert event.user_id == admin.id
    assert event.level == "danger"
    assert event.channel_result == "queued:health_alert"
    assert event.sent_at is None

    notice = _event_queue_row(db, event)
    assert notice is not None
    assert notice.status == "queued"
    assert notice.attempts == 0
    assert notice.next_attempt_at is None
    assert "微信推送通道异常" in notice.text
    assert "ctx-admin" not in notice.text

    # "wechat 能推则推": a fresh operator token sends the queued notice.
    sender = _Sender(SendResult(ok=True, ret=0))
    stats = flush_user_queue(
        db,
        admin.id,
        client=sender,
        now=NOW + timedelta(minutes=6),
    )
    assert stats.sent == 1
    assert len(sender.calls) == 1
    assert "微信推送通道异常" in sender.calls[0][1]
    db.refresh(event)
    db.refresh(notice)
    assert event.channel_result == "sent"
    assert _utc(event.sent_at) == NOW + timedelta(minutes=6)
    assert notice.status == "sent"
    assert send_health_status(db).stale is False
    assert client.get("/api/health").json()["status"] == "ok"


def test_expired_token_only_enqueues_and_never_retries_until_refresh(db):
    user = _user(db, "p15b-expired")
    _context(db, user, at=NOW - timedelta(hours=2))
    _queue_row(db, user)

    stats = flush_user_queue(
        db,
        user.id,
        client=_Sender(SendResult(ok=False, ret=-2, error="ret=-2 prepare failed")),
        now=NOW,
    )
    assert stats.failed == 1

    events = _health_events(db)
    assert len(events) == 1
    event = events[0]
    notice = _event_queue_row(db, event)
    assert notice.status == "queued"
    assert notice.attempts == 0
    assert notice.last_error == HEALTH_ALERT_HOLD
    assert _utc(notice.next_attempt_at) == HEALTH_ALERT_HOLD_UNTIL

    # Additional failures in the same stale window must not fan out more W1
    # events or notices.
    record_send_failure(
        db,
        "ret=-2 still stale",
        ret=-2,
        at=NOW + timedelta(seconds=1),
        user_id=user.id,
    )
    db.commit()
    assert len(_health_events(db)) == 1

    # No token refresh: a later worker tick must not spend a send attempt on
    # the parked W1 notice.
    sender = _Sender(SendResult(ok=True, ret=0))
    stats = flush_user_queue(db, user.id, client=sender, now=NOW + timedelta(seconds=1))
    assert stats.sent == 0
    assert sender.calls == []

    # A token refresh releases the parked W1 notice.  The original
    # prepare_failed row is already terminal and must stay untouched.
    original = db.scalar(
        select(WechatPushQueue).where(
            WechatPushQueue.user_id == user.id,
            WechatPushQueue.alert_event_id.is_(None),
        )
    )
    assert original.status == "failed"
    token = db.get(WechatContextToken, user.id)
    token.updated_at = NOW + timedelta(seconds=2)
    token.context_token = "ctx-refreshed"
    assert reset_retry_backoff(db, user.id) == 1
    db.commit()
    db.refresh(notice)
    assert notice.next_attempt_at is None
    assert notice.last_error is None
    sender = _Sender(SendResult(ok=True, ret=0))
    stats = flush_user_queue(db, user.id, client=sender, now=NOW + timedelta(seconds=3))
    assert stats.sent == 1
    assert len(sender.calls) == 1
    assert "微信推送通道异常" in sender.calls[0][1]
    db.refresh(original)
    assert original.status == "failed"
    db.refresh(event)
    assert event.channel_result == "sent"


def test_due_health_notice_is_reheld_and_released_by_success(db):
    user = _user(db, "p15b-reheld")
    _context(db, user, at=NOW - timedelta(hours=2))
    record_send_failure(
        db,
        "ret=-2 prepare failed",
        ret=-2,
        at=NOW,
        user_id=user.id,
    )
    db.commit()
    event = _health_events(db)[0]
    notice = _event_queue_row(db, event)

    # A lease/retry race may make the row due before token refresh; the flush
    # guard must park it again without spending a send attempt.
    notice.next_attempt_at = NOW + timedelta(seconds=1)
    notice.last_error = None
    db.commit()
    sender = _Sender(SendResult(ok=True, ret=0))
    stats = flush_user_queue(db, user.id, client=sender, now=NOW + timedelta(seconds=1))
    assert stats.skipped == 1
    assert stats.sent == 0
    assert sender.calls == []
    db.refresh(notice)
    assert notice.last_error == HEALTH_ALERT_HOLD
    assert _utc(notice.next_attempt_at) == HEALTH_ALERT_HOLD_UNTIL

    # A real success proves the channel recovered; release the parked notice
    # without requiring another token-refresh hook.
    from app.integrations.wechat.health import record_send_success

    record_send_success(db, at=NOW + timedelta(seconds=2))
    db.commit()
    db.refresh(notice)
    assert notice.next_attempt_at is None
    sender = _Sender(SendResult(ok=True, ret=0))
    stats = flush_user_queue(db, user.id, client=sender, now=NOW + timedelta(seconds=3))
    assert stats.sent == 1
    assert len(sender.calls) == 1
    db.refresh(event)
    assert event.channel_result == "sent"


def test_historical_failed_rows_are_never_resent_by_health_alert(db):
    user = _user(db, "p15b-history")
    _context(db, user, at=NOW - timedelta(hours=2))
    failed = _failed_row(db, user)
    before = (
        failed.status,
        failed.attempts,
        failed.text,
        failed.last_error,
        failed.next_attempt_at,
    )

    record_send_failure(
        db,
        "ret=-2 prepare failed",
        ret=-2,
        at=NOW,
        user_id=user.id,
    )
    db.commit()

    events = _health_events(db)
    assert len(events) == 1
    notice = _event_queue_row(db, events[0])
    assert notice.status == "queued"
    assert _utc(notice.next_attempt_at) == HEALTH_ALERT_HOLD_UNTIL

    sender = _Sender(SendResult(ok=True, ret=0))
    stats = flush_all_queued(db, client=sender)
    assert stats.sent == 0
    assert stats.failed == 0
    assert sender.calls == []

    db.refresh(failed)
    assert (
        failed.status,
        failed.attempts,
        failed.text,
        failed.last_error,
        failed.next_attempt_at,
    ) == before
    assert (
        db.scalar(
            select(func.count())
            .select_from(WechatPushQueue)
            .where(WechatPushQueue.status == "failed")
        )
        == 1
    )


def test_overview_exposes_send_health_for_the_site_banner(db, client, demo_user):
    recorded = record_send_failure(
        db,
        "ret=-2 context_token=ctx-secret prepare failed",
        ret=-2,
        at=NOW,
        user_id=demo_user.id,
    )
    db.commit()

    from tests.conftest import auth_header

    header = auth_header(client)
    body = client.get("/api/overview", headers=header)
    assert body.status_code == 200, body.text
    payload = body.json()
    assert payload["wechat_send_stale"] is True
    assert payload["wechat_send_error"] == recorded
    assert "ctx-secret" not in payload["wechat_send_error"]
    assert payload["wechat_send_since"].startswith("2026-10-28T16:00:25")
    assert client.get("/api/health").json()["status"] == "degraded"
