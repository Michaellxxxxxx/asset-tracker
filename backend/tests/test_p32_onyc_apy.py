"""p32: Kamino ONyc collateral APY from DefiLlama's onre pool (network mocked)."""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from app.integrations import lending_kamino, yield_sources
from app.integrations.lending_common import take_failures
from app.integrations.yield_sources import YieldQuote

ADDR = "0x1111111111111111111111111111111111111111"
ONYC_MINT = "5Y8NV33Vv7WbnLfq3zBcKSdYPrk7g2KoiQoe7M2tcxp5"
FAKE_ONYC_MINT = "ONycmint"


@pytest.fixture(autouse=True)
def _fresh_yields():
    """No quote/failure/discovery state leaks between these tests."""
    yield_sources.clear_cache()
    lending_kamino.reset_caches()
    take_failures()
    yield
    yield_sources.clear_cache()
    lending_kamino.reset_caches()
    take_failures()


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


# --- DefiLlama ONyc pool ---------------------------------------------------


def test_onyc_defillama_hit_uses_latest_chart_point() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "data": [
                    {"timestamp": "2026-10-31T00:00:00Z", "apy": 10.9, "apyBase": 10.9},
                    {"timestamp": "2026-11-01T00:00:00Z", "apy": 11.02, "apyBase": 11.02},
                ]
            },
        )

    quote = yield_sources.onyc_collateral_apy(client=_client(handler))
    assert quote is not None
    assert quote.apy == Decimal("0.1102")
    assert quote.source == "defillama:onre"
    assert quote.asof == "2026-11-01T00:00:00Z"
    assert seen == [
        f"{yield_sources.DEFILLAMA_BASE}/chart/{yield_sources.ONYC_POOL_ID}"
    ]
    assert yield_sources.ONYC_POOL_ID == "7083d6a5-e3cb-4eeb-8204-f1b735e4ecbb"


def test_onyc_defillama_empty_chart_is_missing() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    assert yield_sources.onyc_collateral_apy(client=_client(handler)) is None


def test_onyc_defillama_timeout_is_missing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("defillama timed out", request=request)

    assert yield_sources.onyc_collateral_apy(client=_client(handler)) is None


def test_onyc_defillama_cache_avoids_second_http() -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"data": [{"apy": 11.02}]})

    first = yield_sources.onyc_collateral_apy(client=_client(handler))
    second = yield_sources.onyc_collateral_apy(client=_client(handler))
    assert first is not None and second is not None
    assert first.apy == Decimal("0.1102")
    assert calls["n"] == 1


# --- Kamino normalize ------------------------------------------------------


def _kamino_onyc_payload(
    *,
    mint: str = ONYC_MINT,
    collateral_usd: str = "8659.75",
    debt_usd: str = "5506",
) -> dict:
    return {
        "markets": [
            {
                "market": "MktOnyc",
                "reserves": [
                    {"liquidityTokenMint": mint, "supplyApy": "0", "borrowApy": "0"},
                    {
                        "liquidityTokenMint": "USDCmint",
                        "supplyApy": "0",
                        "borrowApy": "0.075",
                    },
                ],
                "loans": [
                    {
                        "loanId": "OblOnyc",
                        "loanInfo": {
                            "collateral": {
                                "deposits": [
                                    {
                                        "tokenMint": mint,
                                        "tokenName": "ONyc",
                                        "tokenAmount": "8572.31",
                                        "tokenValue": collateral_usd,
                                        "liquidationLtv": "0.75",
                                    }
                                ]
                            },
                            "debt": {
                                "borrows": [
                                    {
                                        "tokenMint": "USDCmint",
                                        "tokenName": "USDC",
                                        "tokenAmount": debt_usd,
                                        "tokenValue": debt_usd,
                                        "liquidationLtv": "0.75",
                                    }
                                ]
                            },
                            "currentLtv": "0.636",
                        },
                    }
                ],
            }
        ]
    }


