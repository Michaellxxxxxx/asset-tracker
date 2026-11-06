"""Solana lending readers (Kamino / Jupiter). Network is mocked; no live key."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.integrations import lending_jupiter, lending_kamino
from app.integrations.lending_common import (
    note_failure,
    take_failures,
    take_source_failures,
)
from app.positions_core import _slim_details, mark_covered_by_position, upsert_positions

FIXTURES = Path(__file__).parent / "fixtures" / "lending_solana"
ADDR = "So1anaWa11et11111111111111111111111111111111"
DETAIL_KEYS = (
    "collateral",
    "borrow",
    "health_factor",
    "ltv",
    "liq_threshold",
    "liq_price",
    "net_apy",
    "interest_paid_usd",
    "interest_earned_usd",
    "pnl_usd",
    "pnl_apr",
)


def _load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _net(row: dict) -> Decimal:
    return Decimal(row["collateral_usdt"]) - Decimal(row["debt_usdt"])


@pytest.fixture(autouse=True)
def _fresh_lending_state():
    """No failure/source notes or market caches leak between tests."""
    lending_kamino.reset_caches()
    lending_jupiter.reset_cooldowns()
    take_source_failures()
    take_failures()
    yield
    lending_kamino.reset_caches()
    lending_jupiter.reset_cooldowns()
    take_source_failures()
    take_failures()


# --- Kamino ---------------------------------------------------------------


def test_kamino_normal_from_fixture_net_hf_and_interest() -> None:
    rows = lending_kamino.fetch_positions(ADDR, use_mock=True)
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "lending"
    assert row["protocol"] == "kamino"
    assert row["chain"] == "solana"
    # Solana addresses stay case-sensitive.
    assert row["account_ref"] == ADDR
    assert row["market_ref"] == "kamino-7u3HeHxYDLhnCoErrtycNokbQYbWGzLs6JSDqGAv5PfF"
    # The obligation is still exposed even though the row is keyed by market.
    assert row["details"]["market"] == "7u3HeHxYDLhnCoErrtycNokbQYbWGzLs6JSDqGAv5PfF"
    assert row["details"]["obligation"] == "KaminoOb1111111111111111111111111111111111"
    assert row["legacy_market_refs"] == [
        "kamino-KaminoOb1111111111111111111111111111111111"
    ]
    assert Decimal(row["collateral_usdt"]) == Decimal("5000.00000000")
    assert Decimal(row["debt_usdt"]) == Decimal("2000.00000000")
    assert Decimal(row["net_value_usdt"]) == _net(row) == Decimal("3000.00000000")
    details = row["details"]
    for key in DETAIL_KEYS:
        assert key in details
    # 5000 * 0.80 / 2000 = 2.0
    assert details["health_factor"] == pytest.approx(2.0)
    assert details["ltv"] == pytest.approx(0.4)
    assert details["liq_threshold"] == pytest.approx(0.8)
    # debt / (amount * liq_threshold) = 2000 / (50 * 0.8) = 50
    assert details["liq_price"] == pytest.approx(50)
    assert details["collateral"][0]["symbol"] == "SOL"
    assert details["collateral"][0]["apy_supply"] == pytest.approx(0.05)
    assert details["collateral"][0]["contract"] == "So11111111111111111111111111111111111111112"
    assert details["borrow"][0]["symbol"] == "USDC"
    assert details["borrow"][0]["apy_borrow"] == pytest.approx(0.06)
    assert details["interest_earned_usd"] == pytest.approx(15)
    assert details["interest_paid_usd"] == pytest.approx(8.25)
    assert details["pnl_usd"] == pytest.approx(25.5)
    # (5000*0.05 - 2000*0.06) / 3000 = 0.04333...
    assert details["net_apy"] == pytest.approx((5000 * 0.05 - 2000 * 0.06) / 3000)
    assert details["apy_estimated"] is False
    assert details["apy_basis"] == "reported_rates"
    assert details["yield_data_missing"] is False
    # Base mode is unchanged and the per-leg LTV inputs are retained.
    assert details["elevation_group"] == 0
    assert details["elevation_ltv_source"] == "base"
    assert [item["side"] for item in details["legs"]] == ["collateral", "debt"]
    assert details["legs"][0]["maxLtv"] == pytest.approx(0.74)
    assert details["legs"][0]["liquidationLtv"] == pytest.approx(0.8)
    assert "liq_price_estimated" not in details


def test_kamino_emode_leg_threshold_drives_liq_and_hf() -> None:
    """Synthetic eMode loan: the leg's effective liquidation LTV wins over base."""
    payload = _load("kamino_user.json")
    loan = payload["markets"][0]["loans"][0]
    loan["elevationGroup"] = 2
    deposit = loan["loanInfo"]["collateral"]["deposits"][0]
    deposit["elevationLiquidationLtv"] = "0.9"
    loan["loanInfo"]["liquidationLtv"] = "0.9"

    rows = lending_kamino.normalize(ADDR, payload)
    details = rows[0]["details"]
    assert details["elevation_group"] == 2
    assert details["elevation_ltv_source"] == "leg_elevation"
    # 5000 * 0.90 / 2000 = 2.25
    assert details["health_factor"] == pytest.approx(2.25)
    assert details["liq_threshold"] == pytest.approx(0.9)
    # 2000 / (50 * 0.90)
    assert details["liq_price"] == pytest.approx(2000 / (50 * 0.9))
    assert details["collateral"][0]["liquidationLtv"] == pytest.approx(0.8)
    assert details["collateral"][0]["effectiveLiquidationLtv"] == pytest.approx(0.9)


def test_kamino_emode_reserve_group_threshold() -> None:
    """Synthetic reserve elevation table is used when the API publishes one."""
    payload = _load("kamino_user.json")
    loan = payload["markets"][0]["loans"][0]
    loan["elevationGroup"] = 3
    loan["loanInfo"]["liquidationLtv"] = "0.88"
    payload["markets"][0]["reserves"][0]["elevationGroups"] = [
        {"elevationGroup": 3, "maxLtv": "0.8", "liquidationLtv": "0.85"}
    ]

    rows = lending_kamino.normalize(ADDR, payload)
    details = rows[0]["details"]
    assert details["elevation_group"] == 3
    assert details["elevation_ltv_source"] == "reserve_elevation"
    assert details["liq_threshold"] == pytest.approx(0.85)
    assert details["collateral"][0]["effectiveLiquidationLtv"] == pytest.approx(0.85)


def test_kamino_emode_loan_level_fallback() -> None:
    """If no leg/reserve group LTV exists, trust the loan-level reported LTV."""
    payload = _load("kamino_user.json")
    loan = payload["markets"][0]["loans"][0]
    loan["elevationGroup"] = 2
    loan["loanInfo"]["liquidationLtv"] = "0.88"

    rows = lending_kamino.normalize(ADDR, payload)
    details = rows[0]["details"]
    assert details["elevation_group"] == 2
    assert details["elevation_ltv_source"] == "loan_elevation"
    assert details["liq_threshold"] == pytest.approx(0.88)
    assert details["health_factor"] == pytest.approx(5000 * 0.88 / 2000)


