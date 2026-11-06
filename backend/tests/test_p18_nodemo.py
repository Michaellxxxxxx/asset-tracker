"""P18: demo login is closed for good; accounts are admin-provisioned only."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt
from sqlalchemy import create_engine, func, inspect, select, text

from app.auth import hash_password
from app.config import settings
from app.migrate import apply_phase2_migrations
from app.models import User

from .conftest import auth_header


def _create_user(db, username: str, password: str, *, is_admin: bool = False) -> User:
    user = User(
        username=username,
        hashed_password=hash_password(password),
        is_admin=is_admin,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _login(client, username: str, password: str):
    return client.post("/api/auth/login", json={"username": username, "password": password})


def test_inactive_account_cannot_login_and_live_token_dies_immediately(client, db):
    user = _create_user(db, "p18member", "member-pass-1")

    token_response = _login(client, "p18member", "member-pass-1")
    assert token_response.status_code == 200, token_response.text
    headers = {"Authorization": f"Bearer {token_response.json()['access_token']}"}
    assert client.get("/api/auth/me", headers=headers).status_code == 200

    # Disabling the account must reject the token that was valid a moment ago.
    user.is_active = False
    db.commit()
    assert client.get("/api/auth/me", headers=headers).status_code == 401

    disabled_login = _login(client, "p18member", "member-pass-1")
    assert disabled_login.status_code == 401
    assert disabled_login.json()["detail"] == "账号已停用"

    # Re-enabling alone is not enough: the old token's version is stale.
    user.is_active = True
    user.token_version += 1
    db.commit()
    assert client.get("/api/auth/me", headers=headers).status_code == 401

    fresh_headers = auth_header(client, username="p18member", password="member-pass-1")
    assert client.get("/api/auth/me", headers=fresh_headers).status_code == 200


def test_demo_account_is_blocked_even_with_the_old_password(client, db):
    demo = User(
        username="demo",
        hashed_password=hash_password("demo123"),
        is_admin=False,
        is_active=False,
        token_version=1,
    )
    db.add(demo)
    db.commit()

    response = _login(client, "demo", "demo123")
    assert response.status_code == 401
    assert response.json()["detail"] == "账号已停用"
    assert "access_token" not in response.json()


def test_legacy_token_without_version_still_works_for_active_accounts(client, db):
    # Tokens issued before this deploy have no "ver" claim; they must keep
    # working for normal accounts and are treated as generation 0.
    user = _create_user(db, "legacyuser", "legacy-pass-1")
    now = datetime.now(timezone.utc)
    legacy_token = jwt.encode(
        {
            "sub": str(user.id),
            "username": user.username,
            "is_admin": user.is_admin,
            "iat": now,
            "exp": now + timedelta(minutes=30),
        },
        settings.jwt_secret,
        algorithm="HS256",
    )
    response = client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {legacy_token}"}
    )
    assert response.status_code == 200, response.text


def test_public_register_is_closed_and_creates_no_account(client, db):
    before = db.scalar(select(func.count()).select_from(User))
    db.commit()

    response = client.post(
        "/api/auth/register",
        json={"username": "sneakyuser", "password": "sneaky-pass-1"},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "注册已关闭，请联系管理员开号"

    # The route refuses before body validation, so even an empty body is a 403.
    assert client.post("/api/auth/register").status_code == 403

    after = db.scalar(select(func.count()).select_from(User))
    db.commit()
    assert after == before
    assert db.scalar(select(User).where(User.username == "sneakyuser")) is None


def test_admin_can_still_create_accounts(client, db):
    _create_user(db, "p18admin", "admin-pass-1", is_admin=True)

    response = client.post(
        "/api/admin/users",
        json={"username": "invited", "password": "invited-pass-1"},
        headers=auth_header(client, username="p18admin", password="admin-pass-1"),
    )
    assert response.status_code == 201, response.text
    assert response.json()["username"] == "invited"

    invited_login = _login(client, "invited", "invited-pass-1")
    assert invited_login.status_code == 200, invited_login.text
    assert invited_login.json()["user"]["is_admin"] is False


def test_sqlite_migration_disables_demo_once_and_keeps_other_accounts(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'old-p18.db'}", future=True)
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE users ("
                "id INTEGER PRIMARY KEY, "
                "username VARCHAR(64) NOT NULL UNIQUE, "
                "hashed_password VARCHAR(255) NOT NULL, "
                "is_admin BOOLEAN NOT NULL DEFAULT FALSE, "
                "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
                ")"
            )
        )
        conn.execute(
            text(
                "INSERT INTO users (id, username, hashed_password, is_admin) VALUES "
                "(1, 'demo', 'x', FALSE), (2, 'admin', 'x', TRUE), (3, 'member', 'x', FALSE)"
            )
        )

    apply_phase2_migrations(engine)
    apply_phase2_migrations(engine)  # idempotent: no second token bump

    columns = {col["name"] for col in inspect(engine).get_columns("users")}
    assert {"is_active", "token_version"} <= columns

    with engine.begin() as conn:
        rows = {
            row.username: (bool(row.is_active), int(row.token_version))
            for row in conn.execute(
                text("SELECT username, is_active, token_version FROM users")
            )
        }
    assert rows["demo"] == (False, 1)
    assert rows["admin"] == (True, 0)
    assert rows["member"] == (True, 0)
    engine.dispose()
