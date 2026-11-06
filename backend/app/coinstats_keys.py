"""A user's own CoinStats Open API key, AES-GCM encrypted in ``api_credentials``.

Provider name is ``coinstats``. One row per user. The plaintext is returned
only to server-side callers (``get_key``); API responses show ``configured`` and
the last 4 characters only.
"""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from .crypto import decrypt_text, encrypt_text
from .models import ApiCredential

PROVIDER = "coinstats"


def _rows(db: Session, user_id: int) -> list[ApiCredential]:
    return (
        db.query(ApiCredential)
        .filter(ApiCredential.user_id == user_id, ApiCredential.exchange_name == PROVIDER)
        .order_by(ApiCredential.id.asc())
        .all()
    )


def save_key(db: Session, user_id: int, api_key: str) -> None:
    """Replace this user's CoinStats key. Commits."""
    for row in _rows(db, user_id):
        db.delete(row)
    db.add(
        ApiCredential(
            user_id=user_id,
            exchange_name=PROVIDER,
            encrypted_payload=encrypt_text(json.dumps({"api_key": api_key.strip()})),
        )
    )
    db.commit()


def delete_key(db: Session, user_id: int) -> bool:
    """Remove this user's CoinStats key. Returns True if one existed. Commits."""
    rows = _rows(db, user_id)
    for row in rows:
        db.delete(row)
    db.commit()
    return bool(rows)


def get_key(db: Session, user_id: int) -> str | None:
    """Plaintext key for this user only, or None. Never log or return this."""
    rows = _rows(db, user_id)
    if not rows:
        return None
    try:
        value = json.loads(decrypt_text(rows[-1].encrypted_payload)).get("api_key")
    except Exception:  # noqa: BLE001 - unreadable row behaves as "not configured"
        return None
    return value or None


def key_status(db: Session, user_id: int) -> dict[str, object]:
    key = get_key(db, user_id)
    return {"configured": bool(key), "last4": key[-4:] if key else None}
