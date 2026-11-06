"""p25 loop-position metrics: spec fixtures, formulas, flows and missing data."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.loop_metrics import compute_loop_metrics
from app.models import Position, PositionSnapshot, User
from app.positions_core import _slim_details
from tests.conftest import auth_header

UTC = timezone.utc


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _snap(
    ts: str,
    net: str,
    coll: str,
    debt: str,
    *,
    apy_net: float | None = None,
    details: dict | None = None,
) -> dict:
    return {
        "ts": _ts(ts),
        "net_value_usdt": Decimal(net),
        "collateral_usdt": Decimal(coll),
        "debt_usdt": Decimal(debt),
        "apy_net": apy_net,
        "details": details or {},
    }


ID1 = {
    "id": 1,
    "protocol": "morpho",
    "chain": "ethereum",
    "label": "Morpho USD3/USDC",
    "collateral_usdt": Decimal("12360.41"),
    "debt_usdt": Decimal("10473.27"),
    "details": {
        "health_factor": 1.080,
        "liq_price": 1.0987,
        "liq_price_basis": "market",
        "net_apy": 0.08079358631580062,
        "collateral": [
            {
                "symbol": "USD3",
                "amount": 12360.41 / 1.1865,
                "usd": 12360.41,
                "apy_supply": 0.06279,
            }
        ],
        "borrow": [
            {
                "symbol": "USDC",
                "amount": 10473.27,
                "usd": 10473.27,
                "apy_borrow": 0.059546,
            }
        ],
    },
}

ID2 = {
    "id": 2,
    "protocol": "morpho",
    "chain": "ethereum",
    "label": "Morpho PT-USD3-17DEC2026/USDC",
    "collateral_usdt": Decimal("8438.26"),
    "debt_usdt": Decimal("6183.48"),
    "details": {
        "health_factor": 1.149,
        "liq_price": 0.8548,
        "liq_price_basis": "oracle",
        "net_apy": 0.20474320244103633,
        "collateral": [
            {
                "symbol": "PT-USD3-17DEC2026",
                "amount": 8438.26 / 0.9821,
                "usd": 8438.26,
                "apy_supply": 0.15006,
            }
        ],
        "borrow": [
            {
                "symbol": "USDC",
                "amount": 6183.48,
                "usd": 6183.48,
                "apy_borrow": 0.13012,
            }
        ],
    },
}


def id1_snapshots() -> list[dict]:
    return [
        _snap("2026-10-28T12:28:00Z", "1590.54", "12356.54", "10766.00"),
        _snap("2026-10-30T12:28:00Z", "1592.00", "12358.00", "10766.00"),
        _snap(
            "2026-10-31T05:24:00Z",
            "1587.11",
            "12353.11",
            "10766.00",
            apy_net=0.08,
        ),
        # The 293 USDC repayment / capital injection: not income.
        _snap(
            "2026-10-31T06:55:00Z",
            "1886.97",
            "12359.97",
            "10473.00",
            apy_net=0.09,
        ),
        _snap(
            "2026-10-31T15:40:00Z",
            "1887.14",
            "12360.41",
            "10473.27",
            apy_net=0.08079358631580062,
        ),
    ]


def id2_snapshots() -> list[dict]:
    return [
        _snap(
            "2026-10-28T18:00:00Z",
            "2251.15",
            "8435.00",
            "6183.85",
            apy_net=-0.25,
        ),
        _snap(
            "2026-10-29T18:00:00Z",
            "2252.10",
            "8436.00",
            "6183.70",
            apy_net=0.31,
        ),
        _snap(
            "2026-10-30T18:00:00Z",
            "2253.40",
            "8437.10",
            "6183.60",
            apy_net=0.20,
        ),
        _snap(
            "2026-10-31T06:40:00Z",
            "2254.78",
            "8438.26",
            "6183.48",
            apy_net=0.20474320244103633,
        ),
    ]


def test_id1_sample_formulas_and_missing_split() -> None:
    result = compute_loop_metrics(ID1, id1_snapshots())

    assert result["principal_usd"] == pytest.approx(1887.14)
    assert result["net_apy"] == pytest.approx(0.08079358631580062)
    assert result["collateral_yield_contrib"] == pytest.approx(0.411268, abs=1e-5)
    assert result["borrow_cost_contrib"] == pytest.approx(-0.330474, abs=1e-5)
    assert result["health_factor"] == pytest.approx(1.080)
    assert result["liq_price"] == pytest.approx(1.0987)
    assert result["current_price"] == pytest.approx(1.1865)
    assert result["distance_to_liq_pct"] == pytest.approx(0.074, abs=5e-4)
    assert result["leverage"] == pytest.approx(6.55, abs=0.01)
    assert result["spread"] == pytest.approx(0.003244)
    assert result["spread_negative"] is False
    assert result["depeg_liq_price"] == pytest.approx(1.0987)
    assert result["depeg_pair"] == "USDC/USD3"
    assert result["borrow_apy_high"] is None
    assert "数据积累中" in result["missing"]["borrow_apy_high"]


def test_id2_sample_formulas() -> None:
    result = compute_loop_metrics(ID2, id2_snapshots())

    assert result["principal_usd"] == pytest.approx(2254.78)
    assert result["net_apy"] == pytest.approx(0.20474320244103633)
    assert result["collateral_yield_contrib"] == pytest.approx(0.56156, abs=1e-4)
    assert result["borrow_cost_contrib"] == pytest.approx(-0.35682, abs=1e-4)
    assert result["health_factor"] == pytest.approx(1.149)
    assert result["distance_to_liq_pct"] == pytest.approx(0.12962, abs=5e-4)
    assert result["leverage"] == pytest.approx(3.74, abs=0.01)
    assert result["spread"] == pytest.approx(0.01994)
    assert result["depeg_liq_price"] == pytest.approx(0.8548)
    assert result["depeg_pair"] == "USDC/PT-USD3-17DEC2026"


def test_id1_repayment_is_a_flow_not_income() -> None:
    result = compute_loop_metrics(ID1, id1_snapshots())

    # Start 1590.54 -> end 1887.14 with a ~293 USDC repayment.  The PnL must
    # exclude that repayment rather than booking the whole net-value jump.
    assert result["pnl"]["net_flows"] == pytest.approx(293.0, abs=0.1)
    assert result["pnl"]["amount"] == pytest.approx(3.60, abs=0.1)
    assert result["pnl"]["amount"] < 10
    assert result["pnl"]["rate"] == pytest.approx(3.60 / 1883.54, abs=1e-4)
    # Historical snapshots have no split rates, so the split is missing.
    assert result["pnl_breakdown"]["price"] is None
    assert result["pnl_breakdown"]["collateral_interest"] is None
    assert result["pnl_breakdown"]["borrow_interest"] is None
    assert "缺少抵押/借款分项利率" in result["missing"]["price_pnl"]


def test_id2_pnl_matches_spec_estimate() -> None:
    result = compute_loop_metrics(ID2, id2_snapshots())
    assert result["pnl"]["net_flows"] == pytest.approx(0.0, abs=0.5)
    assert result["pnl"]["amount"] == pytest.approx(3.63, abs=0.02)
    assert result["pnl"]["rate"] == pytest.approx(3.63 / 2251.15, abs=1e-4)


@pytest.mark.parametrize("position", [ID1, ID2])
def test_windows_are_accumulating_below_seven_days(position) -> None:
    result = compute_loop_metrics(position, id1_snapshots() if position is ID1 else id2_snapshots())
    for key in ("7d", "30d"):
        window = result["apy_windows"][key]
        assert window["accumulating"] is True
        assert window["points"]
        assert window["min"] is not None and window["max"] is not None
        assert window["avg"] is not None
        assert "数据积累中" in result["missing"][f"window_{key}"]


def test_id2_window_uses_available_min_max() -> None:
    result = compute_loop_metrics(ID2, id2_snapshots())
    window = result["apy_windows"]["7d"]
    assert window["min"] == pytest.approx(-0.25)
    assert window["max"] == pytest.approx(0.31)
    # 18:00Z is already the next Beijing day; the daily line has 3 points.
    assert len(window["points"]) == 3
    assert [point["date"] for point in window["points"]] == [
        "2026-10-29",
        "2026-10-30",
        "2026-10-31",
    ]


def _split_snapshot(day: int, net: str, *, same_legs: bool = True) -> dict:
    details = {
        "collateral": [
            {
                "symbol": "USD3",
                "amount": 12000,
                "usd": 12000,
                "apy_supply": 0.10,
            }
        ],
        "borrow": [
            {
                "symbol": "USDC",
                "amount": 6000,
                "usd": 6000,
                "apy_borrow": 0.05,
            }
        ],
    }
    return _snap(f"2026-10-{day:02d}T00:00:00Z", net, "12000", "6000", details=details)


def test_interest_split_identity_and_price_residual() -> None:
    position = {
        "id": 9,
        "protocol": "morpho",
        "chain": "ethereum",
        "collateral_usdt": Decimal("12000"),
        "debt_usdt": Decimal("6000"),
        "details": {
            "health_factor": 2.0,
            "liq_price": 0.75,
            "collateral": [
                {"symbol": "USD3", "amount": 12000, "usd": 12000, "apy_supply": 0.10}
            ],
            "borrow": [
                {"symbol": "USDC", "amount": 6000, "usd": 6000, "apy_borrow": 0.05}
            ],
        },
    }
    snaps = [
        _split_snapshot(24, "6000"),
        _split_snapshot(25, "6000"),
        _split_snapshot(26, "6004.931506849315"),
    ]
    result = compute_loop_metrics(position, snaps)
    breakdown = result["pnl_breakdown"]

    assert breakdown["price"] is not None
    assert breakdown["collateral_interest"] == pytest.approx(12000 * 0.10 * 2 / 365)
    assert breakdown["borrow_interest"] == pytest.approx(6000 * 0.05 * 2 / 365)
    assert breakdown["interest_net"] == pytest.approx(
        breakdown["collateral_interest"] - breakdown["borrow_interest"]
    )
    assert (
        breakdown["price"]
        + breakdown["collateral_interest"]
        - breakdown["borrow_interest"]
    ) == pytest.approx(result["pnl"]["amount"], abs=1e-6)
    assert breakdown["price"] == pytest.approx(0.0, abs=1e-6)
    assert result["pnl"]["net_flows"] == pytest.approx(0.0, abs=1e-6)


def test_single_snapshot_cannot_make_a_pnl_interval() -> None:
    result = compute_loop_metrics(ID1, id1_snapshots()[:1])
    assert result["pnl"]["amount"] is None
    assert "快照不足" in result["missing"]["pnl"]


def test_partial_split_marks_only_unavailable_items() -> None:
    def snap(day: int, *, with_borrow_apy: bool) -> dict:
        details = {
            "collateral": [
                {"symbol": "USD3", "amount": 12000, "usd": 12000, "apy_supply": 0.10}
            ],
            "borrow": [
                {
                    "symbol": "USDC",
                    "amount": 6000,
                    "usd": 6000,
                    **({"apy_borrow": 0.05} if with_borrow_apy else {}),
                }
            ],
        }
        return _snap(f"2026-10-{day:02d}T00:00:00Z", "6000", "12000", "6000", details=details)

    snaps = [snap(24, with_borrow_apy=False), snap(25, with_borrow_apy=False)]
    result = compute_loop_metrics(ID1, snaps)
    breakdown = result["pnl_breakdown"]
    assert breakdown["collateral_interest"] == pytest.approx(12000 * 0.10 / 365)
    assert breakdown["borrow_interest"] is None
    assert breakdown["price"] is None
    assert breakdown["interest_net"] is None
    assert "apy_borrow" in result["missing"]["borrow_interest"]
    assert "分项利率" in result["missing"]["price_pnl"]


def test_negative_spread_flags() -> None:
    position = {
        "id": 10,
        "protocol": "kamino",
        "chain": "solana",
        "collateral_usdt": Decimal("1000"),
        "debt_usdt": Decimal("500"),
        "details": {
            "health_factor": 2.0,
            "liq_price": 0.5,
            "collateral": [
                {"symbol": "SOL", "amount": 10, "usd": 1000, "apy_supply": 0.02}
            ],
            "borrow": [
                {"symbol": "USDC", "amount": 500, "usd": 500, "apy_borrow": 0.08}
            ],
        },
    }
    result = compute_loop_metrics(position, [])
    assert result["spread"] == pytest.approx(-0.06)
    assert result["spread_negative"] is True
    # Non-stable collateral: no depeg liquidation price, but not a "missing"
    # field either (the concept does not apply).
    assert result["depeg_liq_price"] is None
    assert result["depeg_applicable"] is False
    assert "depeg_liq_price" not in result["missing"]


def test_missing_everything_is_null_with_reasons() -> None:
    result = compute_loop_metrics(
        {"id": 11, "protocol": "morpho", "chain": "ethereum", "details": {}},
        [],
    )
    assert result["principal_usd"] is None
    assert result["net_apy"] is None
    assert result["health_factor"] is None
    assert result["liq_price"] is None
    assert result["distance_to_liq_pct"] is None
    assert result["leverage"] is None
    assert result["borrow_apy_high"] is None
    assert result["pnl"]["amount"] is None
    for key in (
        "principal",
        "net_apy",
        "health_factor",
        "liq_price",
        "distance_to_liq_pct",
        "leverage",
        "borrow_apy_high",
        "pnl",
        "price_pnl",
    ):
        assert result["missing"][key]


def test_slim_details_keeps_legs_and_liq_price() -> None:
    slim = _slim_details(
        {
            "health_factor": 1.2,
            "liq_price": 0.9,
            "liq_price_basis": "oracle",
            "collateral": [
                {
                    "symbol": "USD3",
                    "amount": 10,
                    "usd": 12,
                    "apy_supply": 0.06,
                    "contract": "0xignored",
                }
            ],
            "borrow": [
                {
                    "symbol": "USDC",
                    "amount": 5,
                    "usd": 5,
                    "apy_borrow": 0.05,
                    "contract": "0xignored",
                }
            ],
            "pnl_data_missing": True,
        }
    )
    assert slim is not None
    assert slim["liq_price"] == 0.9
    assert slim["liq_price_basis"] == "oracle"
    assert slim["collateral"] == [
        {"symbol": "USD3", "amount": 10, "usd": 12, "apy_supply": 0.06}
    ]
    assert slim["borrow"] == [
        {"symbol": "USDC", "amount": 5, "usd": 5, "apy_borrow": 0.05}
    ]
    assert "contract" not in slim["collateral"][0]


def test_loop_endpoint_reads_snapshots_and_is_user_scoped(
    client, db, demo_user: User
) -> None:
    position = Position(
        user_id=demo_user.id,
        kind="lending",
        protocol="morpho",
        chain="ethereum",
        account_ref="0x" + "1" * 40,
        market_ref="p25-loop-test",
        status="open",
        net_value_usdt=Decimal("1887.14"),
        collateral_usdt=Decimal("12360.41"),
        debt_usdt=Decimal("10473.27"),
        details=ID1["details"],
    )
    db.add(position)
    db.commit()
    db.refresh(position)
    base = datetime(2026, 10, 28, 12, 28, tzinfo=UTC)
    for index, (net, coll, debt) in enumerate(
        [
            ("1590.54", "12356.54", "10766.00"),
            ("1887.14", "12360.41", "10473.27"),
        ]
    ):
        db.add(
            PositionSnapshot(
                position_id=position.id,
                ts=base + timedelta(days=index),
                net_value_usdt=Decimal(net),
                collateral_usdt=Decimal(coll),
                debt_usdt=Decimal(debt),
                apy_net=Decimal("0.08"),
                details={"net_apy": 0.08},
            )
        )
    db.commit()

    headers = auth_header(client)
    response = client.get(f"/api/lending/positions/{position.id}/loop", headers=headers)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["position_id"] == position.id
    assert payload["principal_usd"] == pytest.approx(1887.14)
    assert payload["pnl"]["snapshots"] == 2
    assert payload["missing"]["borrow_apy_high"]

    assert (
        client.get("/api/lending/positions/99999/loop", headers=headers).status_code == 404
    )