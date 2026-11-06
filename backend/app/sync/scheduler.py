"""Interval loop that syncs every user and records the run.

No rq-scheduler: a single thread waits on a wall-clock deadline and calls
:func:`tick`. Waiting is split into short slices that refresh the heartbeat, so
a suspended host resumes with an overdue tick instead of sleeping for another
whole interval. A Redis lock (``SET NX`` with a TTL) makes an overlapping tick a
no-op, so a slow sync cannot start a second one. The lock object is injectable
so tests drive it with fakeredis or a stub.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol

from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from ..config import settings
from ..db import SessionLocal
from ..models import SyncRun, User
from ..snapshots_backfill import backfill_user
from .service import PartialSync, run_sync

logger = logging.getLogger(__name__)

LOCK_KEY = "asset-tracker:sync:lock"
HEARTBEAT_KEY = "asset-tracker:worker:heartbeat"
_LOCK_TTL_FACTOR = 2
_LOCK_TTL_FLOOR_SECONDS = 600
# The scheduler sleeps in short slices so the wall clock, not a suspended
# monotonic clock, decides when the next tick is due. Each slice also refreshes
# the heartbeat, which keeps the healthcheck green while the loop is idle.
_BEAT_SLICE_SECONDS = 15.0
_STALE_RUN_FACTOR = 3
_STALE_RUN_FLOOR_SECONDS = 3600
_ORPHAN_ERROR = "interrupted: worker restarted before this run finished"
_RESUME_GAP_SECONDS = 2
_SENSITIVE_RE = re.compile(
    r"(?i)\b(api[_-]?key|key|secret|token|password|passphrase)\b(\s*[=:]\s*)\S+"
)
# Deadlock (40P01) and serialization failure (40001) abort the transaction, so
# the only safe recovery is to roll it back and run the whole user sync again.
# Three retries with a 1s/2s/4s backoff ride out a migration that is finishing.
_DEADLOCK_RETRY_DELAYS = (1.0, 2.0, 4.0)
_MAX_DEADLOCK_RETRIES = 3


class Lock(Protocol):
    def acquire(self, key: str, token: str, ttl_seconds: int) -> bool: ...

    def release(self, key: str, token: str) -> None: ...

    def beat(self, key: str, ttl_seconds: int) -> None: ...


class RedisLock:
    """SET NX PX lock plus a heartbeat key the compose healthcheck can read."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def acquire(self, key: str, token: str, ttl_seconds: int) -> bool:
        return bool(self._client.set(key, token, nx=True, px=ttl_seconds * 1000))

    def release(self, key: str, token: str) -> None:
        current = self._client.get(key)
        if isinstance(current, bytes):
            current = current.decode("utf-8", "replace")
        if current is not None and current == token:
            self._client.delete(key)

    def beat(self, key: str, ttl_seconds: int) -> None:
        self._client.set(key, str(int(time.time())), px=ttl_seconds * 1000)


