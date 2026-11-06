"""Read-only WeChat context-token freshness, shared by /api/overview and /api/health.

The token itself is never returned; only a boolean and an age in hours. A
missing token is reported as stale with a NULL age (there is nothing to age).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import WechatContextToken

TOKEN_STALE_AFTER = timedelta(hours=24)


@dataclass(frozen=True)
class TokenStatus:
    stale: bool
    age_hours: float | None


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _from_timestamp(value: datetime | None, now: datetime) -> TokenStatus:
    if value is None:
        return TokenStatus(stale=True, age_hours=None)
    age = max((now - _aware(value)).total_seconds() / 3600.0, 0.0)
    return TokenStatus(stale=age * 3600.0 > TOKEN_STALE_AFTER.total_seconds(), age_hours=round(age, 2))


def token_status(
    db: Session,
    *,
    user_id: int | None = None,
    now: datetime | None = None,
) -> TokenStatus:
    """Freshness for one user, or for the whole install when ``user_id`` is None.

    The global form uses the most recent refresh across users and never exposes
    a user id, token, or per-user age.
    """
    moment = _aware(now or datetime.now(timezone.utc))
    if user_id is None:
        # Select the row (not MAX(updated_at)) so the ORM applies the DateTime
        # type on every dialect and we keep a real datetime, not a string.
        latest_row = db.scalar(
            select(WechatContextToken)
            .order_by(WechatContextToken.updated_at.desc())
            .limit(1)
        )
        latest = latest_row.updated_at if latest_row is not None else None
    else:
        row = db.get(WechatContextToken, user_id)
        latest = row.updated_at if row is not None else None
    return _from_timestamp(latest, moment)
