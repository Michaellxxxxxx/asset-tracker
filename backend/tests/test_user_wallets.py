"""Env addresses belong to the wallet owner. Demo never sees them or hits live chain APIs."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from app.auth import hash_password
from app.models import PendingConfirmation, User, UserWallet
from app.sync.service import run_sync


ADMIN_ADDR = "0xabc0000000000000000000000000000000000abc"
SINCE = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture()
def users(db):
    admin = User(username="admin", hashed_password=hash_password("admin-pass"), is_admin=True)
    demo = User(username="demo", hashed_password=hash_password("demo123"), is_admin=False)
    db.add_all([admin, demo])
    db.commit()
    db.add(UserWallet(user_id=admin.id, chain="evm", address=ADMIN_ADDR))
    db.commit()
    return admin, demo


def test_demo_sync_does_not_call_live_or_use_admin_address(db, users, monkeypatch) -> None:
    _admin, demo = users
    # Settings is frozen. Replace the module-level object the sync path reads.
    from dataclasses import replace

    from app.config import settings as live_settings

    patched = replace(
        live_settings,
        zerion_api_key="zerion-secret",
        alchemy_api_key="alchemy-secret",
        evm_addresses=(ADMIN_ADDR,),
        sync_mode="auto",
    )
    monkeypatch.setattr("app.sync.service.settings", patched)
    monkeypatch.setattr("app.snapshots.settings", patched)
    monkeypatch.setattr("app.config.settings", patched)

    def explode(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"demo sync must not call the network: {request.url.host}")

    monkeypatch.setattr(
        httpx.Client,
        "request",
        lambda self, *args, **kwargs: explode(args[1] if len(args) > 1 else httpx.Request("GET", "https://example.invalid")),
    )
    result = run_sync(demo.id, "auto", db=db, since=SINCE)
    assert result.fetched > 0
    rows = db.query(UserWallet).filter(UserWallet.user_id == demo.id).all()
    assert rows == []
    pending_text = " ".join(
        f"{row.source} {row.detail or ''} {row.external_id}"
        for row in db.query(PendingConfirmation).filter(PendingConfirmation.user_id == demo.id)
    )
    assert ADMIN_ADDR.lower() not in pending_text.lower()
    assert "zerion-secret" not in pending_text
    assert "alchemy-secret" not in pending_text