def test_kamino_multi_collateral_liq_price_is_weighted_estimate() -> None:
    """Two collateral legs: estimate a common price move and label it."""
    payload = _load("kamino_user.json")
    loan = payload["markets"][0]["loans"][0]
    loan["loanId"] = "OblMulti"
    info = loan["loanInfo"]
    info["collateral"]["deposits"] = [
        {
            "tokenMint": "So11111111111111111111111111111111111111112",
            "tokenName": "SOL",
            "tokenAmount": "10",
            "tokenValue": "1000",
            "maxLtv": "0.7",
            "liquidationLtv": "0.8",
            "tokenPrice": "100",
        },
        {
            "tokenMint": "BTCMint",
            "tokenName": "BTC",
            "tokenAmount": "1",
            "tokenValue": "1000",
            "maxLtv": "0.4",
            "liquidationLtv": "0.5",
            "tokenPrice": "1000",
        },
    ]
    info["debt"]["borrows"][0]["tokenAmount"] = "1200"
    info["debt"]["borrows"][0]["tokenValue"] = "1200"
    info["currentLtv"] = "0.6"

    rows = lending_kamino.normalize(ADDR, payload)
    details = rows[0]["details"]
    # (1000*0.8 + 1000*0.5) / 2000 = 0.65; HF = 2000*0.65/1200.
    assert details["liq_threshold"] == pytest.approx(0.65)
    assert details["health_factor"] == pytest.approx(2000 * 0.65 / 1200)
    # P_ref = (100*1000 + 1000*1000) / 2000 = 550; f = 1200/(0.65*2000).
    assert details["liq_price"] == pytest.approx(550 * 1200 / (0.65 * 2000))
    assert details["liq_price_estimated"] is True
    assert "多抵押估算" in details["liq_price_note"]
    legs = details["legs"]
    assert [item["side"] for item in legs] == ["collateral", "collateral", "debt"]
    assert legs[0]["maxLtv"] == pytest.approx(0.7)
    assert legs[1]["liquidationLtv"] == pytest.approx(0.5)
    assert legs[2]["amount"] == pytest.approx(1200)


def _onyc_payload(*, with_history: bool = True) -> dict:
    """ONyc carry position: collateral reports 0% supply APY, USDC debt ~7.5%.

    The pnl endpoint says +149.13 USD net and 2999.52 invested; the interest
    history starts 100 days before the fixed moment and says 118.80 paid. Gross
    collateral appreciation is therefore 149.13 + 118.80 = 267.93.
    """
    start = datetime(2026, 7, 17, 12, tzinfo=timezone.utc)
    paid = {
        "totalFeesPaidObligation": {"oblig": {"usdFees": "118.80"}},
        "historicalFeesObligation": (
            {
                "oblig": [
                    {"reserve": {"ts": int(start.timestamp() * 1000), "usdFees": "118.80"}}
                ]
            }
            if with_history
            else {}
        ),
    }
    return {
        "markets": [
            {
                "market": "MktOnyc",
                "reserves": [
                    {"liquidityTokenMint": "ONycmint", "supplyApy": "0", "borrowApy": "0"},
                    {"liquidityTokenMint": "USDCmint", "supplyApy": "0", "borrowApy": "0.075"},
                ],
                "loans": [
                    {
                        "loanId": "OblOnyc",
                        "loanInfo": {
                            "collateral": {
                                "deposits": [
                                    {
                                        "tokenMint": "ONycmint",
                                        "tokenName": "ONyc",
                                        "tokenAmount": "7460.054981938",
                                        "tokenValue": "8647.35",
                                        "liquidationLtv": "0.75",
                                    }
                                ]
                            },
                            "debt": {
                                "borrows": [
                                    {
                                        "tokenMint": "USDCmint",
                                        "tokenName": "USDC",
                                        "tokenAmount": "5498.68",
                                        "tokenValue": "5498.68",
                                        "liquidationLtv": "0.75",
                                    }
                                ]
                            },
                            "currentLtv": "0.636",
                        },
                    }
                ],
                "pnl": {
                    "OblOnyc": {
                        "usd": "149.13",
                        "invested": {"usd": "2999.52"},
                    }
                }
                if with_history
                else {},
                "interest_earned": {
                    "OblOnyc": {"totalFeesEarnedObligation": {"oblig": {"usdFees": "0"}}}
                }
                if with_history
                else {},
                "interest_paid": {"OblOnyc": paid} if with_history else {},
            }
        ]
    }


def test_kamino_onyc_yield_includes_collateral_price_gain(monkeypatch) -> None:
    monkeypatch.setattr(
        lending_kamino, "asof_iso", lambda: "2026-10-25T12:00:00+00:00"
    )
    rows = lending_kamino.normalize(ADDR, _onyc_payload())
    assert len(rows) == 1
    details = rows[0]["details"]

    gross_gain = Decimal("149.13") + Decimal("118.80")
    price_apr = gross_gain / Decimal("8647.35") * Decimal("365") / Decimal("100")
    net = Decimal("8647.35") - Decimal("5498.68")
    expected = float(
        (Decimal("8647.35") * price_apr - Decimal("5498.68") * Decimal("0.075")) / net
    )
    # Not the old borrow-only -13.1%: the collateral's price return is included.
    assert details["net_apy"] == pytest.approx(expected)
    assert details["net_apy"] > 0
    assert details["apy_estimated"] is True
    assert details["apy_basis"] == "pnl_collateral_price_yield"
    assert details["yield_data_missing"] is False
    assert details["pnl_apr"] == pytest.approx(
        149.13 / 2999.52 * 365 / 100
    )


def test_kamino_onyc_missing_yield_data_is_flagged_not_borrow_only(monkeypatch) -> None:
    monkeypatch.setattr(
        lending_kamino, "asof_iso", lambda: "2026-10-25T12:00:00+00:00"
    )
    rows = lending_kamino.normalize(ADDR, _onyc_payload(with_history=False))
    assert len(rows) == 1
    details = rows[0]["details"]
    # Never emit the one-sided borrow cost when collateral yield data is absent.
    assert details["net_apy"] is None
    assert details["apy_estimated"] is True
    assert details["apy_basis"] == "insufficient"
    assert details["yield_data_missing"] is True
    assert details["pnl_apr"] is None


def _leveraged_payload(
    *, pnl_usd: str, supply_apy: str = "0", days: int = 365
) -> dict:
    """Leveraged collateral with no native supply APY (JLP/ONyc shape).

    Collateral 10,000 / debt 1,000 / net 9,000, borrow rate 0, 365-day window,
    so the estimated net APY is exactly ``pnl / 9000`` (gross gain = pnl).
    """
    start = datetime(2026, 10, 25, 12, tzinfo=timezone.utc) - timedelta(days=days)
    paid = {
        "totalFeesPaidObligation": {"oblig": {"usdFees": "0"}},
        "historicalFeesObligation": {
            "oblig": [{"reserve": {"ts": int(start.timestamp() * 1000), "usdFees": "0"}}]
        },
    }
    return {
        "markets": [
            {
                "market": "MktLev",
                "reserves": [
                    {
                        "liquidityTokenMint": "JLPmint",
                        "supplyApy": supply_apy,
                        "borrowApy": "0",
                    },
                    {
                        "liquidityTokenMint": "USDCmint",
                        "supplyApy": "0",
                        "borrowApy": "0",
                    },
                ],
                "loans": [
                    {
                        "loanId": "OblLev",
                        "loanInfo": {
                            "collateral": {
                                "deposits": [
                                    {
                                        "tokenMint": "JLPmint",
                                        "tokenName": "JLP",
                                        "tokenAmount": "10000",
                                        "tokenValue": "10000",
                                        "liquidationLtv": "0.75",
                                    }
                                ]
                            },
                            "debt": {
                                "borrows": [
                                    {
                                        "tokenMint": "USDCmint",
                                        "tokenName": "USDC",
                                        "tokenAmount": "1000",
                                        "tokenValue": "1000",
                                        "liquidationLtv": "0.75",
                                    }
                                ]
                            },
                            "currentLtv": "0.1",
                        },
                    }
                ],
                "pnl": {"OblLev": {"usd": pnl_usd, "invested": {"usd": "9000"}}},
                "interest_earned": {
                    "OblLev": {"totalFeesEarnedObligation": {"oblig": {"usdFees": "0"}}}
                },
                "interest_paid": {"OblLev": paid},
            }
        ]
    }


