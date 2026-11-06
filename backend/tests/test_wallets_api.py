"""三期·11 B: /api/wallets CRUD."""

from __future__ import annotations

from app.auth import hash_password
from app.models import User, UserWallet
from tests.conftest import auth_header

EVM = "0xAbc0000000000000000000000000000000000AbC"
EVM_NORM = EVM.lower()
SOL = "DYw8jCTfwHNRJhhmFcbXvVDTqWMEVFBX6ZKUmG5CNSKK"


def _login(client, username: str, password: str = "pass-word") -> dict[str, str]:
    return auth_header(client, username=username, password=password)


def test_wallets_crud_and_validation(client, db):
    user = User(username="alice", hashed_password=hash_password("pass-word"))
    other = User(username="bob", hashed_password=hash_password("pass-word"))
    db.add_all([user, other])
    db.commit()

    headers = _login(client, "alice")
    empty = client.get("/api/wallets", headers=headers)
    assert empty.status_code == 200 and empty.json() == []

    bad = client.post("/api/wallets", headers=headers, json={"chain": "evm", "address": "0x123"})
    assert bad.status_code == 422

    bad_sol = client.post("/api/wallets", headers=headers, json={"chain": "solana", "address": "@@@"})
    assert bad_sol.status_code == 422

    bad_chain = client.post("/api/wallets", headers=headers, json={"chain": "btc", "address": EVM})
    assert bad_chain.status_code == 422

    created = client.post("/api/wallets", headers=headers, json={"chain": "evm", "address": EVM})
    assert created.status_code == 201
    body = created.json()
    assert body["chain"] == "evm" and body["address"] == EVM_NORM
    wid = body["id"]

    dup = client.post("/api/wallets", headers=headers, json={"chain": "evm", "address": EVM_NORM})
    assert dup.status_code == 409

    sol = client.post("/api/wallets", headers=headers, json={"chain": "solana", "address": SOL})
    assert sol.status_code == 201

    listed = client.get("/api/wallets", headers=headers)
    assert listed.status_code == 200
    assert len(listed.json()) == 2

    # other user cannot delete
    other_headers = _login(client, "bob")
    denied = client.delete(f"/api/wallets/{wid}", headers=other_headers)
    assert denied.status_code == 404
    assert db.get(UserWallet, wid) is not None

    gone = client.delete(f"/api/wallets/{wid}", headers=headers)
    assert gone.status_code == 204
    assert client.get("/api/wallets", headers=headers).json()[0]["chain"] == "solana"


def test_unauthenticated_wallets(client):
    assert client.get("/api/wallets").status_code == 401
