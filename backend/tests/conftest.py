"""SQLite + TestClient fixtures. No network, no live keys."""

from __future__ import annotations

import os
from collections.abc import Generator
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

# Force an in-memory database before app modules build the engine.
os.environ["DATABASE_URL"] = "sqlite+pysqlite:///:memory:"
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production-use")
os.environ.setdefault(
    "MASTER_KEY",
    "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
)
# Blank every live key so sync stays on fixtures even if the developer shell has them.
for _name in (
    "BINANCE_API_KEY",
    "BINANCE_API_SECRET",
    "BITGET_API_KEY",
    "BITGET_API_SECRET",
    "BITGET_API_PASSPHRASE",
    "BYBIT_API_KEY",
    "BYBIT_API_SECRET",
    "KRAKEN_API_KEY",
    "KRAKEN_API_SECRET",
    "GATE_API_KEY",
    "GATE_API_SECRET",
    "ZERION_API_KEY",
    "ALCHEMY_API_KEY",
    "HELIUS_API_KEY",
    "COINGECKO_DEMO_API_KEY",
    "EVM_ADDRESSES",
    "SOLANA_ADDRESSES",
):
    os.environ[_name] = ""
os.environ["SYNC_MODE"] = "mock"
os.environ["SYNC_SINCE"] = "2026-01-01"
# WeChat push stays mock and never dials ilink during tests.
os.environ.setdefault("WECHAT_BOT_TOKEN", "")
os.environ.setdefault("WECHAT_PUSH_MODE", "mock")
os.environ.setdefault("WECHAT_POLL_ENABLED", "0")

from app import models  # noqa: E402
from app.auth import hash_password  # noqa: E402
from app.db import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import LedgerEntry, Snapshot, User  # noqa: E402


@pytest.fixture()
def engine():
    # One shared connection (StaticPool) so the fixture session and the request
    # sessions see the same in-memory database. SQLite allows one writer; if a
    # session keeps a transaction open, the next writer blocks. busy_timeout
    # bounds that wait, and releasing the connection rolls the transaction back
    # so a finished request cannot pin the lock for the rest of the test.
    bind = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False, "timeout": 5},
        poolclass=StaticPool,
        future=True,
    )

    @event.listens_for(bind, "connect")
    def _busy_timeout(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA busy_timeout = 5000")
        cursor.close()

    @event.listens_for(bind, "checkin")
    def _release_transaction(dbapi_connection, _connection_record) -> None:
        if dbapi_connection.in_transaction:
            dbapi_connection.rollback()

    Base.metadata.create_all(bind)
    yield bind
    bind.dispose()


@pytest.fixture()
def db(engine) -> Generator[Session, None, None]:
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client(engine, db) -> Generator[TestClient, None, None]:
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)

    def override_get_db():
        session = factory()
        try:
            yield session
        finally:
            session.rollback()
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture()
def demo_user(db: Session) -> User:
    user = User(username="demo", hashed_password=hash_password("demo123"), is_admin=False)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def auth_header(client: TestClient, username: str = "demo", password: str = "demo123") -> dict[str, str]:
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    token = response.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def seed_nav(db: Session, user_id: int) -> None:
    """Two snapshots and one deposit / one withdraw, mirroring the phase-1 formula."""
    start = datetime(2026, 10, 1, 23, 59, 59, tzinfo=timezone.utc)
    end = datetime(2026, 10, 2, 23, 59, 59, tzinfo=timezone.utc)
    db.add_all(
        [
            Snapshot(user_id=user_id, ts=start, nav_usdt=Decimal("10000.00")),
            Snapshot(user_id=user_id, ts=end, nav_usdt=Decimal("11200.00")),
            LedgerEntry(
                user_id=user_id,
                ts=datetime(2026, 10, 2, 10, 0, tzinfo=timezone.utc),
                amount_usdt=Decimal("1000.00"),
                kind="deposit",
                note="seed deposit",
            ),
            LedgerEntry(
                user_id=user_id,
                ts=datetime(2026, 10, 2, 11, 0, tzinfo=timezone.utc),
                amount_usdt=Decimal("-200.00"),
                kind="withdraw",
                note="seed withdraw",
            ),
        ]
    )
    db.commit()


# --- p28: no test may reach the internet -------------------------------------
# Some code paths build their own httpx client (thegraph, pendle, Alchemy
# fallback inside Zerion, ...). Online they were slow enough to hit the 60s
# timeout; offline they raised DNS errors. Every outbound socket to a non-local
# host now fails at once with a ConnectError-like OSError, identical online and
# offline. Tests that inject httpx.MockTransport never open a socket, so they
# are unaffected; TestClient talks to the ASGI app in-process.
import ipaddress  # noqa: E402
import socket as _socket  # noqa: E402

_LOCAL_HOSTS = {"localhost", "testserver", ""}


def _is_local(host) -> bool:
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode(errors="ignore")
    host = str(host)
    if host in _LOCAL_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class ExternalNetworkBlocked(OSError):
    """Raised when a test tries to reach a real external host."""


@pytest.fixture(autouse=True)
def _block_external_network(monkeypatch):
    real_getaddrinfo = _socket.getaddrinfo
    real_connect = _socket.socket.connect
    real_connect_ex = _socket.socket.connect_ex

    def guarded_getaddrinfo(host, *args, **kwargs):
        if not _is_local(host):
            raise ExternalNetworkBlocked(f"tests must not resolve external host {host!r}")
        return real_getaddrinfo(host, *args, **kwargs)

    def _check(address):
        if isinstance(address, tuple) and address and not _is_local(address[0]):
            raise ExternalNetworkBlocked(f"tests must not connect to {address!r}")

    def guarded_connect(self, address):
        _check(address)
        return real_connect(self, address)

    def guarded_connect_ex(self, address):
        _check(address)
        return real_connect_ex(self, address)

    monkeypatch.setattr(_socket, "getaddrinfo", guarded_getaddrinfo)
    monkeypatch.setattr(_socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(_socket.socket, "connect_ex", guarded_connect_ex)
    yield