def test_kamino_jlp_estimated_apy_outlier_is_suppressed(monkeypatch) -> None:
    monkeypatch.setattr(
        lending_kamino, "asof_iso", lambda: "2026-10-25T12:00:00+00:00"
    )
    # pnl 12240 / 9000 = 136% estimated net APY: too volatile to publish.
    rows = lending_kamino.normalize(ADDR, _leveraged_payload(pnl_usd="12240"))
    assert len(rows) == 1
    details = rows[0]["details"]
    assert details["net_apy"] is None
    assert details["apy_estimated"] is True
    assert details["apy_outlier"] is True
    assert details["apy_note"] == "估算值，仅供参考"
    assert details["apy_estimate_raw"] == pytest.approx(1.36)
    assert details["apy_basis"] == "pnl_collateral_price_yield"
    assert details["yield_data_missing"] is False


def test_kamino_onyc_16_percent_estimate_stays_visible(monkeypatch) -> None:
    monkeypatch.setattr(
        lending_kamino, "asof_iso", lambda: "2026-10-25T12:00:00+00:00"
    )
    # 1476 / 9000 = 16.4%: estimated, but within the display threshold.
    rows = lending_kamino.normalize(ADDR, _leveraged_payload(pnl_usd="1476"))
    assert len(rows) == 1
    details = rows[0]["details"]
    assert details["net_apy"] == pytest.approx(0.164)
    assert details["apy_estimated"] is True
    assert details.get("apy_outlier") is not True
    assert details.get("apy_note") is None
    assert details["apy_basis"] == "pnl_collateral_price_yield"


def test_kamino_estimate_at_100_percent_boundary_is_kept(monkeypatch) -> None:
    monkeypatch.setattr(
        lending_kamino, "asof_iso", lambda: "2026-10-25T12:00:00+00:00"
    )
    # Exactly 100% is not an outlier (strictly greater than 100% is).
    rows = lending_kamino.normalize(ADDR, _leveraged_payload(pnl_usd="9000"))
    assert len(rows) == 1
    details = rows[0]["details"]
    assert details["net_apy"] == pytest.approx(1.0)
    assert details["apy_estimated"] is True
    assert details.get("apy_outlier") is not True


def test_kamino_reported_apy_above_100_percent_is_not_suppressed(monkeypatch) -> None:
    monkeypatch.setattr(
        lending_kamino, "asof_iso", lambda: "2026-10-25T12:00:00+00:00"
    )
    # Protocol-reported 400% supply APY is not an estimate: leave it untouched.
    rows = lending_kamino.normalize(
        ADDR, _leveraged_payload(pnl_usd="12240", supply_apy="4")
    )
    assert len(rows) == 1
    details = rows[0]["details"]
    assert details["net_apy"] == pytest.approx(40000 / 9000)
    assert details["apy_estimated"] is False
    assert details.get("apy_outlier") is not True
    assert details["apy_basis"] == "reported_rates"


def test_kamino_outlier_flags_survive_snapshot_slimming() -> None:
    slim = _slim_details(
        {
            "net_apy": None,
            "apy_estimated": True,
            "apy_outlier": True,
            "apy_note": "估算值，仅供参考",
            "apy_estimate_raw": 1.36,
            "collateral": [],
        }
    )
    assert slim is not None
    assert slim["apy_outlier"] is True
    assert slim["apy_note"] == "估算值，仅供参考"
    assert slim["apy_estimate_raw"] == pytest.approx(1.36)
    assert slim["net_apy"] is None


def test_kamino_empty_wallet() -> None:
    assert lending_kamino.normalize(ADDR, _load("kamino_empty.json")) == []


def test_kamino_http_error_returns_empty_and_notes_failure(caplog: pytest.LogCaptureFixture) -> None:
    take_failures()

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns failed")

    with caplog.at_level("WARNING"):
        rows = lending_kamino.fetch_positions(ADDR, client=_client(handler))
    assert rows == []
    assert "kamino" in take_failures()
    assert "api-key" not in caplog.text
    assert "ConnectError" in caplog.text


def test_kamino_one_bad_loan_does_not_sink_the_other() -> None:
    payload = _load("kamino_user.json")
    payload["markets"][0]["loans"].append({"loanId": "bad", "loanInfo": "nope"})
    rows = lending_kamino.normalize(ADDR, payload)
    assert len(rows) == 1
    assert Decimal(rows[0]["net_value_usdt"]) == Decimal("3000.00000000")


def test_kamino_routes_obligations_then_loans() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        seen.append(path)
        if path.endswith("/summary"):
            return httpx.Response(200, json={"tokens": [{"positions": [{"market": "Mkt111"}]}]})
        if path == "/v2/kamino-market":
            return httpx.Response(200, json={"markets": [{"lendingMarket": "Mkt111"}]})
        if path.endswith("/obligations"):
            return httpx.Response(200, json=[{"obligationAddress": "Obl111"}])
        if path == "/klend/loans/Obl111":
            loan = _load("kamino_user.json")["markets"][0]["loans"][0]
            return httpx.Response(200, json=loan)
        if path.endswith("/reserves/metrics"):
            return httpx.Response(200, json=_load("kamino_user.json")["markets"][0]["reserves"])
        if path.endswith("/pnl"):
            return httpx.Response(200, json={"usd": "1.5"})
        if path.endswith("/interest-fees"):
            return httpx.Response(200, json={"totalFeesEarnedObligation": {"a": {"usdFees": "2"}}})
        if path.endswith("/interest-paid"):
            return httpx.Response(500, json={"error": "down"})
        return httpx.Response(404, json={"error": path})

    take_failures()
    rows = lending_kamino.fetch_positions(ADDR, client=_client(handler))
    assert len(rows) == 1
    assert rows[0]["market_ref"] == "kamino-Mkt111"
    assert rows[0]["details"]["obligation"] == "KaminoOb1111111111111111111111111111111111"
    assert rows[0]["details"]["pnl_usd"] == pytest.approx(1.5)
    assert rows[0]["details"]["interest_earned_usd"] == pytest.approx(2)
    # interest-paid 500 is optional: position still lands and the protocol is
    # not failed, because the position data itself was read.
    assert rows[0]["details"]["interest_paid_usd"] is None
    assert any(path.endswith("/summary") for path in seen)
    assert any(path.endswith("/obligations") for path in seen)
    assert take_source_failures() == {}
    assert "kamino" not in take_failures()


