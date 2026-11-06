"""EVM lending readers (Aave / Morpho / Pendle). Network is mocked; no DB writes."""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.integrations import lending_aave, lending_morpho, lending_pendle, lending_pendle_apy
from app.integrations.lending_aave import normalize_rpc_decoded, normalize_subgraph
from app.integrations.lending_common import health_factor
from app.logging_redact import redact_text
from app.positions_core import upsert_positions

FIXTURES = Path(__file__).parent / "fixtures" / "lending_evm"
ADDR = "0x1111111111111111111111111111111111111111"
ALCHEMY_KEY = "test-alchemy-key-do-not-leak"


def _load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _net(row: dict) -> Decimal:
    return Decimal(row["collateral_usdt"]) - Decimal(row["debt_usdt"])


# --- Aave -----------------------------------------------------------------


def test_aave_normal_from_fixture_net_and_hf() -> None:
    rows = lending_aave.fetch_positions(ADDR, use_mock=True)
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "lending"
    assert row["protocol"] == "aave"
    assert row["chain"] == "ethereum"
    assert row["account_ref"] == ADDR.lower()
    assert row["market_ref"] == "aave-v3-ethereum"
    # 2.5 WETH * 3200 + 2000 USDC = 10000 collateral; 3000 USDT debt.
    assert Decimal(row["collateral_usdt"]) == Decimal("10000.00000000")
    assert Decimal(row["debt_usdt"]) == Decimal("3000.00000000")
    assert Decimal(row["net_value_usdt"]) == _net(row) == Decimal("7000.00000000")
    details = row["details"]
    assert details["health_factor"] == pytest.approx(1.85)
    assert {leg["symbol"] for leg in details["collateral"]} == {"aEthWETH", "aEthUSDC"}
    assert details["borrow"][0]["symbol"] == "variableDebtEthUSDT"
    assert details["borrow"][0]["contract"] == "0x6df1c1e379bc5a00a7b4c6e67a203333772f45a8"
    assert details["liq_price"] is None
    for key in (
        "ltv",
        "liq_threshold",
        "net_apy",
        "interest_paid_usd",
        "interest_earned_usd",
        "pnl_usd",
        "pnl_apr",
    ):
        assert key in details


def test_aave_empty_wallet() -> None:
    assert normalize_subgraph(ADDR, _load("aave_empty.json"), chain="ethereum") == []


def test_aave_http_error_returns_empty(caplog: pytest.LogCaptureFixture) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "test-alchemy" not in str(request.url)
        return httpx.Response(503, json={"error": "down"})

    with caplog.at_level(logging.WARNING):
        rows = lending_aave.fetch_positions(ADDR, client=_client(handler), chains=("ethereum",))
    assert rows == []
    assert "test-alchemy" not in caplog.text
    assert "503" not in caplog.text or "aave" in caplog.text


def test_aave_hf_fallback_when_protocol_omits_it() -> None:
    payload = _load("aave_user.json")
    payload["data"]["user"] = {}
    rows = normalize_subgraph(ADDR, payload, chain="ethereum")
    # weighted threshold: 8000*0.83 + 2000*0.78 = 8200; 8200/3000 = 2.7333...
    assert rows[0]["details"]["health_factor"] == pytest.approx(8200 / 3000)
    assert rows[0]["details"]["liq_threshold"] == pytest.approx(0.82)


def test_aave_rpc_account_data_and_bad_chain_does_not_sink_ethereum() -> None:
    """getUserAccountData words: collateral, debt, available, liqThreshold, ltv, hf."""
    collateral = 10_000 * 10**8
    debt = 3_000 * 10**8
    available = 0
    liq = 8000  # 0.80 in bps
    ltv = 7200
    hf = 185 * 10**16  # 1.85 wad
    words = [collateral, debt, available, liq, ltv, hf]
    blob = "0x" + "".join(f"{word:064x}" for word in words)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if body.get("method") == "eth_call":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": blob})
        return httpx.Response(500, json={"errors": [{"message": "subgraph down"}]})

    rows = lending_aave.fetch_positions(
        ADDR.upper(),
        client=_client(handler),
        chains=("nope", "ethereum"),
        api_key=ALCHEMY_KEY,
    )
    assert len(rows) == 1
    assert rows[0]["account_ref"] == ADDR.lower()
    assert Decimal(rows[0]["net_value_usdt"]) == Decimal("7000.00000000")
    assert rows[0]["details"]["health_factor"] == pytest.approx(1.85)
    assert ALCHEMY_KEY not in rows[0]["market_ref"]


