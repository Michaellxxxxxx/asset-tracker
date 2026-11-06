"""P37: price Kamino Liquidity (KJLP-*) share mints without double counting.

The two ``KJLP-*`` wallet rows are strategy vault shares (kTokens). DefiLlama
has no contract quote for either mint; the public Kamino strategy metrics
``sharePrice`` is the NAV per share. Every test uses a MockTransport and the
module cache is cleared around each test, so no test opens a socket.
"""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from app.integrations.prices import kamino_liquidity
from app.integrations.prices.service import PriceService
from app.integrations.types import ChainBalance
from app.nav_reconcile import reconcile_nav
from app.positions_core import mark_covered_by_position
from app.snapshots import _fold_positions, price_holdings

JSOL_MINT = "J5o36oSuGS4nQ7cfZiAuvmaN2bggcoMoqp7BB7zATtwE"
JSOL_STRATEGY = "DvK76xvo2dnAEkDBSVNFWzfJ8WC49rGkRkkAvnQbYTxS"
USDC_MINT = "FVk7hoHvqAkdpNY5Zxr19WzVxd35jgHcaG4FNr1UUVn9"
USDC_STRATEGY = "E5A71YnEizRMxuYWf9JQN3Cf5yrcPB5Y6QdC23Sc4icr"

JLP_MINT = "27G8MtK7VtTcCHkpASjSDdkWWYfoqT6ggEuKidVJidD4"
USDC_MINT_UNDERLYING = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


@pytest.fixture(autouse=True)
def _fresh_cache():
    kamino_liquidity.clear_cache()
    yield
    kamino_liquidity.clear_cache()


def _kjlp_balances() -> list[ChainBalance]:
    return [
        ChainBalance(
            source="helius",
            asset="KJLP-JSOL",
            amount=Decimal("3.356864"),
            chain="solana",
            contract=JSOL_MINT,
        ),
        ChainBalance(
            source="helius",
            asset="KJLP-USDC",
            amount=Decimal("0.032705"),
            chain="solana",
            contract=USDC_MINT,
        ),
    ]


def _metrics_handler(
    *,
    jsol: httpx.Response | None = None,
    usdc: httpx.Response | None = None,
    calls: list[str] | None = None,
):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "coins.llama.fi" in url:
            return httpx.Response(200, json={"coins": {}})
        if calls is not None:
            calls.append(url)
        if "/strategies/" in url and url.endswith(f"/{JSOL_STRATEGY}/metrics?env=mainnet-beta"):
            assert request.url.params.get("env") == "mainnet-beta"
            return jsol or httpx.Response(200, json={"sharePrice": "0.25"})
        if "/strategies/" in url and url.endswith(f"/{USDC_STRATEGY}/metrics?env=mainnet-beta"):
            assert request.url.params.get("env") == "mainnet-beta"
            return usdc or httpx.Response(200, json={"sharePrice": 1.05})
        return httpx.Response(404, json={"error": "unexpected"})

    return handler


def test_two_kjlp_mints_priced_from_strategy_share_price() -> None:
    calls: list[str] = []
    client = httpx.Client(transport=httpx.MockTransport(_metrics_handler(calls=calls)))
    service = PriceService(client=client, use_mock=False)

    capture = price_holdings(_kjlp_balances(), service)
    by_asset = {item.asset: item for item in capture.holdings}

    jsol = by_asset["KJLP-JSOL"]
    usdc = by_asset["KJLP-USDC"]
    assert jsol.price_source == "kamino_strategy_share"
    assert usdc.price_source == "kamino_strategy_share"
    assert jsol.price_usdt == Decimal("0.25")
    assert usdc.price_usdt == Decimal("1.05")
    assert jsol.value_usdt == Decimal("0.84")  # 3.356864 * 0.25
    assert usdc.value_usdt == Decimal("0.03")  # 0.032705 * 1.05
    assert capture.nav_usdt == Decimal("0.87")
    assert capture.unpriced == []
    assert len(calls) == 2

    # A second capture reuses the 300 s share-price cache: one call per mint.
    again = price_holdings(
        _kjlp_balances(),
        PriceService(client=client, use_mock=False),
    )
    assert again.nav_usdt == capture.nav_usdt
    assert len(calls) == 2


