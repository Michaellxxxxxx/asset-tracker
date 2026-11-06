"""P11c2: a WeChat row is ``sent`` only on an explicit success, and a failed
session never hands an INTRANS connection back to the shared Engine pool.

Every send is mocked; nothing here dials iLink or WeChat.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.db import release_failed_session
from app.integrations.wechat import poll as poll_module
from app.integrations.wechat import worker
from app.integrations.wechat.client import SendResult
from app.integrations.wechat.queue import (
    enqueue_alert,
    flush_user_queue,
    is_delivery_success,
)
from app.models import AlertEvent, User, WechatContextToken
from app.sync import jobs

NOW = datetime(2026, 10, 28, 16, 0, 25, tzinfo=timezone.utc)


class _RejectedToken:
    """ok=False, ret=-2: the ordinary stale-token answer."""

    def send_message(self, context_token: str, text: str) -> SendResult:
        del context_token, text
        return SendResult(ok=False, ret=-2, error="ret=-2")


class _OkButTokenError:
    """ok=True with ret=-2: the 200-empty-body / refresh-window shape.

    This is the exact shape that marked the nine backlog rows as sent; it must
    never pass the guard even though ``ok`` is true.
    """

    def send_message(self, context_token: str, text: str) -> SendResult:
        del context_token, text
        return SendResult(ok=True, ret=-2, error="ret=-2")


class _OkNoErrorText:
    """ok=True, ret=-2, no error text: reason must be synthesised, not lost."""

    def send_message(self, context_token: str, text: str) -> SendResult:
        del context_token, text
        return SendResult(ok=True, ret=-2)


class _Delivered:
    """A real success: ok=True, ret=0."""

    def send_message(self, context_token: str, text: str) -> SendResult:
        del context_token, text
        return SendResult(ok=True, ret=0)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _user(db, name: str) -> User:
    user = User(username=name, hashed_password="not-used")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _seed(db, name: str):
    user = _user(db, name)
    db.add(
        WechatContextToken(
            user_id=user.id, context_token="test-only-token", updated_at=NOW
        )
    )
    event = AlertEvent(
        user_id=user.id,
        level="danger",
        created_at=NOW - timedelta(hours=1),
        channel_result="queued:test",
        sent_at=None,
    )
    db.add(event)
    db.flush()
    row = enqueue_alert(
        db, alert_event_id=event.id, user_id=user.id, text="p11c2 payload"
    )
    db.commit()
    return user, event, row


def test_delivery_guard_rejects_not_ok_token_error() -> None:
    assert is_delivery_success(SendResult(ok=False, ret=-2, error="ret=-2")) is False
    assert is_delivery_success(SendResult(ok=True, ret=-2, error="ret=-2")) is False
    assert is_delivery_success(SendResult(ok=True, ret=-2)) is False
    assert is_delivery_success(SendResult(ok=True, ret=0)) is True
    assert is_delivery_success(SendResult(ok=True, ret=None)) is True


def test_not_ok_ret_minus_two_is_not_sent(db):
    user, event, row = _seed(db, "p11c2-not-ok")

    stats = flush_user_queue(db, user.id, client=_RejectedToken(), now=NOW)

    assert stats.sent == 0 and stats.failed == 1
    db.refresh(row)
    db.refresh(event)
    assert row.status == "queued"
    assert row.attempts == 1
    assert row.sent_at is None
    assert row.last_error == "ret=-2"
    assert row.next_attempt_at is not None
    assert event.channel_result == "queued:ret=-2"
    assert event.sent_at is None


def test_ok_with_ret_minus_two_is_not_sent(db):
    user, event, row = _seed(db, "p11c2-ok-ret-2")

    stats = flush_user_queue(db, user.id, client=_OkButTokenError(), now=NOW)

    assert stats.sent == 0 and stats.failed == 1
    db.refresh(row)
    db.refresh(event)
    assert row.status == "queued"  # token wait, never sent
    assert row.attempts == 1
    assert row.sent_at is None
    assert row.last_error == "ret=-2"
    assert event.channel_result == "queued:ret=-2"
    assert event.sent_at is None


def test_ok_without_error_text_still_records_ret_minus_two(db):
    user, event, row = _seed(db, "p11c2-ok-no-text")

    stats = flush_user_queue(db, user.id, client=_OkNoErrorText(), now=NOW)

    assert stats.sent == 0 and stats.failed == 1
    db.refresh(row)
    db.refresh(event)
    assert row.status == "queued"
    assert row.sent_at is None
    assert row.last_error == "ret=-2"
    assert event.channel_result == "queued:ret=-2"
    assert event.sent_at is None


def test_ok_ret_zero_is_sent(db):
    user, event, row = _seed(db, "p11c2-ok-zero")

    stats = flush_user_queue(db, user.id, client=_Delivered(), now=NOW)

    assert stats.sent == 1 and stats.failed == 0
    db.refresh(row)
    db.refresh(event)
    assert row.status == "sent"
    assert row.attempts == 1
    assert row.last_error is None
    assert _aware(row.sent_at) == NOW
    assert row.next_attempt_at is None
    assert event.channel_result == "sent"
    assert _aware(event.sent_at) == NOW


class _FakeConnection:
    def __init__(self, *, invalidated: bool = False) -> None:
        self.invalidated = invalidated


class _FakeSession:
    def __init__(
        self,
        *,
        rollback_error: Exception | None = None,
        connection: object | None = None,
        connection_error: Exception | None = None,
    ) -> None:
        self.rollbacks = 0
        self.invalidates = 0
        self.closes = 0
        self._rollback_error = rollback_error
        self._connection = connection
        self._connection_error = connection_error

    def connection(self):
        if self._connection_error is not None:
            raise self._connection_error
        return self._connection

    def rollback(self) -> None:
        self.rollbacks += 1
        if self._rollback_error is not None:
            raise self._rollback_error

    def invalidate(self) -> None:
        self.invalidates += 1

    def close(self) -> None:
        self.closes += 1


class _FakeSessionFactory:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    def __enter__(self) -> _FakeSession:
        return self._session

    def __exit__(self, *exc_info) -> bool:
        return False


def test_release_failed_session_rolls_back_clean_connection() -> None:
    session = _FakeSession(connection=_FakeConnection())

    release_failed_session(session, label="test")

    assert session.rollbacks == 1
    assert session.invalidates == 0


def test_release_failed_session_invalidates_when_rollback_fails() -> None:
    session = _FakeSession(
        rollback_error=RuntimeError("connection already closed"),
        connection=_FakeConnection(),
    )

    release_failed_session(session, label="test")

    assert session.rollbacks == 1
    assert session.invalidates == 1


def test_release_failed_session_invalidates_flagged_connection() -> None:
    session = _FakeSession(connection=_FakeConnection(invalidated=True))

    release_failed_session(session, label="test")

    assert session.rollbacks == 1
    assert session.invalidates == 1


class _StopLoop(Exception):
    pass


def _raise_boom(*_args, **_kwargs):
    raise RuntimeError("boom")


def _stop(_seconds: float) -> None:
    raise _StopLoop


def test_flush_loop_rolls_back_after_error(monkeypatch) -> None:
    session = _FakeSession(connection=_FakeConnection())
    monkeypatch.setattr(worker, "SessionLocal", lambda: session)
    monkeypatch.setattr(worker, "flush_all_queued", _raise_boom)
    monkeypatch.setattr(worker.time, "sleep", _stop)

    with pytest.raises(_StopLoop):
        worker._flush_loop(30)

    assert session.rollbacks == 1
    assert session.closes == 1


def test_poll_loop_rolls_back_and_invalidates_after_error(monkeypatch) -> None:
    session = _FakeSession(connection=_FakeConnection(invalidated=True))
    monkeypatch.setattr(worker, "SessionLocal", lambda: session)
    monkeypatch.setattr(worker, "get_wechat_client", lambda: object())
    monkeypatch.setattr(poll_module, "process_updates", _raise_boom)
    monkeypatch.setattr(worker.time, "sleep", _stop)
    monkeypatch.setattr(worker.time, "monotonic", lambda: 0.0)

    with pytest.raises(_StopLoop):
        worker._poll_loop()

    assert session.rollbacks == 1
    assert session.invalidates == 1
    assert session.closes == 1


def test_run_sync_job_rolls_back_on_unexpected_error(monkeypatch) -> None:
    session = _FakeSession(connection=_FakeConnection())
    monkeypatch.setattr(jobs, "SessionLocal", lambda: _FakeSessionFactory(session))

    def _boom(*args, **kwargs):
        raise RuntimeError("pending rollback")

    monkeypatch.setattr(jobs, "record_run", _boom)

    with pytest.raises(RuntimeError, match="pending rollback"):
        jobs.run_sync_job(1, "mock")

    assert session.rollbacks == 1
    assert session.invalidates == 0
