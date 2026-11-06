"""P12a: ret=-2 ``prepare failed`` is terminal and does not spawn retry storms.

The live iLink client answers HTTP 200 with ``ret=-2 errmsg=prepare failed``
when the cached context_token can no longer prepare an outbound message. That
is a definitive pre-delivery failure: the old p11e "unverified" path retried
it three times, and every sync queued a fresh alert, producing hundreds of
terminal rows. These tests pin the terminal classification and the per-token
alert gate. No network calls and no live token.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select

from app.alerts import evaluate_alerts
from app.integrations.wechat import queue as wechat_queue
from app.integrations.wechat.client import SendResult
from app.integrations.wechat.health import (
    record_send_failure,
    send_blocked_for,
    send_health_status,
)
from app.integrations.wechat.queue import (
    PREPARE_FAILED_PREFIX,
    flush_user_queue,
    is_prepare_failed,
    is_unverified_ret_minus_two,
)
from app.models import AlertEvent, Position, User, WechatContextToken, WechatPushQueue
from app.positions_core import upsert_positions

NOW = datetime(2026, 10, 29, 5, 10, 11, tzinfo=timezone.utc)


def _user(db, name: str) -> User:
    user = User(username=name, hashed_password="not-used")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _token(db, user: User, at: datetime = NOW) -> WechatContextToken:
    row = WechatContextToken(user_id=user.id, context_token="ctx-p12a", updated_at=at)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _queue_row(db, user: User, **values) -> WechatPushQueue:
    data = {
        "user_id": user.id,
        "pending_id": None,
        "alert_event_id": None,
        "text": "p12a alert payload",
        "status": "queued",
        "attempts": 0,
    }
    data.update(values)
    row = WechatPushQueue(**data)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


class _PrepareFailedSender:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def send_message(self, context_token: str, text: str) -> SendResult:
        self.calls.append((context_token, text))
        return SendResult(
            ok=False,
            ret=-2,
            error="ret=-2",
            errcode=-2,
            errmsg="prepare failed",
            http_status=200,
        )


class _OkSender:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def send_message(self, context_token: str, text: str) -> SendResult:
        self.calls.append((context_token, text))
        return SendResult(ok=True, ret=0, http_status=200)


def test_prepare_failed_is_definitive_not_unverified():
    result = SendResult(
        ok=False,
        ret=-2,
        error="ret=-2",
        errcode=-2,
        errmsg="prepare failed",
        http_status=200,
    )
    assert is_prepare_failed(result) is True
    assert is_unverified_ret_minus_two(result) is False


def test_prepare_failed_fails_once_and_never_retries(db):
    user = _user(db, "p12a-terminal")
    _token(db, user)
    row = _queue_row(db, user)
    sender = _PrepareFailedSender()

    stats = flush_user_queue(db, user.id, client=sender, now=NOW)
    assert stats.failed == 1
    assert len(sender.calls) == 1
    db.refresh(row)
    assert row.status == "failed"
    assert row.attempts == 1
    assert (row.last_error or "").startswith(PREPARE_FAILED_PREFIX)
    assert "prepare failed" in (row.last_error or "")
    assert row.next_attempt_at is None

    health = send_health_status(db)
    assert health.stale is True
    assert health.last_success_at is None
    assert send_blocked_for(db, user.id) is True

    # A day later the terminal row is still not claimed or sent.
    later = flush_user_queue(db, user.id, client=sender, now=NOW + timedelta(days=1))
    assert later.sent == 0 and later.failed == 0
    assert len(sender.calls) == 1

    # A *new* alert may still try once with the (stale) token, then terminal.
    fresh = _queue_row(db, user, text="new alert after terminal")
    stats = flush_user_queue(
        db, user.id, client=sender, now=NOW + timedelta(days=1, seconds=1)
    )
    assert stats.failed == 1
    assert len(sender.calls) == 2
    db.refresh(fresh)
    assert fresh.status == "failed" and fresh.attempts == 1


def test_already_failed_row_is_never_claimed(db):
    user = _user(db, "p12a-failed-old")
    _token(db, user)
    row = _queue_row(
        db,
        user,
        status="failed",
        attempts=0,
        last_error="prepare_failed: ret=-2 errmsg=prepare failed",
        next_attempt_at=None,
    )
    sender = _OkSender()

    stats = flush_user_queue(db, user.id, client=sender, now=NOW)
    assert stats.sent == 0 and stats.failed == 0 and stats.skipped == 0
    assert sender.calls == []
    db.refresh(row)
    assert row.status == "failed"
    assert row.attempts == 0


def test_terminal_switch_falls_back_to_token_wait(db, monkeypatch):
    user = _user(db, "p12a-switch")
    _token(db, user)
    row = _queue_row(db, user)
    sender = _PrepareFailedSender()
    monkeypatch.setattr(wechat_queue, "TERMINAL_PREPARE_FAILED", False)

    stats = flush_user_queue(db, user.id, client=sender, now=NOW)
    assert stats.failed == 1
    db.refresh(row)
    assert row.status == "queued"
    assert row.attempts == 1
    assert not (row.last_error or "").startswith(PREPARE_FAILED_PREFIX)
    assert row.next_attempt_at is not None


def _lending(hf: float) -> dict:
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


def test_alert_evaluation_does_not_send_while_token_is_stale(db):
    user = _user(db, "p12a-alert-gate")
    token = _token(db, user)
    upsert_positions(db, user.id, [_lending(1.1)])
    db.commit()

    # The last real send failed with prepare failed at NOW; the cached token
    # is exactly as old as that failure, i.e. it has not been refreshed.
    record_send_failure(db, "ret=-2 errmsg=prepare failed", ret=-2, at=NOW)
    db.commit()
    assert send_blocked_for(db, user.id) is True

    sender = _OkSender()
    result = evaluate_alerts(
        db, user.id, now=NOW + timedelta(minutes=1), client=sender
    )
    assert result.fired == 1
    assert result.queued == 0 and result.sent == 0 and result.skipped == 1
    assert sender.calls == []
    event = db.scalar(
        select(AlertEvent).where(AlertEvent.user_id == user.id).order_by(AlertEvent.id.desc())
    )
    assert event is not None and event.channel_result == "skipped:token_refresh_required"
    assert (
        db.scalar(
            select(WechatPushQueue).where(WechatPushQueue.user_id == user.id)
        )
        is None
    )

    # An inbound message refreshes the token; after the (default 6h) cooldown
    # the next evaluation can deliver a fresh alert.
    token.updated_at = NOW + timedelta(minutes=2)
    db.commit()
    assert send_blocked_for(db, user.id) is False

    later = evaluate_alerts(
        db, user.id, now=NOW + timedelta(hours=7), client=sender
    )
    assert later.fired == 1 and later.sent == 1
    assert len(sender.calls) == 1
    delivered = db.scalar(
        select(AlertEvent).where(AlertEvent.user_id == user.id).order_by(AlertEvent.id.desc())
    )
    assert delivered is not None and delivered.channel_result == "sent"
    assert delivered.sent_at is not None
