"""Contract tests against *real* Kamino eMode (elevationGroup) samples.

Fixtures in ``tests/fixtures/kamino/`` were captured read-only from public
endpoints on 2026-10-26 (no API key):

* ``GET https://api.kamino.finance/klend/loans/{obligation}`` — three
  obligations in elevation groups 1/2 (single- and multi-collateral).
* ``GET https://api.kamino.finance/v2/kamino-market/{market}`` — market names.
* ``GET https://api.kamino.finance/kamino-market/{market}/reserves/metrics``
  — reserve APYs; note ``maxLtv`` is 0 for a reserve that is only usable
  through eMode.
* ``getAccountInfo`` on the klend program's ``LendingMarket`` account —
  the elevation-group table, used as an independent cross-check because the
  REST API does not publish it.

Obligation and wallet addresses are replaced with stable pseudonyms; market
ids, token mints, amounts and every numeric field are verbatim. Discovery was
done with a read-only ``getProgramAccounts`` filter, so no real obligation id
is stored in the fixtures.
"""

from __future__ import annotations

import copy
import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.integrations import lending_kamino

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "kamino"
LOANS = json.loads((FIXTURES / "emode_loans.json").read_text(encoding="utf-8"))
GROUPS = json.loads((FIXTURES / "market_elevation_groups.json").read_text(encoding="utf-8"))

ADDRESS = "11111111111111111111111111111111"
# Protocol-side currentLtv/liquidationLtv are rounded differently from our
# Decimal arithmetic; 0.5% relative is the accepted contract tolerance.
HF_TOLERANCE = 0.005

GROUPS_BY_MARKET: dict[str, dict[int, dict]] = {
    market["market"]: {int(key): value for key, value in market["elevation_groups"].items()}
    for market in GROUPS["markets"]
}

ALL_SAMPLES = [
    pytest.param(market, loan, id=loan["loanId"])
    for market in LOANS["markets"]
    for loan in market["loans"]
]


def _relative_error(actual: float, expected: float) -> float:
    return abs(float(actual) - float(expected)) / abs(float(expected))


def _rows(loan: dict, *, reserves: list | None = None, market_id: str | None = None) -> list[dict]:
    payload = {
        "markets": [
            {
                "market": market_id or loan["marketId"],
                "reserves": reserves or [],
                "loans": [loan],
            }
        ]
    }
    return lending_kamino.normalize(ADDRESS, payload)


def _single_row(loan: dict, *, reserves: list | None = None) -> dict:
    rows = _rows(loan, reserves=reserves)
    assert len(rows) == 1, f"expected one row, got {len(rows)}"
    return rows[0]


def _protocol_hf(loan: dict) -> Decimal:
    info = loan["loanInfo"]
    return Decimal(str(info["liquidationLtv"])) / Decimal(str(info["currentLtv"]))


# ---------------------------------------------------------------------------
# provenance / shape of the captured samples
# ---------------------------------------------------------------------------


def test_fixture_provenance_and_real_emode_shape() -> None:
    source = LOANS["source"]
    assert source["api_base"] == "https://api.kamino.finance"
    assert source["loans_endpoint"] == "GET /klend/loans/{obligation}"
    assert source["addresses_redacted"] is True
    # Every captured loan really is in an active elevation group, and every
    # (market, group) pair is covered by the on-chain config fixture.
    loans = [loan for market in LOANS["markets"] for loan in market["loans"]]
    assert len(loans) == 3
    for loan in loans:
        assert loan["elevationGroup"] > 0
        assert loan["loanId"].startswith("EmodeSampleObligation")
        assert loan["user"].startswith("EmodeSampleWallet")
        config = GROUPS_BY_MARKET[loan["marketId"]][loan["elevationGroup"]]
        assert config["id"] == loan["elevationGroup"]
        info = loan["loanInfo"]
        assert Decimal(str(info["currentLtv"])) > 0
        assert Decimal(str(info["liquidationLtv"])) > 0


