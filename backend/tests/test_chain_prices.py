"""Chain parsers, Zerion→Alchemy fallback, and price fallback / cache."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal

import httpx

from app.integrations.chain.alchemy import FIXTURE_PATH as ALCHEMY_FIXTURE
from app.integrations.chain.alchemy import parse_alchemy_transfers
from app.integrations.chain.helius import FIXTURE_PATH as HELIUS_FIXTURE
from app.integrations.chain.helius import parse_helius_transactions
from app.integrations.chain.zerion import FIXTURE_PATH as ZERION_FIXTURE
from app.integrations.chain.zerion import ZerionAdapter, parse_zerion_transactions
from app.integrations.prices.service import PriceService, parse_coingecko, parse_defillama
from app.integrations.types import ChainBalance, NormalizedBalance
from app.snapshots import price_holdings

SINCE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_parse_zerion_fixture() -> None:
    payload = json.loads(ZERION_FIXTURE.read_text(encoding="utf-8"))
    rows = parse_zerion_transactions("0xdemo", payload)
    by_id = {row.external_id: row for row in rows}
    assert by_id["zerion:0xzerionin:0"].direction == "in"
    assert by_id["zerion:0xzerionin:0"].asset == "USDT"
    assert by_id["zerion:0xzerionin:0"].amount == Decimal("300.0")
    claim = by_id["zerion:0xzerionclaim:0"]
    assert claim.asset == "DUST"
    assert "claim" in claim.raw_type


def test_parse_alchemy_directions() -> None:
    payload = json.loads(ALCHEMY_FIXTURE.read_text(encoding="utf-8"))
    rows = parse_alchemy_transfers("0xdemo", payload)
    incoming = next(row for row in rows if row.asset == "USDT")
    outgoing = next(row for row in rows if row.asset == "ETH")
    assert incoming.direction == "in"
    assert incoming.source == "alchemy"
    assert outgoing.direction == "out"
    assert outgoing.amount == Decimal("0.1")


def test_parse_helius_native_and_token() -> None:
    payload = json.loads(HELIUS_FIXTURE.read_text(encoding="utf-8"))
    rows = parse_helius_transactions("So1DemoAddress", payload)
    sol = next(row for row in rows if row.asset == "SOL")
    usdc = next(row for row in rows if row.asset == "USDC")
    assert sol.direction == "in"
    assert sol.amount == Decimal("2")
    assert usdc.amount == Decimal("40")
    assert "kol" in usdc.note.lower()


def test_zerion_falls_back_to_alchemy_when_request_fails(tmp_path, monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "zerion" in str(request.url):
            return httpx.Response(503, json={"error": "down"})
        payload = json.loads(ALCHEMY_FIXTURE.read_text(encoding="utf-8"))
        return httpx.Response(200, json=payload)

    def fake_client(**_kwargs) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handler))

    # Zerion and Alchemy each open their own client. Both go through this handler.
    monkeypatch.setattr("app.integrations.chain.zerion.quiet_client", fake_client)
    monkeypatch.setattr("app.integrations.chain.alchemy.quiet_client", fake_client)
    adapter = ZerionAdapter(
        ["0xdemo"],
        zerion_api_key="zk",
        alchemy_api_key="ak",
        use_mock=False,
    )
    rows = adapter.fetch_transfers(SINCE)
    assert rows
    assert all(row.source == "alchemy" for row in rows)


def test_zerion_without_key_reads_fixture() -> None:
    adapter = ZerionAdapter(["0xdemo"], use_mock=True)
    rows = adapter.fetch_transfers(SINCE)
    assert rows
    assert all(row.ts >= SINCE for row in rows)
    assert all(row.source == "zerion" for row in rows)


def test_price_parsers_and_stablecoin() -> None:
    gecko = parse_coingecko(json.loads((ZERION_FIXTURE.parents[1] / "prices" / "coingecko.json").read_text()))
    assert gecko["BTC"] == Decimal("65000")
    llama = parse_defillama(json.loads((ZERION_FIXTURE.parents[1] / "prices" / "defillama.json").read_text()))
    assert llama["ETH"] == Decimal("3100")

    clock = {"t": 0.0}
    service = PriceService(use_mock=True, now=lambda: clock["t"], ttl_seconds=10)
    assert service.quote_usdt("USDT") == Decimal("1")
    assert service.quote_usdt("BTC") == Decimal("65000")
    # second call is cached: even if we could not refetch, the value sticks
    assert service.quote_usdt("BTC") == Decimal("65000")
    assert service.to_usdt("ETH", Decimal("2")) == Decimal("6400.00")


def test_coingecko_falls_back_to_defillama() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "coingecko.com" in url:
            return httpx.Response(429, json={"error": "rate"})
        if "llama.fi" in url:
            return httpx.Response(
                200,
                json={"coins": {"coingecko:bitcoin": {"symbol": "BTC", "price": 111}}},
            )
        return httpx.Response(404, json={"error": "unexpected"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    service = PriceService(coingecko_api_key="demo", client=client, use_mock=False, ttl_seconds=0)
    assert service.quote_usdt("BTC") == Decimal("111")


PT = "0x00000000000000000000000000000000000000aa"


def test_contract_price_beats_symbol_and_records_source() -> None:
    """DefiLlama ``chain:address`` prices a token the symbol lookup cannot name."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        assert "coingecko.com" not in url
        assert f"ethereum:{PT}" in url
        return httpx.Response(
            200,
            json={"coins": {f"ethereum:{PT}": {"symbol": "PT-SUSDX-3DEC2026", "price": 0.97}}},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    service = PriceService(client=client, use_mock=False, ttl_seconds=0)
    balances = [
        ChainBalance(
            source="zerion",
            asset="PT-SUSDX-3DEC2026",
            amount=Decimal("10"),
            chain="ethereum",
            contract=PT,
            quote_usdt=Decimal("0.50"),
        )
    ]
    capture = price_holdings(balances, service)
    assert capture.nav_usdt == Decimal("9.70")
    assert capture.unpriced == []
    assert capture.holdings[0].price_source == "defillama_contract"
    assert capture.holdings[0].price_usdt == Decimal("0.97")


class _ScriptedTicker:
    """ccxt-shaped public client. Records symbols; raises or returns a last."""

    def __init__(self, quotes: dict[str, Decimal | Exception]) -> None:
        self.quotes = quotes
        self.calls: list[str] = []

    def fetch_ticker(self, market: str) -> dict[str, str]:
        self.calls.append(market)
        quote = self.quotes.get(market)
        if isinstance(quote, Exception):
            raise quote
        if quote is None:
            raise RuntimeError(f"no market {market}")
        return {"last": format(quote, "f"), "close": format(quote, "f")}


def test_exchange_ticker_prices_a_listed_coin() -> None:
    """A Gate/Binance spot row uses the public ticker before DefiLlama."""
    gate = _ScriptedTicker({"GT/USDT": Decimal("10.05"), "GT/USD": Decimal("9")})
    service = PriceService(
        use_mock=True,
        ttl_seconds=60,
        ticker_client={"gateio": gate},
    )
    balances = [
        NormalizedBalance(source="gate", asset="GT", amount=Decimal("2")),
        NormalizedBalance(source="gate", asset="GT", amount=Decimal("0.5")),
    ]
    capture = price_holdings(balances, service)
    assert capture.unpriced == []
    # 2 * 10.05 = 20.10 and 0.5 * 10.05 = 5.025 → 5.02; sum 25.12 (per-row quantize).
    assert capture.nav_usdt == Decimal("25.12")
    assert {item.price_source for item in capture.holdings} == {"exchange_ticker"}
    assert gate.calls == ["GT/USDT"]


def test_exchange_ticker_miss_stays_unpriced() -> None:
    """A failed ticker does not invent a price and does not abort the batch."""
    gate = _ScriptedTicker({"GT/USDT": RuntimeError("down"), "GT/USD": RuntimeError("down")})
    service = PriceService(use_mock=True, ttl_seconds=0, ticker_client={"gateio": gate})
    balances = [
        NormalizedBalance(source="gate", asset="GT", amount=Decimal("1")),
        NormalizedBalance(source="gate", asset="NOPE", amount=Decimal("3")),
    ]
    capture = price_holdings(balances, service)
    assert capture.nav_usdt == Decimal("0.00")
    assert capture.unpriced == ["gate:GT", "gate:NOPE"]
    assert all(item.price_usdt is None for item in capture.holdings)
    # USDT failed, USD was tried, and the second GT row reused the cache.
    assert gate.calls == ["GT/USDT", "GT/USD", "NOPE/USDT", "NOPE/USD"]


def test_exchange_ticker_skips_dust() -> None:
    """A near-zero balance stays unpriced even when the venue has a last price.

    The cut is the amount, not the USDT value: a cheap coin with a real
    balance is still priced.
    """
    gate = _ScriptedTicker({
        "LN/USDT": Decimal("0.006"),
        "FF/USDT": Decimal("0.10"),
    })
    service = PriceService(use_mock=True, ttl_seconds=60, ticker_client={"gateio": gate})
    capture = price_holdings(
        [
            NormalizedBalance(source="gate", asset="LN", amount=Decimal("0.0000000003")),
            NormalizedBalance(source="gate", asset="FF", amount=Decimal("0.01220228")),
        ],
        service,
    )
    assert capture.unpriced == ["gate:LN"]
    priced = next(item for item in capture.holdings if item.asset == "FF")
    assert priced.price_source == "exchange_ticker"
    assert priced.price_usdt == Decimal("0.10")
    assert capture.nav_usdt == Decimal("0.00")  # 0.01220228 * 0.10 quantizes to 0.00


def test_binance_ticker_falls_back_to_gate_when_binance_is_blocked() -> None:
    """HTTP 451 on Binance must not hide a symbol Gate's public book already quotes."""
    binance = _ScriptedTicker({
        "WLFI/USDT": RuntimeError("451"),
        "WLFI/USD": RuntimeError("451"),
    })
    gate = _ScriptedTicker({"WLFI/USDT": Decimal("0.05")})
    service = PriceService(
        use_mock=True,
        ttl_seconds=60,
        ticker_client={"binance": binance, "gateio": gate},
    )
    capture = price_holdings(
        [NormalizedBalance(source="binance", asset="WLFI", amount=Decimal("1.31"))],
        service,
    )
    assert capture.unpriced == []
    assert capture.holdings[0].price_source == "exchange_ticker"
    assert capture.holdings[0].price_usdt == Decimal("0.05")
    assert capture.nav_usdt == Decimal("0.07")
    assert gate.calls == ["WLFI/USDT"]


def test_zerion_price_is_the_last_resort() -> None:
    """No contract hit and no symbol quote: use the Zerion price, tagged as such."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"coins": {}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    service = PriceService(client=client, use_mock=False, ttl_seconds=0)
    balances = [
        ChainBalance(
            source="zerion",
            asset="YT-SUSDX-3DEC2026",
            amount=Decimal("4"),
            chain="ethereum",
            contract="0x00000000000000000000000000000000000000ab",
            quote_usdt=Decimal("0.25"),
        )
    ]
    capture = price_holdings(balances, service)
    assert capture.nav_usdt == Decimal("1.00")
    assert capture.holdings[0].price_source == "zerion"
    assert capture.unpriced == []


def test_backfill_does_not_substitute_the_current_price(monkeypatch) -> None:
    """A historical contract miss stays unpriced. Today's quote must not fill it."""
    from app.snapshots_backfill import price_quantities

    day = datetime(2026, 1, 2, tzinfo=timezone.utc).date()

    def refuse_history(self, keys, day):  # noqa: ANN001 - bound stand-in
        return {}

    monkeypatch.setattr(PriceService, "_defillama_contracts_on", refuse_history)
    service = PriceService(use_mock=False, ttl_seconds=0)
    # The spot fixture knows ETH. The contract row must not inherit that price.
    monkeypatch.setattr(service, "quote_usdt", lambda asset: Decimal("999"))
    quantities = {
        ("zerion", "PT-SUSDX-3DEC2026", "ethereum", PT): Decimal("10"),
    }
    capture = price_quantities(quantities, service, day)
    assert capture.nav_usdt == Decimal("0.00")
    assert capture.unpriced == ["zerion:PT-SUSDX-3DEC2026"]
    assert capture.holdings[0].price_usdt is None
    assert capture.holdings[0].price_source == "none"


def test_expired_yt_is_priced_at_zero_live(monkeypatch) -> None:
    """DefiLlama quotes a matured YT near 1. Expiry wins, so NAV is not inflated.

    PT shares the date suffix and stays on the contract quote. A YT that has
    not matured yet is left to the normal pricing path.
    """
    from app.integrations.prices.service import expired_yt_price

    monkeypatch.setattr(
        "app.snapshots.datetime",
        type("Frozen", (), {"now": staticmethod(lambda tz=None: datetime(2026, 3, 20, tzinfo=timezone.utc))}),
    )
    yt_contract = "0x00000000000000000000000000000000000000ac"
    live_contract = "0x00000000000000000000000000000000000000ad"

    def handler(request: httpx.Request) -> httpx.Response:
        coins = {}
        for key, symbol in (
            (f"ethereum:{PT}", "PT-SUSDX-3DEC2026"),
            (f"ethereum:{yt_contract}", "YT-SUSDAI-20NOV2025"),
            (f"ethereum:{live_contract}", "YT-SUSDX-3DEC2026"),
        ):
            if key in str(request.url):
                coins[key] = {"symbol": symbol, "price": 1.0}
        return httpx.Response(200, json={"coins": coins})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    service = PriceService(client=client, use_mock=False, ttl_seconds=0)
    balances = [
        ChainBalance(
            source="zerion",
            asset="YT-SUSDAI-20NOV2025",
            amount=Decimal("2835"),
            chain="ethereum",
            contract=yt_contract,
            quote_usdt=Decimal("1"),
        ),
        ChainBalance(
            source="zerion",
            asset="PT-SUSDX-3DEC2026",
            amount=Decimal("10"),
            chain="ethereum",
            contract=PT,
            quote_usdt=Decimal("0.50"),
        ),
        ChainBalance(
            source="zerion",
            asset="YT-SUSDX-3DEC2026",
            amount=Decimal("4"),
            chain="ethereum",
            contract=live_contract,
        ),
    ]
    capture = price_holdings(balances, service)
    by_asset = {item.asset: item for item in capture.holdings}
    expired = by_asset["YT-SUSDAI-20NOV2025"]
    assert expired.price_usdt == Decimal("0")
    assert expired.price_source == "expired_yt"
    assert expired.value_usdt == Decimal("0.00")
    assert expired.as_dict()["priced"] is True
    # PT is not a yield token: the DefiLlama contract quote still stands.
    assert by_asset["PT-SUSDX-3DEC2026"].price_source == "defillama_contract"
    assert by_asset["PT-SUSDX-3DEC2026"].price_usdt == Decimal("1.0")
    # Matures 3 Dec 2026, after the frozen today of 20 Mar 2026.
    assert by_asset["YT-SUSDX-3DEC2026"].price_source == "defillama_contract"
    assert capture.nav_usdt == Decimal("14.00")
    assert "zerion:YT-SUSDAI-20NOV2025" not in capture.unpriced
    # Expiry day itself is already worthless; the day before is not.
    assert expired_yt_price("YT-SUSDAI-20NOV2025", date(2025, 11, 20)) == Decimal("0")
    assert expired_yt_price("YT-SUSDAI-20NOV2025", date(2025, 11, 19)) is None


def test_expired_yt_is_priced_at_zero_on_backfill() -> None:
    """A historical day on or after expiry prices the YT at 0, not the contract."""
    from app.snapshots_backfill import price_quantities

    yt_contract = "0x00000000000000000000000000000000000000ac"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"coins": {f"ethereum:{yt_contract}": {"symbol": "YT-SUSDAI-20NOV2025", "price": 0.99}}},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    service = PriceService(client=client, use_mock=False, ttl_seconds=0)
    quantities = {
        ("zerion", "YT-SUSDAI-20NOV2025", "ethereum", yt_contract): Decimal("2835"),
        ("zerion", "PT-SUSDX-3DEC2026", "ethereum", PT): Decimal("10"),
    }
    matured = price_quantities(quantities, service, date(2025, 11, 20))
    matured_yt = next(item for item in matured.holdings if item.asset == "YT-SUSDAI-20NOV2025")
    assert matured_yt.price_usdt == Decimal("0")
    assert matured_yt.price_source == "expired_yt"
    assert matured_yt.value_usdt == Decimal("0.00")
    assert matured_yt.as_dict()["priced"] is True
    assert "zerion:YT-SUSDAI-20NOV2025" not in matured.unpriced
    # The PT has no historical quote in this response, so it stays unpriced.
    assert matured.nav_usdt == Decimal("0.00")

    still_alive = price_quantities(quantities, service, date(2025, 11, 19))
    alive_yt = next(item for item in still_alive.holdings if item.asset == "YT-SUSDAI-20NOV2025")
    assert alive_yt.price_source == "defillama_contract"
    assert alive_yt.price_usdt == Decimal("0.99")
    assert alive_yt.value_usdt == Decimal("2806.65")