def test_kamino_onyc_uses_defillama_onre_apy_in_net_apy(monkeypatch) -> None:
    monkeypatch.setattr(
        lending_kamino, "asof_iso", lambda: "2026-11-01T00:00:00+00:00"
    )
    monkeypatch.setattr(
        lending_kamino,
        "onyc_collateral_apy",
        lambda: YieldQuote(apy=Decimal("0.1102"), source="defillama:onre"),
    )
    rows = lending_kamino.normalize(ADDR, _kamino_onyc_payload())
    assert len(rows) == 1
    details = rows[0]["details"]
    coll = next(leg for leg in details["collateral"] if leg["symbol"] == "ONyc")
    assert coll["apy_supply"] == pytest.approx(0.1102)
    assert coll["apy_source"] == "defillama:onre"
    assert coll["contract"] == ONYC_MINT

    # 8659.75 @ 11.02% collateral, 5506 @ 7.5% debt: hand-computed net carry.
    expected = (
        Decimal("8659.75") * Decimal("0.1102") - Decimal("5506") * Decimal("0.075")
    ) / (Decimal("8659.75") - Decimal("5506"))
    assert details["net_apy"] == pytest.approx(float(expected))
    assert details["apy_estimated"] is False
    assert details["apy_basis"] == "reported_rates"
    assert details["yield_data_missing"] is False
    assert "apy_outlier" not in details


def test_kamino_onyc_feed_miss_is_missing_not_price_estimate(monkeypatch) -> None:
    monkeypatch.setattr(
        lending_kamino, "asof_iso", lambda: "2026-11-01T00:00:00+00:00"
    )
    monkeypatch.setattr(lending_kamino, "onyc_collateral_apy", lambda: None)
    rows = lending_kamino.normalize(ADDR, _kamino_onyc_payload())
    assert len(rows) == 1
    details = rows[0]["details"]
    coll = next(leg for leg in details["collateral"] if leg["symbol"] == "ONyc")
    assert "apy_supply" not in coll
    assert "apy_source" not in coll
    assert details["net_apy"] is None
    assert details["apy_estimated"] is False
    assert details["apy_basis"] == "insufficient"
    assert details["yield_data_missing"] is True


def test_kamino_onyc_feed_exception_is_missing(monkeypatch) -> None:
    monkeypatch.setattr(
        lending_kamino, "asof_iso", lambda: "2026-11-01T00:00:00+00:00"
    )

    def down():
        raise RuntimeError("defillama onre pool down")

    monkeypatch.setattr(lending_kamino, "onyc_collateral_apy", down)
    rows = lending_kamino.normalize(ADDR, _kamino_onyc_payload())
    assert len(rows) == 1
    details = rows[0]["details"]
    coll = next(leg for leg in details["collateral"] if leg["symbol"] == "ONyc")
    assert "apy_supply" not in coll
    assert "apy_source" not in coll
    assert details["net_apy"] is None
    assert details["yield_data_missing"] is True


def test_kamino_fake_onyc_mint_does_not_consult_feed(monkeypatch) -> None:
    monkeypatch.setattr(
        lending_kamino, "asof_iso", lambda: "2026-11-01T00:00:00+00:00"
    )

    def boom():
        raise AssertionError("only the real ONyc mint may read defillama:onre")

    monkeypatch.setattr(lending_kamino, "onyc_collateral_apy", boom)
    rows = lending_kamino.normalize(
        ADDR, _kamino_onyc_payload(mint=FAKE_ONYC_MINT)
    )
    assert len(rows) == 1
    details = rows[0]["details"]
    coll = next(leg for leg in details["collateral"] if leg["symbol"] == "ONyc")
    # The reserve's zero supply rate stays in place; it is not replaced by the
    # official ONyc feed for a lookalike mint.
    assert coll["apy_supply"] == pytest.approx(0)
    assert "apy_source" not in coll
