"""Scheduler tick: every user, recorded status, and a lock that blocks re-entry."""

from __future__ import annotations

from dataclasses import dataclass, field

import fakeredis
from sqlalchemy.orm import Session

from app.auth import hash_password
from app.models import SyncRun, User
from app.sync.scheduler import LOCK_KEY, RedisLock, run_for_all_users, tick


@dataclass
class StubLock:
    held: bool = False
    beats: int = 0
    released: list[str] = field(default_factory=list)

    def acquire(self, key: str, token: str, ttl_seconds: int) -> bool:
        if self.held:
            return False
        self.held = True
        return True

    def release(self, key: str, token: str) -> None:
        self.held = False
        self.released.append(token)

    def beat(self, key: str, ttl_seconds: int) -> None:
        self.beats += 1


def _add_user(db: Session, username: str) -> User:
    user = User(username=username, hashed_password=hash_password("demo123"), is_admin=False)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_tick_syncs_every_user_and_records_ok(db, demo_user: User):
    other = _add_user(db, "admin")
    seen: list[int] = []

    def sync(user_id: int, mode: str | None, *, db: Session) -> None:
        seen.append(user_id)

    result = run_for_all_users(db, trigger="scheduled", sync=sync)
    assert result.status == "ok"
    assert result.user_count == 2
    assert seen == [demo_user.id, other.id]
    row = db.query(SyncRun).one()
    assert row.status == "ok"
    assert row.trigger == "scheduled"
    assert row.error is None
    assert row.duration_ms is not None


def test_tick_records_the_failing_user_and_continues(db, demo_user: User):
    other = _add_user(db, "admin")

    def sync(user_id: int, mode: str | None, *, db: Session) -> None:
        if user_id == demo_user.id:
            raise RuntimeError("adapter down")

    result = run_for_all_users(db, trigger="scheduled", sync=sync)
    assert result.status == "error"
    assert result.user_count == 2
    assert "adapter down" in (result.error or "")
    assert f"user {demo_user.id}" in (result.error or "")
    assert f"user {other.id}" not in (result.error or "")
    row = db.query(SyncRun).one()
    assert row.status == "error"
    assert row.trigger == "scheduled"


def test_tick_skips_when_the_lock_is_held(db, demo_user: User):
    lock = StubLock(held=True)
    called = {"n": 0}

    def sync(user_id: int, mode: str | None, *, db: Session) -> None:
        called["n"] += 1

    result = tick(lock=lock, session_factory=lambda: db, sync=sync, token="worker-a")
    assert result.skipped is True
    assert called["n"] == 0
    assert db.query(SyncRun).count() == 0


def test_tick_acquires_and_releases_the_lock(db, demo_user: User):
    lock = StubLock()

    def sync(user_id: int, mode: str | None, *, db: Session) -> None:
        return None

    result = tick(lock=lock, session_factory=lambda: db, sync=sync, token="worker-a")
    assert result.skipped is False
    assert result.status == "ok"
    assert lock.beats == 1
    assert lock.released == ["worker-a"]
    assert lock.held is False


def test_redis_lock_rejects_a_second_holder():
    client = fakeredis.FakeRedis()
    first = RedisLock(client)
    second = RedisLock(client)
    assert first.acquire(LOCK_KEY, "a", 30) is True
    assert second.acquire(LOCK_KEY, "b", 30) is False
    second.release(LOCK_KEY, "b")
    assert client.get(LOCK_KEY) == b"a"
    first.release(LOCK_KEY, "a")
    assert client.get(LOCK_KEY) is None
    # A holder only deletes its own token.
    first.acquire(LOCK_KEY, "a", 30)
    second.release(LOCK_KEY, "b")
    assert client.get(LOCK_KEY) == b"a"
    first.beat("asset-tracker:worker:heartbeat", 30)
    assert client.get("asset-tracker:worker:heartbeat") is not None
