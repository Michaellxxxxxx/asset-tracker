"""p9d: Jupiter 429 cooldown persistence via Redis (fakeredis only).

No real Redis is used. ``configure_cooldown_redis`` injects fakeredis or a
broken stub, and the default path (no ``REDIS_URL``) stays process-local, which
is also what every other test in the suite relies on.
"""

from __future__ import annotations

import hashlib
import json
import time
from decimal import Decimal
from pathlib import Path

import fakeredis
import httpx
import pytest
from sqlalchemy import select

from app.auth import hash_password
from app.integrations import lending_jupiter
from app.integrations.lending_common import take_failures, take_source_failures
from app.models import Position, User, UserWallet
from app.positions_core import upsert_positions
from app.sync.positions_hook import sync_positions_for_user

FIXTURES = Path(__file__).parent / "fixtures" / "jupiter"
ADDR = "So1anaWa11et11111111111111111111111111111111"
PREFIX = "asset-tracker:jupiter:cooldown"
EARN_REF = "jupiter-lend-earn"
BORROW_REF = "jupiter-lend-borrow-3518"


def _load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _ok_handler(calls: list[str]):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(path)
        if "earn/positions" in path:
            return httpx.Response(200, json=[])
        if "borrow/positions" in path:
            return httpx.Response(200, json=[])
        if path == "/v1/positions":
            return httpx.Response(200, json={"count": 0, "dataList": []})
        return httpx.Response(404)

    return handler


def _always_429(request: httpx.Request) -> httpx.Response:
    return httpx.Response(429, headers={"Retry-After": "0.01"}, json={})


@pytest.fixture(autouse=True)
def _fresh_state():
    lending_jupiter.reset_cooldowns()
    lending_jupiter.configure_cooldown_redis(None)
    take_source_failures()
    take_failures()
    yield
    lending_jupiter.reset_cooldowns()
    lending_jupiter.configure_cooldown_redis(None)
    take_source_failures()
    take_failures()


def test_cooldown_is_persisted_with_hashed_wallet_and_ttl() -> None:
    redis = fakeredis.FakeRedis()
    lending_jupiter.configure_cooldown_redis(redis)
    before = time.time()

    lending_jupiter._cool_down(ADDR, "earn")

    keys = [key.decode() for key in redis.scan_iter(match=f"{PREFIX}:*")]
    assert len(keys) == 1
    key = keys[0]
    # The plaintext address must never appear in a long-lived Redis key.
    assert ADDR not in key
    assert key.endswith(":earn")
    digest = hashlib.sha256(ADDR.encode("utf-8")).hexdigest()
    assert digest in key

    until = int(redis.get(key))
    assert before + 55 <= until <= time.time() + 61
    assert 1 <= redis.ttl(key) <= 60


def test_cooldown_survives_restart_via_shared_redis() -> None:
    server = fakeredis.FakeServer()
    lending_jupiter.configure_cooldown_redis(fakeredis.FakeRedis(server=server))
    lending_jupiter._cool_down(ADDR, "earn")

    # A new process starts with an empty local dict but the same Redis.
    lending_jupiter.reset_cooldowns()
    lending_jupiter.configure_cooldown_redis(fakeredis.FakeRedis(server=server))

    assert lending_jupiter._cooldown_remaining(ADDR, "earn") > 0
    assert lending_jupiter._cooldown_remaining(ADDR, "borrow") == 0
    assert lending_jupiter._cooldown_remaining(ADDR, "perps") == 0


def test_persisted_cooldown_skips_only_the_cooling_source_after_restart() -> None:
    server = fakeredis.FakeServer()
    lending_jupiter.configure_cooldown_redis(fakeredis.FakeRedis(server=server))
    lending_jupiter._cool_down(ADDR, "earn")
    lending_jupiter.reset_cooldowns()
    lending_jupiter.configure_cooldown_redis(fakeredis.FakeRedis(server=server))

    calls: list[str] = []
    rows = lending_jupiter.fetch_positions(
        ADDR, client=_client(_ok_handler(calls)), sleep=lambda _s: None
    )

    assert rows == []
    assert not any("earn/positions" in path for path in calls)
    assert any("borrow/positions" in path for path in calls)
    assert calls.count("/v1/positions") == 1
    # The cooling source is still a failed source, so the hook keeps its old
    # rows and marks them stale; the other two sources are healthy.
    assert take_source_failures() == {"jupiter": ["earn"]}
    assert "jupiter" in take_failures()


def test_expired_persisted_deadline_is_ignored_and_cleaned_up() -> None:
    redis = fakeredis.FakeRedis()
    lending_jupiter.configure_cooldown_redis(redis)
    key = lending_jupiter._cooldown_key(ADDR, "earn")
    redis.set(key, int(time.time()) - 5, ex=60)

    assert lending_jupiter._cooldown_remaining(ADDR, "earn") == 0
    assert redis.get(key) is None

    calls: list[str] = []
    lending_jupiter.fetch_positions(
        ADDR, client=_client(_ok_handler(calls)), sleep=lambda _s: None
    )
    assert any("earn/positions" in path for path in calls)


