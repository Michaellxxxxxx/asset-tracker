"""NAV identity: flat holdings − covered + open position net = snapshot nav.

Read-only. No network. Closed positions must not enter the sum.
"""

from __future__ import annotations

from decimal import Decimal

from app.nav_reconcile import reconcile_nav


def _holding(asset: str, value: str, *, covered: bool = False) -> dict:
    row = {"asset": asset, "value_usdt": value, "source": "zerion"}
    if covered:
        row["covered_by_position"] = True
    return row


def test_identity_holds_when_nothing_is_covered() -> None:
    """Morpho-style: underlying USDC stays in the flat book, position net is added once."""
    breakdown = {
        "positions_included": True,
        "holdings": [_holding("USDC", "5431.41"), _holding("USDT", "427.91")],
    }
    # uncovered 5859.32 + open nets 3833.98 = 9693.30. Closed Jupiter must not enter.
    positions = [
        {"status": "open", "net_value_usdt": "1591.51", "protocol": "morpho"},
        {"status": "open", "net_value_usdt": "2242.47", "protocol": "morpho"},
        {"status": "closed", "net_value_usdt": "23779586", "protocol": "jupiter"},
    ]
    got = reconcile_nav(nav_usdt="9693.30", breakdown=breakdown, positions=positions)
    assert got["ok"] is True
    assert got["covered_usdt"] == "0.00"
    assert got["open_position_net_usdt"] == "3833.98"
    assert got["residual_usdt"] == "0.00"
    assert got["open_positions"] == 2


def test_covered_receipt_is_not_priced_twice() -> None:
    """aToken is covered, so it leaves the flat sum and the position net replaces it."""
    breakdown = {
        "positions_included": True,
        "holdings": [
            _holding("ETH", "1000.00"),
            _holding("aEthWETH", "8000.00", covered=True),
            _holding("variableDebtEthUSDT", "6000.00", covered=True),
        ],
    }
    # uncovered = 1000; position net = 5000 (collateral 8000 − debt 6000, already netted)
    positions = [{"status": "open", "net_value_usdt": "5000.00", "protocol": "aave"}]
    got = reconcile_nav(nav_usdt="6000.00", breakdown=breakdown, positions=positions)
    assert got["ok"] is True
    assert got["flat_holdings_usdt"] == "15000.00"
    assert got["covered_usdt"] == "14000.00"
    assert got["uncovered_usdt"] == "1000.00"
    assert got["expected_usdt"] == "6000.00"
    # Adding the position net on top of the covered receipts would be 19000.
    assert Decimal(got["expected_usdt"]) != Decimal("19000.00")


def test_unfolded_snapshot_is_not_ok() -> None:
    got = reconcile_nav(
        nav_usdt="100.00",
        breakdown={"holdings": [_holding("ETH", "100.00")]},
        positions=[{"status": "open", "net_value_usdt": "50"}],
    )
    assert got["ok"] is False
    assert got["folded"] is False


def test_residual_beyond_a_cent_fails() -> None:
    got = reconcile_nav(
        nav_usdt="100.05",
        breakdown={"positions_included": True, "holdings": [_holding("ETH", "100.00")]},
        positions=[],
    )
    assert got["ok"] is False
    assert got["residual_usdt"] == "0.05"


def test_string_breakdown_parses() -> None:
    import json

    payload = {"positions_included": True, "holdings": [_holding("SOL", "10.00")]}
    got = reconcile_nav(nav_usdt="10.00", breakdown=json.dumps(payload), positions=[])
    assert got["ok"] is True