@pytest.mark.parametrize(
    "stats_key", ["refreshedStats", "refreshed_stats", "obligationStats", "stats"]
)
def test_kamino_merges_obligation_refreshed_stats(stats_key: str) -> None:
    """The obligations list's refreshedStats must reach the loan and win.

    1500 / 2000 = 0.75, while collateral * liquidationLtv / debt is 2.0.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/summary"):
            return httpx.Response(
                200, json={"tokens": [{"positions": [{"market": "Mkt111"}]}]}
            )
        if path == "/v2/kamino-market":
            return httpx.Response(
                200, json={"markets": [{"lendingMarket": "Mkt111"}]}
            )
        if path.endswith("/obligations"):
            return httpx.Response(
                200,
                json=[{
                    "obligationAddress": "Obl111",
                    stats_key: {
                        "borrowLiquidationLimit": "1500",
                        "userTotalBorrowBorrowFactorAdjusted": "2000",
                    },
                }],
            )
        if path == "/klend/loans/Obl111":
            loan = _load("kamino_user.json")["markets"][0]["loans"][0]
            return httpx.Response(200, json=loan)
        if path.endswith("/reserves/metrics"):
            return httpx.Response(200, json=[])
        if path.endswith("/pnl"):
            return httpx.Response(200, json={})
        if path.endswith("/interest-fees"):
            return httpx.Response(200, json={})
        if path.endswith("/interest-paid"):
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"error": path})

    rows = lending_kamino.fetch_positions(ADDR, client=_client(handler))
    assert len(rows) == 1
    details = rows[0]["details"]
    assert details["health_factor"] == pytest.approx(0.75)
    assert details["hf_basis"] == "protocol_stats"


@pytest.mark.parametrize(
    "stats",
    [None, {"borrowLiquidationLimit": "1500"}],
    ids=["absent", "incomplete"],
)
def test_kamino_without_usable_obligation_stats_keeps_formula(
    stats: dict | None,
) -> None:
    """No (or partial) stats must not be invented; the legacy formula stays."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/summary"):
            return httpx.Response(
                200, json={"tokens": [{"positions": [{"market": "Mkt111"}]}]}
            )
        if path == "/v2/kamino-market":
            return httpx.Response(
                200, json={"markets": [{"lendingMarket": "Mkt111"}]}
            )
        if path.endswith("/obligations"):
            item: dict = {"obligationAddress": "Obl111"}
            if stats is not None:
                item["refreshedStats"] = stats
            return httpx.Response(200, json=[item])
        if path == "/klend/loans/Obl111":
            loan = _load("kamino_user.json")["markets"][0]["loans"][0]
            return httpx.Response(200, json=loan)
        if path.endswith("/reserves/metrics"):
            return httpx.Response(200, json=[])
        if path.endswith("/pnl"):
            return httpx.Response(200, json={})
        if path.endswith("/interest-fees"):
            return httpx.Response(200, json={})
        if path.endswith("/interest-paid"):
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"error": path})

    rows = lending_kamino.fetch_positions(ADDR, client=_client(handler))
    assert len(rows) == 1
    details = rows[0]["details"]
    # 5000 * 0.8 / 2000 = 2.0; borrowFactor is 1, so not the borrow_factor basis.
    assert details["health_factor"] == pytest.approx(2.0)
    assert details["hf_basis"] == "ltv_formula"


def test_kamino_retries_429_with_retry_after() -> None:
    calls = {"summary": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/summary"):
            calls["summary"] += 1
            if calls["summary"] == 1:
                return httpx.Response(429, headers={"Retry-After": "0.02"})
            return httpx.Response(200, json={"tokens": []})
        if path == "/v2/kamino-market":
            return httpx.Response(200, json={"markets": []})
        return httpx.Response(404)

    slept: list[float] = []
    rows = lending_kamino.fetch_positions(
        ADDR, client=_client(handler), sleep=slept.append
    )
    assert rows == []
    assert calls["summary"] == 2
    assert slept == [0.02]
    assert take_source_failures() == {}
    assert "kamino" not in take_failures()


def test_kamino_summary_does_not_hide_borrow_only_market() -> None:
    """A debt-only obligation in a market absent from the earn summary is read."""
    seen_markets: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/summary"):
            return httpx.Response(200, json={"tokens": [{"positions": [{"market": "MktEarn"}]}]})
        if path == "/v2/kamino-market":
            return httpx.Response(200, json={"markets": [
                {"lendingMarket": "MktEarn"},
                {"lendingMarket": "MktDebt"},
            ]})
        if path == "/kamino-market/MktEarn/users/%s/obligations" % ADDR:
            return httpx.Response(200, json=[])
        if path == "/kamino-market/MktDebt/users/%s/obligations" % ADDR:
            seen_markets.append("MktDebt")
            return httpx.Response(200, json=[{"obligationAddress": "OblDebt"}])
        if path == "/klend/loans/OblDebt":
            return httpx.Response(200, json={
                "loanId": "OblDebt",
                "loanInfo": {
                    "collateral": {"deposits": []},
                    "debt": {"borrows": [{
                        "tokenMint": "USDCmint",
                        "tokenName": "USDC",
                        "tokenAmount": "2000",
                        "tokenValue": "2000",
                        "liquidationLtv": "0.8",
                    }]},
                    "currentLtv": "0",
                },
            })
        if path.endswith("/reserves/metrics"):
            return httpx.Response(200, json=[])
        if path.endswith(("/pnl", "/interest-fees", "/interest-paid")):
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"error": path})

    rows = lending_kamino.fetch_positions(ADDR, client=_client(handler))
    assert seen_markets == ["MktDebt"]
    assert len(rows) == 1
    assert rows[0]["market_ref"] == "kamino-MktDebt"
    assert rows[0]["details"]["obligation"] == "OblDebt"
    assert rows[0]["legacy_market_refs"] == ["kamino-OblDebt"]
    assert Decimal(rows[0]["debt_usdt"]) == Decimal("2000.00000000")
    assert "kamino" not in take_failures()


def test_kamino_summary_failure_falls_back_to_full_list() -> None:
    """The earn summary is an optimization; a full list scan is authoritative."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/summary"):
            return httpx.Response(500, json={"error": "down"})
        if path == "/v2/kamino-market":
            return httpx.Response(200, json={"markets": [{"lendingMarket": "MktA"}]})
        if path.endswith("/obligations"):
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={"error": path})

    rows = lending_kamino.fetch_positions(
        ADDR, client=_client(handler), sleep=lambda _s: None
    )
    assert rows == []
    assert take_source_failures() == {}
    assert "kamino" not in take_failures()


def test_kamino_market_list_failure_keeps_protocol_failed() -> None:
    """Without the full list a debt-only market can be missed, so no closes."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/summary"):
            return httpx.Response(200, json={"tokens": [{"positions": [{"market": "MktA"}]}]})
        if path == "/v2/kamino-market":
            return httpx.Response(500, json={"error": "down"})
        if path.endswith("/obligations"):
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={"error": path})

    rows = lending_kamino.fetch_positions(
        ADDR, client=_client(handler), sleep=lambda _s: None
    )
    assert rows == []
    assert take_source_failures() == {"kamino": ["markets"]}
    assert "kamino" in take_failures()