@dataclass
class TickResult:
    status: str
    trigger: str
    user_count: int
    error: str | None
    duration_ms: int
    skipped: bool = False


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    """SQLite hands back naive datetimes even for ``DateTime(timezone=True)``."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _wall_duration_ms(started: datetime, finished: datetime) -> int:
    """Wall-clock duration, never negative.

    The monotonic clock stops while the host is suspended, so it reported 176s
    for a run that actually spanned sixteen hours. The persisted duration must
    follow the wall clock.
    """
    return max(0, int((_as_utc(finished) - _as_utc(started)).total_seconds() * 1000))


def _lock_ttl(interval: int) -> int:
    return max(interval * _LOCK_TTL_FACTOR, _LOCK_TTL_FLOOR_SECONDS)


def _stale_run_max_age(interval: int) -> int:
    return max(interval * _STALE_RUN_FACTOR, _STALE_RUN_FLOOR_SECONDS)


def _error_summary(exc: BaseException) -> str:
    """Short single-line exception text with obvious secrets masked."""
    text = str(exc).replace("\n", " ")
    text = _SENSITIVE_RE.sub(r"\1\2***", text)
    return text[:200]


def _pgcode(exc: BaseException) -> str | None:
    """Best-effort PostgreSQL SQLSTATE from SQLAlchemy/psycopg wrappers."""
    for candidate in (getattr(exc, "orig", None), exc):
        if candidate is None:
            continue
        code = getattr(candidate, "pgcode", None) or getattr(candidate, "sqlstate", None)
        if code:
            return str(code)
    return None


def _is_retryable_txn_error(exc: BaseException) -> bool:
    """True only for deadlock and serialization failures, never for timeouts."""
    if not isinstance(exc, OperationalError):
        return False
    code = _pgcode(exc)
    if code in {"40P01", "40001"}:
        return True
    # Some test doubles and drivers expose only the class/message. Keep this
    # narrow so a connection reset or lock_timeout is not retried forever.
    orig = getattr(exc, "orig", exc)
    text = f"{type(orig).__name__}: {orig}".lower()
    return any(
        marker in text
        for marker in (
            "deadlockdetected",
            "deadlock detected",
            "serializationfailure",
            "serialization failure",
        )
    )


def _run_user_transaction(
    db: Session,
    action: Callable[[], Any],
    *,
    user_id: int | None,
    sleep: Callable[[float], None],
    max_retries: int = _MAX_DEADLOCK_RETRIES,
) -> Any:
    """Run one user's sync, retrying the whole transaction after a deadlock.

    PostgreSQL marks the transaction aborted before the exception reaches us, so
    ``rollback`` is mandatory and happens before the backoff. Non-deadlock
    errors are re-raised untouched; the caller records the final failure.
    """
    retries = 0
    while True:
        try:
            return action()
        except Exception as exc:  # noqa: BLE001 - filter below
            if not _is_retryable_txn_error(exc) or retries >= max_retries:
                raise
            db.rollback()
            delay = _DEADLOCK_RETRY_DELAYS[min(retries, len(_DEADLOCK_RETRY_DELAYS) - 1)]
            retries += 1
            logger.warning(
                "sync %s hit a database transaction conflict (%s); "
                "retry %s/%s in %ss",
                f"user {user_id}" if user_id is not None else "action",
                _pgcode(exc) or type(exc).__name__,
                retries,
                max_retries,
                delay,
            )
            sleep(delay)


def _as_sleep(sleep: Callable[[float], None] | None) -> Callable[[float], None]:
    return sleep if sleep is not None else time.sleep


def _safe_beat(lock: Lock, ttl: int) -> None:
    try:
        lock.beat(HEARTBEAT_KEY, ttl)
    except Exception as exc:  # noqa: BLE001 - a Redis hiccup must not stop the loop
        logger.warning("scheduler heartbeat failed (%s: %s)", type(exc).__name__, _error_summary(exc))


def _notify_progress(on_progress: Callable[[], None] | None) -> None:
    if on_progress is None:
        return
    try:
        on_progress()
    except Exception as exc:  # noqa: BLE001 - progress reporting must not abort a sync
        logger.warning("scheduler progress callback failed (%s: %s)", type(exc).__name__, _error_summary(exc))


def latest_run(db: Session) -> SyncRun | None:
    """Most recent run across every user and every trigger, not one caller's jobs."""
    return db.scalar(select(SyncRun).order_by(SyncRun.id.desc()).limit(1))


