"""P11a: terminal WeChat queue rows must be truthful in alert history.

The queue rows are audit/retry state and remain read-only here.  Only
``alert_events.channel_result`` / ``sent_at`` may be repaired.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.integrations.wechat.client import MockWechatClient, SendResult
from app.integrations.wechat.queue import (
    AlertReconcileStats,
    enqueue_alert,
    flush_all_queued,
    flush_user_queue,
    reconcile_alert_event_results,
)
from app.models import AlertEvent, User, WechatContextToken, WechatPushQueue

NOW = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)


class _Permanent:
    def send_message(self, context_token: str, text: str) -> SendResult:
        del context_token, text
        return SendResult(ok=False, ret=400, error="bad_request")


def _user(db, name: str) -> User:
    user = User(username=name, hashed_password="not-used")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _event(
    db,
    user: User,
    *,
    channel_result: str | None,
    sent_at: datetime | None = None,
) -> AlertEvent:
    event = AlertEvent(
        user_id=user.id,
        level="danger",
        created_at=NOW - timedelta(hours=1),
        channel_result=channel_result,
        sent_at=sent_at,
    )
    db.add(event)
    db.flush()
    return event


def _queue(
    db,
    user: User,
    event: AlertEvent,
    *,
    status: str,
    attempts: int,
    last_error: str | None,
) -> WechatPushQueue:
    row = WechatPushQueue(
        alert_event_id=event.id,
        user_id=user.id,
        text="payload must not change",
        status=status,
        attempts=attempts,
        last_error=last_error,
        sent_at=None,
        next_attempt_at=NOW + timedelta(minutes=10),
    )
    db.add(row)
    db.flush()
    return row


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def _queue_snapshot(row: WechatPushQueue) -> tuple:
    return (
        row.id,
        row.status,
        row.attempts,
        row.text,
        row.last_error,
        _as_utc(row.sent_at),
        _as_utc(row.next_attempt_at),
        _as_utc(row.created_at),
    )


def test_failed_queue_row_repairs_history_and_is_idempotent(db):
    user = _user(db, "p11a-failed")
    event = _event(
        db,
        user,
        channel_result="queued:ret=-2",
        # Deliberately stale: a failed attempt must never look delivered.
        sent_at=NOW - timedelta(minutes=5),
    )
    row = _queue(
        db, user, event, status="failed", attempts=5, last_error="ret=-2"
    )
    db.commit()
    before = _queue_snapshot(row)

    stats = reconcile_alert_event_results(db, now=NOW)

    assert stats == AlertReconcileStats(scanned=1, updated=1)
    db.refresh(event)
    db.refresh(row)
    assert event.channel_result == "failed:ret=-2"
    assert event.sent_at is None
    assert _queue_snapshot(row) == before

    # Second call is a no-op: no reset, no duplicate send, no queue mutation.
    again = reconcile_alert_event_results(db, now=NOW)
    assert again == AlertReconcileStats(scanned=1, updated=0)
    db.refresh(event)
    db.refresh(row)
    assert event.channel_result == "failed:ret=-2"
    assert _queue_snapshot(row) == before


def test_legacy_stale_token_exhausted_row_is_reconciled_read_only(db):
    user = _user(db, "p11a-stale-token")
    db.add(
        WechatContextToken(
            user_id=user.id,
            context_token="test-only-token",
            updated_at=NOW - timedelta(days=5),
        )
    )
    event = _event(db, user, channel_result="queued:ret=-2")
    row = _queue(
        db, user, event, status="queued", attempts=8, last_error="ret=-2"
    )
    db.commit()
    before = _queue_snapshot(row)

    stats = reconcile_alert_event_results(db, now=NOW, include_stale_token=True)

    assert stats.updated == 1
    db.refresh(event)
    db.refresh(row)
    assert event.channel_result == "failed:ret=-2"
    assert event.sent_at is None
    assert row.status == "queued"
    assert row.attempts == 8
    assert _queue_snapshot(row) == before


def test_reconcile_keeps_prefix_and_ignores_sent_or_already_failed(db):
    user = _user(db, "p11a-prefix")
    reconcile_event = _event(
        db, user, channel_result="reconcile_fail:queued:ret=-2"
    )
    _queue(
        db, user, reconcile_event, status="failed", attempts=5, last_error="ret=-2"
    )

    sent_event = _event(db, user, channel_result="sent", sent_at=NOW)
    _queue(
        db, user, sent_event, status="failed", attempts=5, last_error="ret=-2"
    )

    failed_event = _event(db, user, channel_result="failed:ret=-2")
    _queue(
        db, user, failed_event, status="failed", attempts=5, last_error="ret=-2"
    )
    db.commit()

    stats = reconcile_alert_event_results(db, now=NOW)

    assert stats == AlertReconcileStats(scanned=3, updated=1)
    db.refresh(reconcile_event)
    db.refresh(sent_event)
    db.refresh(failed_event)
    assert reconcile_event.channel_result == "reconcile_fail:failed:ret=-2"
    assert sent_event.channel_result == "sent"
    assert failed_event.channel_result == "failed:ret=-2"


def test_flush_all_reconciles_failed_history_without_resending(db):
    user = _user(db, "p11a-flush-all")
    event = _event(db, user, channel_result="queued:ret=-2")
    row = _queue(
        db, user, event, status="failed", attempts=5, last_error="ret=-2"
    )
    db.commit()
    before = _queue_snapshot(row)
    sender = MockWechatClient()

    stats = flush_all_queued(db, client=sender)

    assert stats.sent == 0 and stats.failed == 0
    assert sender.sent == []
    db.refresh(event)
    db.refresh(row)
    assert event.channel_result == "failed:ret=-2"
    assert _queue_snapshot(row) == before


def test_retry_does_not_downgrade_a_corrected_failure(db):
    user = _user(db, "p11a-no-downgrade")
    db.add(
        WechatContextToken(
            user_id=user.id,
            context_token="test-only-token",
            updated_at=NOW,
        )
    )
    event = _event(db, user, channel_result="failed:ret=-2")
    row = enqueue_alert(
        db, alert_event_id=event.id, user_id=user.id, text="retry payload"
    )
    db.commit()

    flush_user_queue(db, user.id, client=_Permanent(), now=NOW)

    db.refresh(event)
    db.refresh(row)
    assert row.status == "queued"
    assert row.attempts == 1
    assert event.channel_result == "failed:ret=-2"
    assert event.sent_at is None
