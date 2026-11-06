"""p29: WECHAT_FLUSH_MIN_ID — rows with id <= N are never claimed, flushed,
reset or released. Mock sender only; no network."""

from __future__ import annotations

from datetime import datetime, timezone

import dataclasses

import pytest

from app.config import settings
from app.integrations.wechat.client import SendResult
from app.integrations.wechat.health import HEALTH_ALERT_HOLD, HEALTH_ALERT_HOLD_UNTIL
from app.integrations.wechat import queue as q
from app.models import User, WechatContextToken, WechatPushQueue

NOW = datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)


class MockWechatClient:  # name matters: queue treats it as mock mode
    def __init__(self) -> None:
        self.texts: list[str] = []

    def send_text(self, *args, **kwargs):
        self.texts.append(kwargs.get("text") or (args[-1] if args else ""))
        return SendResult(ok=True, ret=0)

    def __getattr__(self, name):  # tolerate other send_* entry points
        if name.startswith("send"):
            return self.send_text
        raise AttributeError(name)


def _user(db):
    u = User(username="u29", hashed_password="x")
    db.add(u)
    db.commit()
    db.add(WechatContextToken(user_id=u.id, context_token="ctx", updated_at=NOW))
    db.commit()
    return u


def _row(db, u, text, **kw):
    r = WechatPushQueue(user_id=u.id, text=text, status=kw.pop("status", "queued"),
                        attempts=kw.pop("attempts", 0), **kw)
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


def _snap(db, rid):
    r = db.get(WechatPushQueue, rid)
    db.refresh(r)
    return (r.status, r.attempts, r.next_attempt_at, r.last_error)


@pytest.fixture()
def min_id(monkeypatch):
    def _set(v):
        monkeypatch.setattr(q, "_settings", dataclasses.replace(settings, wechat_flush_min_id=v))
    return _set


def test_flush_entrypoints_skip_old_rows(db, min_id):
    u = _user(db)
    old = _row(db, u, "old-queued")
    old_failed = _row(db, u, "old-failed", status="failed", attempts=3, last_error="x")
    min_id(old_failed.id)
    before = (_snap(db, old.id), _snap(db, old_failed.id))
    new = _row(db, u, "new-queued")
    sender = MockWechatClient()
    q.flush_all_queued(db, client=sender)
    q.flush_user_queue(db, u.id, client=sender, now=NOW)
    assert (_snap(db, old.id), _snap(db, old_failed.id)) == before
    assert db.get(WechatPushQueue, new.id).status == "sent"
    assert all("old" not in (t or "") for t in sender.texts)


def test_flush_all_skips_user_with_only_old_rows(db, min_id):
    u = _user(db)
    old = _row(db, u, "old-queued")
    min_id(old.id)
    sender = MockWechatClient()
    stats = q.flush_all_queued(db, client=sender)
    assert stats.sent == 0 and sender.texts == []
    assert db.get(WechatPushQueue, old.id).status == "queued"


def test_reset_and_release_skip_old_rows(db, min_id):
    u = _user(db)
    old = _row(db, u, "old-held", last_error=HEALTH_ALERT_HOLD,
               next_attempt_at=HEALTH_ALERT_HOLD_UNTIL, attempts=2)
    min_id(old.id)
    new = _row(db, u, "new-held", last_error=HEALTH_ALERT_HOLD,
               next_attempt_at=HEALTH_ALERT_HOLD_UNTIL, attempts=2)
    before = _snap(db, old.id)
    assert q.release_health_alerts(db) == 1
    assert q.reset_retry_backoff(db, u.id) == 1
    db.commit()
    assert _snap(db, old.id) == before
    n = db.get(WechatPushQueue, new.id)
    assert n.attempts == 0 and n.next_attempt_at is None


def test_min_id_zero_unchanged(db, min_id):
    min_id(0)
    u = _user(db)
    a = _row(db, u, "a")
    held = _row(db, u, "h", last_error=HEALTH_ALERT_HOLD, next_attempt_at=HEALTH_ALERT_HOLD_UNTIL)
    assert q.release_health_alerts(db) == 1
    db.commit()
    sender = MockWechatClient()
    q.flush_all_queued(db, client=sender)
    assert db.get(WechatPushQueue, a.id).status == "sent"
    assert db.get(WechatPushQueue, held.id).status == "sent"