def test_market_group_config_matches_loan_borrow_reserve() -> None:
    """The decoded group must be the one for this debt asset (guards offsets)."""
    reserves = {
        market["market"]: {
            row["reserve"]: row["liquidityTokenMint"] for row in market["reserves"]
        }
        for market in LOANS["markets"]
    }
    for market in LOANS["markets"]:
        for loan in market["loans"]:
            config = GROUPS_BY_MARKET[market["market"]][loan["elevationGroup"]]
            borrow_mints = {
                leg["tokenMint"] for leg in loan["loanInfo"]["debt"]["borrows"]
            }
            assert config["debt_reserve_mint"] in borrow_mints
            assert config["debt_reserve"] in reserves[market["market"]]


# ---------------------------------------------------------------------------
# eMode-effective per-leg LTV / HF against the real protocol values
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("market,loan", ALL_SAMPLES)
def test_emode_per_leg_ltv_equals_market_group_config(market: dict, loan: dict) -> None:
    config = GROUPS_BY_MARKET[market["market"]][loan["elevationGroup"]]
    expected_max_ltv = Decimal(config["ltv_pct"]) / Decimal(100)
    expected_liq_ltv = Decimal(config["liquidation_threshold_pct"]) / Decimal(100)

    row = _single_row(loan, reserves=market.get("reserves"))
    assert row["details"]["elevation_group"] == loan["elevationGroup"]
    assert row["details"]["elevation_group_field_present"] is True
    assert not row["details"].get("elevation_degraded")

    for leg in row["details"]["collateral"]:
        assert leg["maxLtv"] == pytest.approx(float(expected_max_ltv))
        assert leg["liquidationLtv"] == pytest.approx(float(expected_liq_ltv))
    # The loans API publishes the eMode value in the *plain* liquidationLtv
    # field; it must not be mistaken for the reserve's base-mode value.
    assert row["details"]["liq_threshold"] == pytest.approx(float(expected_liq_ltv))


@pytest.mark.parametrize("market,loan", ALL_SAMPLES)
def test_emode_health_factor_matches_protocol_implied(market: dict, loan: dict) -> None:
    row = _single_row(loan, reserves=market.get("reserves"))
    expected = _protocol_hf(loan)
    assert row["details"]["health_factor"] is not None
    assert _relative_error(row["details"]["health_factor"], expected) < HF_TOLERANCE

    info = loan["loanInfo"]
    assert row["details"]["ltv"] == pytest.approx(float(info["currentLtv"]), rel=HF_TOLERANCE)
    assert row["details"]["liq_threshold"] == pytest.approx(
        float(info["liquidationLtv"]), rel=HF_TOLERANCE
    )


def test_reserve_metrics_base_max_ltv_zero_is_not_used() -> None:
    """rstSOL has reserve-metrics maxLtv 0 but eMode maxLtv 0.8."""
    market = LOANS["markets"][0]
    rstsol = next(r for r in market["reserves"] if r["liquidityToken"] == "rstSOL")
    assert Decimal(rstsol["maxLtv"]) == 0

    loan = market["loans"][0]
    row = _single_row(loan, reserves=market["reserves"])
    leg = row["details"]["collateral"][0]
    assert leg["symbol"] == "rstSOL"
    # If base reserve metrics leaked in, this would be 0 and HF would collapse.
    assert leg["maxLtv"] == pytest.approx(0.8)
    assert row["details"]["health_factor"] == pytest.approx(
        float(_protocol_hf(loan)), rel=HF_TOLERANCE
    )


def test_multi_collateral_emode_threshold_matches_protocol() -> None:
    market = LOANS["markets"][1]
    loan = market["loans"][1]
    assert len(loan["loanInfo"]["collateral"]["deposits"]) == 2
    row = _single_row(loan, reserves=market["reserves"])
    assert len(row["details"]["collateral"]) == 2
    assert row["details"]["liq_threshold"] == pytest.approx(0.95, rel=1e-9)
    assert _relative_error(row["details"]["health_factor"], _protocol_hf(loan)) < HF_TOLERANCE


