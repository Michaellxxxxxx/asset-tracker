"""Phase-5 A: portfolio structure/items/override API."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

from app.auth import hash_password
from app.models import AssetOverride, Position, Snapshot, User
from tests.conftest import auth_header

TOTAL = 2000.0


def _sync_snapshot(
    db,
    user_id: int,
    holdings: list[dict],
    nav: float = 1000.0,
    ts: datetime | None = None,
) -> None:
    db.add(
        Snapshot(
            user_id=user_id,
            ts=ts or datetime(2026, 10, 30, 15, 0, tzinfo=timezone.utc),
            nav_usdt=Decimal(str(nav)),
            source="sync",
            breakdown=json.dumps({"holdings": holdings}),
        )
    )
    db.commit()


def _holdings() -> list[dict]:
    return [
        {
            "source": "binance",
            "asset": "USDC",
            "amount": "690",
            "value_usdt": "690",
            "priced": True,
            "live": True,
        },
        {
            "source": "binance",
            "asset": "USDT",
            "amount": "10",
            "value_usdt": "10",
            "priced": True,
            "live": True,
        },
        {
            "source": "binance",
            "asset": "ARB",
            "amount": "300",
            "value_usdt": "300",
            "priced": True,
            "live": True,
        },
    ]


def _position(db, user_id: int) -> Position:
    row = Position(
        user_id=user_id,
        kind="lending",
        protocol="aave",
        chain="evm",
        account_ref="0xowner",
        market_ref="USDC",
        label="Aave USDC",
        status="open",
        net_value_usdt=Decimal("1000"),
        collateral_usdt=Decimal("1900"),
        debt_usdt=Decimal("900"),
        details={
            "health_factor": 1.1,
            "collateral": [{"symbol": "WETH", "usd": 1900}],
            "borrow": [{"symbol": "USDC", "usd": 900}],
        },
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _seed_portfolio(db, user_id: int) -> Position:
    _sync_snapshot(db, user_id, _holdings())
    return _position(db, user_id)


def test_structure_matches_overview_nav_and_percentages(client, db, demo_user: User):
    _seed_portfolio(db, demo_user.id)
    headers = auth_header(client)
    body = client.get("/api/portfolio/structure", headers=headers).json()
    overview = client.get("/api/overview", headers=headers).json()

    assert body["total_usdt"] == overview["current_nav_usdt"] == TOTAL
    assert sum(bucket["pct"] for bucket in body["by_class"]) == 100.0
    assert sum(bucket["pct"] for bucket in body["by_risk"]) == 100.0
    by_class = {bucket["class"]: bucket for bucket in body["by_class"]}
    assert by_class["cash"]["value_usdt"] == 700.0
    assert by_class["crypto"]["value_usdt"] == 300.0
    assert by_class["defi"]["value_usdt"] == 1000.0
    assert by_class["cash"]["pct"] == 35.0
    assert by_class["defi"]["pct"] == 50.0
    # USDC + USDT are both cash; all six class buckets are always present.
    assert list(by_class) == ["cash", "crypto", "stock", "defi", "option", "other"]
    by_risk = {bucket["risk"]: bucket for bucket in body["by_risk"]}
    assert by_risk["high"]["value_usdt"] == 1300.0
    assert body["high_risk_pct"] == 65.0


def test_items_expose_fields_misc_and_filters(client, db, demo_user: User):
    _seed_portfolio(db, demo_user.id)
    headers = auth_header(client)
    body = client.get("/api/portfolio/items", headers=headers).json()
    assert body["total_usdt"] == TOTAL
    assert len(body["items"]) == 4
    by_name = {row["name"]: row for row in body["items"]}
    usdc = by_name["USDC"]
    assert usdc["key"] == "symbol:binance:usdc"
    assert usdc["asset_class"] == "cash"
    assert usdc["risk_level"] == "low"
    assert usdc["overridden"] is False
    assert usdc["misc"] is False
    assert usdc["kind"] == "holding"
    assert by_name["USDT"]["misc"] is True
    assert by_name["ARB"]["risk_level"] == "high"
    assert by_name["Aave USDC"]["kind"] == "position"
    assert by_name["Aave USDC"]["position_id"] is not None
    assert by_name["Aave USDC"]["value_usdt"] == 1000.0

    cash = client.get("/api/portfolio/items?class=cash", headers=headers).json()
    assert {row["name"] for row in cash["items"]} == {"USDC", "USDT"}
    high = client.get("/api/portfolio/items?risk=high", headers=headers).json()
    assert {row["name"] for row in high["items"]} == {"ARB", "Aave USDC"}


def test_overview_and_positions_carry_classification(client, db, demo_user: User):
    _seed_portfolio(db, demo_user.id)
    headers = auth_header(client)
    overview = client.get("/api/overview", headers=headers).json()
    rows = {row["asset"]: row for row in overview["holdings"]}
    assert rows["USDC"]["asset_class"] == "cash"
    assert rows["USDC"]["risk_level"] == "low"
    assert rows["USDC"]["risk_reason"]
    assert rows["USDC"]["overridden"] is False

    positions = client.get("/api/lending/summary", headers=headers).json()["positions"]
    assert positions[0]["asset_class"] == "defi"
    assert positions[0]["risk_level"] == "high"
    assert positions[0]["overridden"] is False

    listed = client.get("/api/positions", headers=headers).json()
    assert listed[0]["asset_class"] == "defi"
    assert listed[0]["risk_reason"]


def test_override_wins_delete_restores_and_sync_does_not_overwrite(client, db, demo_user: User):
    _seed_portfolio(db, demo_user.id)
    headers = auth_header(client)
    key = "symbol:binance:usdc"

    put = client.put(
        "/api/portfolio/override",
        headers=headers,
        json={"key": key, "asset_class": "defi", "risk_level": "high", "reason": "人工归类"},
    )
    assert put.status_code == 200, put.text
    assert put.json()["overridden"] is True

    row = db.query(AssetOverride).filter(AssetOverride.user_id == demo_user.id).one()
    assert row.asset_class == "defi"
    before = db.query(AssetOverride).count()

    items = client.get("/api/portfolio/items", headers=headers).json()["items"]
    usdc = next(item for item in items if item["name"] == "USDC")
    assert usdc["asset_class"] == "defi"
    assert usdc["risk_level"] == "high"
    assert usdc["risk_reason"] == "人工归类"
    assert usdc["overridden"] is True
    structure = client.get("/api/portfolio/structure", headers=headers).json()
    by_class = {bucket["class"]: bucket for bucket in structure["by_class"]}
    assert by_class["cash"]["value_usdt"] == 10.0
    assert by_class["defi"]["value_usdt"] == 1690.0

    # A later sync rewrites snapshots/positions but must not touch overrides.
    _sync_snapshot(
        db,
        demo_user.id,
        _holdings(),
        nav=1100.0,
        ts=datetime(2026, 10, 31, 15, 0, tzinfo=timezone.utc),
    )
    assert db.query(AssetOverride).count() == before
    items = client.get("/api/portfolio/items", headers=headers).json()["items"]
    usdc = next(item for item in items if item["name"] == "USDC")
    assert usdc["overridden"] is True
    assert usdc["asset_class"] == "defi"

    deleted = client.delete(f"/api/portfolio/override/{key}", headers=headers)
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True
    items = client.get("/api/portfolio/items", headers=headers).json()["items"]
    usdc = next(item for item in items if item["name"] == "USDC")
    assert usdc["overridden"] is False
    assert (usdc["asset_class"], usdc["risk_level"]) == ("cash", "low")
    again = client.delete(f"/api/portfolio/override/{key}", headers=headers)
    assert again.json()["deleted"] is False


def test_override_is_user_isolated(client, db, demo_user: User):
    _seed_portfolio(db, demo_user.id)
    other = User(username="p5-other", hashed_password=hash_password("other-pass"))
    db.add(other)
    db.commit()
    db.refresh(other)
    other_headers = auth_header(client, username="p5-other", password="other-pass")
    owner_headers = auth_header(client)
    key = "symbol:binance:usdc"

    client.put(
        "/api/portfolio/override",
        headers=owner_headers,
        json={"key": key, "asset_class": "stock"},
    )
    other_items = client.get("/api/portfolio/items?class=cash", headers=other_headers).json()
    assert other_items["items"] == []
    owner_cash = client.get("/api/portfolio/items?class=cash", headers=owner_headers).json()
    assert {row["name"] for row in owner_cash["items"]} == {"USDT"}

    # The other user cannot delete or see the owner's override.
    client.delete(f"/api/portfolio/override/{key}", headers=other_headers)
    owner_items = client.get("/api/portfolio/items", headers=owner_headers).json()["items"]
    usdc = next(item for item in owner_items if item["name"] == "USDC")
    assert usdc["asset_class"] == "stock"
    assert usdc["overridden"] is True


def test_unknown_enum_values_are_400(client, db, demo_user: User):
    headers = auth_header(client)
    bad_class = client.put(
        "/api/portfolio/override",
        headers=headers,
        json={"key": "symbol:binance:usdc", "asset_class": "bogus"},
    )
    assert bad_class.status_code == 400
    bad_risk = client.put(
        "/api/portfolio/override",
        headers=headers,
        json={"key": "symbol:binance:usdc", "risk_level": "extreme"},
    )
    assert bad_risk.status_code == 400
    assert client.get("/api/portfolio/items?class=bogus", headers=headers).status_code == 400
    assert client.get("/api/portfolio/items?risk=extreme", headers=headers).status_code == 400