def test_aave_no_debt_sentinel_hf_is_null() -> None:
    account = {
        "totalCollateralBase": 1_000 * 10**8,
        "totalDebtBase": 0,
        "ltv": 0,
        "currentLiquidationThreshold": 8000,
        "healthFactor": 2**256 - 1,
    }
    rows = normalize_rpc_decoded(ADDR, "ethereum", account, [])
    assert rows[0]["details"]["health_factor"] is None
    assert Decimal(rows[0]["debt_usdt"]) == 0


def test_aave_graphql_market_net_and_one_bad_market_skipped() -> None:
    legs = _load("aave_gql.json")
    state = _load("aave_gql_state.json")
    calls = {"state": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        query = body.get("query") or ""
        if "userMarketState" in query:
            calls["state"] += 1
            if calls["state"] == 1:
                return httpx.Response(500, json={"errors": [{"message": "state down"}]})
            return httpx.Response(200, json=state)
        if "userSupplies" in query:
            # Second market in the same response must survive the first state's failure.
            other = {"name": "AaveV3EthereumLido", "address": "0x4e033931ad43597d96D6bcc25c280717730B58B1"}
            extra_supplies = json.loads(json.dumps(legs["data"]["supplies"]))
            for row in extra_supplies:
                row["market"] = other
            extra_borrow = json.loads(json.dumps(legs["data"]["borrows"][0]))
            extra_borrow["market"] = other
            payload = {
                "data": {
                    "supplies": legs["data"]["supplies"] + extra_supplies,
                    "borrows": legs["data"]["borrows"] + [extra_borrow],
                }
            }
            return httpx.Response(200, json=payload)
        return httpx.Response(404, json={"error": "unexpected"})

    rows = lending_aave.fetch_positions(ADDR, client=_client(handler), chains=("ethereum",))
    assert len(rows) == 1
    row = rows[0]
    assert row["market_ref"] == "aave-v3-ethereum-0x4e033931ad43597d96d6bcc25c280717730b58b1"
    assert Decimal(row["collateral_usdt"]) == Decimal("10000.00000000")
    assert Decimal(row["debt_usdt"]) == Decimal("3000.00000000")
    assert Decimal(row["net_value_usdt"]) == _net(row)
    assert row["details"]["health_factor"] == pytest.approx(1.85)
    assert row["details"]["liq_threshold"] == pytest.approx(0.82)
    assert {leg["symbol"] for leg in row["details"]["collateral"]} == {"WETH", "USDC"}


def test_aave_rows_feed_upsert(db) -> None:
    from app.auth import hash_password
    from app.models import User

    user = User(username="aave-feed", hashed_password=hash_password("pass-word"))
    db.add(user)
    db.commit()
    rows = lending_aave.fetch_positions(ADDR, use_mock=True)
    written = upsert_positions(db, user.id, rows)
    assert len(written) == 1
    assert written[0].protocol == "aave"
    assert written[0].net_value_usdt == Decimal("7000.00000000")


# --- Morpho ---------------------------------------------------------------


def test_morpho_normal_two_markets_and_broken_market_skipped() -> None:
    rows = lending_morpho.normalize(ADDR, _load("morpho_user.json"))
    assert len(rows) == 2
    borrowed = next(row for row in rows if Decimal(row["debt_usdt"]) > 0)
    assert borrowed["protocol"] == "morpho"
    assert borrowed["chain"] == "ethereum"
    assert borrowed["account_ref"] == ADDR.lower()
    assert Decimal(borrowed["collateral_usdt"]) == Decimal("6000.00000000")
    assert Decimal(borrowed["debt_usdt"]) == Decimal("1500.00000000")
    assert Decimal(borrowed["net_value_usdt"]) == _net(borrowed)
    # Protocol omitted HF: 6000 * 0.86 / 1500 = 3.44
    assert borrowed["details"]["health_factor"] == pytest.approx(3.44)
    assert borrowed["details"]["liq_threshold"] == pytest.approx(0.86)
    assert borrowed["details"]["liq_price"] == pytest.approx(1500 / (1.8 * 0.86))
    # Collateral (WETH) has no known yield. market supplyApy is the loan rate,
    # not the collateral rate, so net APY must not be computed as a fake negative.
    assert borrowed["details"]["yield_data_missing"] is True
    assert borrowed["details"]["net_apy"] is None
    coll_leg = next(leg for leg in borrowed["details"]["collateral"] if leg["symbol"] == "WETH")
    assert "apy_supply" not in coll_leg
    assert borrowed["details"]["borrow"][0]["apy_borrow"] == pytest.approx(0.055)
    supplied = next(row for row in rows if Decimal(row["debt_usdt"]) == 0)
    assert Decimal(supplied["net_value_usdt"]) == Decimal("5000.00000000")
    assert supplied["details"]["borrow"] == []
    assert supplied["details"]["health_factor"] is None
    # Supplier-only: the loan asset earns supplyApy, so net APY stays known.
    assert supplied["details"].get("yield_data_missing") is not True
    assert supplied["details"]["net_apy"] == pytest.approx(0.04)
    assert supplied["details"]["collateral"][0]["apy_supply"] == pytest.approx(0.04)


def _morpho_nested_price_payload() -> dict:
    """The 2026-10 Morpho schema: ``Asset.price`` is ``{usd}``, not ``priceUsd``."""
    payload = _load("morpho_user.json")
    for position in payload["data"]["userByAddress"]["marketPositions"]:
        market = position.get("market") or {}
        for key in ("loanAsset", "collateralAsset"):
            asset = market.get(key)
            if isinstance(asset, dict) and "priceUsd" in asset:
                asset["price"] = {"usd": asset.pop("priceUsd")}
    return payload


def test_morpho_normalize_accepts_current_nested_asset_price() -> None:
    legacy = lending_morpho.normalize(ADDR, _load("morpho_user.json"))
    current = lending_morpho.normalize(ADDR, _morpho_nested_price_payload())
    assert len(legacy) == len(current) == 2
    for old, new in zip(legacy, current):
        assert new["label"] == old["label"]
        assert Decimal(new["collateral_usdt"]) == Decimal(old["collateral_usdt"])
        assert Decimal(new["debt_usdt"]) == Decimal(old["debt_usdt"])
        assert new["details"]["health_factor"] == pytest.approx(old["details"]["health_factor"])
        assert new["details"]["liq_price"] == pytest.approx(old["details"]["liq_price"])


def test_morpho_fetch_queries_nested_asset_price() -> None:
    """A scalar ``priceUsd`` selection is a GraphQL 400 on the live schema."""
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(200, json=_morpho_nested_price_payload())

    rows = lending_morpho.fetch_positions(ADDR, client=_client(handler))
    assert len(rows) == 2
    assert requests, "adapter did not call the Morpho API"
    query = requests[0]["query"]
    assert "price { usd }" in query
    assert "priceUsd" not in query


def test_morpho_unknown_collateral_apy_omits_net_apy() -> None:
    """PT/USD3-style collateral: supplyApy present, but it is not the collateral yield."""
    payload = _load("morpho_user.json")
    positions = payload["data"]["userByAddress"]["marketPositions"]
    positions[0]["market"]["state"] = {"supplyApy": 0.03, "borrowApy": 0.055}
    positions[0]["market"]["collateralAsset"]["symbol"] = "PT-USD3"
    rows = lending_morpho.normalize(ADDR, payload)
    borrowed = next(row for row in rows if Decimal(row["debt_usdt"]) > 0)
    assert borrowed["details"]["net_apy"] is None
    assert borrowed["details"]["yield_data_missing"] is True
    coll = next(leg for leg in borrowed["details"]["collateral"] if leg["symbol"] == "PT-USD3")
    assert "apy_supply" not in coll
    assert borrowed["details"]["borrow"][0]["apy_borrow"] == pytest.approx(0.055)


def test_morpho_empty_user() -> None:
    assert lending_morpho.normalize(ADDR, _load("morpho_empty.json")) == []


def test_morpho_http_error_returns_empty(caplog: pytest.LogCaptureFixture) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns failed")

    with caplog.at_level(logging.WARNING):
        rows = lending_morpho.fetch_positions(ADDR, client=_client(handler))
    assert rows == []
    text = caplog.text
    assert "blue-api.morpho.org" not in text or "?" not in text
    assert "ConnectError" in text


def test_morpho_mock_and_net_equals_collateral_minus_debt() -> None:
    rows = lending_morpho.fetch_positions(ADDR, use_mock=True)
    assert rows
    for row in rows:
        assert Decimal(row["net_value_usdt"]) == _net(row)
        assert row["kind"] == "lending"


# --- Pendle ---------------------------------------------------------------


def test_pendle_returns_only_leverage_not_pt_yt_lp() -> None:
    rows = lending_pendle.normalize(ADDR, _load("pendle_user.json"), chain="ethereum")
    assert len(rows) == 1
    row = rows[0]
    assert row["protocol"] == "pendle"
    assert row["market_ref"] == "pendle-loop-pt-susdx"
    assert Decimal(row["collateral_usdt"]) == Decimal("1960.00000000")
    assert Decimal(row["debt_usdt"]) == Decimal("1000.00000000")
    assert Decimal(row["net_value_usdt"]) == _net(row) == Decimal("960.00000000")
    # No protocol HF: 1960 * 0.86 / 1000 = 1.6856
    assert row["details"]["health_factor"] == pytest.approx(1960 * 0.86 / 1000)
    symbols = {leg["symbol"] for leg in row["details"]["collateral"]}
    assert "PT-sUSDx" in symbols
    assert "PLP-sUSDx" not in symbols


def test_pendle_spot_only_is_empty() -> None:
    assert lending_pendle.normalize(ADDR, _load("pendle_spot_only.json"), chain="ethereum") == []
    assert lending_pendle.fetch_positions(ADDR, use_mock=False, client=_client(lambda _r: httpx.Response(200, json=_load("pendle_spot_only.json")))) == []


def test_pendle_loop_api_shape_net_and_hf() -> None:
    payload = _load("pendle_loop.json")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["owner"] == ADDR.lower()
        assert "dashboard" not in str(request.url)
        return httpx.Response(200, json=payload)

    rows = lending_pendle.fetch_positions(ADDR, client=_client(handler))
    assert len(rows) == 1
    row = rows[0]
    assert row["protocol"] == "pendle"
    assert row["market_ref"] == "pendle-loop-pt-susdx"
    assert Decimal(row["net_value_usdt"]) == _net(row) == Decimal("960.00000000")
    # Pendle reports max(0, 1 - LTV/LLTV) = 0.16856. Alerts use collateral * LLTV / debt.
    assert row["details"]["health_factor"] == pytest.approx(1960 * 0.86 / 1000)
    assert row["details"]["pendle_health_factor"] == pytest.approx(0.16856)
    assert row["details"]["liq_threshold"] == pytest.approx(0.86)
    assert row["details"]["liq_price"] == pytest.approx(0.58)
    assert row["details"]["collateral"][0]["symbol"] == "PT"


def test_pendle_http_error_returns_empty() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    assert lending_pendle.fetch_positions(ADDR, client=_client(handler)) == []


def test_pendle_mock_does_not_emit_susdx_spot() -> None:
    rows = lending_pendle.fetch_positions(ADDR, use_mock=True)
    assert len(rows) == 1
    blob = json.dumps(rows)
    assert "PLP-sUSDx" not in blob
    assert "YT-sUSDx" not in blob


# --- shared ---------------------------------------------------------------


def test_health_factor_helper_divide_and_sentinel() -> None:
    assert health_factor(None, collateral_usd=Decimal("100"), debt_usd=Decimal("40"), liq_threshold=Decimal("0.8")) == pytest.approx(2.0)
    assert health_factor(Decimal("1e30"), collateral_usd=Decimal("100"), debt_usd=Decimal("0"), liq_threshold=Decimal("0.8")) is None
    assert health_factor("1.2", collateral_usd=Decimal("1"), debt_usd=Decimal("1"), liq_threshold=Decimal("1")) == pytest.approx(1.2)


# --- Pendle implied APY (Morpho PT collateral) ----------------------------

PT_ADDR = "0x7f47c3e6b2c00fc4eb4d5ae50d0ab0ab6888eb4d"
IMPLIED = 0.15346028625504649


@pytest.fixture(autouse=False)
def _clear_pendle_cache():
    lending_pendle_apy.clear_cache()
    yield
    lending_pendle_apy.clear_cache()


def test_pendle_implied_apy_hit(_clear_pendle_cache) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["pt"] == PT_ADDR
        assert "/v1/1/markets" in str(request.url)
        return httpx.Response(200, json=_load("pendle_pt_usd3_17dec2026_market.json"))

    quote = lending_pendle_apy.pendle_implied_apy("ethereum", PT_ADDR, client=_client(handler))
    assert quote is not None
    assert quote.implied_apy == pytest.approx(IMPLIED)
    assert quote.source == "pendle_implied"
    assert quote.expired is False


def test_pendle_implied_apy_miss(_clear_pendle_cache) -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=_load("pendle_markets_empty.json"))

    usd3 = "0x056b269eb1f75477a8666ae8c7fe01b64dd55ecc"
    assert lending_pendle_apy.pendle_implied_apy(1, usd3, client=_client(handler)) is None
    # total=0 is a 300s negative cache, not a per-sync miss.
    assert lending_pendle_apy.pendle_implied_apy("ethereum", usd3.upper(), client=_client(handler)) is None
    assert calls["n"] == 1


