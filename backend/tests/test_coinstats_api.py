"""CoinStats key API. Client is monkeypatched; no network, no live keys."""

from __future__ import annotations

from app.auth import hash_password
from app.models import ApiCredential, User
from tests.conftest import auth_header

DEMO_KEY = "demo-coinstats-key-AAAA"
ADMIN_KEY = "admin-coinstats-key-BBBB"


class _CoinStatsError(Exception):
    """Raised by the fake client. The router catches the real CoinStatsError class,
    so this must be that class (monkeypatched onto the router module)."""

    def __init__(self, kind: str, user_message: str, status: int | None = None) -> None:
        self.kind = kind
        self.user_message = user_message
        self.status = status
        super().__init__(user_message)


class FakeClient:
    """Stand-in for CoinStatsClient. Records the key it was built with."""

    calls: list[str] = []
    result: dict = {
        "total": 20000,
        "used": 12,
        "remaining": 19988,
        "subscription": "FREE",
    }
    error: _CoinStatsError | None = None

    def __init__(self, api_key: str, **_kwargs: object) -> None:
        self.api_key = api_key
        FakeClient.calls.append(api_key)

    def usage_credits(self) -> dict:
        if FakeClient.error is not None:
            raise FakeClient.error
        return dict(FakeClient.result)


def _patch(monkeypatch) -> None:
    FakeClient.calls = []
    FakeClient.error = None
    FakeClient.result = {
        "total": 20000,
        "used": 12,
        "remaining": 19988,
        "subscription": "FREE",
    }
    monkeypatch.setattr("app.routers.coinstats.CoinStatsClient", FakeClient)
    monkeypatch.setattr("app.routers.coinstats.CoinStatsError", _CoinStatsError)
    monkeypatch.setattr("app.routers.coinstats.get_last_usage", lambda _key: None)


def _users(db) -> tuple[User, User]:
    demo = User(username="demo", hashed_password=hash_password("demo123"), is_admin=False)
    admin = User(username="admin", hashed_password=hash_password("admin-pass"), is_admin=True)
    db.add_all([demo, admin])
    db.commit()
    return demo, admin


def test_key_requires_login(client) -> None:
    assert client.get("/api/coinstats/key").status_code == 401
    assert client.put("/api/coinstats/key", json={"api_key": DEMO_KEY}).status_code == 401
    assert client.delete("/api/coinstats/key").status_code == 401
    assert client.get("/api/coinstats/credits").status_code == 401


def test_put_rejects_bad_keys(client, db, monkeypatch) -> None:
    _users(db)
    _patch(monkeypatch)
    headers = auth_header(client)
    for bad in ("short", "has space inside", "line\nbreak!!", "   ", "x" * 257, "中文密钥abcd"):
        response = client.put("/api/coinstats/key", headers=headers, json={"api_key": bad})
        assert response.status_code == 422, bad
        assert "8" in response.text


def test_users_cannot_see_each_others_keys(client, db, monkeypatch) -> None:
    _users(db)
    _patch(monkeypatch)
    demo_headers = auth_header(client, "demo", "demo123")
    admin_headers = auth_header(client, "admin", "admin-pass")

    empty = client.get("/api/coinstats/key", headers=demo_headers)
    assert empty.status_code == 200
    assert empty.json() == {"configured": False, "last4": None}

    saved = client.put("/api/coinstats/key", headers=demo_headers, json={"api_key": f"  {DEMO_KEY}  "})
    assert saved.status_code == 200, saved.text
    assert saved.json() == {"configured": True, "last4": DEMO_KEY[-4:]}
    assert DEMO_KEY not in saved.text

    # Admin has no key of their own and must not see demo's.
    other = client.get("/api/coinstats/key", headers=admin_headers)
    assert other.status_code == 200
    assert other.json() == {"configured": False, "last4": None}
    assert DEMO_KEY not in other.text

    admin_saved = client.put(
        "/api/coinstats/key", headers=admin_headers, json={"api_key": ADMIN_KEY}
    )
    assert admin_saved.json() == {"configured": True, "last4": ADMIN_KEY[-4:]}

    # Overwrite demo's key; admin's row is untouched.
    replacement = "demo-coinstats-key-CCCC"
    again = client.put("/api/coinstats/key", headers=demo_headers, json={"api_key": replacement})
    assert again.json() == {"configured": True, "last4": "CCCC"}
    still_admin = client.get("/api/coinstats/key", headers=admin_headers)
    assert still_admin.json()["last4"] == ADMIN_KEY[-4:]
    assert replacement not in still_admin.text
    assert ADMIN_KEY not in again.text

    # Ciphertext must not contain the plaintext key.
    rows = db.query(ApiCredential).filter(ApiCredential.exchange_name == "coinstats").all()
    assert len(rows) == 2
    blob = b"".join(bytes(row.encrypted_payload) for row in rows)
    assert replacement.encode() not in blob
    assert ADMIN_KEY.encode() not in blob
    assert DEMO_KEY.encode() not in blob


