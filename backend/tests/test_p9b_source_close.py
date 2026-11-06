"""p9b: source-level close/stale granularity for lending syncs.

All adapters are monkeypatched; the test uses the in-memory SQLite fixture and
never touches the network, relay env or the WeChat queue.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select

from app.auth import hash_password
from app.integrations.lending_common import (
    log_failure,
    take_failures,
    take_source_failures,
)
from app.models import Position, User, UserWallet
from app.positions_core import upsert_positions
from app.sync.positions_hook import (
    last_failed_protocols,
    sync_positions_for_user,
)

ADDR = "So1anaWa11etP9b111111111111111111111111111111"
EARN_REF = "jupiter-lend-earn"
BORROW_REF = "jupiter-lend-borrow-77"
PERPS_REF = "jupiter-perps-P9bPerp111111111111111111111111111111"
KAMINO_A_REF = "kamino-MktP9bA"
KAMINO_B_REF = "kamino-MktP9bB"
SEED_ASOF = "2026-02-01T00:00:00+00:00"


@pytest.fixture(autouse=True)
def _fresh_failure_state():
    take_source_failures()
    take_failures()
    yield
    take_source_failures()
    take_failures()


def _user_with_solana_wallet(db) -> User:
    user = User(username="p9b-source", hashed_password=hash_password("pass-word"))
    db.add(user)
    db.commit()
    db.add(UserWallet(user_id=user.id, chain="solana", address=ADDR))
    db.commit()
    return user


def _row(market_ref: str, net: str = "10", **details: Any) -> dict:
    return {
        "kind": "lending",
        "protocol": "jupiter" if market_ref.startswith("jupiter") else "kamino",
        "chain": "solana",
        "account_ref": ADDR,
        "market_ref": market_ref,
        "status": "open",
        "net_value_usdt": net,
        "collateral_usdt": net,
        "debt_usdt": "0",
        "asof": SEED_ASOF,
        "details": details or None,
    }


def _seed(db, user: User, *rows: dict) -> dict[str, Position]:
    upsert_positions(db, user.id, list(rows), seen_protocols=None)
    stored = db.scalars(select(Position).where(Position.user_id == user.id)).all()
    return {row.market_ref: row for row in stored}


def _sync(db, user: User) -> None:
    sync_positions_for_user(db, user.id, None, use_mock=False)


def _position(db, user: User, market_ref: str) -> Position:
    return db.scalar(
        select(Position).where(
            Position.user_id == user.id,
            Position.market_ref == market_ref,
        )
    )


def test_jupiter_earn_failure_closes_clean_borrow_sibling(db, monkeypatch) -> None:
    user = _user_with_solana_wallet(db)
    _seed(db, user, _row(EARN_REF, "100"), _row(BORROW_REF, "1"))

    def fake_jupiter(_address, **_kwargs):
        log_failure("jupiter", "solana", RuntimeError("earn down"), source="earn")
        return []

    monkeypatch.setattr("app.sync.positions_hook.fetch_kamino", lambda *_a, **_k: [])
    monkeypatch.setattr("app.sync.positions_hook.fetch_jupiter", fake_jupiter)

    _sync(db, user)

    earn = _position(db, user, EARN_REF)
    assert earn.status == "open"
    assert earn.details["stale"] is True
    assert Decimal(str(earn.collateral_usdt)) == Decimal("100")

    borrow = _position(db, user, BORROW_REF)
    assert borrow.status == "closed"
    assert borrow.close_reason is None

    # Reconcile still gets the precise source, not a whole-protocol failure.
    assert last_failed_protocols() == ("jupiter:earn",)


def test_jupiter_perps_failure_updates_earn_and_keeps_perps(db, monkeypatch) -> None:
    user = _user_with_solana_wallet(db)
    _seed(db, user, _row(EARN_REF, "100"), _row(PERPS_REF, "5"))

    fresh_earn = _row(EARN_REF, "123.45", refreshed=True)

    def fake_jupiter(_address, **_kwargs):
        log_failure("jupiter", "solana", RuntimeError("perps down"), source="perps")
        return [fresh_earn]

    monkeypatch.setattr("app.sync.positions_hook.fetch_kamino", lambda *_a, **_k: [])
    monkeypatch.setattr("app.sync.positions_hook.fetch_jupiter", fake_jupiter)

    _sync(db, user)

    earn = _position(db, user, EARN_REF)
    assert earn.status == "open"
    assert Decimal(str(earn.collateral_usdt)) == Decimal("123.45")
    assert not earn.details.get("stale")
    assert earn.details.get("refreshed") is True

    perps = _position(db, user, PERPS_REF)
    assert perps.status == "open"
    assert perps.details["stale"] is True
    assert Decimal(str(perps.collateral_usdt)) == Decimal("5")


def test_whole_protocol_failure_keeps_every_row_stale(db, monkeypatch) -> None:
    user = _user_with_solana_wallet(db)
    _seed(db, user, _row(EARN_REF, "100"), _row(BORROW_REF, "1"))

    def exploding_jupiter(_address, **_kwargs):
        raise RuntimeError("jupiter adapter down")

    monkeypatch.setattr("app.sync.positions_hook.fetch_kamino", lambda *_a, **_k: [])
    monkeypatch.setattr("app.sync.positions_hook.fetch_jupiter", exploding_jupiter)

    _sync(db, user)

    for ref in (EARN_REF, BORROW_REF):
        row = _position(db, user, ref)
        assert row.status == "open"
        assert row.details["stale"] is True
    assert last_failed_protocols() == ("jupiter",)


def test_unknown_jupiter_row_is_kept_while_clean_sibling_closes(db, monkeypatch) -> None:
    user = _user_with_solana_wallet(db)
    legacy_ref = "jupiter-legacy-unknown"
    _seed(db, user, _row(EARN_REF, "100"), _row(BORROW_REF, "1"), _row(legacy_ref, "2"))

    def fake_jupiter(_address, **_kwargs):
        log_failure("jupiter", "solana", RuntimeError("earn down"), source="earn")
        return []

    monkeypatch.setattr("app.sync.positions_hook.fetch_kamino", lambda *_a, **_k: [])
    monkeypatch.setattr("app.sync.positions_hook.fetch_jupiter", fake_jupiter)

    _sync(db, user)

    unknown = _position(db, user, legacy_ref)
    assert unknown.status == "open"
    assert unknown.details["stale"] is True
    assert _position(db, user, BORROW_REF).status == "closed"


def test_unknown_failure_source_keeps_whole_protocol(db, monkeypatch) -> None:
    user = _user_with_solana_wallet(db)
    _seed(db, user, _row(EARN_REF, "100"), _row(BORROW_REF, "1"))

    def fake_jupiter(_address, **_kwargs):
        log_failure("jupiter", "solana", RuntimeError("weird"), source="mystery")
        return []

    monkeypatch.setattr("app.sync.positions_hook.fetch_kamino", lambda *_a, **_k: [])
    monkeypatch.setattr("app.sync.positions_hook.fetch_jupiter", fake_jupiter)

    _sync(db, user)

    for ref in (EARN_REF, BORROW_REF):
        row = _position(db, user, ref)
        assert row.status == "open"
        assert row.details["stale"] is True
    assert last_failed_protocols() == ("jupiter",)


def test_upsert_unknown_source_note_closes_nothing(db) -> None:
    """Defense in depth for callers that pass notes straight to upsert."""
    user = _user_with_solana_wallet(db)
    _seed(db, user, _row(EARN_REF, "100"), _row(BORROW_REF, "1"))

    upsert_positions(
        db,
        user.id,
        [],
        failed_protocols=["jupiter:mystery"],
        seen_protocols=["jupiter"],
    )

    for ref in (EARN_REF, BORROW_REF):
        assert _position(db, user, ref).status == "open"


def test_kamino_single_market_failure_closes_clean_market(db, monkeypatch) -> None:
    user = _user_with_solana_wallet(db)
    _seed(
        db,
        user,
        _row(KAMINO_A_REF, "10", market="MktP9bA", obligation="OblA"),
        _row(KAMINO_B_REF, "20", market="MktP9bB", obligation="OblB"),
    )

    def fake_kamino(_address, **_kwargs):
        log_failure("kamino", "solana", RuntimeError("market A down"), source="market:MktP9bA")
        return []

    monkeypatch.setattr("app.sync.positions_hook.fetch_kamino", fake_kamino)
    monkeypatch.setattr("app.sync.positions_hook.fetch_jupiter", lambda *_a, **_k: [])

    _sync(db, user)

    market_a = _position(db, user, KAMINO_A_REF)
    assert market_a.status == "open"
    assert market_a.details["stale"] is True

    market_b = _position(db, user, KAMINO_B_REF)
    assert market_b.status == "closed"
    assert last_failed_protocols() == ("kamino:market:MktP9bA",)


def test_stale_since_is_stable_then_clears_on_recovery(db, monkeypatch) -> None:
    user = _user_with_solana_wallet(db)
    _seed(db, user, _row(PERPS_REF, "5"))
    state = {"down": True}

    def fake_jupiter(_address, **_kwargs):
        if state["down"]:
            log_failure("jupiter", "solana", RuntimeError("perps down"), source="perps")
            return []
        return [_row(PERPS_REF, "8", recovered=True)]

    monkeypatch.setattr("app.sync.positions_hook.fetch_kamino", lambda *_a, **_k: [])
    monkeypatch.setattr("app.sync.positions_hook.fetch_jupiter", fake_jupiter)

    _sync(db, user)
    first = _position(db, user, PERPS_REF).details["stale_since"]
    assert first == SEED_ASOF

    _sync(db, user)
    second = _position(db, user, PERPS_REF).details["stale_since"]
    assert second == first

    state["down"] = False
    _sync(db, user)
    recovered = _position(db, user, PERPS_REF)
    assert recovered.status == "open"
    assert "stale" not in recovered.details
    assert "stale_since" not in recovered.details
    assert Decimal(str(recovered.collateral_usdt)) == Decimal("8")
