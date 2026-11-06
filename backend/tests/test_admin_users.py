"""三期·16: admin list / create / reset-password."""

from __future__ import annotations

from app.auth import hash_password
from app.models import User
from tests.conftest import auth_header

SECRET_KEYS = {"hashed_password", "password"}


def _admin(db) -> User:
    user = User(username="admin", hashed_password=hash_password("admin123"), is_admin=True)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _assert_no_secrets(payload) -> None:
    rows = payload if isinstance(payload, list) else [payload]
    for row in rows:
        assert SECRET_KEYS.isdisjoint(row.keys())
        assert "hashed_password" not in str(row)


def test_admin_list_create_and_reset(client, db, demo_user: User):
    admin = _admin(db)
    headers = auth_header(client, "admin", "admin123")

    listed = client.get("/api/admin/users", headers=headers)
    assert listed.status_code == 200
    body = listed.json()
    assert [row["id"] for row in body] == sorted(row["id"] for row in body)
    assert {row["username"] for row in body} == {"admin", "demo"}
    admin_row = next(row for row in body if row["username"] == "admin")
    demo_row = next(row for row in body if row["username"] == "demo")
    assert admin_row["is_admin"] is True
    assert demo_row["is_admin"] is False
    assert "created_at" in admin_row
    _assert_no_secrets(body)

    created = client.post(
        "/api/admin/users",
        headers=headers,
        json={"username": "newbie", "password": "secret1"},
    )
    assert created.status_code == 201
    created_body = created.json()
    assert created_body["username"] == "newbie"
    assert created_body["is_admin"] is False
    assert created_body["id"] > admin.id
    _assert_no_secrets(created_body)

    duplicate = client.post(
        "/api/admin/users",
        headers=headers,
        json={"username": "newbie", "password": "secret1"},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == "用户名已存在"

    missing = client.post(
        "/api/admin/users/999999/reset-password",
        headers=headers,
        json={"password": "secret2"},
    )
    assert missing.status_code == 404
    assert missing.json()["detail"] == "用户不存在"

    short = client.post(
        f"/api/admin/users/{demo_user.id}/reset-password",
        headers=headers,
        json={"password": "123"},
    )
    assert short.status_code == 422

    reset = client.post(
        f"/api/admin/users/{demo_user.id}/reset-password",
        headers=headers,
        json={"password": "newpass1"},
    )
    assert reset.status_code == 200
    reset_body = reset.json()
    assert reset_body["id"] == demo_user.id
    assert reset_body["username"] == "demo"
    assert reset_body["is_admin"] is False
    _assert_no_secrets(reset_body)

    db.refresh(demo_user)
    assert demo_user.is_admin is False

    old = client.post("/api/auth/login", json={"username": "demo", "password": "demo123"})
    assert old.status_code == 401
    new = client.post("/api/auth/login", json={"username": "demo", "password": "newpass1"})
    assert new.status_code == 200
    newbie = client.post("/api/auth/login", json={"username": "newbie", "password": "secret1"})
    assert newbie.status_code == 200


def test_demo_forbidden_and_anonymous_unauthorized(client, db, demo_user: User):
    _admin(db)
    demo_headers = auth_header(client, "demo", "demo123")

    for method, path, payload in (
        ("get", "/api/admin/users", None),
        ("post", "/api/admin/users", {"username": "xuser", "password": "secret1"}),
        ("post", f"/api/admin/users/{demo_user.id}/reset-password", {"password": "secret2"}),
    ):
        kwargs = {"headers": demo_headers}
        if payload is not None:
            kwargs["json"] = payload
        response = getattr(client, method)(path, **kwargs)
        assert response.status_code == 403, response.text
        assert response.json()["detail"] == "需要管理员权限"

        anonymous_kwargs = {}
        if payload is not None:
            anonymous_kwargs["json"] = payload
        anonymous = getattr(client, method)(path, **anonymous_kwargs)
        assert anonymous.status_code == 401