def test_delete_then_unconfigured(client, db, monkeypatch) -> None:
    _users(db)
    _patch(monkeypatch)
    headers = auth_header(client)
    client.put("/api/coinstats/key", headers=headers, json={"api_key": DEMO_KEY})

    removed = client.delete("/api/coinstats/key", headers=headers)
    assert removed.status_code == 200
    assert removed.json() == {"configured": False, "last4": None}

    after = client.get("/api/coinstats/key", headers=headers)
    assert after.json() == {"configured": False, "last4": None}

    # Deleting when nothing is stored is still 200.
    again = client.delete("/api/coinstats/key", headers=headers)
    assert again.status_code == 200
    assert again.json()["configured"] is False


def test_credits_unconfigured_skips_client(client, db, monkeypatch) -> None:
    _users(db)
    _patch(monkeypatch)
    headers = auth_header(client)
    body = client.get("/api/coinstats/credits", headers=headers)
    assert body.status_code == 200
    assert body.json() == {"configured": False}
    assert FakeClient.calls == []


def test_credits_uses_client_and_hides_key(client, db, monkeypatch) -> None:
    _users(db)
    _patch(monkeypatch)
    monkeypatch.setattr(
        "app.routers.coinstats.get_last_usage",
        lambda key: {"remaining": 19900} if key == DEMO_KEY else None,
    )
    headers = auth_header(client)
    client.put("/api/coinstats/key", headers=headers, json={"api_key": DEMO_KEY})

    body = client.get("/api/coinstats/credits", headers=headers)
    assert body.status_code == 200, body.text
    assert body.json() == {
        "configured": True,
        "total": 20000,
        "used": 12,
        "remaining": 19988,
        "subscription": "FREE",
        "last_sync_credits": {"remaining": 19900},
    }
    assert DEMO_KEY not in body.text
    assert FakeClient.calls == [DEMO_KEY]

    # Admin's credits call uses admin's key, not demo's.
    admin_headers = auth_header(client, "admin", "admin-pass")
    client.put("/api/coinstats/key", headers=admin_headers, json={"api_key": ADMIN_KEY})
    admin_body = client.get("/api/coinstats/credits", headers=admin_headers)
    assert admin_body.status_code == 200
    assert admin_body.json()["last_sync_credits"] is None
    assert ADMIN_KEY not in admin_body.text
    assert DEMO_KEY not in admin_body.text
    assert FakeClient.calls[-1] == ADMIN_KEY


def test_credits_maps_coinstats_errors(client, db, monkeypatch) -> None:
    _users(db)
    _patch(monkeypatch)
    headers = auth_header(client)
    client.put("/api/coinstats/key", headers=headers, json={"api_key": DEMO_KEY})

    cases = [
        ("auth", "CoinStats API key 无效或已被停用", 400),
        ("rate_limit", "CoinStats 请求太频繁", 429),
        ("timeout", "连接 CoinStats 超时，稍后会自动重试", 504),
        ("upstream", "CoinStats 服务暂不可用（HTTP 503）", 502),
    ]
    for kind, message, code in cases:
        FakeClient.error = _CoinStatsError(kind, message, status=code)
        response = client.get("/api/coinstats/credits", headers=headers)
        assert response.status_code == code, kind
        assert response.json()["detail"] == message
        assert DEMO_KEY not in response.text