def test_pendle_implied_apy_expired(_clear_pendle_cache) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_load("pendle_pt_expired.json"))

    quote = lending_pendle_apy.pendle_implied_apy("ethereum", "0x" + "11" * 20, client=_client(handler))
    assert quote is not None
    assert quote.implied_apy == 0.0
    assert quote.expired is True


def test_pendle_implied_apy_timeout_500_400_return_none(_clear_pendle_cache) -> None:
    def timeout(_request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("slow")

    def server(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    def bad(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"message": "pt must be an Ethereum address"})

    assert lending_pendle_apy.pendle_implied_apy("ethereum", PT_ADDR, client=_client(timeout)) is None
    assert lending_pendle_apy.pendle_implied_apy("ethereum", PT_ADDR, client=_client(server)) is None
    assert lending_pendle_apy.pendle_implied_apy("ethereum", PT_ADDR, client=_client(bad)) is None
    # Unknown chain does not call the network.
    assert lending_pendle_apy.pendle_implied_apy("solana", PT_ADDR, client=_client(lambda _r: (_ for _ in ()).throw(AssertionError("no http")))) is None


def test_pendle_implied_apy_429_skips_rest_of_round(_clear_pendle_cache) -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, headers={"x-ratelimit-reset": "4102444800"}, json={"message": "slow down"})

    other = "0x" + "ab" * 20
    assert lending_pendle_apy.pendle_implied_apy("ethereum", PT_ADDR, client=_client(handler)) is None
    assert lending_pendle_apy.pendle_implied_apy("ethereum", other, client=_client(handler)) is None
    assert calls["n"] == 1


