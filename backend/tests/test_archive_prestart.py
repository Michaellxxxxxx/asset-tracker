"""三期·11 A: archive pending rows before returns_start_date."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.archive_prestart import ARCHIVE_REASON, archive_prestart_pending
from app.auth import hash_password
from app.models import PendingConfirmation, User

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _user(db, name: str, start: date | None) -> User:
    user = User(username=name, hashed_password=hash_password("x"), returns_start_date=start)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _pending(db, user: User, *, ts: datetime | None, created: datetime, status: str = "pending", title: str = "t") -> PendingConfirmation:
    row = PendingConfirmation(
        user_id=user.id,
        title=title,
        detail="d",
        status=status,
        ts=ts,
        created_at=created,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_archives_only_pending_before_start(db):
    admin = _user(db, "admin_a", date(2026, 10, 23))
    demo = _user(db, "demo_a", None)
    cutoff = datetime(2026, 10, 23, tzinfo=SHANGHAI).astimezone(timezone.utc)
    before = cutoff - timedelta(days=3)
    after = cutoff + timedelta(hours=1)  # strictly after baseline midnight
    recent_created = datetime(2026, 10, 24, tzinfo=timezone.utc)

    old = _pending(db, admin, ts=before, created=recent_created, title="old")
    keep = _pending(db, admin, ts=after, created=recent_created, title="keep")
    confirmed = _pending(db, admin, ts=before, created=recent_created, status="confirmed", title="conf")
    rejected = _pending(db, admin, ts=before, created=recent_created, status="rejected", title="rej")
    demo_old = _pending(db, demo, ts=before, created=recent_created, title="demo")

    first = archive_prestart_pending(db)
    assert first.archived == 1
    assert first.by_user["admin_a"] == 1
    assert "demo_a" not in first.by_user  # no baseline → not scanned

    db.refresh(old)
    db.refresh(keep)
    db.refresh(confirmed)
    db.refresh(rejected)
    db.refresh(demo_old)
    assert old.status == "rejected" and old.suggested_reason == ARCHIVE_REASON
    assert keep.status == "pending"
    assert confirmed.status == "confirmed"
    assert rejected.status == "rejected"
    assert demo_old.status == "pending"

    second = archive_prestart_pending(db)
    assert second.archived == 0


def test_falls_back_to_created_at_when_ts_null(db):
    user = _user(db, "admin_b", date(2026, 10, 23))
    cutoff = datetime(2026, 10, 23, tzinfo=SHANGHAI).astimezone(timezone.utc)
    old = _pending(db, user, ts=None, created=cutoff.replace(day=10))
    new = _pending(db, user, ts=None, created=cutoff.replace(day=25))
    result = archive_prestart_pending(db)
    assert result.archived == 1
    db.refresh(old)
    db.refresh(new)
    assert old.status == "rejected"
    assert new.status == "pending"
