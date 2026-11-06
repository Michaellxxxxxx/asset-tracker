"""CoinStats Binance proxy. Every call is an httpx.MockTransport — no network."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest

from app.auth import hash_password
from app.coinstats_keys import save_key
from app.config import settings as live_settings
from app.integrations.exchanges.coinstats_adapter import (
    CoinStatsBinanceAdapter,
    CoinStatsClient,
    CoinStatsError,
    _direction_of,
    _map_transfer,
    get_last_usage,
    reset_state_for_tests,
)
from app.attribution import attribute
from app.integrations.exchanges.errors import describe_exchange_error
from app.integrations.exchanges.factory import build_exchange_adapter
from app.models import User
from app.sync.service import run_sync
from app.wallets import user_exchange_credentials

BINANCE = {"apiKey": "binance-key-value", "secret": "binance-secret-value"}
CS_KEY = "coinstats-key-value-1234"
SINCE = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _clean_caches() -> None:
    reset_state_for_tests()
    yield
    reset_state_for_tests()


class Script:
    """Queue of responses. ``calls`` records method + path + query, never the key."""

    def __init__(self, steps: list[httpx.Response | Exception]) -> None:
        self.steps = list(steps)
        self.calls: list[tuple[str, str, dict]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path, dict(request.url.params)))
        if not self.steps:
            raise AssertionError(f"unexpected request {request.method} {request.url.path}")
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def _json(status: int, payload: dict, **headers: str) -> httpx.Response:
    return httpx.Response(status, json=payload, headers=headers)


def _client(script: Script, *, sleep=None, clock=None) -> CoinStatsClient:
    kwargs = {"transport": httpx.MockTransport(script), "sleep": sleep or (lambda _s: None)}
    if clock is not None:
        kwargs["clock"] = clock
    return CoinStatsClient(CS_KEY, **kwargs)


def _balance_body(rows: list[dict], portfolio_id: str = "pf-1", status: str = "synced") -> dict:
    return {
        "balances": rows,
        "portfolio": {"id": portfolio_id, "status": status, "connectionId": "binance", "name": "binance"},
    }


def _usage(remaining: int = 100) -> httpx.Response:
    return _json(200, {"totalCredits": 20000, "usedCredits": 10, "remainingCredits": remaining, "subscription": "FREE"})


def _tx(raw_type: str, *, item_id: str, symbol: str, count: float, date: str = "2026-02-01T00:00:00.000Z",
        action: str | None = None, address: str = "0xabc", txid: str = "0xtx") -> dict:
    if action is None:
        action = "Received" if count > 0 else "Sent"
    return {
        "type": raw_type,
        "date": date,
        "coinData": {"count": count, "symbol": symbol, "currentValue": 10},
        "transactions": [
            {
                "action": action,
                "items": [
                    {
                        "id": item_id,
                        "coin": {"id": symbol.lower(), "symbol": symbol, "name": symbol},
                        "count": count,
                        "totalWorth": 10,
                        "toAddress": address if count > 0 else "",
                        "fromAddress": "" if count > 0 else address,
                    }
                ],
            }
        ],
        "hash": {"id": txid},
    }


def test_balance_maps_known_unknown_and_skips_non_positive() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/v1/exchange/balance":
            return _json(200, _balance_body([
                {"coinId": "bitcoin", "amount": 0.5, "price": 60000},
                {"coinId": "ethereum", "amount": 0, "price": 3000},
                {"coinId": "tether", "amount": -1, "price": 1},
                {"coinId": "some-new-coin", "amount": 3, "price": 2},
            ]))
        if request.url.path == "/v1/coins/some-new-coin":
            return _json(200, {"id": "some-new-coin", "symbol": "NEW", "name": "New"})
        if request.url.path == "/v1/usage/credits":
            return _usage()
        raise AssertionError(request.url.path)

    client = CoinStatsClient(CS_KEY, transport=httpx.MockTransport(handler), sleep=lambda _s: None)
    rows = CoinStatsBinanceAdapter(BINANCE, CS_KEY, client=client).fetch_balances()
    by_asset = {row.asset: row for row in rows}
    assert set(by_asset) == {"BTC", "NEW"}
    assert by_asset["BTC"].amount == Decimal("0.5")
    assert by_asset["BTC"].source == "binance"
    assert by_asset["BTC"].raw == {"coinId": "bitcoin", "price": 60000}
    assert by_asset["NEW"].amount == Decimal("3")
    assert "/v1/coins/some-new-coin" in seen
    # A second unknown coin that 404s falls back to the uppercased coinId.
    def fallback(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/exchange/balance":
            return _json(200, _balance_body([{"coinId": "mystery-token", "amount": 1, "price": 1}]))
        if request.url.path.startswith("/v1/coins/"):
            return _json(404, {"statusCode": 404, "message": "Not Found"})
        return _usage()

    reset_state_for_tests()
    other = CoinStatsClient(CS_KEY, transport=httpx.MockTransport(fallback), sleep=lambda _s: None)
    again = CoinStatsBinanceAdapter(BINANCE, CS_KEY, client=other).fetch_balances()
    assert [row.asset for row in again] == ["MYSTERY-TOKEN"]


def test_transfers_map_in_out_external_id_and_stop_on_empty_page() -> None:
    pages = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/exchange/balance":
            return _json(200, _balance_body([]))
        if request.url.path == "/v1/exchange/sync":
            assert request.method == "PATCH"
            assert request.url.params["portfolioId"] == "pf-1"
            return _json(200, {"success": True})
        if request.url.path == "/v1/exchange/status":
            return _json(200, {"status": "synced"})
        if request.url.path == "/v1/exchange/transactions":
            pages["n"] += 1
            assert request.url.params["from"] == "2026-01-01T00:00:00Z"
            assert request.url.params["limit"] == "100"
            if pages["n"] == 1:
                return _json(200, {"meta": {"page": 1, "limit": 100}, "result": [
                    _tx("Deposit", item_id="dep-1", symbol="ETH", count=1.5, address="0xin", txid="tx-in"),
                    _tx("withdraw|sent", item_id="", symbol="USDT", count=-20, address="0xout", txid="tx-out"),
                    _tx("Swap", item_id="swap-1", symbol="BTC", count=0.01),
                    _tx("MysteryType", item_id="nope", symbol="ADA", count=4),
                ]})
            return _json(200, {"meta": {"page": 2, "limit": 100}, "result": []})
        if request.url.path == "/v1/usage/credits":
            return _usage()
        raise AssertionError(request.url.path)

    client = CoinStatsClient(CS_KEY, transport=httpx.MockTransport(handler), sleep=lambda _s: None)
    adapter = CoinStatsBinanceAdapter(BINANCE, CS_KEY, client=client)
    transfers = adapter.fetch_transfers(SINCE)
    assert pages["n"] == 2  # second page empty → stop, no third request
    assert [(row.direction, row.asset, row.amount, row.external_id, row.txid) for row in transfers] == [
        ("in", "ETH", Decimal("1.5"), "dep-1", "tx-in"),
        ("out", "USDT", Decimal("20"), _sha_fallback(), "tx-out"),
    ]
    assert transfers[0].address == "0xin"
    assert transfers[1].address == "0xout"
    assert all(row.source == "binance" for row in transfers)
    trades = adapter.fetch_trades(SINCE)
    assert len(trades) == 1
    assert trades[0].symbol.startswith("BTC/")
    assert trades[0].source == "binance"


def _sha_fallback() -> str:
    import hashlib

    return hashlib.sha1(b"withdraw|sent|2026-02-01T00:00:00.000Z|USDT|-20").hexdigest()


def test_roll_out_fixture_keeps_out_direction_and_attributes_transfer() -> None:
    """#897 (redacted): Binance Earn Roll Out must not become a withdrawal."""
    entry = {
        "type": "Roll Out",
        "date": "2026-10-23T16:50:22.000Z",
        "coinData": {"count": -0.04630139, "symbol": "USDT", "currentValue": 0.05},
        "transactions": [
            {
                "action": "Sent",
                "items": [
                    {
                        "id": "1555e049172c36e1GROUP13036611514roll_out",
                        "coin": {"id": "tether", "symbol": "USDT", "name": "Tether"},
                        "count": -0.04630139,
                        "fromAddress": "",
                        "toAddress": "",
                    }
                ],
            }
        ],
    }
    assert _direction_of("Roll Out") == "out"
    assert _direction_of("RollOut") == "out"
    assert _direction_of("roll_out") == "out"
    assert _direction_of("Roll In") == "in"

    rows = _map_transfer(entry, "out")
    assert len(rows) == 1
    row = rows[0]
    assert row.direction == "out"
    assert row.external_id == "1555e049172c36e1GROUP13036611514roll_out"
    assert row.amount == Decimal("0.04630139")

    suggestion = attribute(row)
    assert suggestion.category != "withdraw"
    assert suggestion.category == "transfer"
    assert "内部转账" in suggestion.reason
    assert "Roll Out" in suggestion.reason