def test_pendle_implied_apy_cache_hit_one_http(_clear_pendle_cache) -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=_load("pendle_pt_usd3_17dec2026_market.json"))

    first = lending_pendle_apy.pendle_implied_apy("ethereum", PT_ADDR, client=_client(handler))
    second = lending_pendle_apy.pendle_implied_apy("ethereum", PT_ADDR.upper(), client=_client(handler))
    assert first is not None and second is not None
    assert second.implied_apy == pytest.approx(IMPLIED)
    assert calls["n"] == 1


def test_pendle_implied_nonfinite_is_failure(_clear_pendle_cache) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        body = _load("pendle_pt_usd3_17dec2026_market.json")
        body["results"][0]["impliedApy"] = None
        return httpx.Response(200, json=body)

    assert lending_pendle_apy.pendle_implied_apy("ethereum", PT_ADDR, client=_client(handler)) is None


def _morpho_pt_payload() -> dict:
    payload = _load("morpho_user.json")
    pos = payload["data"]["userByAddress"]["marketPositions"][0]
    pos["state"]["collateralUsd"] = 8410.73378595
    pos["state"]["borrowAssetsUsd"] = 6168.55294061
    pos["state"]["borrowApy"] = 0.07416359363366337
    pos["market"]["state"] = {"supplyApy": 0.06633, "borrowApy": 0.07416359363366337}
    pos["market"]["collateralAsset"]["symbol"] = "PT-USD3-17DEC2026"
    pos["market"]["collateralAsset"]["address"] = PT_ADDR
    return payload