class _BrokenRedis:
    """Every command fails like a dead/refusing Redis."""

    def get(self, *_args, **_kwargs):
        raise ConnectionError("redis is down")

    def set(self, *_args, **_kwargs):
        raise ConnectionError("redis is down")

    def delete(self, *_args, **_kwargs):
        raise ConnectionError("redis is down")

    def scan_iter(self, *_args, **_kwargs):
        raise ConnectionError("redis is down")


def test_broken_redis_falls_back_to_local_cooldown() -> None:
    lending_jupiter.configure_cooldown_redis(_BrokenRedis())
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return _always_429(request)

    rows = lending_jupiter.fetch_positions(
        ADDR, client=_client(handler), sleep=lambda _s: None
    )
    assert rows == []
    # Three sources x four tries; the Redis write failures were swallowed.
    assert len(calls) == 12
    assert lending_jupiter._cooldown_remaining(ADDR, "earn") > 0
    assert take_source_failures() == {"jupiter": ["borrow", "earn", "perps"]}
    assert "jupiter" in take_failures()

    # Second sync is served by the in-process deadline; no request, no raise.
    calls.clear()
    rows = lending_jupiter.fetch_positions(
        ADDR, client=_client(handler), sleep=lambda _s: None
    )
    assert rows == []
    assert calls == []
    assert take_source_failures() == {"jupiter": ["borrow", "earn", "perps"]}


def test_broken_redis_does_not_break_a_normal_sync() -> None:
    lending_jupiter.configure_cooldown_redis(_BrokenRedis())
    calls: list[str] = []

    rows = lending_jupiter.fetch_positions(
        ADDR, client=_client(_ok_handler(calls)), sleep=lambda _s: None
    )

    assert rows == []
    assert len(calls) == 3
    assert take_source_failures() == {}
    assert "jupiter" not in take_failures()


def test_restart_cooldown_keeps_failed_source_stale_and_sibling_fresh(
    db, monkeypatch
) -> None:
    user = User(username="p9d-jupiter", hashed_password=hash_password("pass-word"))
    db.add(user)
    db.commit()
    db.add(UserWallet(user_id=user.id, chain="solana", address=ADDR))
    db.commit()
    upsert_positions(
        db,
        user.id,
        [
            {
                "kind": "lending",
                "protocol": "jupiter",
                "chain": "solana",
                "account_ref": ADDR,
                "market_ref": EARN_REF,
                "status": "open",
                "net_value_usdt": "100",
                "collateral_usdt": "100",
                "debt_usdt": "0",
                "details": {"health_factor": None},
            },
            {
                "kind": "lending",
                "protocol": "jupiter",
                "chain": "solana",
                "account_ref": ADDR,
                "market_ref": BORROW_REF,
                "status": "open",
                "net_value_usdt": "1",
                "collateral_usdt": "1",
                "debt_usdt": "0",
                "details": {},
            },
        ],
    )

    server = fakeredis.FakeServer()
    lending_jupiter.configure_cooldown_redis(fakeredis.FakeRedis(server=server))
    lending_jupiter._cool_down(ADDR, "earn")
    lending_jupiter.reset_cooldowns()  # simulate a fresh process
    lending_jupiter.configure_cooldown_redis(fakeredis.FakeRedis(server=server))

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        assert "earn/positions" not in path, "earn is cooling and must be skipped"
        if "borrow/positions" in path:
            return httpx.Response(200, json=_load("jup_borrow_position_sample.json"))
        if path == "/v1/positions":
            return httpx.Response(200, json=_load("jup_perps_position_sample.json"))
        return httpx.Response(404)

    def fake_jupiter(address, **_kwargs):
        return lending_jupiter.fetch_positions(
            address, client=_client(handler), sleep=lambda _s: None
        )

    monkeypatch.setattr("app.sync.positions_hook.fetch_kamino", lambda *_a, **_k: [])
    monkeypatch.setattr("app.sync.positions_hook.fetch_jupiter", fake_jupiter)

    sync_positions_for_user(db, user.id, None, use_mock=False)

    rows = {
        row.market_ref: row
        for row in db.scalars(select(Position).where(Position.user_id == user.id))
    }
    earn = rows[EARN_REF]
    assert earn.status == "open"
    assert earn.details["stale"] is True
    assert Decimal(str(earn.collateral_usdt)) == Decimal("100")

    borrow = rows[BORROW_REF]
    assert borrow.status == "open"
    assert not borrow.details.get("stale")
    assert Decimal(str(borrow.collateral_usdt)) > Decimal("1")
