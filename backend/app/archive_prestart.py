"""Archive pending confirmations that occurred before a user's returns_start_date.

Occurrence time is ``COALESCE(ts, created_at)``: sync rows carry the event
timestamp in ``ts``; phase-1 rows only have ``created_at``. The cutoff is
00:00 Asia/Shanghai on ``returns_start_date``, matching ``returns_start_utc``.

Only ``status='pending'`` rows are touched. Confirmed / rejected stay put.
Users without a baseline are skipped. Safe to re-run (idempotent).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .models import PendingConfirmation, User

_SHANGHAI = ZoneInfo("Asia/Shanghai")
ARCHIVE_REASON = "起点前自动归档"


@dataclass(frozen=True)
class ArchiveResult:
    users_scanned: int
    archived: int
    by_user: dict[str, int]


def _cutoff_utc(start_date) -> datetime:
    midnight = datetime(start_date.year, start_date.month, start_date.day, tzinfo=_SHANGHAI)
    return midnight.astimezone(timezone.utc)


def archive_prestart_pending(db: Session) -> ArchiveResult:
    """Reject pending rows whose occurrence is strictly before each user's baseline."""
    users = list(
        db.scalars(select(User).where(User.returns_start_date.is_not(None)).order_by(User.id)).all()
    )
    by_user: dict[str, int] = {}
    total = 0
    for user in users:
        assert user.returns_start_date is not None
        cutoff = _cutoff_utc(user.returns_start_date)
        # SQLite and Postgres both accept COALESCE in UPDATE...WHERE via ORM filter.
        occurred = func.coalesce(PendingConfirmation.ts, PendingConfirmation.created_at)
        stmt = (
            update(PendingConfirmation)
            .where(
                PendingConfirmation.user_id == user.id,
                PendingConfirmation.status == "pending",
                occurred < cutoff,
            )
            .values(status="rejected", suggested_reason=ARCHIVE_REASON)
        )
        result = db.execute(stmt)
        count = int(result.rowcount or 0)
        by_user[user.username] = count
        total += count
    db.commit()
    return ArchiveResult(users_scanned=len(users), archived=total, by_user=by_user)