def test_morpho_pt_uses_pendle_implied_not_underlying(monkeypatch, _clear_pendle_cache) -> None:
    monkeypatch.setattr(
        lending_morpho,
        "pendle_implied_apy",
        lambda chain, address, **_kwargs: lending_pendle_apy.PendleImpliedQuote(implied_apy=IMPLIED, expired=False),
    )
    rows = lending_morpho.normalize(ADDR, _morpho_pt_payload())
    borrowed = next(row for row in rows if Decimal(row["debt_usdt"]) > 0)
    coll = next(leg for leg in borrowed["details"]["collateral"] if str(leg["symbol"]).startswith("PT-"))
    assert coll["apy_supply"] == pytest.approx(IMPLIED)
    assert coll["apy_source"] == "pendle_implied"
    assert "apy_expired" not in coll
    # Must not pick up Morpho supplyApy (0.06633) or Pendle underlyingApy.
    assert coll["apy_supply"] != pytest.approx(0.06633)
    earned = Decimal("8410.73378595") * Decimal(str(IMPLIED))
    paid = Decimal("6168.55294061") * Decimal("0.07416359363366337")
    net = Decimal("8410.73378595") - Decimal("6168.55294061")
    assert borrowed["details"]["net_apy"] == pytest.approx(float((earned - paid) / net))
    assert borrowed["details"]["net_apy"] == pytest.approx(0.37161657, abs=1e-6)
    assert borrowed["details"]["apy_estimated"] is True
    assert borrowed["details"]["yield_data_missing"] is False


