"""Exchange fixture parsing, pagination and retry. No network."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.integrations.exchanges.ccxt_adapter import CCXTAdapter
from app.integrations.exchanges.mock import MockExchangeAdapter
from app.integrations.exchanges.normalize import normalize_balance, normalize_trade, normalize_transfer
from app.integrations.exchanges.retry import RetryableError, call_with_retry

SINCE = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.mark.parametrize("exchange", ["binance", "bitget", "bybit", "kraken", "gate"])
def test_mock_exchange_parses_fixtures(exchange: str) -> None:
    adapter = MockExchangeAdapter(exchange)
    balances = adapter.fetch_balances()
    transfers = adapter.fetch_transfers(SINCE)
    trades = adapter.fetch_trades(SINCE)
    assert balances, exchange
    assert all(item.amount > 0 for item in balances)
    assert transfers, exchange
    assert {item.direction for item in transfers} <= {"in", "out"}
    assert all(item.ts >= SINCE for item in transfers)
    assert all(item.source == exchange for item in transfers)
    # trades may be empty except binance
    assert all(item.source == exchange for item in trades)


def test_binance_filters_pre_since_and_directions() -> None:
    adapter = MockExchangeAdapter("binance")
    transfers = adapter.fetch_transfers(SINCE)
    ids = {item.external_id for item in transfers}
    assert "dep-binance-1" in ids
    assert "wd-binance-1" in ids
    assert "dep-binance-old" not in ids
    deposit = next(item for item in transfers if item.external_id == "dep-binance-1")
    withdraw = next(item for item in transfers if item.external_id == "wd-binance-1")
    assert deposit.direction == "in"
    assert deposit.asset == "USDT"
    assert deposit.amount == 2500
    assert deposit.txid == "0xbinancedeposit"
    assert withdraw.direction == "out"


def test_normalize_balance_skips_zero() -> None:
    rows = normalize_balance("binance", {"total": {"USDT": "1", "ETH": "0", "BTC": None}})
    assert [row.asset for row in rows] == ["USDT"]


def test_normalize_trade_and_transfer_drop_undated() -> None:
    assert normalize_transfer("binance", {"currency": "USDT", "amount": 1}) is None
    assert normalize_trade("binance", {"symbol": "BTC/USDT", "amount": 1}) is None


class _FakeClient:
    """Two pages of deposits, one empty withdrawal page, one retry then success."""

    def __init__(self) -> None:
        self.deposit_calls: list[int] = []
        self.failures_left = 1

    def fetch_balance(self) -> dict:
        if self.failures_left:
            self.failures_left -= 1
            raise RetryableError("rate limit")
        return {"total": {"USDT": "5"}}

    def fetch_deposits(self, code, since, limit):  # noqa: ARG002
        self.deposit_calls.append(since)
        if len(self.deposit_calls) == 1:
            # Full page. The last row shares a timestamp with the tail so the
            # adapter must request another page instead of assuming it is done.
            return [
                {
                    "id": f"d{i}",
                    "timestamp": 1767225600000 + i,
                    "currency": "USDT",
                    "amount": 1,
                    "type": "deposit",
                }
                for i in range(limit)
            ]
        return [
            {
                "id": "d-last",
                "timestamp": 1767225600000 + limit - 1,
                "currency": "USDT",
                "amount": 2,
                "type": "deposit",
            }
        ]

    def fetch_withdrawals(self, code, since, limit):  # noqa: ARG002
        return []

    def fetch_my_trades(self, symbol, since, limit, params=None):  # noqa: ARG002
        # Binance history is paged with fromId and since=None. One page, then stop.
        if params and params.get("fromId"):
            return []
        return [
            {
                "id": "t1",
                "timestamp": 1767225600000,
                "symbol": symbol,
                "side": "buy",
                "amount": 1,
                "cost": 10,
            }
        ]

    def load_markets(self) -> dict:
        return {"USDT/USDT": {"active": True, "spot": True}}


def test_ccxt_pagination_and_retry_do_not_sleep() -> None:
    sleeps: list[float] = []
    client = _FakeClient()
    adapter = CCXTAdapter(
        "binance",
        {"apiKey": "k", "secret": "s"},
        sleep=sleeps.append,
        client=client,
        page_limit=100,
    )
    balances = adapter.fetch_balances()
    assert balances[0].amount == 5
    assert sleeps, "retry should have invoked the injected sleeper"
    assert client.failures_left == 0

    transfers = adapter.fetch_transfers(SINCE)
    assert len(client.deposit_calls) >= 2
    assert len(transfers) == 101  # full page + trailing row
    assert transfers[0].source == "binance"
    trades = adapter.fetch_trades(SINCE)
    assert trades


def test_retry_stops_on_non_retryable() -> None:
    def boom() -> None:
        raise ValueError("bad request")

    with pytest.raises(ValueError):
        call_with_retry(boom, sleep=lambda _s: None, attempts=3)


def test_ccxt_rejects_unknown_exchange() -> None:
    with pytest.raises(ValueError):
        CCXTAdapter("ftx", {"apiKey": "k", "secret": "s"}, client=object())
