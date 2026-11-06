"""Phase-4 P0: overview adds open-position NAV and flags covered holdings."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

from app.auth import hash_password
from app.models import Snapshot, User
from app.positions_core import load_mock_positions, positions_net_value, upsert_positions
from tests.conftest import auth_header, seed_nav

AAVE_WETH = "0x4d5F47FA6A74757f35C14fD3a6Ef8E3C9BC514E8"
AAVE_USDC = "0x98C23E9d8f34FEFb1B7BD6a91B7FF122F4e16F5c"
AAVE_DEBT = "0x6df1C1E379bC5a00a7b4C6e67A203333772f45A8"
MORPHO_COLLAT = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

MOCK_NET = 15600.0  # 7000 + 4500 + 2000 + 2500 + (-400)


def _user(db, username: str) -> User:
    user = User(username=username, hashed_password=hash_password("pass-word"))
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _sync_snapshot(db, user_id: int, holdings: list[dict], nav: str = "100.00") -> None:
    moment = datetime(2026, 10, 23, 15, 0, tzinfo=timezone.utc)
    db.add(
        Snapshot(
            user_id=user_id,
            ts=moment,
            nav_usdt=Decimal(nav),
            source="sync",
            breakdown=json.dumps({"holdings": holdings}),
        )
    )
    db.commit()


def _covered_holdings() -> list[dict]:
    return [
        {
            "source": "zerion",
            "asset": "aEthWETH",
            "amount": "2.5",
            "value_usdt": "8000",
            "priced": True,
            "live": True,
            "contract": AAVE_WETH,
        },
        {
            "source": "zerion",
            "asset": "aEthUSDC",
            "amount": "2000",
            "value_usdt": "2000",
            "priced": True,
            "live": True,
            "contract": AAVE_USDC.upper(),
        },
        {
            "source": "zerion",
            "symbol": "variableDebtEthUSDT",
            "asset": "variableDebtEthUSDT",
            "amount": "3000",
            "value_usdt": "3000",
            "priced": True,
            "live": True,
            "contract": AAVE_DEBT,
        },
        {
            "source": "zerion",
            "asset": "mwETH",
            "amount": "1.8",
            "value_usdt": "6000",
            "priced": True,
            "live": True,
            "contract": MORPHO_COLLAT,
        },
        {
            "source": "zerion",
            "asset": "WETH",
            "amount": "0.01",
            "value_usdt": "100",
            "priced": True,
            "live": True,
            "contract": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
        },
        {
            "source": "zerion",
            "asset": "PLP-SUSDX-3DEC2026",
            "amount": "1",
            "value_usdt": "6009",
            "priced": True,
            "live": True,
            "contract": "0xpendlelp",
        },
        {
            "source": "zerion",
            "asset": "PT-sUSDx-3DEC2026",
            "amount": "1",
            "value_usdt": "2486",
            "priced": True,
            "live": True,
        },
        {
            "source": "zerion",
            "asset": "YT-sUSDx-3DEC2026",
            "amount": "1",
            "value_usdt": "103",
            "priced": True,
            "live": True,
        },
        {
            "source": "zerion",
            "asset": "ARB",
            "amount": "10",
            "value_usdt": "20",
            "priced": True,
            "live": True,
        },
    ]


def test_overview_nav_unchanged_without_positions(client, db, demo_user: User):
    seed_nav(db, demo_user.id)
    body = client.get("/api/overview", headers=auth_header(client)).json()
    assert body["nav_source"] == "seed"
    assert body["current_nav_usdt"] == 11200.0
    assert body["positions_net_value_usdt"] == 0.0
    assert len(body["series"]) == 2
    assert body["holdings"] == []


def test_overview_nav_adds_open_positions(client, db, demo_user: User):
    seed_nav(db, demo_user.id)
    before = client.get("/api/overview", headers=auth_header(client)).json()
    series_before = before["series"]

    upsert_positions(db, demo_user.id, load_mock_positions())
    assert float(positions_net_value(db, demo_user.id)) == MOCK_NET

    body = client.get("/api/overview", headers=auth_header(client)).json()
    assert body["positions_net_value_usdt"] == MOCK_NET
    assert body["current_nav_usdt"] == before["current_nav_usdt"] + MOCK_NET
    # Seed curve is not rewritten when positions exist.
    assert body["series"] == series_before
    assert body["nav_source"] == "seed"
    assert body["holdings"] == []


def test_overview_marks_covered_holdings(client, db, demo_user: User):
    holdings = _covered_holdings()
    _sync_snapshot(db, demo_user.id, holdings, nav="100.00")
    upsert_positions(db, demo_user.id, load_mock_positions())

    body = client.get("/api/overview", headers=auth_header(client)).json()
    assert body["current_nav_usdt"] == 100.0 + MOCK_NET
    assert body["positions_net_value_usdt"] == MOCK_NET
    # Series stays the snapshot curve; positions are not appended as a point.
    assert [point["nav_usdt"] for point in body["series"]] == [100.0]

    flags = {row["asset"]: row["covered_by_position"] for row in body["holdings"]}
    assert flags["aEthWETH"] is True
    assert flags["aEthUSDC"] is True
    assert flags["variableDebtEthUSDT"] is True
    assert flags["mwETH"] is True
    assert flags["WETH"] is False
    assert flags["PLP-SUSDX-3DEC2026"] is False
    assert flags["PT-sUSDx-3DEC2026"] is False
    assert flags["YT-sUSDx-3DEC2026"] is False
    assert flags["ARB"] is False
    # This snapshot was stored before the fold (no positions_included), so the
    # hero still adds the position net on top. A folded snapshot must not.
    assert len(body["holdings"]) == len(holdings)


def test_overview_does_not_double_count_folded_snapshot(client, db, demo_user: User):
    """NAV already equals uncovered holdings + open position net."""
    holdings = _covered_holdings()
    covered_assets = {"aEthWETH", "aEthUSDC", "variableDebtEthUSDT", "mwETH"}
    uncovered = sum(float(row["value_usdt"]) for row in holdings if row["asset"] not in covered_assets)
    # 100 is the raw holding sum the old writer stored. The folded writer
    # stores uncovered + position net instead, and says so on the breakdown.
    folded_nav = uncovered + MOCK_NET
    moment = datetime(2026, 10, 23, 16, 0, tzinfo=timezone.utc)
    db.add(
        Snapshot(
            user_id=demo_user.id,
            ts=moment,
            nav_usdt=Decimal(str(folded_nav)),
            source="sync",
            breakdown=json.dumps(
                {"holdings": holdings, "positions_included": True, "nav_usdt": str(folded_nav)}
            ),
        )
    )
    db.commit()
    upsert_positions(db, demo_user.id, load_mock_positions())

    body = client.get("/api/overview", headers=auth_header(client)).json()
    assert body["nav_source"] == "sync"
    assert body["positions_net_value_usdt"] == MOCK_NET
    assert body["current_nav_usdt"] == folded_nav
    assert body["current_nav_usdt"] != folded_nav + MOCK_NET
    flags = {row["asset"]: row["covered_by_position"] for row in body["holdings"]}
    assert flags["aEthWETH"] is True
    assert flags["variableDebtEthUSDT"] is True
    assert flags["PT-sUSDx-3DEC2026"] is False
    assert flags["PLP-SUSDX-3DEC2026"] is False
    assert len(body["holdings"]) == len(holdings)


def test_overview_positions_are_user_scoped(client, db):
    owner = _user(db, "ov-owner")
    other = _user(db, "ov-other")
    seed_nav(db, owner.id)
    seed_nav(db, other.id)
    _sync_snapshot(db, owner.id, _covered_holdings(), nav="50.00")
    _sync_snapshot(
        db,
        other.id,
        [
            {
                "source": "zerion",
                "asset": "aEthWETH",
                "amount": "1",
                "value_usdt": "1",
                "priced": True,
                "live": True,
                "contract": AAVE_WETH,
            }
        ],
        nav="80.00",
    )
    upsert_positions(db, owner.id, load_mock_positions())

    other_headers = auth_header(client, username="ov-other", password="pass-word")
    other_body = client.get("/api/overview", headers=other_headers).json()
    # Other user's latest snapshot is after the seed curve, so NAV is the sync tip.
    assert other_body["current_nav_usdt"] == 80.0
    assert other_body["positions_net_value_usdt"] == 0.0
    assert other_body["holdings"][0]["asset"] == "aEthWETH"
    assert other_body["holdings"][0]["covered_by_position"] is False

    owner_headers = auth_header(client, username="ov-owner", password="pass-word")
    owner_body = client.get("/api/overview", headers=owner_headers).json()
    assert owner_body["current_nav_usdt"] == 50.0 + MOCK_NET
    assert owner_body["positions_net_value_usdt"] == MOCK_NET
    owner_flags = {row["asset"]: row["covered_by_position"] for row in owner_body["holdings"]}
    assert owner_flags["aEthWETH"] is True
    assert owner_flags["PT-sUSDx-3DEC2026"] is False