def test_kamino_empty_market_probe_is_cached() -> None:
    """A market with no obligation is not re-probed on the next sync."""
    calls = {"empty": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/summary"):
            return httpx.Response(200, json={"tokens": [{"positions": [{"market": "MktEarn"}]}]})
        if path == "/v2/kamino-market":
            return httpx.Response(200, json={"markets": [
                {"lendingMarket": "MktEarn"},
                {"lendingMarket": "MktEmpty"},
            ]})
        if path.endswith("/obligations"):
            if "MktEmpty" in path:
                calls["empty"] += 1
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={"error": path})

    client = _client(handler)
    assert lending_kamino.fetch_positions(ADDR, client=client) == []
    assert calls["empty"] == 1
    assert lending_kamino.fetch_positions(ADDR, client=client) == []
    assert calls["empty"] == 1


def test_kamino_one_market_failure_keeps_other_market() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/summary"):
            return httpx.Response(200, json={"tokens": [{"positions": [{"market": "MktBad"}]}]})
        if path == "/v2/kamino-market":
            return httpx.Response(200, json={"markets": [
                {"lendingMarket": "MktBad"},
                {"lendingMarket": "MktGood"},
            ]})
        if path == "/kamino-market/MktBad/users/%s/obligations" % ADDR:
            raise httpx.ConnectError("market down", request=request)
        if path == "/kamino-market/MktGood/users/%s/obligations" % ADDR:
            return httpx.Response(200, json=[{"obligationAddress": "OblGood"}])
        if path == "/klend/loans/OblGood":
            return httpx.Response(200, json={
                "loanId": "OblGood",
                "loanInfo": {
                    "collateral": {"deposits": []},
                    "debt": {"borrows": [{
                        "tokenMint": "USDCmint",
                        "tokenName": "USDC",
                        "tokenAmount": "500",
                        "tokenValue": "500",
                        "liquidationLtv": "0.8",
                    }]},
                    "currentLtv": "0",
                },
            })
        if path.endswith("/reserves/metrics"):
            return httpx.Response(200, json=[])
        if path.endswith(("/pnl", "/interest-fees", "/interest-paid")):
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"error": path})

    rows = lending_kamino.fetch_positions(
        ADDR, client=_client(handler), sleep=lambda _s: None
    )
    assert [row["market_ref"] for row in rows] == ["kamino-MktGood"]
    assert rows[0]["details"]["obligation"] == "OblGood"
    assert take_source_failures() == {"kamino": ["market:MktBad"]}
    assert "kamino" in take_failures()


def test_kamino_missing_price_keeps_debt_and_flags_it(caplog: pytest.LogCaptureFixture) -> None:
    payload = _load("kamino_user.json")
    loan = payload["markets"][0]["loans"][0]
    loan["loanInfo"]["collateral"]["deposits"][0]["tokenValue"] = "0"
    loan["loanInfo"]["debt"]["borrows"][0]["tokenValue"] = "0"
    with caplog.at_level("WARNING"):
        rows = lending_kamino.normalize(ADDR, payload)
    assert len(rows) == 1
    details = rows[0]["details"]
    assert details["price_missing"] is True
    assert set(details["price_missing_legs"]) == {"collateral", "debt"}
    assert Decimal(rows[0]["debt_usdt"]) == Decimal("0.00000000")
    # Both quantities survive in the legs even though USD is unknown.
    assert details["collateral"][0]["amount"] == pytest.approx(50)
    assert details["borrow"][0]["amount"] == pytest.approx(2000)
    assert "has debt but no health factor" in caplog.text


def test_kamino_rows_feed_upsert(db) -> None:
    from app.auth import hash_password
    from app.models import User

    user = User(username="kamino-feed", hashed_password=hash_password("pass-word"))
    db.add(user)
    db.commit()
    rows = lending_kamino.fetch_positions(ADDR, use_mock=True)
    written = upsert_positions(db, user.id, rows)
    assert len(written) == 1
    assert written[0].protocol == "kamino"
    assert written[0].chain == "solana"
    assert written[0].net_value_usdt == Decimal("3000.00000000")


# --- Jupiter --------------------------------------------------------------


def test_jupiter_earn_borrow_perps_from_fixture() -> None:
    rows = lending_jupiter.fetch_positions(ADDR, use_mock=True)
    by_market = {row["market_ref"]: row for row in rows}
    assert set(by_market) == {
        "jupiter-lend-earn",
        "jupiter-lend-borrow-3518",
        "jupiter-perps-PerpPos111111111111111111111111111111111111",
    }
    earn = by_market["jupiter-lend-earn"]
    assert earn["protocol"] == "jupiter"
    assert earn["chain"] == "solana"
    assert earn["account_ref"] == ADDR
    assert Decimal(earn["collateral_usdt"]) == Decimal("1500.00000000")
    assert Decimal(earn["debt_usdt"]) == Decimal("0.00000000")
    assert Decimal(earn["net_value_usdt"]) == _net(earn)
    assert earn["details"]["health_factor"] is None
    assert earn["details"]["collateral"][0]["symbol"] == "jlUSDC"
    assert earn["details"]["collateral"][0]["apy_supply"] == pytest.approx(0.0435)
    assert earn["details"]["net_apy"] == pytest.approx(0.0435)
    assert "price_missing" not in earn["details"]
    for key in DETAIL_KEYS:
        assert key in earn["details"]

    borrow = by_market["jupiter-lend-borrow-3518"]
    # 2 SOL * 100 = 200 collateral; 800 USDC debt.
    assert Decimal(borrow["collateral_usdt"]) == Decimal("200.00000000")
    assert Decimal(borrow["debt_usdt"]) == Decimal("800.00000000")
    assert Decimal(borrow["net_value_usdt"]) == _net(borrow) == Decimal("-600.00000000")
    # 200 * 0.85 / 800 = 0.2125
    assert borrow["details"]["health_factor"] == pytest.approx(0.2125)
    assert borrow["details"]["liq_threshold"] == pytest.approx(0.85)
    assert borrow["details"]["borrow"][0]["apy_borrow"] == pytest.approx(0.0581)
    assert borrow["details"]["collateral"][0]["apy_supply"] == pytest.approx(0.0382)

    perp = by_market["jupiter-perps-PerpPos111111111111111111111111111111111111"]
    # Equity = margin 1000 + pnl 200. The 3000 notional is not debt; the
    # borrowed part already shows up in pnl, so adding it back would double-count.
    assert Decimal(perp["collateral_usdt"]) == Decimal("1200.00000000")
    assert Decimal(perp["debt_usdt"]) == Decimal("0.00000000")
    assert Decimal(perp["net_value_usdt"]) == _net(perp) == Decimal("1200.00000000")
    assert perp["details"]["pnl_usd"] == pytest.approx(200)
    assert perp["details"]["interest_paid_usd"] == pytest.approx(5)
    assert perp["details"]["liq_price"] == pytest.approx(80)
    # long HF = mark / liq = 110 / 80
    assert perp["details"]["health_factor"] == pytest.approx(110 / 80)
    assert perp["details"]["ltv"] is None
    assert perp["details"]["margin_usd"] == pytest.approx(1000)
    assert perp["details"]["notional_usd"] == pytest.approx(3000)
    assert perp["details"]["side"] == "long"
    assert perp["details"]["borrow"] == []
    assert perp["label"] == "Jupiter Perps long SOL"