def test_metrics_failure_or_missing_share_price_stays_unpriced() -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(
            _metrics_handler(
                jsol=httpx.Response(503, json={"error": "down"}),
                usdc=httpx.Response(200, json={"totalValueLocked": "1"}),
            )
        )
    )
    service = PriceService(client=client, use_mock=False)

    capture = price_holdings(_kjlp_balances(), service)

    assert capture.nav_usdt == Decimal("0.00")
    assert capture.unpriced == ["helius:KJLP-JSOL", "helius:KJLP-USDC"]
    assert all(item.price_usdt is None for item in capture.holdings)
    assert all(item.price_source == "none" for item in capture.holdings)


def test_unknown_mint_never_requests_kamino() -> None:
    calls: list[str] = []
    unknown = "So11111111111111111111111111111111111111112"

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        return httpx.Response(200, json={"coins": {}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    service = PriceService(client=client, use_mock=False)
    capture = price_holdings(
        [
            ChainBalance(
                source="helius",
                asset="KJLP-NOPE",
                amount=Decimal("1"),
                chain="solana",
                contract=unknown,
            )
        ],
        service,
    )

    assert capture.unpriced == ["helius:KJLP-NOPE"]
    assert capture.holdings[0].price_usdt is None
    assert not any("api.kamino.finance" in url for url in calls)
    # Even with an explicit client, a non-allow-listed mint returns None at once.
    assert kamino_liquidity.quote_share_price(f"  {unknown}  ", client=client) is None
    assert not any("api.kamino.finance" in url for url in calls)


def test_kjlp_share_and_open_kamino_lending_do_not_double_count() -> None:
    client = httpx.Client(transport=httpx.MockTransport(_metrics_handler()))
    capture = price_holdings(_kjlp_balances(), PriceService(client=client, use_mock=False))

    # A Kamino lending position's legs are the underlying JLP / USDC, not the
    # KJLP vault share. The share row is therefore not covered by the position.
    positions = [
        {
            "protocol": "kamino",
            "status": "open",
            "net_value_usdt": "150.00",
            "details": {
                "collateral": [{"symbol": "JLP", "contract": JLP_MINT, "amount": "3.7"}],
                "borrow": [
                    {
                        "symbol": "USDC",
                        "contract": USDC_MINT_UNDERLYING,
                        "amount": "30.0",
                    }
                ],
            },
        }
    ]
    marked = mark_covered_by_position(
        [item.as_dict() for item in capture.holdings],
        positions,
    )
    assert [row["covered_by_position"] for row in marked] == [False, False]

    _fold_positions(capture, positions)
    report = reconcile_nav(
        nav_usdt=capture.nav_usdt,
        breakdown=capture.breakdown_json(),
        positions=positions,
    )

    assert report["covered_rows"] == 0
    assert report["uncovered_usdt"] == "0.87"
    assert report["open_position_net_usdt"] == "150.00"
    assert report["expected_usdt"] == "150.87"
    assert report["residual_usdt"] == "0.00"
    assert report["ok"] is True
    # The share stays in the flat holdings, so the NAV identity holds once.
    assert all(item.covered_by_position is False for item in capture.holdings)


def test_mock_price_service_without_client_never_calls_kamino(monkeypatch) -> None:
    def _boom(*_args, **_kwargs):
        raise AssertionError("mock PriceService must not open a socket")

    monkeypatch.setattr("app.integrations.prices.service.quote_share_price", _boom)
    monkeypatch.setattr(kamino_liquidity, "quiet_client", _boom)

    capture = price_holdings(_kjlp_balances(), PriceService(use_mock=True))

    assert capture.nav_usdt == Decimal("0.00")
    assert capture.unpriced == ["helius:KJLP-JSOL", "helius:KJLP-USDC"]
    assert all(item.price_usdt is None for item in capture.holdings)
