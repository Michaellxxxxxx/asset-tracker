"""A real wallet never falls back to fixtures.

Zerion failing (including a 429 that survives Retry-After) reads Alchemy.
Both failing writes no snapshot and does not open the fixture. A user who owns
a wallet skips exchanges and chains that have no key. Demo, with no wallet,
still fills every source from fixtures.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import func, select

from app.config import settings as live_settings
from app.integrations.chain.alchemy import AlchemyAdapter
from app.integrations.chain.zerion import ZerionAdapter
from app.integrations.prices.service import PriceService
from app.models import Snapshot, SyncRun, User, UserWallet
from app.snapshots import SnapshotUnavailable, write_sync_snapshot
from app.sync.scheduler import run_for_all_users
from app.sync.service import PartialSync, run_sync

WALLET = "0xabc0000000000000000000000000000000000abc"
SINCE = datetime(2026, 1, 1, tzinfo=timezone.utc)
FIXTURE_ETH = Decimal("0.25")
FIXTURE_USDC = Decimal("400")


def _patch_settings(monkeypatch, **overrides):
    patched = replace(live_settings, **overrides)
    for target in (
        "app.config.settings",
        "app.sync.service.settings",
        "app.snapshots.settings",
        "app.snapshots_backfill.settings",
    ):
        monkeypatch.setattr(target, patched)
    return patched


def _prices() -> PriceService:
    return PriceService(use_mock=True)


def _wallet_user(db, username: str = "admin") -> User:
    from app.auth import hash_password

    user = User(username=username, hashed_password=hash_password("pw"), is_admin=True)
    db.add(user)
    db.commit()
    db.add(UserWallet(user_id=user.id, chain="evm", address=WALLET))
    db.commit()
    db.refresh(user)
    return user


class _Script:
    """httpx transport that answers from a queue and records every URL."""

    def __init__(self, replies: list[httpx.Response]) -> None:
        self.replies = list(replies)
        self.urls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.urls.append(str(request.url))
        if not self.replies:
            return httpx.Response(500, json={"error": "no more replies"})
        return self.replies.pop(0)


def _alchemy_balance_replies() -> list[httpx.Response]:
    """eth_getBalance, alchemy_getTokenBalances, alchemy_getTokenMetadata."""
    eth = httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": hex(10**18)})
    tokens = httpx.Response(
        200,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "tokenBalances": [
                    {"contractAddress": "0xusdc", "tokenBalance": hex(250 * 10**6)},
                    {"contractAddress": "0xzero", "tokenBalance": "0x0"},
                    {"contractAddress": "0xspam", "tokenBalance": hex(10**18)},
                ]
            },
        },
    )
    usdc = httpx.Response(200, json={"result": {"symbol": "USDC", "decimals": 6}})
    spam = httpx.Response(200, json={"result": {"symbol": "claim-reward.xyz", "decimals": 18}})
    return [eth, tokens, usdc, spam]


def test_zerion_429_retries_then_falls_back_to_alchemy(monkeypatch) -> None:
    slept: list[float] = []
    # Zerion stays rate-limited. The first 429 names Retry-After; the retries
    # do not, so they fall through to exponential backoff. Each reply is a new
    # response object: httpx consumes one on read.
    def limited(retry_after: str | None = None) -> httpx.Response:
        headers = {"Retry-After": retry_after} if retry_after else {}
        return httpx.Response(429, headers=headers, json={"error": "slow down"})

    zerion_script = _Script([limited("5"), limited(), limited(), limited()])
    alchemy_script = _Script(_alchemy_balance_replies())

    def fake_zerion(**_kwargs) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(zerion_script))

    def fake_alchemy(**_kwargs) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(alchemy_script))

    monkeypatch.setattr("app.integrations.chain.zerion.quiet_client", fake_zerion)
    monkeypatch.setattr("app.integrations.chain.alchemy.quiet_client", fake_alchemy)
    adapter = ZerionAdapter(
        [WALLET],
        zerion_api_key="zk",
        alchemy_api_key="ak",
        allow_fixture=False,
        sleep=slept.append,
    )
    rows = adapter.fetch_balances()
    # Retry-After once, then 2**attempt (2s, 4s). A 429 that survives that walks to Alchemy.
    assert slept == [5.0, 2.0, 4.0]
    assert any("alchemy.com" in url for url in alchemy_script.urls)
    by_asset = {row.asset: row.amount for row in rows}
    assert by_asset["ETH"] == Decimal("1")
    assert by_asset["USDC"] == Decimal("250")
    # The zero balance and the spam symbol are dropped, and the fixture is not used.
    assert FIXTURE_ETH not in by_asset.values()
    assert "claim-reward.xyz" not in by_asset
    assert all(row.source == "alchemy" for row in rows)


def test_zerion_failure_uses_alchemy_transfers(monkeypatch) -> None:
    transfer = {
        "hash": "0xfromalchemy",
        "from": "0x2222222222222222222222222222222222222222",
        "to": WALLET,
        "asset": "USDT",
        "category": "erc20",
        "value": 12,
        "metadata": {"blockTimestamp": "2026-04-01T00:00:00Z"},
    }
    # One direction returns the transfer; the opposite direction is empty.
    # fetch_transfers queries toAddress and fromAddress, so two replies.
    payload = httpx.Response(200, json={"result": {"transfers": [transfer]}})
    empty = httpx.Response(200, json={"result": {"transfers": []}})
    zerion_script = _Script([httpx.Response(503, json={"error": "down"})])
    alchemy_script = _Script([payload, empty])

    def fake_zerion(**_kwargs) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(zerion_script))

    def fake_alchemy(**_kwargs) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(alchemy_script))

    monkeypatch.setattr("app.integrations.chain.zerion.quiet_client", fake_zerion)
    monkeypatch.setattr("app.integrations.chain.alchemy.quiet_client", fake_alchemy)
    adapter = ZerionAdapter(
        [WALLET],
        zerion_api_key="zk",
        alchemy_api_key="ak",
        allow_fixture=False,
        sleep=lambda _seconds: None,
    )
    rows = adapter.fetch_transfers(SINCE)
    assert [row.source for row in rows] == ["alchemy"]
    assert rows[0].external_id == "alchemy:0xfromalchemy:0"
    assert "zerion" not in json.dumps([row.asset for row in rows])


def test_both_providers_failing_raises_without_fixture(tmp_path, monkeypatch) -> None:
    opened: list[str] = []
    real_open = open

    def guarding_open(path, *args, **kwargs):
        opened.append(str(path))
        return real_open(path, *args, **kwargs)

    script = _Script(
        [
            httpx.Response(500, json={"error": "zerion down"}),
            httpx.Response(500, json={"error": "alchemy down"}),
        ]
    )
    client = httpx.Client(transport=httpx.MockTransport(script))
    # ZerionAdapter builds its Alchemy fallback without the injected client, so
    # that fallback would dial eth-mainnet.g.alchemy.com for real. Hand it a
    # mocked client that answers 500, which raise_for_status turns into a real
    # httpx.HTTPStatusError, the same online and offline.
    alchemy_calls: list[str] = []

    def alchemy_down(request: httpx.Request) -> httpx.Response:
        alchemy_calls.append(str(request.url.host))
        return httpx.Response(500, json={"error": "alchemy down"}, request=request)

    monkeypatch.setattr(
        "app.integrations.chain.alchemy.quiet_client",
        lambda **_kwargs: httpx.Client(transport=httpx.MockTransport(alchemy_down)),
    )
    adapter = ZerionAdapter(
        [WALLET],
        zerion_api_key="zk",
        alchemy_api_key="ak",
        client=client,
        allow_fixture=False,
        sleep=lambda _seconds: None,
    )
    import builtins

    builtins.open = guarding_open
    try:
        with pytest.raises(httpx.HTTPStatusError):
            adapter.fetch_balances()
    finally:
        builtins.open = real_open
    assert not any("fixtures" in path for path in opened)
    assert alchemy_calls and all("alchemy" in host for host in alchemy_calls)


def test_alchemy_balance_without_key_raises_when_fixtures_forbidden() -> None:
    adapter = AlchemyAdapter([WALLET], api_key="", allow_fixture=False)
    with pytest.raises(RuntimeError):
        adapter.fetch_balances()


def test_wallet_user_skips_mock_sources_and_marks_live(db, monkeypatch) -> None:
    user = _wallet_user(db)
    _patch_settings(
        monkeypatch,
        zerion_api_key="zk",
        alchemy_api_key="ak",
        sync_mode="auto",
    )
    zerion_script = _Script([httpx.Response(503, json={"error": "zerion down"})])
    alchemy_script = _Script(_alchemy_balance_replies())

    def fake_zerion(**_kwargs) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(zerion_script))

    def fake_alchemy(**_kwargs) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(alchemy_script))

    monkeypatch.setattr("app.integrations.chain.zerion.quiet_client", fake_zerion)
    monkeypatch.setattr("app.integrations.chain.alchemy.quiet_client", fake_alchemy)

    moment = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
    row = write_sync_snapshot(db, user.id, mode="auto", ts=moment, prices=_prices())
    db.commit()
    assert row is not None
    body = json.loads(row.breakdown or "{}")
    assets = {item["asset"] for item in body["holdings"]}
    # Fixture exchange rows (binance BTC 0.015) and fixture SOL must not be mixed in.
    assert "BTC" not in assets
    assert "SOL" not in assets
    assert assets == {"ETH", "USDC"}
    assert all(item["live"] is True for item in body["holdings"])
    skipped = {item["source"]: item["reason"] for item in body["skipped"]}
    assert skipped["binance"] == "no api key"
    assert skipped["solana"] == "no wallet address"
    # 1 ETH * 3200 + 250 USDC. The fixture's 0.25 ETH / 400 USDC is not the NAV.
    assert row.nav_usdt == Decimal("3450.00")


def test_both_failing_writes_no_snapshot_and_records_partial(db, monkeypatch) -> None:
    user = _wallet_user(db)
    _patch_settings(monkeypatch, zerion_api_key="zk", alchemy_api_key="ak", sync_mode="live")
    def failing(status: int, message: str):
        def fake(**_kwargs) -> httpx.Client:
            return httpx.Client(
                transport=httpx.MockTransport(lambda _request: httpx.Response(status, json={"error": message}))
            )

        return fake

    monkeypatch.setattr("app.integrations.chain.zerion.quiet_client", failing(503, "z"))
    monkeypatch.setattr("app.integrations.chain.alchemy.quiet_client", failing(502, "a"))

    before = db.scalar(select(func.count()).select_from(Snapshot)) or 0
    with pytest.raises(SnapshotUnavailable) as caught:
        write_sync_snapshot(db, user.id, mode="live", ts=datetime(2026, 10, 21, tzinfo=timezone.utc), prices=_prices())
    assert "key" not in str(caught.value).lower()
    assert WALLET not in str(caught.value)
    assert db.scalar(select(func.count()).select_from(Snapshot)) == before

    def sync(user_id: int, mode: str | None, *, db=db) -> None:
        run_sync(user_id, "live", db=db, since=SINCE)

    result = run_for_all_users(db, trigger="manual", mode="live", sync=sync)
    assert result.status == "partial"
    run = db.query(SyncRun).one()
    assert run.status == "partial"
    assert run.error
    assert "zk" not in run.error
    assert "alchemy-secret" not in run.error
    assert WALLET not in run.error
    assert db.scalar(select(func.count()).select_from(Snapshot)) == before


def test_demo_stays_fully_mocked(db, demo_user: User) -> None:
    moment = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
    row = write_sync_snapshot(db, demo_user.id, mode="mock", ts=moment, prices=_prices())
    db.commit()
    assert row is not None
    # Same fixture NAV as before the fallback change.
    assert row.nav_usdt == Decimal("5345.50")
    body = json.loads(row.breakdown or "{}")
    assert body["skipped"] == []
    assert body["holdings"]
    assert all(item["live"] is False for item in body["holdings"])
    sources = {item["source"] for item in body["holdings"]}
    assert {"binance", "zerion", "helius"} <= sources