def test_jupiter_empty_wallet() -> None:
    assert lending_jupiter.normalize(ADDR, _load("jupiter_empty.json")) == []


def test_jupiter_zero_share_underlying_balance_is_dust() -> None:
    """Jupiter returns zero-share earn rows with a residual underlyingBalance.

    With no shares and no underlyingAssets the wallet holds nothing in the
    market; the balance figure is dust metadata, not a position. This is what
    ids 3 and 4 in the live DB hit before the share guard.
    """
    payload = _load("jupiter_user.json")
    payload["earn"] = [
        {
            "token": {
                "address": "DustMint",
                "symbol": "jlDUST",
                "decimals": 6,
                "totalRate": "100",
                "asset": {"symbol": "DUST", "decimals": 6, "price": "1"},
            },
            "ownerAddress": ADDR,
            "shares": "0",
            "underlyingAssets": "0",
            "underlyingBalance": "1100",
        }
    ]
    payload["borrow"] = []
    payload["perps"] = {"count": 0, "dataList": []}
    assert lending_jupiter.normalize(ADDR, payload) == []


def test_jupiter_earn_missing_price_keeps_position_and_flags_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A real Earn quantity with no price is not dust and must not vanish."""
    payload = _load("jupiter_user.json")
    earn = payload["earn"][0]
    earn["token"]["asset"]["price"] = "0"
    # No balance fallback either: the position is still held, the USD is unknown.
    earn["underlyingBalance"] = "0"
    with caplog.at_level("WARNING"):
        rows = lending_jupiter.normalize(ADDR, payload)
    row = {r["market_ref"]: r for r in rows}["jupiter-lend-earn"]
    assert row["details"]["price_missing"] is True
    assert row["details"]["price_missing_legs"] == ["collateral"]
    assert Decimal(row["collateral_usdt"]) == Decimal("0.00000000")
    assert Decimal(row["net_value_usdt"]) == Decimal("0.00000000")
    # The underlying quantity survives even though the USD value is unknown.
    assert row["details"]["collateral"][0]["amount"] == pytest.approx(1500)
    assert "unpriced holdings" in caplog.text
    assert "jlUSDC" in caplog.text
    # No wallet address or token amount in the warning.
    assert ADDR not in caplog.text
    assert "1500" not in caplog.text


def test_jupiter_earn_missing_price_uses_available_balance(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The missing-price row keeps the balance-based estimate and says so."""
    payload = _load("jupiter_user.json")
    payload["earn"][0]["token"]["asset"]["price"] = "0"
    with caplog.at_level("WARNING"):
        rows = lending_jupiter.normalize(ADDR, payload)
    row = {r["market_ref"]: r for r in rows}["jupiter-lend-earn"]
    assert row["details"]["price_missing"] is True
    assert row["details"]["price_missing_legs"] == ["collateral"]
    # 1500 underlying units of balance is the only available valuation.
    assert Decimal(row["collateral_usdt"]) == Decimal("1500.00000000")
    assert Decimal(row["net_value_usdt"]) == Decimal("1500.00000000")
    assert row["details"]["collateral"][0]["amount"] == pytest.approx(1500)
    assert "unpriced holdings" in caplog.text


def test_jupiter_earn_partial_missing_price_keeps_priced_net() -> None:
    """One unpriced receipt does not hide the priced one; net uses what it has."""
    payload = _load("jupiter_user.json")
    payload["earn"].append({
        "token": {
            "address": "UnpricedMint",
            "symbol": "jlBONK",
            "decimals": 6,
            "totalRate": "200",
            "asset": {"symbol": "BONK", "decimals": 6, "price": "0"},
        },
        "ownerAddress": ADDR,
        "shares": "1000000",
        "underlyingAssets": "1000000",
        "underlyingBalance": "0",
    })
    rows = lending_jupiter.normalize(ADDR, payload)
    row = {r["market_ref"]: r for r in rows}["jupiter-lend-earn"]
    assert row["details"]["price_missing"] is True
    assert row["details"]["price_missing_legs"] == ["collateral"]
    assert Decimal(row["collateral_usdt"]) == Decimal("1500.00000000")
    assert Decimal(row["net_value_usdt"]) == Decimal("1500.00000000")
    assert [leg["symbol"] for leg in row["details"]["collateral"]] == ["jlUSDC", "jlBONK"]
    assert row["details"]["collateral"][1]["amount"] == pytest.approx(1)
    assert row["details"]["collateral"][1]["usd"] == pytest.approx(0)


def test_jupiter_earn_zero_share_dust_ignored_without_price() -> None:
    """Dust is still dropped before the missing-price rule can keep it."""
    payload = _load("jupiter_user.json")
    payload["earn"] = [
        {
            "token": {
                "address": "DustMint",
                "symbol": "jlDUST",
                "decimals": 6,
                "totalRate": "100",
                "asset": {"symbol": "DUST", "decimals": 6, "price": "0"},
            },
            "ownerAddress": ADDR,
            "shares": "0",
            "underlyingAssets": "0",
            "underlyingBalance": "1100",
        }
    ]
    payload["borrow"] = []
    payload["perps"] = {"count": 0, "dataList": []}
    assert lending_jupiter.normalize(ADDR, payload) == []


def test_jupiter_one_source_down_keeps_other_sources() -> None:
    """A failed source must not discard the sources that did answer.

    The borrow row still lands; the protocol and the ``earn`` source are noted
    so the hook keeps the old earn/perp rows and marks them stale.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if "earn/positions" in request.url.path:
            return httpx.Response(503, json={"error": "down"})
        if "borrow/positions" in request.url.path:
            return httpx.Response(200, json=_load("jupiter_user.json")["borrow"])
        if request.url.path == "/v1/positions":
            return httpx.Response(200, json={"count": 0, "dataList": []})
        return httpx.Response(404)

    rows = lending_jupiter.fetch_positions(
        ADDR, client=_client(handler), sleep=lambda _s: None
    )
    assert [row["market_ref"] for row in rows] == ["jupiter-lend-borrow-3518"]
    assert take_source_failures() == {"jupiter": ["earn"]}
    assert "jupiter" in take_failures()


def test_jupiter_retries_429_then_succeeds() -> None:
    """A burst 429 is a blip, not a failed protocol, once a retry returns 200."""
    calls = {"earn": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "earn/positions" in path:
            calls["earn"] += 1
            if calls["earn"] == 1:
                return httpx.Response(429, headers={"Retry-After": "0.01"}, json={"error": "slow down"})
            return httpx.Response(200, json=_load("jupiter_user.json")["earn"])
        if "borrow/positions" in path:
            return httpx.Response(200, json=_load("jupiter_user.json")["borrow"])
        if path == "/v1/positions":
            return httpx.Response(200, json=_load("jupiter_user.json")["perps"])
        return httpx.Response(404)

    take_failures()
    slept: list[float] = []
    rows = lending_jupiter.fetch_positions(
        ADDR,
        client=_client(handler),
        sleep=slept.append,
    )
    assert calls["earn"] == 2
    # Retry-After (0.01s) is respected but the exponential backoff and jitter
    # are added on top: 0.01 + 0.5 + [0, 0.25].
    assert len(slept) == 1
    assert 0.51 <= slept[0] <= 0.76
    assert rows
    assert "jupiter" not in take_failures()


def test_jupiter_429_exhausted_keeps_protocol_failed() -> None:
    """Still failing after the retries notes the protocol and returns no rows."""
    def handler(request: httpx.Request) -> httpx.Response:
        if "earn/positions" in request.url.path:
            return httpx.Response(429, json={"error": "slow down"})
        if "borrow/positions" in request.url.path:
            return httpx.Response(200, json=[])
        if request.url.path == "/v1/positions":
            return httpx.Response(200, json={"count": 0, "dataList": []})
        return httpx.Response(404)

    rows = lending_jupiter.fetch_positions(ADDR, client=_client(handler), sleep=lambda _s: None)
    assert rows == []
    # earn failed and borrow/perps were clean; the protocol still fails so the
    # old earn row is not closed.
    assert take_source_failures() == {"jupiter": ["earn"]}
    assert "jupiter" in take_failures()


def test_jupiter_429_cooldown_skips_only_the_failed_source() -> None:
    """After exhausted 429s each (wallet, source) pair is skipped, not the wallet."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(429, headers={"Retry-After": "0.01"}, json={"error": "slow down"})

    rows = lending_jupiter.fetch_positions(
        ADDR, client=_client(handler), sleep=lambda _s: None
    )
    assert rows == []
    # Three sources x four tries; every exhausted 429 starts that source's cooldown.
    assert len(calls) == 12
    assert "jupiter" in take_failures()

    calls.clear()
    rows = lending_jupiter.fetch_positions(
        ADDR, client=_client(handler), sleep=lambda _s: None
    )
    assert rows == []
    assert calls == []
    # Cooldown is per source, so all three are reported, each as its own source.
    assert take_source_failures() == {"jupiter": ["borrow", "earn", "perps"]}
    assert "jupiter" in take_failures()


