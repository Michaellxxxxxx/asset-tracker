"""P7a: WeChat flush backoff, pending-token wait, and truthful delivery states.

All sends are mocked; nothing here dials iLink or WeChat.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.alerts import evaluate_alerts
from app.auth import hash_password
from app.integrations.wechat.client import MockWechatClient, SendResult
from app.integrations.wechat.poll import handle_inbound_text
from app.integrations.wechat.queue import (
    _MAX_ATTEMPTS,
    backoff_delay,
    enqueue_alert,
    flush_user_queue,
    is_token_error,
    reset_retry_backoff,
)
from app.integrations.wechat.status import token_status
from app.models import AlertEvent, User, WechatContextToken, WechatPushQueue
from app.positions_core import upsert_positions
from tests.conftest import auth_header

NOW = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)


class _TokenExpired:
    """The iLink symptom behind the six stuck pushes: ret=-2."""

    def send_message(self, context_token: str, text: str) -> SendResult:
        del context_token, text
        return SendResult(ok=False, ret=-2, error="ret=-2")


class _Permanent:
    """Non-token failure (bad payload); this must eventually become failed."""

    def send_message(self, context_token: str, text: str) -> SendResult:
        del context_token, text
        return SendResult(ok=False, ret=400, error="bad_request")


def _user(db, username: str) -> User:
    user = User(username=username, hashed_password=hash_password("pass-word"))
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _lending(hf: float = 1.1) -> dict:
    return {
        "kind": "lending",
        "protocol": "aave",
        "chain": "ethereum",
        "account_ref": "0xabc",
        "market_ref": "mkt",
        "label": "aave mkt",
        "status": "open",
        "net_value_usdt": "100",
        "collateral_usdt": "200",
        "debt_usdt": "100",
        "details": {"health_factor": hf},
    }


def _owned_event(db, user: User, *, created_at: datetime = NOW) -> AlertEvent:
    event = AlertEvent(
        user_id=user.id,
        level="danger",
        hf=None,
        created_at=created_at,
        sent_at=None,
        channel_result="queued:test",
    )
    db.add(event)
    db.flush()
    return event


def _queue(db, user: User) -> list[WechatPushQueue]:
    return list(
        db.scalars(
            select(WechatPushQueue)
            .where(WechatPushQueue.user_id == user.id)
            .order_by(WechatPushQueue.id.asc())
        ).all()
    )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def test_backoff_sequence_and_token_classifier() -> None:
    assert [backoff_delay(n).total_seconds() for n in range(1, 9)] == [
        30,
        60,
        120,
        240,
        480,
        960,
        1920,
        3600,
    ]
    assert backoff_delay(50).total_seconds() == 3600
    assert is_token_error("ret=-2") is True
    assert is_token_error("context_token expired") is True
    assert is_token_error("", -2) is True
    assert is_token_error("bad_request") is False
    assert is_token_error(None) is False


def test_missing_token_keeps_alert_pending_without_burning_attempts(db):
    user = _user(db, "p7a-no-token")
    event = _owned_event(db, user)
    row = enqueue_alert(db, alert_event_id=event.id, user_id=user.id, text="hi")
    db.commit()

    stats = flush_user_queue(db, user.id, client=_TokenExpired(), now=NOW)
    assert stats.skipped == 1
    db.refresh(row)
    assert row.status == "queued"
    assert row.attempts == 0
    assert _aware(row.next_attempt_at) == NOW + timedelta(seconds=30)
    db.refresh(event)
    assert event.channel_result == "queued:no_context_token"
    assert event.sent_at is None

    # Inside the backoff gate the row is not even selected.
    gate = _aware(row.next_attempt_at) - timedelta(seconds=1)
    assert flush_user_queue(db, user.id, client=_TokenExpired(), now=gate).skipped == 0


def test_expired_token_stays_queued_and_backs_off(db):
    user = _user(db, "p7a-expired")
    db.add(WechatContextToken(user_id=user.id, context_token="ctx-test", updated_at=NOW))
    event = _owned_event(db, user)
    row = enqueue_alert(db, alert_event_id=event.id, user_id=user.id, text="hi")
    db.commit()

    moment = NOW
    delays = []
    for _ in range(5):
        stats = flush_user_queue(db, user.id, client=_TokenExpired(), now=moment)
        assert stats.failed == 1
        db.refresh(row)
        assert row.status == "queued"  # ret=-2 is never terminal
        delays.append((_aware(row.next_attempt_at) - moment).total_seconds())
        moment = _aware(row.next_attempt_at) + timedelta(seconds=1)
    assert delays == [30, 60, 120, 240, 480]
    assert row.attempts == _MAX_ATTEMPTS
    assert row.status == "queued"
    db.refresh(event)
    assert event.channel_result == "queued:ret=-2"
    assert event.sent_at is None


def test_token_refresh_resets_backoff_and_resumes_send(db):
    user = _user(db, "p7a-refresh")
    db.add(WechatContextToken(user_id=user.id, context_token="ctx-old", updated_at=NOW))
    event = _owned_event(db, user)
    row = enqueue_alert(db, alert_event_id=event.id, user_id=user.id, text="hi")
    row.attempts = 3
    row.next_attempt_at = NOW + timedelta(hours=1)
    row.last_error = "ret=-2"
    db.commit()

    sender = MockWechatClient()
    handle_inbound_text(db, user.id, "help", "ctx-new", client=sender)

    db.refresh(row)
    assert row.status == "sent"
    assert row.attempts == 0
    assert row.next_attempt_at is None
    assert len(sender.sent) == 1
    token = db.get(WechatContextToken, user.id)
    assert token.context_token == "ctx-new"
    db.refresh(event)
    assert event.channel_result == "mock"
    assert event.sent_at is not None

    # Already sent: a second refresh/flush never resends it.
    handle_inbound_text(db, user.id, "help", "ctx-newer", client=sender)
    assert len(sender.sent) == 1


def test_permanent_error_fails_at_max_attempts(db):
    user = _user(db, "p7a-permanent")
    db.add(WechatContextToken(user_id=user.id, context_token="ctx-test", updated_at=NOW))
    event = _owned_event(db, user)
    row = enqueue_alert(db, alert_event_id=event.id, user_id=user.id, text="hi")
    db.commit()

    moment = NOW
    for _ in range(_MAX_ATTEMPTS - 1):
        flush_user_queue(db, user.id, client=_Permanent(), now=moment)
        db.refresh(row)
        assert row.status == "queued"
        moment = _aware(row.next_attempt_at) + timedelta(seconds=1)

    flush_user_queue(db, user.id, client=_Permanent(), now=moment)
    db.refresh(row)
    assert row.status == "failed"
    assert row.attempts == _MAX_ATTEMPTS
    assert row.next_attempt_at is None
    db.refresh(event)
    assert event.channel_result == "failed:bad_request"
    assert event.sent_at is None

    # Failed is terminal: later flushes never select or send it.
    stats = flush_user_queue(db, user.id, client=MockWechatClient(), now=NOW + timedelta(days=1))
    assert stats.sent == 0 and stats.failed == 0
    db.refresh(row)
    assert row.status == "failed"


def test_legacy_failed_pushes_are_never_revived(db):
    user = _user(db, "p7a-legacy")
    rows = []
    for index in range(6):
        row = WechatPushQueue(
            user_id=user.id,
            pending_id=None,
            alert_event_id=None,
            text=f"legacy {index}",
            status="failed",
            attempts=5,
            last_error="ret=-2",
            sent_at=None,
        )
        db.add(row)
        rows.append(row)
    db.commit()

    assert reset_retry_backoff(db, user.id) == 0
    db.commit()

    sender = MockWechatClient()
    handle_inbound_text(db, user.id, "help", "ctx-fresh", client=sender)
    assert sender.sent == []
    for row in rows:
        db.refresh(row)
        assert row.status == "failed"
        assert row.attempts == 5
        assert row.last_error == "ret=-2"


def test_channel_result_three_states(db):
    user = _user(db, "p7a-states")
    event = _owned_event(db, user)
    row = enqueue_alert(db, alert_event_id=event.id, user_id=user.id, text="hi")
    db.commit()

    flush_user_queue(db, user.id, client=_TokenExpired(), now=NOW)
    db.refresh(event)
    assert event.channel_result == "queued:no_context_token"
    assert event.sent_at is None

    db.add(WechatContextToken(user_id=user.id, context_token="ctx-test", updated_at=NOW))
    db.commit()
    flush_user_queue(db, user.id, client=MockWechatClient(), now=NOW + timedelta(minutes=1))
    db.refresh(event)
    assert event.channel_result == "mock"
    assert event.sent_at is not None

    failed_event = _owned_event(db, user)
    failed_row = enqueue_alert(
        db, alert_event_id=failed_event.id, user_id=user.id, text="bad"
    )
    db.commit()
    moment = NOW + timedelta(minutes=2)
    for _ in range(_MAX_ATTEMPTS):
        flush_user_queue(db, user.id, client=_Permanent(), now=moment)
        db.refresh(failed_row)
        if failed_row.status == "failed":
            break
        moment = _aware(failed_row.next_attempt_at) + timedelta(seconds=1)
    db.refresh(failed_event)
    assert failed_row.status == "failed"
    assert failed_event.channel_result == "failed:bad_request"
    assert failed_event.sent_at is None
    assert row.status == "sent"


def test_failed_alert_does_not_start_cooldown(db):
    user = _user(db, "p7a-cooldown")
    db.add(WechatContextToken(user_id=user.id, context_token="ctx-test", updated_at=NOW))
    upsert_positions(db, user.id, [_lending(1.1)])
    db.commit()

    first = evaluate_alerts(db, user.id, now=NOW, client=_Permanent())
    assert first.fired == 1
    event = db.scalar(select(AlertEvent).where(AlertEvent.user_id == user.id))
    assert event.channel_result == "queued:bad_request"

    # The queue later gives up: failed notices must not suppress the next firing.
    event.channel_result = "failed:bad_request"
    event.sent_at = None
    db.commit()

    second = evaluate_alerts(
        db, user.id, now=NOW + timedelta(minutes=5), client=MockWechatClient()
    )
    assert second.fired == 1
    levels = [
        row.level
        for row in db.scalars(select(AlertEvent).where(AlertEvent.user_id == user.id)).all()
    ]
    assert levels == ["danger", "danger"]


def test_token_status_boundaries(db):
    user = _user(db, "p7a-stale")
    fresh = token_status(db, user_id=user.id, now=NOW)
    assert fresh.stale is True and fresh.age_hours is None

    token = WechatContextToken(
        user_id=user.id, context_token="ctx-test", updated_at=NOW - timedelta(hours=23)
    )
    db.add(token)
    db.commit()
    recent = token_status(db, user_id=user.id, now=NOW)
    assert recent.stale is False
    assert recent.age_hours == 23.0

    token.updated_at = NOW - timedelta(hours=25)
    db.commit()
    old = token_status(db, user_id=user.id, now=NOW)
    assert old.stale is True
    assert old.age_hours == 25.0


def test_token_status_global_uses_latest_refresh(db):
    old_user = _user(db, "p7a-global-old")
    new_user = _user(db, "p7a-global-new")
    db.add(
        WechatContextToken(
            user_id=old_user.id, context_token="ctx-a", updated_at=NOW - timedelta(hours=25)
        )
    )
    db.add(
        WechatContextToken(
            user_id=new_user.id, context_token="ctx-b", updated_at=NOW - timedelta(hours=1)
        )
    )
    db.commit()

    global_status = token_status(db, now=NOW)
    assert global_status.stale is False
    assert global_status.age_hours == 1.0
    assert token_status(db, user_id=old_user.id, now=NOW).stale is True


def test_overview_and_health_expose_stale_fields(client, db, demo_user):
    other = _user(db, "p7a-other")
    real_now = datetime.now(timezone.utc)
    db.add(
        WechatContextToken(
            user_id=demo_user.id,
            context_token="ctx-demo",
            updated_at=real_now - timedelta(hours=25),
        )
    )
    db.add(
        WechatContextToken(
            user_id=other.id,
            context_token="ctx-other",
            updated_at=real_now - timedelta(hours=1),
        )
    )
    db.commit()

    headers = auth_header(client)
    overview = client.get("/api/overview", headers=headers)
    assert overview.status_code == 200
    body = overview.json()
    assert body["wechat_token_stale"] is True
    assert 24.5 < body["wechat_token_age_hours"] < 25.5

    # health is global: the freshest token wins and no user detail leaks.
    health = client.get("/api/health").json()
    assert health["wechat_token_stale"] is False
    assert 0.5 < health["wechat_token_age_hours"] < 1.5
    assert "user" not in str(health).lower()
