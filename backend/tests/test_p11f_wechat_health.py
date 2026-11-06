"""P11f: send health, cursor safety/idempotency, and paced flushes.

Everything here is offline: senders/getupdates are fakes, no iLink calls.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.integrations.wechat import poll
from app.integrations.wechat import queue as wechat_queue
from app.integrations.wechat.client import (
    InboundMessage,
    SendResult,
    UpdatesResult,
)
from app.integrations.wechat.health import (
    record_send_failure,
    record_send_success,
    send_health_status,
)
from app.integrations.wechat.poll import (
    handle_inbound_text,
    process_updates,
    upsert_context_token,
)
from app.integrations.wechat.queue import flush_user_queue
from app.models import (
    LedgerEntry,
    PendingConfirmation,
    User,
    WechatContextToken,
    WechatPollState,
    WechatPushQueue,
)

NOW = datetime(2026, 10, 28, 16, 0, 25, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _fast_health_tests(monkeypatch):
    """Pacing has its own test; do not sleep 1s in every other case."""
    monkeypatch.setattr(wechat_queue, "FLUSH_MIN_INTERVAL_SECONDS", 0.0)


def _user(db, name: str = "p11f-user") -> User:
    user = User(username=name, hashed_password="not-used")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _admin(db) -> User:
    user = User(username="admin", hashed_password="not-used")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _context(db, user: User, token: str = "ctx-p11f") -> None:
    db.add(WechatContextToken(user_id=user.id, context_token=token, updated_at=NOW))
    db.commit()


def _queue_row(db, user: User, text: str = "p11f payload") -> WechatPushQueue:
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


class _Sender:
    def __init__(self, result: SendResult) -> None:
        self.result = result
        self.calls: list[tuple[str, str]] = []

    def send_message(self, context_token: str, text: str) -> SendResult:
        self.calls.append((context_token, text))
        return self.result


# --------------------------------------------------------------------------
# W1: /api/health send fields, redaction, stale window + one-shot alert.
# --------------------------------------------------------------------------


def test_health_reports_success_and_clears_stale(db, client):
    user = _user(db)
    _context(db, user)
    _queue_row(db, user)

    stats = flush_user_queue(
        db,
        user.id,
        client=_Sender(SendResult(ok=True, ret=0)),
        now=NOW,
    )
    assert stats.sent == 1

    health = send_health_status(db)
    assert health.stale is False
    assert health.last_success_at == NOW
    assert health.last_send_error is None

    body = client.get("/api/health").json()
    assert body["wechat_send_stale"] is False
    assert body["last_send_error"] is None
    assert body["last_success_at"] == NOW.isoformat()


def test_health_ret_minus_two_marks_stale_redacts_and_recovers(db, client):
    user = _user(db)
    _context(db, user)
    _queue_row(db, user)
    secret = "ctx-SECRET-0123456789abcdef"
    raw_error = (
        "ret=-2 context_token=" + secret + " Authorization: Bearer supersecret "
        + "x" * 300
    )

    stats = flush_user_queue(
        db,
        user.id,
        client=_Sender(
            SendResult(ok=False, ret=-2, error=raw_error, errcode=-2, errmsg=raw_error)
        ),
        now=NOW,
    )
    assert stats.failed == 1

    health = send_health_status(db)
    assert health.stale is True
    assert health.last_send_error is not None
    assert len(health.last_send_error) <= 200
    assert secret not in health.last_send_error
    assert "supersecret" not in health.last_send_error
    assert health.last_success_at is None

    body = client.get("/api/health").json()
    assert body["wechat_send_stale"] is True
    assert body["last_send_error"] == health.last_send_error
    assert body["last_success_at"] is None

    # A later real success clears the window; no token refresh is needed.
    # p15b: the queued W1 notice is released by that success and is delivered
    # in the same pass, so this flush sends the recovery row plus the notice.
    _queue_row(db, user, "after recovery")
    sender = _Sender(SendResult(ok=True, ret=0))
    stats = flush_user_queue(
        db,
        user.id,
        client=sender,
        now=NOW + timedelta(seconds=1),
    )
    assert stats.sent == 2
    assert any("微信推送通道异常" in text for _, text in sender.calls)
    health = send_health_status(db)
    assert health.stale is False
    assert health.last_send_error is None
    assert health.last_success_at == NOW + timedelta(seconds=1)


def test_stale_alert_logs_once_and_rearms_after_recovery(db, caplog):
    caplog.set_level(logging.ERROR, logger="app.integrations.wechat.health")

    for offset in (0, 30, 60):
        record_send_failure(
            db,
            "ret=-2 context_token=ctx-secret",
            ret=-2,
            at=NOW + timedelta(seconds=offset),
        )
        db.commit()

    alerts = [
        record.message
        for record in caplog.records
        if "send health ALERT" in record.message
    ]
    assert len(alerts) == 1
    assert "ctx-secret" not in alerts[0]
    assert send_health_status(db).stale is True

    record_send_success(db, at=NOW + timedelta(minutes=1))
    db.commit()
    assert send_health_status(db).stale is False

    # A new failure window gets exactly one new alert.
    record_send_failure(db, "ret=-2 again", ret=-2, at=NOW + timedelta(minutes=2))
    db.commit()
    alerts = [
        record.message
        for record in caplog.records
        if "send health ALERT" in record.message
    ]
    assert len(alerts) == 2


# --------------------------------------------------------------------------
# W3: one round is capped at 5 sends, >=1s apart, 20/minute.
# --------------------------------------------------------------------------


class _FakeClock:
    def __init__(self) -> None:
        self.value = 1000.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


def test_backlog_50_sends_at_most_5_per_round_with_1s_gap(db, monkeypatch):
    user = _user(db)
    _context(db, user)
    for index in range(50):
        _queue_row(db, user, f"backlog-{index}")

    clock = _FakeClock()
    sender = _Sender(SendResult(ok=True, ret=0))
    monkeypatch.setattr(wechat_queue, "FLUSH_BATCH_LIMIT", 5)
    monkeypatch.setattr(wechat_queue, "FLUSH_MIN_INTERVAL_SECONDS", 1.0)
    monkeypatch.setattr(wechat_queue, "FLUSH_PER_MINUTE_LIMIT", 20)

    stats = flush_user_queue(
        db,
        user.id,
        client=sender,
        now=NOW,
        clock=clock.now,
        sleep=clock.sleep,
    )

    assert stats.sent == 5
    assert len(sender.calls) == 5
    assert len(clock.sleeps) == 4
    assert all(seconds >= 1.0 for seconds in clock.sleeps)
    assert db.scalar(
        select(func.count())
        .select_from(WechatPushQueue)
        .where(WechatPushQueue.status == "queued")
    ) == 45


def test_per_minute_cap_defers_remaining_rows(db, monkeypatch):
    user = _user(db)
    _context(db, user)
    for index in range(21):
        _queue_row(db, user, f"minute-{index}")

    clock = _FakeClock()
    sender = _Sender(SendResult(ok=True, ret=0))
    monkeypatch.setattr(wechat_queue, "FLUSH_BATCH_LIMIT", 25)
    monkeypatch.setattr(wechat_queue, "FLUSH_MIN_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(wechat_queue, "FLUSH_PER_MINUTE_LIMIT", 20)

    first = flush_user_queue(
        db, user.id, client=sender, now=NOW, clock=clock.now, sleep=clock.sleep
    )
    assert first.sent == 20

    clock.value += 61
    second = flush_user_queue(
        db, user.id, client=sender, now=NOW + timedelta(seconds=61), clock=clock.now, sleep=clock.sleep
    )
    assert second.sent == 1
    assert len(sender.calls) == 21


# --------------------------------------------------------------------------
# W4: cursor advances only after a whole batch succeeds; retries + dedupe.
# --------------------------------------------------------------------------


class _BatchClient:
    def __init__(self, batches: list[UpdatesResult]) -> None:
        self.batches = batches
        self.get_calls: list[str] = []
        self.sent: list[tuple[str, str]] = []

    def get_updates(self, buf: str, timeout_sec: int = 35) -> UpdatesResult:
        del timeout_sec
        self.get_calls.append(buf)
        return self.batches[min(len(self.get_calls) - 1, len(self.batches) - 1)]

    def send_message(self, context_token: str, text: str) -> SendResult:
        self.sent.append((context_token, text))
        return SendResult(ok=True, ret=0)


def _set_buf(db, value: str) -> None:
    db.add(WechatPollState(key="get_updates_buf", value=value))
    db.commit()


def _buf(db) -> str:
    row = db.get(WechatPollState, "get_updates_buf")
    return row.value if row is not None else ""


def test_poll_mid_batch_failure_keeps_cursor_and_retries_same_batch(db, monkeypatch):
    _admin(db)
    _set_buf(db, "buf-1")
    batch = UpdatesResult(
        msgs=(
            InboundMessage("u1", "help", "ctx-1"),
            InboundMessage("u1", "help", "ctx-2"),
        ),
        new_buf="buf-2",
    )
    client = _BatchClient([batch])

    real_handle = poll.handle_inbound_text
    calls = {"n": 0}

    def flaky_handle(db_session, user_id, text, context_token, sender):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated processing failure")
        return real_handle(db_session, user_id, text, context_token, sender)

    monkeypatch.setattr(poll, "handle_inbound_text", flaky_handle)

    with pytest.raises(RuntimeError, match="simulated processing failure"):
        process_updates(db, client)

    assert _buf(db) == "buf-1"

    # Retry: same cursor is polled again and the failed message is handled.
    replies = process_updates(db, client)
    assert replies
    assert client.get_calls == ["buf-1", "buf-1"]
    assert _buf(db) == "buf-2"


class _ReplyFailClient(_BatchClient):
    def send_message(self, context_token: str, text: str) -> SendResult:
        self.sent.append((context_token, text))
        return SendResult(ok=False, ret=-2, error="ret=-2")


def test_poll_reply_ret_minus_two_marks_stale_and_keeps_cursor(db):
    _admin(db)
    _set_buf(db, "buf-1")
    client = _ReplyFailClient(
        [
            UpdatesResult(
                msgs=(InboundMessage("u1", "help", "ctx-reply"),),
                new_buf="buf-2",
            )
        ]
    )

    with pytest.raises(RuntimeError, match="reply send failed"):
        process_updates(db, client)

    assert _buf(db) == "buf-1"
    health = send_health_status(db)
    assert health.stale is True
    assert health.last_send_error is not None
    assert "ret=-2" in health.last_send_error


def test_poll_not_ok_does_not_advance_cursor(db):
    _admin(db)
    _set_buf(db, "buf-1")
    client = _BatchClient(
        [UpdatesResult(msgs=(), new_buf="buf-2", ok=False)]
    )

    assert process_updates(db, client) == []
    assert client.get_calls == ["buf-1"]
    assert _buf(db) == "buf-1"


def test_duplicate_inbound_token_does_not_flush_twice(db, monkeypatch):
    user = _user(db)
    _context(db, user, "ctx-old")
    _queue_row(db, user, "one")
    _queue_row(db, user, "two")

    calls: list[int] = []
    real_flush = poll.flush_user_queue

    def counting_flush(*args, **kwargs):
        calls.append(1)
        return real_flush(*args, **kwargs)

    monkeypatch.setattr(poll, "flush_user_queue", counting_flush)

    handle_inbound_text(db, user.id, "help", "ctx-duplicate")
    handle_inbound_text(db, user.id, "help", "ctx-duplicate")

    assert len(calls) == 1
    assert db.scalar(
        select(func.count())
        .select_from(WechatPushQueue)
        .where(WechatPushQueue.status == "sent")
    ) == 2


def test_duplicate_confirmation_does_not_insert_second_ledger(db):
    user = _user(db)
    item = PendingConfirmation(
        user_id=user.id,
        title="duplicate check",
        detail="p11f idempotency",
        amount_usdt=Decimal("10"),
        status="pending",
        suggested_category="deposit",
    )
    db.add(item)
    db.commit()
    db.refresh(item)

    first = handle_inbound_text(db, user.id, f"确认入账 #{item.id}", "")
    second = handle_inbound_text(db, user.id, f"确认入账 #{item.id}", "")

    assert "已确认" in first
    assert "已确认" in second
    assert db.scalar(select(func.count()).select_from(LedgerEntry)) == 1