def test_jupiter_retry_after_is_capped() -> None:
    """A huge Retry-After is capped before exponential backoff/jitter is added."""
    def handler(request: httpx.Request) -> httpx.Response:
        if "earn/positions" in request.url.path:
            return httpx.Response(429, headers={"Retry-After": "999"}, json={})
        if "borrow/positions" in request.url.path:
            return httpx.Response(200, json=[])
        if request.url.path == "/v1/positions":
            return httpx.Response(200, json={"count": 0, "dataList": []})
        return httpx.Response(404)

    slept: list[float] = []
    rows = lending_jupiter.fetch_positions(
        ADDR, client=_client(handler), sleep=slept.append
    )
    assert rows == []
    assert len(slept) == 3
    assert 8.5 <= slept[0] <= 8.75
    assert 9.0 <= slept[1] <= 9.25
    assert 10.0 <= slept[2] <= 10.25


def test_jupiter_timeout_retries_then_fails() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "perps" in request.url.host:
            raise httpx.ReadTimeout("timed out", request=request)
        if "earn/positions" in request.url.path:
            return httpx.Response(200, json=[])
        if "borrow/positions" in request.url.path:
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    take_failures()
    slept: list[float] = []
    rows = lending_jupiter.fetch_positions(ADDR, client=_client(handler), sleep=slept.append)
    assert rows == []
    # Exponential backoff 0.5 / 1.0 / 2.0 plus [0, 0.25] jitter.
    assert len(slept) == 3
    assert 0.5 <= slept[0] <= 0.75
    assert 1.0 <= slept[1] <= 1.25
    assert 2.0 <= slept[2] <= 2.25
    assert "jupiter" in take_failures()


def test_jupiter_all_sources_down_returns_empty() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    assert lending_jupiter.fetch_positions(
        ADDR, client=_client(handler), sleep=lambda _s: None
    ) == []
    assert "jupiter" in take_failures()


def test_jupiter_short_health_factor_is_liq_over_mark() -> None:
    payload = _load("jupiter_user.json")
    payload["earn"] = []
    payload["borrow"] = []
    leg = payload["perps"]["dataList"][0]
    leg["side"] = "short"
    leg["markPriceUsd"] = "100000000"
    leg["liquidationPriceUsd"] = "150000000"
    rows = lending_jupiter.normalize(ADDR, payload)
    assert len(rows) == 1
    assert rows[0]["details"]["health_factor"] == pytest.approx(1.5)


@pytest.mark.parametrize(
    ("side", "pnl", "equity", "debt"),
    [
        ("long", "200000000", "1200", "0"),
        ("long", "-200000000", "800", "0"),
        ("short", "200000000", "1200", "0"),
        ("short", "-200000000", "800", "0"),
        ("long", "-1500000000", "0", "500"),
    ],
)
def test_jupiter_perps_equity_is_margin_plus_pnl(
    side: str, pnl: str, equity: str, debt: str
) -> None:
    payload = _load("jupiter_user.json")
    payload["earn"] = []
    payload["borrow"] = []
    item = payload["perps"]["dataList"][0]
    item["side"] = side
    item["pnlAfterFeesUsd"] = pnl
    rows = lending_jupiter.normalize(ADDR, payload)
    assert len(rows) == 1
    row = rows[0]
    assert Decimal(row["collateral_usdt"]) == Decimal(equity).quantize(Decimal("0.00000001"))
    assert Decimal(row["debt_usdt"]) == Decimal(debt).quantize(Decimal("0.00000001"))
    assert Decimal(row["net_value_usdt"]) == _net(row)
    assert row["details"]["ltv"] is None
    assert row["details"]["collateral"][0]["amount"] == pytest.approx(1000)


def test_jupiter_missing_price_keeps_debt_and_flags_it(caplog: pytest.LogCaptureFixture) -> None:
    payload = _load("jupiter_user.json")
    borrow = payload["borrow"][0]
    borrow["vault"]["supplyToken"]["price"] = "0"
    borrow["vault"]["borrowToken"]["price"] = "0"
    with caplog.at_level("WARNING"):
        rows = lending_jupiter.normalize(ADDR, payload)
    per_borrow = [row for row in rows if row["market_ref"].startswith("jupiter-lend-borrow-")]
    assert len(per_borrow) == 1
    row = per_borrow[0]
    assert row["details"]["price_missing"] is True
    assert set(row["details"]["price_missing_legs"]) == {"collateral", "debt"}
    assert Decimal(row["debt_usdt"]) == Decimal("0.00000000")
    assert row["details"]["borrow"][0]["amount"] == pytest.approx(800)
    assert "has debt but no health factor" in caplog.text


def test_jupiter_rows_feed_upsert(db) -> None:
    from app.auth import hash_password
    from app.models import User

    user = User(username="jupiter-feed", hashed_password=hash_password("pass-word"))
    db.add(user)
    db.commit()
    rows = lending_jupiter.fetch_positions(ADDR, use_mock=True)
    written = upsert_positions(db, user.id, rows)
    assert len(written) == 3
    assert {row.protocol for row in written} == {"jupiter"}


