"""p23: USD3 DefiLlama and JLP Jupiter yield feeds (network is mocked)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.integrations import lending_kamino, lending_morpho, lending_pendle_apy, yield_sources
from app.integrations.lending_common import take_failures
from app.integrations.yield_sources import YieldQuote

FIXTURES = Path(__file__).parent / "fixtures" / "lending_evm"
ADDR = "0x1111111111111111111111111111111111111111"
JLP_MINT = "27G8MtK7VtTcCHkpASjSDdkWWYfoqT6ggEuKidVJidD4"


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


def _load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# --- DefiLlama USD3 -------------------------------------------------------


def test_usd3_defillama_hit_uses_latest_chart_point() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "data": [
                    {"timestamp": "2026-10-30T00:00:00Z", "apy": 5.9, "apyBase": 5.9},
                    {"timestamp": "2026-10-31T00:00:00Z", "apy": 6.33, "apyBase": 6.33},
                ]
            },
        )

    quote = yield_sources.usd3_collateral_apy(client=_client(handler))
    assert quote is not None
    assert quote.apy == Decimal("0.0633")
    assert quote.source == "defillama:3jane-lending"
    assert quote.asof == "2026-10-31T00:00:00Z"
    assert seen == [
        f"{yield_sources.DEFILLAMA_BASE}/chart/{yield_sources.USD3_POOL_ID}"
    ]


def test_usd3_defillama_empty_chart_is_missing() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    assert yield_sources.usd3_collateral_apy(client=_client(handler)) is None


def test_usd3_defillama_timeout_is_missing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("defillama timed out", request=request)

    assert yield_sources.usd3_collateral_apy(client=_client(handler)) is None


def test_usd3_defillama_cache_avoids_second_http() -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"data": [{"apy": 6.33}]})

    first = yield_sources.usd3_collateral_apy(client=_client(handler))
    second = yield_sources.usd3_collateral_apy(client=_client(handler))
    assert first is not None and second is not None
    assert calls["n"] == 1


# --- Jupiter JLP ----------------------------------------------------------


def test_jlp_info_hit_reads_jlp_apy_pct() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == yield_sources.JUPITER_JLP_URL
        return httpx.Response(
            200,
            json={
                "jlpApyPct": 6.28,
                "jlpAprPct": 6.09,
                "jlpAprLastUpdatedTimestamp": 1761840000,
            },
        )

    quote = yield_sources.jlp_collateral_apy(client=_client(handler))
    assert quote is not None
    assert quote.apy == Decimal("0.0628")
    assert quote.source == "jupiter:jlp-info"
    assert quote.asof == 1761840000


def test_jlp_info_without_apy_is_missing_not_zero() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jlpAprPct": 6.09})

    assert yield_sources.jlp_collateral_apy(client=_client(handler)) is None


# --- Morpho collateral leg ------------------------------------------------


def _morpho_usd3_payload() -> dict:
    payload = _load("morpho_user.json")
    position = payload["data"]["userByAddress"]["marketPositions"][0]
    position["market"]["collateralAsset"]["symbol"] = "USD3"
    return payload


def test_morpho_usd3_match_is_exact() -> None:
    assert lending_morpho._is_usd3("ethereum", "usd3") is True
    assert lending_morpho._is_usd3("ethereum", "PT-USD3-17DEC2026") is False
    assert lending_morpho._is_usd3("ethereum", "sUSD3") is False
    assert lending_morpho._is_usd3("base", "USD3") is False


def test_morpho_usd3_uses_defillama_apy_in_net_apy(monkeypatch) -> None:
    monkeypatch.setattr(
        lending_morpho,
        "usd3_collateral_apy",
        lambda **_kwargs: YieldQuote(
            apy=Decimal("0.0633"), source="defillama:3jane-lending"
        ),
    )
    rows = lending_morpho.normalize(ADDR, _morpho_usd3_payload())
    borrowed = next(row for row in rows if Decimal(row["debt_usdt"]) > 0)
    coll = next(leg for leg in borrowed["details"]["collateral"] if leg["symbol"] == "USD3")
    assert coll["apy_supply"] == pytest.approx(0.0633)
    assert coll["apy_source"] == "defillama:3jane-lending"

    # Fixture: 6000 collateral, 1500 debt @ 5.5%, USD3 @ 6.33%.
    expected = (
        Decimal("6000") * Decimal("0.0633") - Decimal("1500") * Decimal("0.055")
    ) / Decimal("4500")
    assert borrowed["details"]["net_apy"] == pytest.approx(float(expected))
    assert borrowed["details"].get("apy_estimated") is not True
    assert borrowed["details"]["yield_data_missing"] is False


def test_morpho_usd3_feed_miss_keeps_yield_missing(monkeypatch) -> None:
    monkeypatch.setattr(lending_morpho, "usd3_collateral_apy", lambda **_k: None)
    rows = lending_morpho.normalize(ADDR, _morpho_usd3_payload())
    borrowed = next(row for row in rows if Decimal(row["debt_usdt"]) > 0)
    coll = next(leg for leg in borrowed["details"]["collateral"] if leg["symbol"] == "USD3")
    assert "apy_supply" not in coll
    assert "apy_source" not in coll
    assert borrowed["details"]["yield_data_missing"] is True
    assert borrowed["details"]["net_apy"] is None


def test_morpho_pt_usd3_does_not_consult_defillama(monkeypatch) -> None:
    def boom(**_kwargs):
        raise AssertionError("PT-USD3 must use Pendle, not the USD3 pool")

    monkeypatch.setattr(lending_morpho, "usd3_collateral_apy", boom)
    monkeypatch.setattr(
        lending_morpho,
        "pendle_implied_apy",
        lambda *_a, **_k: lending_pendle_apy.PendleImpliedQuote(
            implied_apy=0.15, expired=False
        ),
    )
    payload = _load("morpho_user.json")
    position = payload["data"]["userByAddress"]["marketPositions"][0]
    position["market"]["collateralAsset"]["symbol"] = "PT-USD3-17DEC2026"
    position["market"]["collateralAsset"]["address"] = (
        "0x7f47c3e6b2c00fc4eb4d5ae50d0ab0ab6888eb4d"
    )
    rows = lending_morpho.normalize(ADDR, payload)
    borrowed = next(row for row in rows if Decimal(row["debt_usdt"]) > 0)
    coll = next(
        leg
        for leg in borrowed["details"]["collateral"]
        if str(leg["symbol"]).startswith("PT-")
    )
    assert coll["apy_supply"] == pytest.approx(0.15)
    assert coll["apy_source"] == "pendle_implied"


# --- Kamino JLP collateral leg -------------------------------------------


def _kamino_jlp_payload(*, borrow_apy: str = "0.08") -> dict:
    return {
        "markets": [
            {
                "market": "MktJlp",
                "reserves": [
                    {"liquidityTokenMint": JLP_MINT, "supplyApy": "0", "borrowApy": "0"},
                    {
                        "liquidityTokenMint": "USDCmint",
                        "supplyApy": "0",
                        "borrowApy": borrow_apy,
                    },
                ],
                "loans": [
                    {
                        "loanId": "OblJlp",
                        "loanInfo": {
                            "collateral": {
                                "deposits": [
                                    {
                                        "tokenMint": JLP_MINT,
                                        "tokenName": "JLP",
                                        "tokenAmount": "9306",
                                        "tokenValue": "9306",
                                        "liquidationLtv": "0.75",
                                    }
                                ]
                            },
                            "debt": {
                                "borrows": [
                                    {
                                        "tokenMint": "USDCmint",
                                        "tokenName": "USDC",
                                        "tokenAmount": "4594.66",
                                        "tokenValue": "4594.66",
                                        "liquidationLtv": "0.75",
                                    }
                                ]
                            },
                            "currentLtv": "0.4937",
                        },
                    }
                ],
            }
        ]
    }


def test_kamino_jlp_uses_jupiter_apy_in_net_apy(monkeypatch) -> None:
    monkeypatch.setattr(
        lending_kamino, "asof_iso", lambda: "2026-10-31T00:00:00+00:00"
    )
    monkeypatch.setattr(
        lending_kamino,
        "jlp_collateral_apy",
        lambda: YieldQuote(apy=Decimal("0.0628"), source="jupiter:jlp-info"),
    )
    rows = lending_kamino.normalize(ADDR, _kamino_jlp_payload())
    assert len(rows) == 1
    details = rows[0]["details"]
    coll = next(leg for leg in details["collateral"] if leg["symbol"] == "JLP")
    assert coll["apy_supply"] == pytest.approx(0.0628)
    assert coll["apy_source"] == "jupiter:jlp-info"

    # 9306 @ 6.28% collateral, 4594.66 @ 8% debt: hand-computed net carry.
    expected = (
        Decimal("9306") * Decimal("0.0628")
        - Decimal("4594.66") * Decimal("0.08")
    ) / (Decimal("9306") - Decimal("4594.66"))
    assert details["net_apy"] == pytest.approx(float(expected))
    assert details["apy_estimated"] is False
    assert details["apy_basis"] == "reported_rates"
    assert details["yield_data_missing"] is False
    assert "apy_estimate_raw" not in details


def test_kamino_jlp_official_apy_keeps_price_estimate_as_audit(monkeypatch) -> None:
    monkeypatch.setattr(
        lending_kamino, "asof_iso", lambda: "2026-10-31T00:00:00+00:00"
    )
    monkeypatch.setattr(
        lending_kamino,
        "jlp_collateral_apy",
        lambda: YieldQuote(apy=Decimal("0.0628"), source="jupiter:jlp-info"),
    )
    payload = _kamino_jlp_payload()
    payload["markets"][0].update(
        {
            "pnl": {"OblJlp": {"usd": "12240", "invested": {"usd": "4711.34"}}},
            "interest_earned": {
                "OblJlp": {
                    "totalFeesEarnedObligation": {"oblig": {"usdFees": "0"}}
                }
            },
            "interest_paid": {
                "OblJlp": {
                    "totalFeesPaidObligation": {"oblig": {"usdFees": "0"}},
                    "historicalFeesObligation": {
                        "oblig": [
                            {"reserve": {"ts": 1761868800000, "usdFees": "0"}}
                        ]
                    },
                }
            },
        }
    )
    rows = lending_kamino.normalize(ADDR, payload)
    details = rows[0]["details"]

    # Official Jupiter APY is published; the legacy price-return (252%) is only
    # an audit value and must not be suppressed or promoted to net_apy.
    expected = (
        Decimal("9306") * Decimal("0.0628")
        - Decimal("4594.66") * Decimal("0.08")
    ) / (Decimal("9306") - Decimal("4594.66"))
    assert details["net_apy"] == pytest.approx(float(expected))
    assert details["apy_estimated"] is False
    assert details.get("apy_outlier") is not True
    assert "apy_note" not in details
    raw = (
        Decimal("12240") - Decimal("4594.66") * Decimal("0.08")
    ) / (Decimal("9306") - Decimal("4594.66"))
    assert details["apy_estimate_raw"] == pytest.approx(float(raw))
    assert raw > 1


def test_kamino_jlp_feed_failure_marks_yield_missing(monkeypatch) -> None:
    monkeypatch.setattr(
        lending_kamino, "asof_iso", lambda: "2026-10-31T00:00:00+00:00"
    )

    def down():
        raise RuntimeError("jupiter jlp-info down")

    monkeypatch.setattr(lending_kamino, "jlp_collateral_apy", down)
    rows = lending_kamino.normalize(ADDR, _kamino_jlp_payload())
    assert len(rows) == 1
    details = rows[0]["details"]
    coll = next(leg for leg in details["collateral"] if leg["symbol"] == "JLP")
    assert "apy_supply" not in coll
    assert "apy_source" not in coll
    assert details["net_apy"] is None
    assert details["apy_estimated"] is False
    assert details["yield_data_missing"] is True