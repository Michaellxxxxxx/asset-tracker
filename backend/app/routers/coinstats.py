"""CoinStats Open API key for the logged-in user only.

Responses never include the full key — only ``configured`` and the last 4
characters. Credits are read through the route-1 client (free endpoint).
"""

from __future__ import annotations

import logging
import string

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..coinstats_keys import delete_key, get_key, key_status, save_key
from ..db import get_db
from ..integrations.exchanges.coinstats_adapter import CoinStatsClient, CoinStatsError, get_last_usage
from ..models import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/coinstats", tags=["coinstats"])

_MIN_KEY_LEN = 8
_MAX_KEY_LEN = 256
_KEY_INVALID = "CoinStats API key 须为 8～256 位可见 ASCII 字符，且不能包含空白"

# CoinStatsError.kind → HTTP status. auth is a bad key the user can fix (400);
# rate_limit stays 429; timeout is 504; anything else upstream is 502.
_ERROR_STATUS = {
    "auth": status.HTTP_400_BAD_REQUEST,
    "rate_limit": status.HTTP_429_TOO_MANY_REQUESTS,
    "timeout": status.HTTP_504_GATEWAY_TIMEOUT,
    "upstream": status.HTTP_502_BAD_GATEWAY,
    "ip_whitelist": status.HTTP_502_BAD_GATEWAY,
    "not_synced": status.HTTP_502_BAD_GATEWAY,
    "bad_request": status.HTTP_502_BAD_GATEWAY,
}


class KeyIn(BaseModel):
    """PUT body. The plaintext is stored encrypted and never echoed back."""

    model_config = ConfigDict(extra="forbid")

    api_key: str

    @field_validator("api_key")
    @classmethod
    def _visible_ascii(cls, value: str) -> str:
        cleaned = value.strip()
        if not _MIN_KEY_LEN <= len(cleaned) <= _MAX_KEY_LEN:
            raise ValueError(_KEY_INVALID)
        if any(ch not in string.printable or ch in string.whitespace for ch in cleaned):
            raise ValueError(_KEY_INVALID)
        return cleaned


class KeyStatusOut(BaseModel):
    configured: bool
    last4: str | None = None


class CreditsOut(BaseModel):
    model_config = ConfigDict(json_schema_extra={"description": "credits"})

    configured: bool
    total: int | None = None
    used: int | None = None
    remaining: int | None = None
    subscription: str | None = None
    last_sync_credits: dict | None = None

    def model_dump(self, **kwargs):  # type: ignore[override]
        data = super().model_dump(**kwargs)
        if not data.get("configured"):
            return {"configured": False}
        return data


def _status_for(user_id: int, db: Session) -> KeyStatusOut:
    status_row = key_status(db, user_id)
    return KeyStatusOut(configured=bool(status_row["configured"]), last4=status_row["last4"])  # type: ignore[arg-type]


@router.get("/key", response_model=KeyStatusOut)
def read_key(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> KeyStatusOut:
    return _status_for(current_user.id, db)


@router.put("/key", response_model=KeyStatusOut)
def put_key(
    payload: KeyIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> KeyStatusOut:
    # No outbound call: just replace this user's encrypted row.
    save_key(db, current_user.id, payload.api_key)
    logger.info("coinstats key saved user_id=%s", current_user.id)
    return _status_for(current_user.id, db)


@router.delete("/key", response_model=KeyStatusOut)
def remove_key(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> KeyStatusOut:
    delete_key(db, current_user.id)
    logger.info("coinstats key deleted user_id=%s", current_user.id)
    return KeyStatusOut(configured=False, last4=None)


@router.get("/credits", response_model=None)
def read_credits(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CreditsOut:
    api_key = get_key(db, current_user.id)
    if not api_key:
        # Contract: unconfigured is exactly {"configured": false}, no null fields.
        return {"configured": False}  # type: ignore[return-value]

    try:
        usage = CoinStatsClient(api_key).usage_credits()
    except CoinStatsError as exc:
        # user_message is already Chinese and scrubbed by the adapter.
        code = _ERROR_STATUS.get(exc.kind, status.HTTP_502_BAD_GATEWAY)
        raise HTTPException(status_code=code, detail=exc.user_message) from exc

    last = get_last_usage(api_key)
    return CreditsOut(
        configured=True,
        total=int(usage["total"]),
        used=int(usage["used"]),
        remaining=int(usage["remaining"]),
        subscription=str(usage["subscription"]),
        last_sync_credits=last,
    )