def test_solana_receipts_are_covered_and_underlying_is_not() -> None:
    """jlUSDC (Jupiter earn receipt) is covered. Raw SOL collateral is not,
    because the wallet's own SOL is not the protocol position.
    """
    rows = lending_jupiter.fetch_positions(ADDR, use_mock=True)
    holdings = [
        {"asset": "jlUSDC", "contract": "9BEcn9aPEmhSPbPQeFGjidFo9G6j1s5mYh3f4j2kLend", "value_usdt": "1500"},
        {"asset": "SOL", "contract": "So11111111111111111111111111111111111111112", "value_usdt": "100"},
        {"asset": "USDC", "value_usdt": "20"},
    ]
    marked = mark_covered_by_position(holdings, rows)
    flags = {row["asset"]: row["covered_by_position"] for row in marked}
    assert flags["jlUSDC"] is True
    assert flags["SOL"] is False
    assert flags["USDC"] is False


def test_live_hook_solana_error_does_not_close(db, monkeypatch) -> None:
    """Kamino raising keeps its old row. A clean empty Jupiter read closes its row."""
    from app.auth import hash_password
    from app.models import User, UserWallet
    from app.sync.positions_hook import sync_positions_for_user

    user = User(username="sol-hook", hashed_password=hash_password("pass-word"))
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
                "protocol": "kamino",
                "chain": "solana",
                "account_ref": ADDR,
                "market_ref": "kamino-old",
                "status": "open",
                "net_value_usdt": "10",
                "collateral_usdt": "10",
                "debt_usdt": "0",
                "details": {"health_factor": 2},
            },
            {
                "kind": "lending",
                "protocol": "jupiter",
                "chain": "solana",
                "account_ref": ADDR,
                "market_ref": "jupiter-old",
                "status": "open",
                "net_value_usdt": "5",
                "collateral_usdt": "5",
                "debt_usdt": "0",
                "details": {},
            },
        ],
    )

    def _boom(_address, **_kwargs):
        raise RuntimeError("kamino down")

    def _no_evm(*_args, **_kwargs):
        raise AssertionError("evm adapter called without an evm wallet")

    monkeypatch.setattr("app.sync.positions_hook.fetch_kamino", _boom)
    monkeypatch.setattr("app.sync.positions_hook.fetch_jupiter", lambda *_a, **_k: [])
    monkeypatch.setattr("app.sync.positions_hook.fetch_aave", _no_evm)

    written = sync_positions_for_user(db, user.id, None, use_mock=False)
    assert written == 0
    from app.models import Position
    from sqlalchemy import select

    status = {
        row.protocol: row.status
        for row in db.scalars(select(Position).where(Position.user_id == user.id))
    }
    assert status["kamino"] == "open"
    assert status["jupiter"] == "closed"
    kamino = db.scalar(
        select(Position).where(
            Position.user_id == user.id, Position.protocol == "kamino"
        )
    )
    assert kamino is not None and kamino.details["stale"] is True
    assert kamino.details["stale_since"]


def test_live_hook_kamino_optional_failure_still_closes_repaid(db, monkeypatch) -> None:
    """A 500 on pnl/interest must not keep a repaid obligation open.

    The main obligation read succeeds and only returns OblKeep; OblGone was
    repaid. Optional feeds fail, but the protocol is not failed and the
    reported reserve APYs still produce an APY, so OblGone closes.
    """
    from app.auth import hash_password
    from app.models import Position, User, UserWallet
    from app.sync.positions_hook import sync_positions_for_user
    from sqlalchemy import select

    user = User(username="kamino-optional-hook", hashed_password=hash_password("pass-word"))
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
                "protocol": "kamino",
                "chain": "solana",
                "account_ref": ADDR,
                "market_ref": "kamino-OblKeep",
                "status": "open",
                "net_value_usdt": "10",
                "collateral_usdt": "10",
                "debt_usdt": "0",
                "details": {},
            },
            {
                "kind": "lending",
                "protocol": "kamino",
                "chain": "solana",
                "account_ref": ADDR,
                "market_ref": "kamino-OblGone",
                "status": "open",
                "net_value_usdt": "5",
                "collateral_usdt": "5",
                "debt_usdt": "0",
                "details": {},
            },
        ],
    )

    fixture = _load("kamino_user.json")["markets"][0]
    loan = fixture["loans"][0]
    loan["loanId"] = "OblKeep"

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/summary"):
            return httpx.Response(200, json={"tokens": []})
        if path == "/v2/kamino-market":
            return httpx.Response(200, json={"markets": [{"lendingMarket": "MktOpt"}]})
        if path.endswith("/obligations"):
            return httpx.Response(200, json=[{"obligationAddress": "OblKeep"}])
        if path == "/klend/loans/OblKeep":
            return httpx.Response(200, json=loan)
        if path.endswith("/reserves/metrics"):
            return httpx.Response(200, json=fixture["reserves"])
        if path.endswith(("/pnl", "/interest-fees", "/interest-paid")):
            return httpx.Response(404, json={"error": "optional down"})
        return httpx.Response(404, json={"error": path})

    def _kamino(address, **_kwargs):
        return lending_kamino.fetch_positions(
            address, client=_client(handler), sleep=lambda _s: None
        )

    monkeypatch.setattr("app.sync.positions_hook.fetch_kamino", _kamino)
    monkeypatch.setattr("app.sync.positions_hook.fetch_jupiter", lambda *_a, **_k: [])
    monkeypatch.setattr(
        "app.sync.positions_hook.fetch_aave",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no evm wallet")),
    )

    written = sync_positions_for_user(db, user.id, None, use_mock=False)
    assert written == 1
    status = {
        row.market_ref: row.status
        for row in db.scalars(select(Position).where(Position.user_id == user.id))
    }
    assert status["kamino-OblKeep"] == "open"
    assert status["kamino-OblGone"] == "closed"
    keep = db.scalar(
        select(Position).where(Position.user_id == user.id, Position.market_ref == "kamino-OblKeep")
    )
    assert keep is not None
    assert keep.details["apy_basis"] == "reported_rates"
    assert keep.details["net_apy"] == pytest.approx((5000 * 0.05 - 2000 * 0.06) / 3000)
    assert take_source_failures() == {}
    assert "kamino" not in take_failures()


def test_live_hook_jupiter_failure_keeps_old_rows(db, monkeypatch) -> None:
    """A Jupiter outage (even one that returns []) must not close its rows.

    The adapter notes the failure and returns no rows; the hook treats that as
    "not read", not "clean empty wallet".
    """
    from app.auth import hash_password
    from app.models import Position, User, UserWallet
    from app.sync.positions_hook import sync_positions_for_user
    from sqlalchemy import select

    user = User(username="jup-hook", hashed_password=hash_password("pass-word"))
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
                "market_ref": "jupiter-old",
                "status": "open",
                "net_value_usdt": "5",
                "collateral_usdt": "5",
                "debt_usdt": "0",
                "details": {},
            }
        ],
    )

    def _jupiter_down(_address, **_kwargs):
        note_failure("jupiter")
        return []

    monkeypatch.setattr("app.sync.positions_hook.fetch_kamino", lambda *_a, **_k: [])
    monkeypatch.setattr("app.sync.positions_hook.fetch_jupiter", _jupiter_down)

    written = sync_positions_for_user(db, user.id, None, use_mock=False)
    assert written == 0
    row = db.scalar(select(Position).where(Position.user_id == user.id))
    assert row is not None and row.status == "open"
    assert row.details["stale"] is True
    assert row.details["stale_since"]


def test_note_failure_roundtrip() -> None:
    take_failures()
    note_failure("kamino")
    assert take_failures() == {"kamino"}
    assert take_failures() == set()