def _begin_run(
    db: Session,
    *,
    started: datetime,
    trigger: str,
    user_count: int = 0,
) -> SyncRun:
    """Insert the ``running`` row before the action.

    The id exists while positions are written, and ``run_sync`` reads it from
    ``db.info``. ``_persist_run`` later updates this same row instead of adding
    a second one, so the status table keeps one row per execution.
    """
    row = SyncRun(
        started_at=started,
        status="running",
        trigger=trigger,
        user_count=user_count,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    db.info["sync_run_id"] = row.id
    return row


def record_run(
    db: Session,
    *,
    trigger: str,
    action: Callable[[], Any],
    user_count: int = 1,
    sleep: Callable[[float], None] | None = None,
) -> Any:
    """Persist one ``sync_runs`` row around ``action``.

    The row is created first (status ``running``) so the action can stamp the
    positions it writes with the run id. It is finalized before ``action``'s
    exception is re-raised, so a failed manual sync or backfill still shows up
    in ``GET /api/sync/status``.
    """
    started = _now()
    begun = time.monotonic()
    run = _begin_run(db, started=started, trigger=trigger, user_count=user_count)
    try:
        result = _run_user_transaction(
            db,
            action,
            user_id=None,
            sleep=_as_sleep(sleep),
        )
    except PartialSync as exc:
        # The action already committed the transfers it could. Record partial,
        # then re-raise so the caller can still return the sync counts.
        message = _error_summary(exc)[:500]
        _persist_run(
            db,
            started=started,
            begun=begun,
            status="partial",
            trigger=trigger,
            user_count=user_count,
            error=message,
            run_id=run.id,
        )
        raise
    except Exception as exc:
        db.rollback()
        message = f"{type(exc).__name__}: {_error_summary(exc)}"[:500]
        _persist_run(
            db,
            started=started,
            begun=begun,
            status="error",
            trigger=trigger,
            user_count=user_count,
            error=message,
            run_id=run.id,
        )
        raise
    _persist_run(
        db,
        started=started,
        begun=begun,
        status="ok",
        trigger=trigger,
        user_count=user_count,
        error=_skipped_error(result),
        run_id=run.id,
    )
    return result


def _skipped_error(result: Any) -> str | None:
    """Empty-key venues are not a failure, but the reason is still recorded."""
    skipped = getattr(result, "skipped", None)
    if not skipped:
        return None
    text = "; ".join(str(item) for item in skipped)
    note = text if text.startswith("skipped:") else f"skipped: {text}"
    return note[:500]


def _persist_run(
    db: Session,
    *,
    started: datetime,
    begun: float | None = None,
    status: str,
    trigger: str,
    user_count: int,
    error: str | None,
    run_id: int | None = None,
    finished: datetime | None = None,
) -> SyncRun:
    """Finalize one run row. ``duration_ms`` is the wall-clock difference.

    ``begun`` (a monotonic timestamp) is accepted for callers that still pass it
    but is deliberately not used for the persisted value: monotonic time does
    not advance while the host is suspended.
    """
    finished = finished or _now()
    duration_ms = _wall_duration_ms(started, finished)
    row = db.get(SyncRun, run_id) if run_id is not None else None
    if row is None:
        row = SyncRun(
            started_at=started,
            finished_at=finished,
            status=status,
            trigger=trigger,
            user_count=user_count,
            error=error,
            duration_ms=duration_ms,
        )
        db.add(row)
    else:
        row.finished_at = finished
        row.status = status
        row.trigger = trigger
        row.user_count = user_count
        row.error = error
        row.duration_ms = duration_ms
    db.commit()
    return row


def _mark_interrupted(row: SyncRun, now: datetime, message: str) -> None:
    row.status = "error"
    row.finished_at = now
    row.error = message[:500]
    row.duration_ms = _wall_duration_ms(row.started_at, now)


def reap_orphan_runs(
    session_factory: Callable[[], Session],
    lock: Lock,
    *,
    token: str = "scheduler",
    ttl_seconds: int | None = None,
) -> int:
    """Mark ``running`` rows left by a previous worker process as ``error``.

    Only the lock holder may reap: if another tick is alive it owns the lock and
    a ``running`` row may be legitimate. The reaper releases the lock with the
    same token immediately afterwards, so the first tick can acquire it.
    """
    interval = max(settings.sync_interval_seconds, 1)
    ttl = ttl_seconds if ttl_seconds is not None else _lock_ttl(interval)
    if not lock.acquire(LOCK_KEY, token, ttl):
        return 0
    try:
        db = session_factory()
        try:
            now = _now()
            rows = list(db.scalars(select(SyncRun).where(SyncRun.status == "running")).all())
            for row in rows:
                _mark_interrupted(row, now, _ORPHAN_ERROR)
            if rows:
                db.commit()
            logger.info("scheduler reaped %s orphan run(s)", len(rows))
            return len(rows)
        finally:
            db.close()
    finally:
        lock.release(LOCK_KEY, token)


def reap_stale_runs(
    db: Session,
    *,
    max_age_seconds: int | None = None,
    now: datetime | None = None,
) -> int:
    """Fail ``running`` rows older than ``max_age_seconds``.

    Manual and backfill rows share the threshold. The caller runs this before
    creating the current tick's row, so that row can never be reaped here.
    """
    if max_age_seconds is None:
        max_age_seconds = _stale_run_max_age(max(settings.sync_interval_seconds, 1))
    max_age = max(int(max_age_seconds), 0)
    current = now or _now()
    cutoff = current - timedelta(seconds=max_age)
    rows = list(db.scalars(select(SyncRun).where(SyncRun.status == "running")).all())
    stale = [row for row in rows if _as_utc(row.started_at) < cutoff]
    message = f"stale: run exceeded {max_age}s without finishing"
    for row in stale:
        _mark_interrupted(row, current, message)
    if stale:
        db.commit()
        logger.info("scheduler reaped %s stale run(s) older than %ss", len(stale), max_age)
    return len(stale)


def run_for_all_users(
    db: Session,
    *,
    trigger: str,
    mode: str | None = None,
    sync: Callable[..., Any] = run_sync,
    on_progress: Callable[[], None] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> TickResult:
    """Sync every user, then persist one ``sync_runs`` row.

    A failing user is recorded and the loop continues with the rest. The run is
    ``error`` when any user failed, ``ok`` otherwise. ``trigger`` is
    ``manual``, ``scheduled`` or ``backfill``. ``on_progress`` runs before and
    after each user so the caller can refresh its heartbeat; a hung single user
    stops the heartbeat, which is the intended deadlock signal.
    """
    started = _now()
    begun = time.monotonic()
    run = _begin_run(db, started=started, trigger=trigger)
    errors: list[str] = []
    partials: list[str] = []
    notes: list[str] = []
    # Read ids, then end the read transaction. Holding ACCESS SHARE on ``users``
    # across the network-bound sync below is what let startup migrate (which
    # needs ACCESS EXCLUSIVE on ``users``) deadlock with pending writes in id168.
    # Commit (not rollback) because the session is configured with
    # expire_on_commit=False; rollback would expire caller-owned SyncRun rows.
    user_ids = list(db.scalars(select(User.id).order_by(User.id.asc())).all())
    db.commit()
    sleep_fn = _as_sleep(sleep)
    for user_id in user_ids:
        _notify_progress(on_progress)
        try:
            if trigger == "backfill":
                outcome = _run_user_transaction(
                    db,
                    lambda: backfill_user(db, user_id, mode=mode),
                    user_id=user_id,
                    sleep=sleep_fn,
                )
            else:
                outcome = _run_user_transaction(
                    db,
                    lambda: sync(user_id, mode, db=db),
                    user_id=user_id,
                    sleep=sleep_fn,
                )
            note = _skipped_error(outcome)
            if note:
                notes.append(note)
        except PartialSync as exc:
            # Transfers were committed; the snapshot or a side source was not.
            # Not a hard failure, but the run must say so.
            partials.append(f"user {user_id}: {_error_summary(exc)}"[:200])
            logger.warning("sync partial for user %s", user_id)
        except Exception as exc:  # noqa: BLE001 - one user must not abort the round
            db.rollback()
            errors.append(f"user {user_id}: {type(exc).__name__}: {_error_summary(exc)}"[:200])
            logger.warning("scheduled sync failed for user %s (%s)", user_id, type(exc).__name__)
        finally:
            _notify_progress(on_progress)

    if errors:
        status = "error"
    elif partials:
        status = "partial"
    else:
        status = "ok"
    errors.extend(partials)
    errors.extend(notes)
    message = "; ".join(errors)[:500] if errors else None
    row = _persist_run(
        db,
        started=started,
        begun=begun,
        status=status,
        trigger=trigger,
        user_count=len(user_ids),
        error=message,
        run_id=run.id,
    )
    return TickResult(
        status=status,
        trigger=trigger,
        user_count=len(user_ids),
        error=message,
        duration_ms=row.duration_ms or 0,
    )


def tick(
    *,
    lock: Lock,
    trigger: str = "scheduled",
    mode: str | None = None,
    session_factory: Callable[[], Session] = SessionLocal,
    sync: Callable[..., Any] = run_sync,
    token: str = "scheduler",
    on_progress: Callable[[], None] | None = None,
    stale_after_seconds: int | None = None,
    sleep: Callable[[float], None] | None = None,
) -> TickResult:
    """One pass. Returns a skipped result when another pass holds the lock."""
    interval = max(settings.sync_interval_seconds, 1)
    ttl = _lock_ttl(interval)
    if not lock.acquire(LOCK_KEY, token, ttl):
        return TickResult(status="ok", trigger=trigger, user_count=0, error=None, duration_ms=0, skipped=True)
    try:
        _safe_beat(lock, ttl)
        db = session_factory()
        try:
            max_age = stale_after_seconds if stale_after_seconds is not None else _stale_run_max_age(interval)
            try:
                reap_stale_runs(db, max_age_seconds=max_age)
            except Exception as exc:  # noqa: BLE001 - reaping must not skip the sync
                db.rollback()
                logger.warning("scheduler stale-run reap failed (%s: %s)", type(exc).__name__, _error_summary(exc))
            return run_for_all_users(
                db,
                trigger=trigger,
                mode=mode,
                sync=sync,
                on_progress=on_progress,
                sleep=sleep,
            )
        finally:
            db.close()
    finally:
        lock.release(LOCK_KEY, token)


def loop(
    *,
    lock: Lock,
    interval_seconds: int | None = None,
    stop: threading.Event | None = None,
    session_factory: Callable[[], Session] = SessionLocal,
    token: str = "scheduler",
    slice_seconds: float | None = None,
) -> None:
    """Tick on a wall-clock schedule, refreshing the heartbeat while idle.

    Each wait is split into short slices so a host suspend cannot hide the next
    due time from the loop: ``time.time()`` jumps forward on resume, the slice
    exits, and the overdue tick runs immediately.
    """
    interval = max(interval_seconds if interval_seconds is not None else settings.sync_interval_seconds, 1)
    slice_seconds = slice_seconds if slice_seconds is not None else _BEAT_SLICE_SECONDS
    slice_seconds = max(float(slice_seconds), 0.01)
    flag = stop or threading.Event()
    ttl = _lock_ttl(interval)
    if flag.is_set():
        return
    try:
        reap_orphan_runs(session_factory, lock, token=token, ttl_seconds=ttl)
    except Exception as exc:  # noqa: BLE001 - startup reaping must not stop the loop
        logger.warning("scheduler orphan-run reap failed (%s: %s)", type(exc).__name__, _error_summary(exc))

    next_due = time.time()
    last_tick_end: float | None = None
    while not flag.is_set():
        now = time.time()
        if now >= next_due:
            if last_tick_end is not None:
                gap = now - last_tick_end
                if gap > interval * _RESUME_GAP_SECONDS:
                    logger.info("scheduler resumed after gap of %ss", int(gap))
            try:
                tick(
                    lock=lock,
                    trigger="scheduled",
                    session_factory=session_factory,
                    token=token,
                    on_progress=lambda: _safe_beat(lock, ttl),
                )
            except Exception as exc:  # noqa: BLE001 - the loop survives a broken tick
                logger.warning(
                    "scheduler tick failed (%s: %s)",
                    type(exc).__name__,
                    _error_summary(exc),
                )
            last_tick_end = time.time()
            next_due = last_tick_end + interval
            continue

        remaining = next_due - now
        _safe_beat(lock, ttl)
        flag.wait(min(slice_seconds, max(remaining, 0.0)))
