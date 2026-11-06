"""P11e: lease-based WeChat flush, unverified ret=-2, and redaction.

All sends are mocked; nothing here dials iLink or WeChat.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.db import Base
from app.integrations.wechat.client import SendResult, redact_send_text
from app.integrations.wechat.queue import (
    SEND_CLAIM_LEASE,
    UNVERIFIED_MAX_ATTEMPTS,
    UNVERIFIED_PREFIX,
    enqueue_alert,
    flush_user_queue,
    reset_retry_backoff,
)
from app.models import AlertEvent, User, WechatContextToken, WechatPushQueue

NOW = datetime(2026, 10, 28, 16, 0, 25, tzinfo=timezone.utc)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _user(db: Session, name: str) -> User:
    user = User(username=name, hashed_password="not-used")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _context(db: Session, user: User, token: str = "ctx-test") -> None:
    db.add(
        WechatContextToken(user_id=user.id, context_token=token, updated_at=NOW)
    )
    db.commit()


def _queue_row(
    db: Session,
    user: User,
    *,
    text: str = "p11e payload",
    status: str = "queued",
    attempts: int = 0,
    last_error: str | None = None,
    next_attempt_at: datetime | None = None,
) -> WechatPushQueue:
    row = WechatPushQueue(
        user_id=user.id,
        pending_id=None,
        alert_event_id=None,
        text=text,
        status=status,
        attempts=attempts,
        last_error=last_error,
        next_attempt_at=next_attempt_at,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


class _CountingSender:
    def __init__(self, result: SendResult | None = None) -> None:
        self.result = result or SendResult(ok=True, ret=0)
        self.calls = 0
        self.threads: list[int] = []

    def send_message(self, context_token: str, text: str) -> SendResult:
        del context_token, text
        self.calls += 1
        self.threads.append(threading.get_ident())
        # Hold the "network call" open so a second flush really overlaps; with
        # no lease it would see status='queued' and send the same row again.
        time.sleep(0.2)
        return self.result


class _OkRetMinusTwo:
    """HTTP/ok success with iLink ret=-2: may have been delivered."""

    def __init__(self) -> None:
        self.calls = 0

    def send_message(self, context_token: str, text: str) -> SendResult:
        del context_token, text
        self.calls += 1
        return SendResult(ok=True, ret=-2, error="ret=-2", errcode=-2, errmsg="ret=-2")


class _NotOkRetMinusTwo:
    """Ordinary stale-token answer: keep the P7a token-wait policy.

    The live client returns ok=False for every non-zero application code, but
    only knows this is a token answer when the response carries no HTTP status
    (legacy/mock shape).
    """

    def send_message(self, context_token: str, text: str) -> SendResult:
        del context_token, text
        return SendResult(ok=False, ret=-2, error="ret=-2")


class _NotOkRetMinusTwoHttp200:
    """HTTP 200 + ret=-2: the live-client shape that may have been delivered."""

    def __init__(self) -> None:
        self.calls = 0

    def send_message(self, context_token: str, text: str) -> SendResult:
        del context_token, text
        self.calls += 1
        return SendResult(
            ok=False,
            ret=-2,
            error="ret=-2",
            errcode=-2,
            errmsg="ret=-2",
            http_status=200,
        )


def _alert(db: Session, user: User) -> AlertEvent:
    event = AlertEvent(
        user_id=user.id,
        level="danger",
        created_at=NOW - timedelta(hours=1),
        channel_result="queued:test",
        sent_at=None,
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


# --------------------------------------------------------------------------
# ① Concurrent flush must not send the same row twice.
# --------------------------------------------------------------------------


def test_concurrent_flush_sends_each_row_once(tmp_path):
    """Two independent sessions/flushes race on one due row.

    A file-backed SQLite database is used because the normal test fixture uses
    one shared in-memory connection; this test needs real concurrent writers.
    """
    path = tmp_path / "p11e-concurrent.sqlite"
    engine = create_engine(
        f"sqlite+pysqlite:///{path}",
        connect_args={"check_same_thread": False, "timeout": 15},
        poolclass=NullPool,
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _busy_timeout(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA busy_timeout = 15000")
        cursor.close()

    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)

    setup = factory()
    try:
        user = _user(setup, "p11e-concurrent")
        _context(setup, user, "ctx-concurrent")
        row = _queue_row(setup, user, text="only once")
        user_id, row_id = user.id, row.id
    finally:
        setup.close()

    sender = _CountingSender()
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def _flush() -> None:
        session = factory()
        try:
            barrier.wait(timeout=5)
            flush_user_queue(session, user_id, client=sender, now=NOW)
        except BaseException as exc:  # noqa: BLE001 - surfaced by the main thread
            errors.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=_flush) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert not errors, errors
    assert sender.calls == 1
    check = factory()
    try:
        stored = check.get(WechatPushQueue, row_id)
        assert stored is not None
        assert stored.status == "sent"
        assert stored.attempts == 1
    finally:
        check.close()
        engine.dispose()


def test_crashed_lease_expires_and_row_is_reclaimed(db):
    user = _user(db, "p11e-lease-expiry")
    _context(db, user)
    row = _queue_row(
        db,
        user,
        text="after crash",
        attempts=1,
        last_error="sending:crashed-process",
        next_attempt_at=NOW - timedelta(seconds=1),
    )
    sender = _CountingSender()

    stats = flush_user_queue(db, user.id, client=sender, now=NOW)

    assert stats.sent == 1
    assert sender.calls == 1
    db.refresh(row)
    assert row.status == "sent"
    assert row.last_error is None
    assert row.next_attempt_at is None


def test_active_lease_is_not_reset_by_token_refresh(db):
    user = _user(db, "p11e-lease-reset")
    row = _queue_row(
        db,
        user,
        text="in flight",
        attempts=2,
        last_error="sending:still-running",
        next_attempt_at=NOW + SEND_CLAIM_LEASE,
    )

    assert reset_retry_backoff(db, user.id) == 0
    db.commit()
    db.refresh(row)
    assert row.attempts == 2
    assert row.last_error == "sending:still-running"
    assert _aware(row.next_attempt_at) == NOW + SEND_CLAIM_LEASE


# --------------------------------------------------------------------------
# ③ ret=-2 policy.
# --------------------------------------------------------------------------


def test_ok_ret_minus_two_fails_unverified_after_n(db):
    user = _user(db, "p11e-unverified")
    _context(db, user)
    event = _alert(db, user)
    row = enqueue_alert(
        db, alert_event_id=event.id, user_id=user.id, text="unverified payload"
    )
    db.commit()
    sender = _OkRetMinusTwo()

    moment = NOW
    for attempt in range(1, UNVERIFIED_MAX_ATTEMPTS + 1):
        stats = flush_user_queue(db, user.id, client=sender, now=moment)
        assert stats.sent == 0
        db.refresh(row)
        db.refresh(event)
        if attempt < UNVERIFIED_MAX_ATTEMPTS:
            assert row.status == "queued"
            assert row.attempts == attempt
            assert row.sent_at is None
            assert row.next_attempt_at is not None
            moment = _aware(row.next_attempt_at) + timedelta(seconds=1)
        else:
            assert row.status == "failed"
            assert row.attempts == UNVERIFIED_MAX_ATTEMPTS
            assert row.sent_at is None
            assert row.next_attempt_at is None
            assert (row.last_error or "").startswith(UNVERIFIED_PREFIX)
            assert "ret=-2" in (row.last_error or "")
            assert event.channel_result is not None
            assert event.channel_result.startswith("failed:")
            assert "unverified" in event.channel_result
            assert event.sent_at is None

    assert sender.calls == UNVERIFIED_MAX_ATTEMPTS

    # Terminal: no later flush may select it again.
    later = moment + timedelta(days=30)
    stats = flush_user_queue(db, user.id, client=sender, now=later)
    assert stats.sent == 0 and stats.failed == 0
    assert sender.calls == UNVERIFIED_MAX_ATTEMPTS
    db.refresh(row)
    assert row.status == "failed"


def test_not_ok_http_200_ret_minus_two_is_also_unverified(db):
    """The live client says ok=False for an HTTP-200 ret=-2; still bounded."""
    user = _user(db, "p11e-unverified-live-shape")
    _context(db, user)
    event = _alert(db, user)
    row = enqueue_alert(
        db, alert_event_id=event.id, user_id=user.id, text="live shape"
    )
    db.commit()
    sender = _NotOkRetMinusTwoHttp200()

    moment = NOW
    for attempt in range(1, UNVERIFIED_MAX_ATTEMPTS + 1):
        stats = flush_user_queue(db, user.id, client=sender, now=moment)
        assert stats.sent == 0
        db.refresh(row)
        db.refresh(event)
        if attempt < UNVERIFIED_MAX_ATTEMPTS:
            assert row.status == "queued"
            moment = _aware(row.next_attempt_at) + timedelta(seconds=1)
        else:
            assert row.status == "failed"
            assert (row.last_error or "").startswith(UNVERIFIED_PREFIX)
            assert event.channel_result is not None
            assert event.channel_result.startswith("failed:")
            assert "unverified" in event.channel_result

    assert sender.calls == UNVERIFIED_MAX_ATTEMPTS
    later = moment + timedelta(days=30)
    assert flush_user_queue(db, user.id, client=sender, now=later).sent == 0
    assert sender.calls == UNVERIFIED_MAX_ATTEMPTS


def test_not_ok_ret_minus_two_keeps_token_wait_policy(db):
    user = _user(db, "p11e-token-policy")
    _context(db, user)
    row = _queue_row(db, user, text="stale token")
    sender = _NotOkRetMinusTwo()

    moment = NOW
    for _ in range(5):
        stats = flush_user_queue(db, user.id, client=sender, now=moment)
        assert stats.failed == 1
        db.refresh(row)
        assert row.status == "queued"
        assert not (row.last_error or "").startswith(UNVERIFIED_PREFIX)
        moment = _aware(row.next_attempt_at) + timedelta(seconds=1)

    assert row.attempts == 5
    assert row.last_error == "ret=-2"


def test_ok_ret_minus_two_with_token_text_is_token_class(db):
    class _TokenText:
        def send_message(self, context_token: str, text: str) -> SendResult:
            del context_token, text
            return SendResult(
                ok=True, ret=-2, error="context_token expired", errmsg="context_token expired"
            )

    user = _user(db, "p11e-token-text")
    _context(db, user)
    row = _queue_row(db, user, text="token text")
    sender = _TokenText()

    moment = NOW
    for _ in range(UNVERIFIED_MAX_ATTEMPTS + 2):
        flush_user_queue(db, user.id, client=sender, now=moment)
        db.refresh(row)
        assert row.status == "queued"
        assert not (row.last_error or "").startswith(UNVERIFIED_PREFIX)
        moment = _aware(row.next_attempt_at) + timedelta(seconds=1)


# --------------------------------------------------------------------------
# ③ reset_retry_backoff must not revive terminal unverified rows.
# --------------------------------------------------------------------------


def test_reset_retry_backoff_does_not_revive_unverified_row(db):
    user = _user(db, "p11e-reset-unverified")
    row = _queue_row(
        db,
        user,
        status="failed",
        attempts=UNVERIFIED_MAX_ATTEMPTS,
        last_error="unverified: ret=-2 errcode=-2 errmsg=ret=-2",
        next_attempt_at=None,
    )

    assert reset_retry_backoff(db, user.id) == 0
    db.commit()
    db.refresh(row)
    assert row.status == "failed"
    assert row.attempts == UNVERIFIED_MAX_ATTEMPTS
    assert row.last_error == "unverified: ret=-2 errcode=-2 errmsg=ret=-2"

    sender = _CountingSender()
    stats = flush_user_queue(db, user.id, client=sender, now=NOW + timedelta(days=1))
    assert stats.sent == 0 and stats.failed == 0
    assert sender.calls == 0


# --------------------------------------------------------------------------
# ② Redaction.
# --------------------------------------------------------------------------


def test_redaction_removes_token_key_and_url_parameters():
    secret = "ctx-SUPERSECRET-0123456789abcdef0123456789abcdef"
    samples = [
        f"token={secret}",
        f"context_token={secret}",
        f"context_token: {secret}",
        f"api_key={secret}",
        f"Authorization: Bearer {secret}",
        f"https://ilink.example/send?access_token={secret}&x=1",
        f"sk-live-{secret}",
        f"0123456789abcdef0123456789abcdef0123456789abcdef",
    ]

    for sample in samples:
        cleaned = redact_send_text(sample, 200)
        assert secret not in cleaned
        assert len(cleaned) <= 200
        assert "SUPERSECRET" not in cleaned
        assert "0123456789abcdef0123456789abcdef" not in cleaned

    cleaned = redact_send_text("context_token=" + secret, 200)
    assert "context_token=***" in cleaned
    assert "?***" in redact_send_text(
        f"https://ilink.example/send?token={secret}", 200
    )


def test_send_failure_writes_redacted_summary(db):
    secret = "ctx-LEAK-0123456789abcdef0123456789abcdef"

    class _Leaky:
        def send_message(self, context_token: str, text: str) -> SendResult:
            del context_token, text
            return SendResult(
                ok=False,
                ret=400,
                error="bad_request",
                errcode=400,
                errmsg=f"token={secret} url=https://ilink.example/send?key={secret}",
            )

    user = _user(db, "p11e-leaky")
    _context(db, user)
    row = _queue_row(db, user, text="leaky")

    flush_user_queue(db, user.id, client=_Leaky(), now=NOW)

    db.refresh(row)
    error = row.last_error or ""
    assert "LEAK" not in error
    assert secret not in error
    assert "errcode=400" in error
    assert len(error) <= 200


def test_unverified_summary_is_truncated_to_200(db):
    class _Noisy:
        def send_message(self, context_token: str, text: str) -> SendResult:
            del context_token, text
            return SendResult(
                ok=True,
                ret=-2,
                error="ret=-2",
                errcode=-2,
                errmsg="x" * 500,
            )

    user = _user(db, "p11e-truncate")
    _context(db, user)
    row = _queue_row(db, user, text="truncate")
    sender = _Noisy()

    moment = NOW
    for _ in range(UNVERIFIED_MAX_ATTEMPTS):
        flush_user_queue(db, user.id, client=sender, now=moment)
        db.refresh(row)
        if row.status != "failed":
            moment = _aware(row.next_attempt_at) + timedelta(seconds=1)

    assert row.status == "failed"
    assert (row.last_error or "").startswith(UNVERIFIED_PREFIX)
    assert len(row.last_error or "") <= 200