def test_morpho_pt_miss_marks_yield_missing(monkeypatch, _clear_pendle_cache) -> None:
    monkeypatch.setattr(lending_morpho, "pendle_implied_apy", lambda *_a, **_k: None)
    # The PT fixture is retagged as USD3 here; keep the DefiLlama feed offline.
    monkeypatch.setattr(lending_morpho, "usd3_collateral_apy", lambda **_k: None)
    payload = _morpho_pt_payload()
    payload["data"]["userByAddress"]["marketPositions"][0]["market"]["collateralAsset"]["symbol"] = "USD3"
    rows = lending_morpho.normalize(ADDR, payload)
    borrowed = next(row for row in rows if Decimal(row["debt_usdt"]) > 0)
    coll = next(leg for leg in borrowed["details"]["collateral"] if leg["symbol"] == "USD3")
    assert "apy_supply" not in coll
    assert "apy_source" not in coll
    assert borrowed["details"]["yield_data_missing"] is True
    assert borrowed["details"]["net_apy"] is None
    assert borrowed["details"]["net_apy"] != 0


def test_morpho_pt_expired_is_zero_not_missing(monkeypatch, _clear_pendle_cache) -> None:
    monkeypatch.setattr(
        lending_morpho,
        "pendle_implied_apy",
        lambda *_a, **_k: lending_pendle_apy.PendleImpliedQuote(implied_apy=0.0, expired=True),
    )
    rows = lending_morpho.normalize(ADDR, _morpho_pt_payload())
    borrowed = next(row for row in rows if Decimal(row["debt_usdt"]) > 0)
    coll = next(leg for leg in borrowed["details"]["collateral"] if str(leg["symbol"]).startswith("PT-"))
    assert coll["apy_supply"] == 0.0
    assert coll["apy_source"] == "pendle_implied"
    assert coll["apy_expired"] is True
    assert borrowed["details"]["yield_data_missing"] is False
    assert borrowed["details"].get("apy_estimated") is not True
    assert borrowed["details"]["net_apy"] is not None
    assert borrowed["details"]["net_apy"] < 0


def test_morpho_fetch_positions_pendle_failure_does_not_raise(monkeypatch, _clear_pendle_cache) -> None:
    def boom(*_a, **_k):
        raise RuntimeError("pendle down")

    monkeypatch.setattr(lending_morpho, "pendle_implied_apy", boom)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_load("morpho_user.json"))

    rows = lending_morpho.fetch_positions(ADDR, client=_client(handler))
    assert rows
    borrowed = next(row for row in rows if Decimal(row["debt_usdt"]) > 0)
    assert borrowed["details"]["yield_data_missing"] is True
    assert borrowed["details"]["net_apy"] is None


def test_logs_redact_alchemy_key(caplog: pytest.LogCaptureFixture) -> None:
    leaked = f"https://eth-mainnet.g.alchemy.com/v2/{ALCHEMY_KEY}?q={ADDR}"
    assert ALCHEMY_KEY not in redact_text(leaked)
    assert ADDR not in redact_text(leaked)
