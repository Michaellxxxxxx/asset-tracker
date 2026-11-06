"""p10b: the scheduler must survive host suspend and never stay silently stuck.

All tests are offline: a fake wall clock, stub locks, and the shared SQLite
session fixture. No Redis or network is touched.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.auth import hash_password
from app.models import SyncRun, User
from app.sync import scheduler
from app.sync.scheduler import (
    _persist_run,
    reap_orphan_runs,
    reap_stale_runs,
    run_for_all_users,
    tick,
)


@dataclass
class StubLock:
    held: bool = False
    beats: int = 0
    released: list[str] = field(default_factory=list)
    ttls: list[int] = field(default_factory=list)
    beat_error: Exception | None = None

    def acquire(self, key: str, token: str, ttl_seconds: int) -> bool:
        if self.held:
            return False
        self.held = True
        self.ttls.append(ttl_seconds)
        return True

    def release(self, key: str, token: str) -> None:
        self.held = False
        self.released.append(token)

    def beat(self, key: str, ttl_seconds: int) -> None:
        if self.beat_error is not None:
            raise self.beat_error
        self.beats += 1


class FakeClock:
    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeStop:
    """A stop event whose wait advances the fake clock instead of sleeping."""

    def __init__(
        self,
        clock: FakeClock,
        *,
        after_waits: int | None = None,
        advance: float | None = None,
    ) -> None:
        self.clock = clock
        self.after_waits = after_waits
        self.advance = advance
        self.waits: list[float] = []
        self._set = False

    def is_set(self) -> bool:
        return self._set

    def set(self) -> None:
        self._set = True

    def wait(self, timeout: float) -> bool:
        self.waits.append(timeout)
        self.clock.advance(self.advance if self.advance is not None else timeout)
        if self.after_waits is not None and len(self.waits) >= self.after_waits:
            self._set = True
        return self._set


def _add_user(db: Session, username: str) -> User:
    user = User(username=username, hashed_password=hash_password("demo123"), is_admin=False)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _add_run(
    db: Session,
    *,
    status: str = "running",
    started_at: datetime | None = None,
    trigger: str = "scheduled",
) -> SyncRun:
    row = SyncRun(
        started_at=started_at or datetime.now(timezone.utc),
        status=status,
        trigger=trigger,
        user_count=1,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_loop_beats_repeatedly_while_waiting(monkeypatch, db):
    clock = FakeClock()
    stop = FakeStop(clock, after_waits=3)
    lock = StubLock()
    ticks: list[dict] = []
    monkeypatch.setattr(scheduler.time, "time", clock)
    monkeypatch.setattr(scheduler, "tick", lambda **kwargs: ticks.append(kwargs))

    scheduler.loop(
        lock=lock,
        interval_seconds=100,
        stop=stop,
        session_factory=lambda: db,
        slice_seconds=5,
    )

    assert len(ticks) == 1
    assert lock.beats == 3
    assert stop.waits == [5.0, 5.0, 5.0]


def test_loop_ticks_immediately_after_wall_clock_jump(monkeypatch, db, caplog):
    """A suspend makes time.time() jump; the next slice must tick at once."""
    clock = FakeClock()
    stop = FakeStop(clock, after_waits=10, advance=5000)
    lock = StubLock()
    ticks: list[dict] = []
    monkeypatch.setattr(scheduler.time, "time", clock)
    caplog.set_level(logging.INFO, logger="app.sync.scheduler")

    def fake_tick(**kwargs):
        ticks.append(kwargs)
        if len(ticks) == 2:
            stop.set()

    monkeypatch.setattr(scheduler, "tick", fake_tick)

    scheduler.loop(
        lock=lock,
        interval_seconds=100,
        stop=stop,
        session_factory=lambda: db,
        slice_seconds=5,
    )

    assert len(ticks) == 2
    assert len(stop.waits) == 1  # the suspend happened in the first slice
    assert stop.waits[0] == 5
    assert "scheduler resumed after gap of" in caplog.text


def test_loop_stop_event_exits_within_one_slice(monkeypatch, db):
    clock = FakeClock()
    stop = FakeStop(clock, after_waits=1)
    lock = StubLock()
    ticks: list[dict] = []
    monkeypatch.setattr(scheduler.time, "time", clock)
    monkeypatch.setattr(scheduler, "tick", lambda **kwargs: ticks.append(kwargs))

    scheduler.loop(
        lock=lock,
        interval_seconds=100,
        stop=stop,
        session_factory=lambda: db,
        slice_seconds=7,
    )

    assert len(ticks) == 1
    assert len(stop.waits) == 1
    assert stop.waits[0] <= 7


def test_loop_survives_a_failing_heartbeat(monkeypatch, db):
    clock = FakeClock()
    stop = FakeStop(clock, after_waits=2)
    lock = StubLock(beat_error=RuntimeError("redis unavailable"))
    ticks: list[dict] = []
    monkeypatch.setattr(scheduler.time, "time", clock)
    monkeypatch.setattr(scheduler, "tick", lambda **kwargs: ticks.append(kwargs))

    scheduler.loop(
        lock=lock,
        interval_seconds=100,
        stop=stop,
        session_factory=lambda: db,
        slice_seconds=4,
    )

    assert len(ticks) == 1
    assert len(stop.waits) == 2


def test_loop_survives_timeout_from_tick(monkeypatch, db):
    clock = FakeClock()
    stop = FakeStop(clock, after_waits=20)
    lock = StubLock()
    calls: list[int] = []
    monkeypatch.setattr(scheduler.time, "time", clock)

    def fake_tick(**kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError("sync timed out")
        stop.set()

    monkeypatch.setattr(scheduler, "tick", fake_tick)

    scheduler.loop(
        lock=lock,
        interval_seconds=10,
        stop=stop,
        session_factory=lambda: db,
        slice_seconds=5,
    )

    assert len(calls) == 2


def test_loop_logs_tick_failure_without_secrets(monkeypatch, db, caplog):
    clock = FakeClock()
    stop = FakeStop(clock, after_waits=1)
    lock = StubLock()
    monkeypatch.setattr(scheduler.time, "time", clock)
    caplog.set_level(logging.WARNING, logger="app.sync.scheduler")

    def fake_tick(**kwargs):
        raise RuntimeError("api_key=SECRET-VALUE")

    monkeypatch.setattr(scheduler, "tick", fake_tick)

    scheduler.loop(
        lock=lock,
        interval_seconds=100,
        stop=stop,
        session_factory=lambda: db,
        slice_seconds=5,
    )

    assert "scheduler tick failed" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "SECRET-VALUE" not in caplog.text
    assert "api_key=***" in caplog.text


def test_reap_orphan_runs_leaves_rows_when_lock_is_held(db):
    lock = StubLock(held=True)
    row = _add_run(db, status="running")

    assert reap_orphan_runs(lambda: db, lock) == 0
    db.refresh(row)
    assert row.status == "running"
    assert lock.released == []


def test_reap_orphan_runs_marks_running_and_releases_lock(db):
    lock = StubLock()
    old_started = datetime.now(timezone.utc) - timedelta(hours=16)
    first = _add_run(db, status="running", started_at=old_started)
    second = _add_run(db, status="running")
    done = _add_run(db, status="ok")
    failed = _add_run(db, status="error")

    assert reap_orphan_runs(lambda: db, lock, token="worker-a") == 2

    db.expire_all()
    for row in (db.get(SyncRun, first.id), db.get(SyncRun, second.id)):
        assert row.status == "error"
        assert row.error == "interrupted: worker restarted before this run finished"
        assert row.finished_at is not None
        assert row.duration_ms is not None and row.duration_ms >= 0
    assert db.get(SyncRun, done.id).status == "ok"
    assert db.get(SyncRun, failed.id).status == "error"
    assert lock.held is False
    assert lock.released == ["worker-a"]


def test_reap_stale_runs_only_fails_old_running_rows(db):
    now = datetime.now(timezone.utc)
    old = _add_run(db, status="running", started_at=now - timedelta(hours=2))
    fresh = _add_run(db, status="running", started_at=now - timedelta(minutes=5))
    done = _add_run(db, status="ok", started_at=now - timedelta(hours=2))

    assert reap_stale_runs(db, max_age_seconds=3600, now=now) == 1

    db.refresh(old)
    db.refresh(fresh)
    db.refresh(done)
    assert old.status == "error"
    assert old.error == "stale: run exceeded 3600s without finishing"
    assert scheduler._as_utc(old.finished_at) == now
    assert fresh.status == "running"
    assert done.status == "ok"


def test_tick_reaps_stale_rows_but_not_the_row_it_creates(db, demo_user):
    now = datetime.now(timezone.utc)
    old = _add_run(db, status="running", started_at=now - timedelta(hours=2))

    def sync(user_id: int, mode: str | None, *, db: Session) -> None:
        return None

    result = tick(
        lock=StubLock(),
        session_factory=lambda: db,
        sync=sync,
        stale_after_seconds=60,
    )

    assert result.status == "ok"
    db.expire_all()
    old = db.get(SyncRun, old.id)
    assert old.status == "error"
    assert old.error == "stale: run exceeded 60s without finishing"
    rows = db.query(SyncRun).order_by(SyncRun.id.asc()).all()
    assert [row.status for row in rows] == ["error", "ok"]


def test_persist_run_uses_wall_clock_duration(db):
    finished = datetime.now(timezone.utc)
    started = finished - timedelta(seconds=16 * 3600)
    # 176s of monotonic time is what the old code recorded; the wall clock spans 16h.
    row = _persist_run(
        db,
        started=started,
        status="ok",
        trigger="scheduled",
        user_count=1,
        error=None,
        finished=finished,
    )

    assert row.duration_ms == 16 * 3600 * 1000
    assert row.finished_at == finished


def test_on_progress_runs_before_and_after_every_user(db, demo_user):
    other = _add_user(db, "admin")
    events: list[tuple] = []
    seen: list[int] = []

    def progress() -> None:
        events.append(("progress",))

    def sync(user_id: int, mode: str | None, *, db: Session) -> None:
        seen.append(user_id)
        events.append(("sync", user_id))

    result = run_for_all_users(db, trigger="scheduled", sync=sync, on_progress=progress)

    assert result.status == "ok"
    assert seen == [demo_user.id, other.id]
    assert events == [
        ("progress",),
        ("sync", demo_user.id),
        ("progress",),
        ("progress",),
        ("sync", other.id),
        ("progress",),
    ]


def test_timeout_for_one_user_does_not_stop_the_round(db, demo_user):
    other = _add_user(db, "admin")
    seen: list[int] = []

    def sync(user_id: int, mode: str | None, *, db: Session) -> None:
        if user_id == demo_user.id:
            raise TimeoutError("adapter timed out")
        seen.append(user_id)

    result = run_for_all_users(db, trigger="scheduled", sync=sync)

    assert result.status == "error"
    assert "TimeoutError" in (result.error or "")
    assert seen == [other.id]
