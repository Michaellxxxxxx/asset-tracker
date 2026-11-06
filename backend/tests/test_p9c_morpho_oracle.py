"""p9c: Morpho liquidation price on the oracle / health-factor basis.

Pure normalization against local JSON fixtures. No network, no DB writes.
"""

from __future__ import annotations

import copy
import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.integrations import lending_morpho

FIXTURES = Path(__file__).parent / "fixtures" / "morpho"
ADDR = "0x1111111111111111111111111111111111111111"

PT_OLD_LIQ = Decimal("0.83487")
PT_MARKET_PRICE = Decimal("979.1567112885805") / Decimal(1000)
PT_ORACLE_PRICE = Decimal("0.9560770122137407966293229760")


@pytest.fixture(autouse=True)
def _offline_pendle(monkeypatch: pytest.MonkeyPatch) -> None:
    """PT collateral would otherwise call the Pendle quote API."""
    monkeypatch.setattr(lending_morpho, "pendle_implied_apy", lambda *_a, **_k: None)


def _payload(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _position(payload: dict) -> dict:
    return payload["data"]["userByAddress"]["marketPositions"][0]


def _row(payload: dict) -> dict:
    rows = lending_morpho.normalize(ADDR, payload)
    assert len(rows) == 1
    return rows[0]


def test_pt_oracle_basis_moves_liq_price_to_hf_implied_value() -> None:
    """PT-USD3 sample: protocol HF 1.14528, lltv 0.86, old 0.83487 -> ~0.855."""
    row = _row(_payload("morpho_pt_oracle.json"))
    details = row["details"]

    # The protocol HF stays authoritative and untouched.
    assert details["health_factor"] == pytest.approx(1.14528)
    assert details["ltv"] == pytest.approx(0.733272, rel=1e-5)
    assert details["liq_threshold"] == pytest.approx(0.86)
    # The old market-price formula would still be 0.83487.
    assert PT_OLD_LIQ == pytest.approx(Decimal("717.9882") / (Decimal(1000) * Decimal("0.86")))
    assert details["liq_price"] == pytest.approx(0.8550, abs=0.001)
    assert details["liq_price"] == pytest.approx(
        float(PT_OLD_LIQ * PT_MARKET_PRICE / PT_ORACLE_PRICE), rel=1e-9
    )
    assert details["liq_price_basis"] == "oracle"
    assert details.get("liq_price_estimated") is not True
    # Position details must stay JSON-native for the API response.
    json.dumps(row)


def test_pt_without_oracle_price_uses_hf_implied_price() -> None:
    payload = copy.deepcopy(_payload("morpho_pt_oracle.json"))
    _position(payload)["market"]["state"].pop("price")

    details = _row(payload)["details"]
    assert details["liq_price"] == pytest.approx(float(PT_MARKET_PRICE / Decimal("1.14528")))
    assert details["liq_price_basis"] == "hf_implied"
    assert details["liq_price_estimated"] is True
    assert "按协议健康度反推" in details["liq_price_note"]
    assert "oracle 基差" in details["liq_price_note"]
    # Still the same authoritative HF.
    assert details["health_factor"] == pytest.approx(1.14528)


def test_ordinary_market_basis_under_half_percent_keeps_old_formula() -> None:
    """A 0.4% oracle gap is noise: keep the old value to avoid UI churn."""
    payload = copy.deepcopy(_payload("morpho_ordinary_oracle.json"))
    position = _position(payload)
    market_price = Decimal(6000) / Decimal("1.8")
    position["market"]["state"]["price"] = str(
        int(market_price / Decimal("1.004") * Decimal(10) ** 24)
    )

    details = _row(payload)["details"]
    old = Decimal(1500) / (Decimal("1.8") * Decimal("0.86"))
    assert details["liq_price"] == pytest.approx(float(old))
    assert details["liq_price_basis"] == "market"
    assert details["liq_price_estimated"] is True


def test_ordinary_market_basis_over_half_percent_uses_oracle() -> None:
    """A 2% oracle basis is material: use the oracle-equivalent market price."""
    payload = copy.deepcopy(_payload("morpho_ordinary_oracle.json"))
    position = _position(payload)
    market_price = Decimal(6000) / Decimal("1.8")
    position["market"]["state"]["price"] = str(
        int(market_price / Decimal("1.02") * Decimal(10) ** 24)
    )

    details = _row(payload)["details"]
    old = Decimal(1500) / (Decimal("1.8") * Decimal("0.86"))
    assert details["liq_price"] == pytest.approx(float(old * Decimal("1.02")), rel=1e-12)
    assert details["liq_price_basis"] == "oracle"
    assert details.get("liq_price_estimated") is not True


def test_oracle_price_converts_loan_units_to_usd() -> None:
    """state.price is in loan units; a non-USD loan needs its USD price."""
    raw = str(int(Decimal("1000") * Decimal(10) ** 24))
    value = lending_morpho._oracle_price(
        {"price": raw}, 18, 6, Decimal("2000"), "2"
    )
    assert value == pytest.approx(Decimal("2000"))


def test_sentinel_health_factor_is_not_used_for_reverse_price() -> None:
    payload = copy.deepcopy(_payload("morpho_pt_oracle.json"))
    position = _position(payload)
    position["healthFactor"] = "1000000000000000000"
    position["market"]["state"].pop("price")

    details = _row(payload)["details"]
    assert details["liq_price"] == pytest.approx(float(PT_OLD_LIQ))
    assert details["liq_price_basis"] == "market"
    assert details["liq_price_estimated"] is True


def test_ordinary_market_with_matching_oracle_keeps_old_formula() -> None:
    details = _row(_payload("morpho_ordinary_oracle.json"))["details"]
    old = Decimal(1500) / (Decimal("1.8") * Decimal("0.86"))
    assert details["liq_price"] == pytest.approx(float(old))
    assert details["liq_price_basis"] == "market"
    assert details["liq_price_estimated"] is True


def test_no_health_factor_falls_back_to_market_and_marks_estimate() -> None:
    payload = copy.deepcopy(_payload("morpho_ordinary_oracle.json"))
    position = _position(payload)
    position["healthFactor"] = None
    position["market"]["state"].pop("price")

    details = _row(payload)["details"]
    old = Decimal(1500) / (Decimal("1.8") * Decimal("0.86"))
    assert details["liq_price"] == pytest.approx(float(old))
    assert details["liq_price_basis"] == "market"
    assert details["liq_price_estimated"] is True


def test_no_debt_has_no_liq_price() -> None:
    payload = copy.deepcopy(_payload("morpho_ordinary_oracle.json"))
    position = _position(payload)
    position["healthFactor"] = None
    position["state"]["borrowAssets"] = "0"
    position["state"]["borrowAssetsUsd"] = 0

    details = _row(payload)["details"]
    assert details["liq_price"] is None
    assert "liq_price_basis" not in details
    assert "liq_price_estimated" not in details


@pytest.mark.parametrize(
    "bad_price",
    [
        None,
        "",
        "not-a-number",
        "0",
        # PAXG-like raw value: a non-standard oracle scale, not a 1.13e12 basis.
        "4527890836037385528501520899344450697626",
    ],
)
def test_unusable_oracle_price_falls_back_to_hf(bad_price: object) -> None:
    payload = copy.deepcopy(_payload("morpho_pt_oracle.json"))
    _position(payload)["market"]["state"]["price"] = bad_price

    details = _row(payload)["details"]
    assert details["liq_price"] == pytest.approx(float(PT_MARKET_PRICE / Decimal("1.14528")))
    assert details["liq_price_basis"] == "hf_implied"
    assert details["liq_price_estimated"] is True


def test_missing_fields_do_not_raise() -> None:
    payload = {
        "data": {
            "userByAddress": {
                "chainId": 1,
                "marketPositions": [
                    {"market": {}},
                    {
                        "market": {
                            "loanAsset": {"symbol": "USDC", "decimals": "bad"},
                            "collateralAsset": None,
                            "lltv": None,
                            "state": None,
                        }
                    },
                ],
            }
        }
    }
    assert lending_morpho.normalize(ADDR, payload) == []


def test_debt_without_lltv_has_no_liq_price_and_does_not_raise() -> None:
    payload = copy.deepcopy(_payload("morpho_pt_oracle.json"))
    position = _position(payload)
    position["healthFactor"] = "not-a-number"
    position["market"]["lltv"] = None
    position["market"]["state"] = None

    row = _row(payload)
    assert row["details"]["liq_price"] is None
    assert "liq_price_basis" not in row["details"]
    assert row["details"]["liq_threshold"] is None