# ---------------------------------------------------------------------------
# schema drift must fail loudly / degrade explicitly, never silently miscompute
# ---------------------------------------------------------------------------


def test_base_liquidation_threshold_does_not_override_effective_ltv() -> None:
    market = LOANS["markets"][0]
    loan = copy.deepcopy(market["loans"][0])
    # Some payload variants also carry a base-mode liquidationThreshold. The
    # eMode-effective value is liquidationLtv (0.9), not the base 0.8.
    loan["loanInfo"]["collateral"]["deposits"][0]["liquidationThreshold"] = "0.8"
    row = _single_row(loan, reserves=market["reserves"])
    assert row["details"]["liq_threshold"] == pytest.approx(0.9)
    assert _relative_error(row["details"]["health_factor"], _protocol_hf(loan)) < HF_TOLERANCE


def test_missing_effective_ltv_degrades_instead_of_using_base() -> None:
    market = LOANS["markets"][0]
    loan = copy.deepcopy(market["loans"][0])
    for leg in loan["loanInfo"]["collateral"]["deposits"]:
        leg.pop("liquidationLtv", None)
        leg.pop("maxLtv", None)
    loan["loanInfo"].pop("liquidationLtv", None)

    row = _single_row(loan, reserves=market["reserves"])
    details = row["details"]
    assert details["elevation_group"] == 1
    assert details["elevation_ltv_source"] == "elevation_missing"
    assert details["elevation_degraded"] is True
    assert details["elevation_note"]
    # No base-mode guess: HF and threshold are withheld rather than wrong.
    assert details["liq_threshold"] is None
    assert details["health_factor"] is None


def test_partially_missing_leg_ltv_degrades() -> None:
    """One leg without eMode LTV must not be silently dropped from the weight."""
    market = LOANS["markets"][1]
    loan = copy.deepcopy(market["loans"][1])
    loan["loanInfo"]["collateral"]["deposits"][1].pop("liquidationLtv", None)
    loan["loanInfo"].pop("liquidationLtv", None)

    row = _single_row(loan, reserves=market["reserves"])
    details = row["details"]
    assert details["elevation_degraded"] is True
    assert details["elevation_ltv_source"] == "elevation_missing"
    assert details["liq_threshold"] is None
    assert details["health_factor"] is None


def test_renamed_elevation_group_field_is_marked_base() -> None:
    market = LOANS["markets"][1]
    loan = copy.deepcopy(market["loans"][0])
    loan.pop("elevationGroup", None)
    loan["loanInfo"].pop("elevationGroup", None)

    row = _single_row(loan, reserves=market["reserves"])
    details = row["details"]
    assert details["elevation_group"] == 0
    assert details["elevation_group_field_present"] is False
    assert details["elevation_ltv_source"] == "base"
    assert "elevation_degraded" not in details
    # The per-leg values are already eMode-effective, so a rename of only the
    # group id degrades the *label*, not the arithmetic.
    assert _relative_error(row["details"]["health_factor"], _protocol_hf(loan)) < HF_TOLERANCE


def test_loan_level_liquidation_ltv_fallback() -> None:
    """When per-leg LTV disappears, the loan-level reported value is used."""
    market = LOANS["markets"][1]
    loan = copy.deepcopy(market["loans"][0])
    for leg in loan["loanInfo"]["collateral"]["deposits"]:
        leg.pop("liquidationLtv", None)
        leg.pop("maxLtv", None)

    row = _single_row(loan, reserves=market["reserves"])
    details = row["details"]
    assert details["elevation_ltv_source"] == "loan_elevation"
    assert not details.get("elevation_degraded")
    assert details["liq_threshold"] == pytest.approx(0.95)
    assert _relative_error(row["details"]["health_factor"], _protocol_hf(loan)) < HF_TOLERANCE