def test_real_withdrawal_still_attributes_withdraw() -> None:
    rows = _map_transfer(
        _tx("withdrawal", item_id="wd-real", symbol="USDT", count=-20, address="0xredacted", txid="tx-real"),
        "out",
    )
    assert attribute(rows[0]).category == "withdraw"


def test_exchange_earn_family_is_internal_transfer() -> None:
    for raw_type in ("Earn", "Simple Earn", "Flexible Savings", "Savings", "Auto-Subscribe"):
        rows = _map_transfer(
            _tx(raw_type, item_id=f"earn-{raw_type}", symbol="USDT", count=-10, address="", txid=""),
            "out",
        )
        suggestion = attribute(rows[0])
        assert suggestion.category == "transfer", raw_type
        assert "内部转账" in suggestion.reason


def test_sync_then_status_poll_then_transactions() -> None:
    order: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        order.append(request.url.path)
        if request.url.path == "/v1/exchange/balance":
            return _json(200, _balance_body([], status="syncing"))
        if request.url.path == "/v1/exchange/sync":
            return _json(200, {"success": True})
        if request.url.path == "/v1/exchange/status":
            status = "synced" if order.count("/v1/exchange/status") >= 2 else "syncing"
            return _json(200, {"status": status})
        if request.url.path == "/v1/exchange/transactions":
            return _json(200, {"result": []})
        if request.url.path == "/v1/usage/credits":
            return _usage()
        raise AssertionError(request.url.path)

    client = CoinStatsClient(CS_KEY, transport=httpx.MockTransport(handler), sleep=lambda _s: None)
    CoinStatsBinanceAdapter(BINANCE, CS_KEY, client=client).fetch_transfers(SINCE)
    assert order.index("/v1/exchange/sync") < order.index("/v1/exchange/status")
    assert order.index("/v1/exchange/transactions") > order.index("/v1/exchange/status")
    assert order.count("/v1/exchange/status") == 2


