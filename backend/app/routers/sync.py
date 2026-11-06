"""POST /api/sync and GET /api/sync/status."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..config import resolve_sync_mode, settings
from ..db import get_db
from ..models import SyncReconcileLog, User
from ..schemas import (
    BackfillOut,
    SyncAccepted,
    SyncJobStatus,
    SyncReconcileItemOut,
    SyncReconcileOut,
    SyncStats,
)
from ..snapshots_backfill import backfill_user
from ..sync.scheduler import latest_run, record_run
from ..sync.service import PartialSync, run_sync

router = APIRouter(prefix="/sync", tags=["sync"])


@router.post("", response_model=SyncStats | SyncAccepted)
def trigger_sync(
    async_: bool = Query(default=False, alias="async"),
    mode: str | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SyncStats | SyncAccepted:
    chosen = mode or settings.sync_mode
    try:
        resolve_sync_mode(chosen)
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    if async_:
        from ..sync.queue import enqueue_sync

        try:
            job_id = enqueue_sync(current_user.id, chosen)
        except Exception as exc:  # noqa: BLE001 - surface a clean 503 without leaking redis internals
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="同步队列不可用",
            ) from exc
        return SyncAccepted(job_id=job_id)

    try:
        result = record_run(
            db,
            trigger="manual",
            action=lambda: run_sync(current_user.id, chosen, db=db),
        )
    except PartialSync as exc:
        # Transfers landed and the run is recorded as partial. Still answer 200
        # so the UI refreshes; the status endpoint carries last_error.
        result = exc.result
    return SyncStats(**result.as_dict())


@router.get("/status", response_model=SyncJobStatus)
def sync_status(
    job_id: str | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SyncJobStatus:
    """Report one RQ job, or the latest recorded run when id is omitted.

    Omitting ``job_id`` answers from ``sync_runs`` alone. That table already
    holds every manual, scheduled and backfill execution, and reading it does
    not open Redis — a live worker blocked on the queue must not stall this
    call, and tests must not depend on the host's Redis. A specific ``job_id``
    still needs the queue; a dead queue then returns 503.
    """
    _ = current_user  # auth gate; job ids are unguessable UUIDs issued to the caller
    run = latest_run(db)
    if not job_id:
        return _idle(run)

    from ..sync.queue import fetch_job

    try:
        return _job_status(fetch_job(job_id), run)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="同步队列不可用",
        ) from exc


@router.get("/reconcile", response_model=SyncReconcileOut)
def sync_reconcile(
    limit: int = Query(default=20, ge=1, le=200),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SyncReconcileOut:
    """This user's recent auto-reconcile rows, newest first, plus latest status.

    Read-only and user-isolated. ``status`` is null until the first sync has
    written a snapshot with a reconcile row.
    """
    rows = list(
        db.scalars(
            select(SyncReconcileLog)
            .where(SyncReconcileLog.user_id == current_user.id)
            .order_by(SyncReconcileLog.id.desc())
            .limit(limit)
        ).all()
    )
    items = [SyncReconcileItemOut.model_validate(row) for row in rows]
    latest = items[0] if items else None
    return SyncReconcileOut(
        status=latest.status if latest is not None else None,
        latest=latest,
        items=items,
    )


@router.post("/backfill", response_model=BackfillOut)
def trigger_backfill(
    mode: str | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> BackfillOut:
    """Rebuild this user's daily NAV synchronously. Repeating it only updates."""
    chosen = mode or settings.sync_mode
    try:
        resolve_sync_mode(chosen)
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    result = record_run(
        db,
        trigger="backfill",
        action=lambda: backfill_user(db, current_user.id, mode=chosen),
    )
    return BackfillOut(
        created=result.created,
        updated=result.updated,
        from_=result.start,
        to=result.end,
    )


def _run_fields(run: object | None) -> dict[str, object]:
    if run is None:
        return {"interval_seconds": settings.sync_interval_seconds}
    finished = getattr(run, "finished_at", None) or getattr(run, "started_at", None)
    return {
        "last_run_at": finished,
        "last_status": getattr(run, "status", None),
        "last_error": getattr(run, "error", None),
        "last_trigger": getattr(run, "trigger", None),
        "interval_seconds": settings.sync_interval_seconds,
    }


def _idle(run: object | None) -> SyncJobStatus:
    return SyncJobStatus(status="idle", **_run_fields(run))


def _job_status(job: object, run: object | None = None) -> SyncJobStatus:
    status_name = job.get_status(refresh=True)  # type: ignore[attr-defined]
    label = status_name.value if hasattr(status_name, "value") else str(status_name)
    result = None
    raw = getattr(job, "result", None)
    if isinstance(raw, dict) and {"fetched", "enqueued", "duplicates"} <= raw.keys():
        result = SyncStats(**{key: int(raw[key]) for key in ("fetched", "enqueued", "duplicates")})
    if getattr(job, "is_failed", False):
        label = "failed"
    return SyncJobStatus(job_id=getattr(job, "id", None), status=label, result=result, **_run_fields(run))
