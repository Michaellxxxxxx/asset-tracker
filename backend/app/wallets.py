"""Per-user wallet lookup. Env addresses belong only to WALLET_OWNER_USERNAME."""

from __future__ import annotations

from sqlalchemy.orm import Session

from .config import exchange_credentials, settings
from . import coinstats_keys
from .models import User, UserWallet


def user_addresses(db: Session, user_id: int, chain: str) -> tuple[str, ...]:
    """Addresses owned by this user. Never returns another user's wallets."""
    rows = (
        db.query(UserWallet.address)
        .filter(UserWallet.user_id == user_id, UserWallet.chain == chain)
        .order_by(UserWallet.id.asc())
        .all()
    )
    return tuple(address for (address,) in rows if address)


def user_exchange_credentials(db: Session, user_id: int) -> dict[str, dict[str, str]]:
    """Exchange keys belong to ``EXCHANGE_OWNER_USERNAME`` only.

    Anyone else — including demo — gets ``{}`` even when the process has keys.
    An empty owner means nobody owns them.
    """
    owner = settings.exchange_owner_username.strip()
    if not owner:
        return {}
    username = db.query(User.username).filter(User.id == user_id).scalar()
    if username is None or username != owner:
        return {}
    creds = exchange_credentials()
    # Copy before attaching the per-user CoinStats key so the process-wide
    # exchange map is never mutated.
    if "binance" in creds:
        key = coinstats_keys.get_key(db, user_id)
        if key:
            creds = {**creds, "_coinstats": {"apiKey": key}}
    return creds