def test_ttl_cache_does_not_repeat_requests() -> None:
    hits = {"balance": 0, "tx": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/exchange/balance":
            hits["balance"] += 1
            return _json(200, _balance_body([{"coinId": "bitcoin", "amount": 1, "price": 1}]))
        if request.url.path == "/v1/exchange/sync":
            return _json(200, {"success": True})
        if request.url.path == "/v1/exchange/status":
            return _json(200, {"status": "synced"})
        if request.url.path == "/v1/exchange/transactions":
            hits["tx"] += 1
            # Page 1 has a row; the next page is empty so pagination stops.
            if hits["tx"] == 1:
                return _json(200, {"result": [_tx("Deposit", item_id="d", symbol="BTC", count=1)]})
            return _json(200, {"result": []})
        if request.url.path == "/v1/usage/credits":
            return _usage()
        raise AssertionError(request.url.path)

    client = CoinStatsClient(CS_KEY, transport=httpx.MockTransport(handler), sleep=lambda _s: None)
    adapter = CoinStatsBinanceAdapter(BINANCE, CS_KEY, client=client)
    first = adapter.fetch_balances()
    second = adapter.fetch_balances()
    assert [row.asset for row in first] == ["BTC"]
    assert [row.asset for row in second] == ["BTC"]
    adapter.fetch_transfers(SINCE)
    adapter.fetch_transfers(SINCE)
    adapter.fetch_balances()
    assert hits == {"balance": 1, "tx": 2}
    recorded = get_last_usage(CS_KEY)
    assert recorded is not None and recorded["remaining"] == 100


def test_rate_limit_backs_off_then_succeeds_and_exhausts() -> None:
    sleeps: list[float] = []
    now = {"t": 0.0}

    def clock() -> float:
        return now["t"]

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now["t"] += seconds

    script = Script([
        _json(429, {"statusCode": 429, "message": "Rate limit exceeded"}, **{"Retry-After": "0.5"}),
        _usage(50),
    ])
    client = _client(script, sleep=sleep, clock=clock)
    usage = client.usage_credits()
    assert usage["remaining"] == 50
    # 0.5 is the Retry-After backoff. The following gap (if any) is the 0.55s
    # throttle between the retried call and the next one, not another backoff.
    assert sleeps[0] == 0.5

    sleeps.clear()
    exhausted = Script([
        _json(429, {"statusCode": 429, "message": "Rate limit exceeded"}),
        _json(429, {"statusCode": 429, "message": "Rate limit exceeded"}),
        _json(429, {"statusCode": 429, "message": "Rate limit exceeded"}),
        _json(429, {"statusCode": 429, "message": "Rate limit exceeded"}),
        _json(429, {"statusCode": 429, "message": "Rate limit exceeded"}),
    ])
    client = _client(exhausted, sleep=sleep, clock=clock)
    with pytest.raises(CoinStatsError) as caught:
        client.usage_credits()
    assert caught.value.kind == "rate_limit"
    assert caught.value.status == 429
    assert [s for s in sleeps if s in (0.5, 1.0, 2.0, 4.0)] == [0.5, 1.0, 2.0, 4.0]
    assert CS_KEY not in str(caught.value)


def test_auth_ip_whitelist_timeout_not_synced_and_scrubbed() -> None:
    auth = Script([_json(401, {"statusCode": 401, "message": f"bad {CS_KEY}"})])
    with pytest.raises(CoinStatsError) as caught:
        _client(auth).usage_credits()
    assert caught.value.kind == "auth"
    assert "openapi.coinstats.app" in caught.value.user_message
    assert CS_KEY not in str(caught.value)
    assert str(caught.value) == caught.value.user_message

    forbidden = Script([_json(403, {"statusCode": 403, "message": "Forbidden"})])
    with pytest.raises(CoinStatsError) as caught:
        _client(forbidden).usage_credits()
    assert caught.value.kind == "auth"

    whitelist = Script([_json(400, {"statusCode": 400, "message": "IP[203.0.113.5] not in whitelist"})])
    with pytest.raises(CoinStatsError) as caught:
        _client(whitelist).usage_credits()
    assert caught.value.kind == "ip_whitelist"
    assert "203.0.113.5" in caught.value.user_message
    assert "不限制 IP" in caught.value.user_message

    request_ip = Script([_json(403, {"statusCode": 403, "message": "request ip: 198.51.100.9 refused"})])
    with pytest.raises(CoinStatsError) as caught:
        _client(request_ip).usage_credits()
    assert caught.value.kind == "ip_whitelist"
    assert "198.51.100.9" in caught.value.user_message

    no_ip = Script([_json(400, {"statusCode": 400, "message": "IP not in whitelist"})])
    with pytest.raises(CoinStatsError) as caught:
        _client(no_ip).usage_credits()
    assert caught.value.kind == "ip_whitelist"
    assert "IP " not in caught.value.user_message.split("报错里的 IP")[1][:3]

    timed = Script([httpx.TimeoutException("timed out")])
    with pytest.raises(CoinStatsError) as caught:
        _client(timed).usage_credits()
    assert caught.value.kind == "timeout"
    assert "超时" in caught.value.user_message

    conflict = Script([_json(409, {"statusCode": 409, "message": "Transactions not synced"})])
    with pytest.raises(CoinStatsError) as caught:
        _client(conflict).usage_credits()
    assert caught.value.kind == "not_synced"

    described = describe_exchange_error(caught.value, [CS_KEY], venue="binance")
    assert described.startswith("binance: ")
    assert CS_KEY not in described


def test_throttle_interval_uses_injected_clock() -> None:
    now = {"t": 100.0}
    sleeps: list[float] = []

    def clock() -> float:
        return now["t"]

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now["t"] += seconds

    script = Script([_usage(1), _usage(2)])
    client = _client(script, sleep=sleep, clock=clock)
    client.usage_credits()
    client.usage_credits()
    assert sleeps and sleeps[0] >= 0.5
    assert sleeps[0] >= 0.55 - 1e-9


def test_factory_selects_coinstats_only_with_key() -> None:
    from app.integrations.exchanges.ccxt_adapter import CCXTAdapter
    from app.integrations.exchanges.coinstats_adapter import CoinStatsBinanceAdapter as Adapter

    proxied = build_exchange_adapter("binance", {"binance": BINANCE, "_coinstats": {"apiKey": CS_KEY}})
    assert isinstance(proxied, Adapter)
    direct = build_exchange_adapter("binance", {"binance": BINANCE})
    assert isinstance(direct, CCXTAdapter)
    other = build_exchange_adapter("gate", {"gate": BINANCE, "_coinstats": {"apiKey": CS_KEY}})
    assert isinstance(other, CCXTAdapter)
    assert other.source == "gate"


def test_wallets_attach_coinstats_key_only_for_owner(db, monkeypatch) -> None:
    admin = User(username="admin", hashed_password=hash_password("admin-pass"), is_admin=True)
    demo = User(username="demo", hashed_password=hash_password("demo123"), is_admin=False)
    db.add_all([admin, demo])
    db.commit()
    patched = replace(
        live_settings,
        binance_api_key="binance-key-value",
        binance_api_secret="binance-secret-value",
        exchange_owner_username="admin",
    )
    monkeypatch.setattr("app.config.settings", patched)
    monkeypatch.setattr("app.wallets.settings", patched)

    # Owner without a CoinStats key keeps the previous shape.
    plain = user_exchange_credentials(db, admin.id)
    assert "binance" in plain
    assert "_coinstats" not in plain

    save_key(db, admin.id, CS_KEY)
    owned = user_exchange_credentials(db, admin.id)
    assert owned["binance"]["apiKey"] == "binance-key-value"
    assert owned["_coinstats"] == {"apiKey": CS_KEY}
    # The process-wide map is not mutated by the copy.
    assert "_coinstats" not in plain

    demo_creds = user_exchange_credentials(db, demo.id)
    assert demo_creds == {}
    save_key(db, demo.id, "demo-coinstats-key")
    assert user_exchange_credentials(db, demo.id) == {}


def test_run_sync_uses_coinstats_and_never_ccxt(db, monkeypatch) -> None:
    admin = User(username="admin", hashed_password=hash_password("admin-pass"), is_admin=True)
    db.add(admin)
    db.commit()
    patched = replace(
        live_settings,
        binance_api_key="binance-key-value",
        binance_api_secret="binance-secret-value",
        exchange_owner_username="admin",
        sync_mode="live",
    )
    monkeypatch.setattr("app.config.settings", patched)
    monkeypatch.setattr("app.wallets.settings", patched)
    monkeypatch.setattr("app.sync.service.settings", patched)
    monkeypatch.setattr("app.snapshots.settings", patched)
    save_key(db, admin.id, CS_KEY)

    def explode(*_args, **_kwargs):
        raise AssertionError("CCXTAdapter must not be constructed")

    monkeypatch.setattr("app.integrations.exchanges.ccxt_adapter.CCXTAdapter", explode)
    monkeypatch.setattr("app.integrations.exchanges.factory.CCXTAdapter", explode)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("X-API-KEY") == CS_KEY
        if request.url.path == "/v1/exchange/balance":
            body = json.loads(request.content)
            assert body["connectionFields"]["apiKey"] == "binance-key-value"
            assert body["connectionFields"]["apiSecret"] == "binance-secret-value"
            return _json(200, _balance_body([{"coinId": "bitcoin", "amount": 1, "price": 1}]))
        if request.url.path == "/v1/exchange/sync":
            return _json(200, {"success": True})
        if request.url.path == "/v1/exchange/status":
            return _json(200, {"status": "synced"})
        if request.url.path == "/v1/exchange/transactions":
            return _json(200, {"result": [
                _tx("Received", item_id="r-1", symbol="BTC", count=0.25, date="2026-03-01T00:00:00.000Z"),
            ]})
        if request.url.path == "/v1/usage/credits":
            return _usage()
        raise AssertionError(request.url.path)

    monkeypatch.setattr(
        "app.integrations.exchanges.coinstats_adapter.CoinStatsClient",
        lambda api_key, **kwargs: CoinStatsClient(api_key, transport=httpx.MockTransport(handler), sleep=lambda _s: None),
    )
    result = run_sync(admin.id, "live", db=db, since=SINCE)
    assert result.fetched >= 1
