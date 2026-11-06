"""三期·12: overview holdings from latest sync snapshot."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.integrations.chain.zerion import parse_zerion_positions
from app.integrations.types import ChainBalance, NormalizedBalance
from app.models import Snapshot, User
from app.snapshots import latest_sync_holdings, price_holdings, write_sync_snapshot
from app.integrations.prices.service import PriceService
from tests.conftest import auth_header


def _prices() -> PriceService:
    return PriceService(use_mock=True)


def test_overview_holdings_from_sync(client, db, demo_user: User):
    moment = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
    write_sync_snapshot(db, demo_user.id, mode="mock", ts=moment, prices=_prices())
    db.commit()

    body = client.get("/api/overview", headers=auth_header(client)).json()
    assert body["nav_source"] == "demo"
    assert body["provenance"] == "demo"
    assert body["live"] is False
    assert body["current_nav_usdt"] == 5345.5
    assert body["holdings_asof"].startswith("2026-10-21T12:00:00")
    assert isinstance(body["holdings"], list)
    assert len(body["holdings"]) >= 4
    sample = body["holdings"][0]
    assert {"source", "asset", "amount", "value_usdt", "priced", "live"} <= set(sample)
    # Same snapshot as current_nav tip.
    assert body["latest_snapshot_ts"].startswith("2026-10-21T12:00:00")
    assert body["holdings_asof"] == body["latest_snapshot_ts"]


def test_overview_holdings_empty_without_sync(client, db, demo_user: User):
    from tests.conftest import seed_nav

    seed_nav(db, demo_user.id)
    body = client.get("/api/overview", headers=auth_header(client)).json()
    assert body["nav_source"] == "seed"
    assert body["holdings"] == []
    assert body["holdings_asof"] is None
    # NAV still from seed — unchanged by holdings field.
    assert body["current_nav_usdt"] == 11200.0


def test_overview_holdings_ignore_backfill_breakdown(client, db, demo_user: User):
    """Backfill may carry breakdown, but holdings only come from sync."""
    backfill_ts = datetime(2026, 10, 23, 23, 59, 59, tzinfo=timezone.utc)
    sync_ts = datetime(2026, 10, 23, 15, 0, 0, tzinfo=timezone.utc)
    bf_payload = {
        "holdings": [
            {
                "source": "backfill",
                "asset": "FAKE",
                "amount": "1",
                "price_usdt": "999",
                "value_usdt": "999",
                "priced": True,
                "live": False,
            }
        ]
    }
    sync_payload = {
        "holdings": [
            {
                "source": "binance",
                "asset": "USDT",
                "amount": "100",
                "price_usdt": "1",
                "value_usdt": "100",
                "priced": True,
                "live": True,
            }
        ]
    }
    db.add_all(
        [
            Snapshot(
                user_id=demo_user.id,
                ts=sync_ts,
                nav_usdt=Decimal("100.00"),
                source="sync",
                breakdown=json.dumps(sync_payload),
            ),
            Snapshot(
                user_id=demo_user.id,
                ts=backfill_ts,
                nav_usdt=Decimal("999.00"),
                source="backfill",
                breakdown=json.dumps(bf_payload),
            ),
        ]
    )
    db.commit()

    body = client.get("/api/overview", headers=auth_header(client)).json()
    assert body["nav_source"] == "sync"
    assert body["current_nav_usdt"] == 100.0
    assert len(body["holdings"]) == 1
    assert body["holdings"][0]["asset"] == "USDT"
    assert body["holdings"][0]["source"] == "binance"
    assert body["holdings_asof"].startswith("2026-10-23T15:00:00")

    rows, asof = latest_sync_holdings(db, demo_user.id)
    assert len(rows) == 1
    assert asof.replace(tzinfo=timezone.utc) == sync_ts if asof.tzinfo is None else asof == sync_ts


def test_overview_holdings_empty_when_breakdown_broken(client, db, demo_user: User):
    db.add(
        Snapshot(
            user_id=demo_user.id,
            ts=datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc),
            nav_usdt=Decimal("10.00"),
            source="sync",
            breakdown="{not-json",
        )
    )
    db.commit()
    body = client.get("/api/overview", headers=auth_header(client)).json()
    assert body["holdings"] == []
    assert body["holdings_asof"] is not None
    assert body["current_nav_usdt"] == 10.0


PT = "0x00000000000000000000000000000000000000aa"


def test_price_holdings_persists_chain_contract_in_breakdown():
    """Chain rows keep chain+contract; exchange rows omit both."""
    balances = [
        ChainBalance(
            source="zerion",
            asset="USDC",
            amount=Decimal("25"),
            chain="ethereum",
            contract=PT,
        ),
        ChainBalance(
            source="zerion",
            asset="ETH",
            amount=Decimal("0.1"),
            chain="   ",
            contract="",
        ),
        NormalizedBalance(source="binance", asset="BTC", amount=Decimal("0.01")),
    ]
    capture = price_holdings(balances, _prices())
    payload = json.loads(capture.breakdown_json())
    by_asset = {row["asset"]: row for row in payload["holdings"]}

    chain_row = by_asset["USDC"]
    assert chain_row["chain"] == "ethereum"
    assert chain_row["contract"] == PT

    # Blank chain/contract are omitted, not written as empty strings.
    eth = by_asset["ETH"]
    assert "chain" not in eth
    assert "contract" not in eth

    exchange = by_asset["BTC"]
    assert exchange["source"] == "binance"
    assert "contract" not in exchange
    assert "chain" not in exchange
    # No provider logo: omit the key rather than writing null.
    assert "icon_url" not in chain_row
    assert "icon_url" not in eth
    assert "icon_url" not in exchange


def test_overview_holdings_exposes_chain_contract(client, db, demo_user: User):
    """GET /api/overview passes chain/contract through and omits them when absent."""
    moment = datetime(2026, 10, 24, 8, 0, tzinfo=timezone.utc)
    payload = {
        "holdings": [
            {
                "source": "zerion",
                "asset": "USDC",
                "amount": "25",
                "price_usdt": "1",
                "value_usdt": "25",
                "priced": True,
                "live": True,
                "chain": "ethereum",
                "contract": PT,
            },
            {
                "source": "binance",
                "asset": "BTC",
                "amount": "0.01",
                "price_usdt": "65000",
                "value_usdt": "650",
                "priced": True,
                "live": True,
            },
        ],
        "nav_usdt": "675",
    }
    db.add(
        Snapshot(
            user_id=demo_user.id,
            ts=moment,
            nav_usdt=Decimal("675.00"),
            source="sync",
            breakdown=json.dumps(payload),
        )
    )
    db.commit()

    body = client.get("/api/overview", headers=auth_header(client)).json()
    by_asset = {row["asset"]: row for row in body["holdings"]}
    assert by_asset["USDC"]["chain"] == "ethereum"
    assert by_asset["USDC"]["contract"] == PT
    assert by_asset["BTC"].get("contract") in (None, "")
    assert "contract" not in by_asset["BTC"] or by_asset["BTC"]["contract"] is None
    assert by_asset["BTC"].get("chain") in (None, "")

    rows, _asof = latest_sync_holdings(db, demo_user.id)
    via = {row["asset"]: row for row in rows}
    assert via["USDC"]["chain"] == "ethereum"
    assert via["USDC"]["contract"] == PT
    assert "contract" not in via["BTC"]
    assert "chain" not in via["BTC"]


ICON = "https://cdn.zerion.io/usdc.png"


def test_price_holdings_persists_icon_url_in_breakdown():
    """Zerion logo rides along; missing or non-http icons are omitted."""
    balances = [
        ChainBalance(
            source="zerion",
            asset="USDC",
            amount=Decimal("25"),
            chain="ethereum",
            contract=PT,
            icon_url=ICON,
        ),
        ChainBalance(
            source="zerion",
            asset="ETH",
            amount=Decimal("0.1"),
            chain="ethereum",
            icon_url="   ",
        ),
        ChainBalance(
            source="zerion",
            asset="DUST",
            amount=Decimal("1"),
            icon_url="not-a-url",
        ),
    ]
    capture = price_holdings(balances, _prices())
    payload = json.loads(capture.breakdown_json())
    by_asset = {row["asset"]: row for row in payload["holdings"]}
    assert by_asset["USDC"]["icon_url"] == ICON
    assert "icon_url" not in by_asset["ETH"]
    assert "icon_url" not in by_asset["DUST"]


def test_zerion_positions_icon_url_object_and_string():
    payload = {
        "data": [
            {
                "id": "0x00000000000000000000000000000000000000aa-ethereum-asset-asset",
                "attributes": {
                    "quantity": {"numeric": "10"},
                    "price": 1,
                    "fungible_info": {
                        "symbol": "USDC",
                        "icon": {"url": ICON},
                        "implementations": [
                            {"chain_id": "ethereum", "address": PT},
                        ],
                    },
                },
                "relationships": {"chain": {"data": {"id": "ethereum"}}},
            },
            {
                "id": "base-ethereum-asset-asset",
                "attributes": {
                    "quantity": {"numeric": "0.5"},
                    "fungible_info": {"symbol": "ETH", "icon": "https://cdn.example/eth.png"},
                },
            },
            {
                "id": "base-bsc-asset-asset",
                "attributes": {
                    "quantity": {"numeric": "1"},
                    "fungible_info": {"symbol": "BNB", "icon": {"url": "ipfs://nope"}},
                },
            },
        ]
    }
    rows = {row.asset: row for row in parse_zerion_positions("0xabc", payload)}
    assert rows["USDC"].icon_url == ICON
    assert rows["ETH"].icon_url == "https://cdn.example/eth.png"
    assert rows["BNB"].icon_url is None


def test_overview_holdings_exposes_icon_url(client, db, demo_user: User):
    moment = datetime(2026, 10, 25, 8, 0, tzinfo=timezone.utc)
    payload = {
        "holdings": [
            {
                "source": "zerion",
                "asset": "USDC",
                "amount": "25",
                "price_usdt": "1",
                "value_usdt": "25",
                "priced": True,
                "live": True,
                "chain": "ethereum",
                "contract": PT,
                "icon_url": ICON,
            },
            {
                "source": "binance",
                "asset": "BTC",
                "amount": "0.01",
                "price_usdt": "65000",
                "value_usdt": "650",
                "priced": True,
                "live": True,
            },
        ],
        "nav_usdt": "675",
    }
    db.add(
        Snapshot(
            user_id=demo_user.id,
            ts=moment,
            nav_usdt=Decimal("675.00"),
            source="sync",
            breakdown=json.dumps(payload),
        )
    )
    db.commit()

    body = client.get("/api/overview", headers=auth_header(client)).json()
    by_asset = {row["asset"]: row for row in body["holdings"]}
    assert by_asset["USDC"]["icon_url"] == ICON
    assert by_asset["BTC"].get("icon_url") in (None, "")

    rows, _asof = latest_sync_holdings(db, demo_user.id)
    via = {row["asset"]: row for row in rows}
    assert via["USDC"]["icon_url"] == ICON
    assert "icon_url" not in via["BTC"]


def _holding(
    asset: str,
    value: str | None,
    *,
    source: str = "binance",
    priced: bool = True,
    amount: str = "1",
) -> dict:
    row = {
        "source": source,
        "asset": asset,
        "amount": amount,
        "value_usdt": "0" if value is None else value,
        "priced": priced,
        "live": True,
    }
    if priced and value is not None:
        row["price_usdt"] = value
    return row


def test_overview_misc_threshold_edges_and_nav_unchanged(client, db, demo_user: User):
    """49.99 folds in; exactly 50 stays out. NAV is the snapshot, not the fold."""
    holdings = [
        _holding("BIG", "50", source="binance"),
        _holding("EDGE", "49.99", source="binance"),
        _holding("TINY", "1.5", source="gate"),
    ]
    moment = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)
    db.add(
        Snapshot(
            user_id=demo_user.id,
            ts=moment,
            nav_usdt=Decimal("101.49"),
            source="sync",
            breakdown=json.dumps({"holdings": holdings, "nav_usdt": "101.49"}),
        )
    )
    db.commit()

    body = client.get("/api/overview", headers=auth_header(client)).json()
    assert body["current_nav_usdt"] == 101.49
    assert body["misc_threshold_usdt"] == 50.0
    assert body["misc"]["count"] == 2
    assert body["misc"]["total_usdt"] == pytest.approx(51.49)
    assets = {row["asset"] for row in body["holdings"]}
    assert assets == {"BIG", "EDGE", "TINY"}


def test_overview_misc_merges_across_sources_before_threshold(client, db, demo_user: User):
    """Same symbol, case-insensitive, summed across sources. 30+25 stays out."""
    holdings = [
        _holding("usdc", "30", source="binance"),
        _holding("USDC", "25", source="zerion"),
        _holding("dust", "20", source="binance"),
        _holding("DUST", "20", source="gate"),
    ]
    moment = datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)
    db.add(
        Snapshot(
            user_id=demo_user.id,
            ts=moment,
            nav_usdt=Decimal("95.00"),
            source="sync",
            breakdown=json.dumps({"holdings": holdings, "nav_usdt": "95"}),
        )
    )
    db.commit()

    body = client.get("/api/overview", headers=auth_header(client)).json()
    assert body["current_nav_usdt"] == 95.0
    assert body["misc"]["count"] == 1
    assert body["misc"]["total_usdt"] == pytest.approx(40.0)
    assert len(body["holdings"]) == 4


def test_overview_misc_unpriced_counts_without_adding_value(client, db, demo_user: User):
    """Unpriced assets join 杂项 and contribute 0 to the priced total."""
    holdings = [
        _holding("PENDLE", "80", source="zerion"),
        _holding("JUNK", None, source="zerion", priced=False),
        _holding("speck", "0.4", source="helius"),
    ]
    moment = datetime(2026, 11, 2, 11, 0, tzinfo=timezone.utc)
    db.add(
        Snapshot(
            user_id=demo_user.id,
            ts=moment,
            nav_usdt=Decimal("80.40"),
            source="sync",
            breakdown=json.dumps({"holdings": holdings, "nav_usdt": "80.40"}),
        )
    )
    db.commit()

    body = client.get("/api/overview", headers=auth_header(client)).json()
    assert body["current_nav_usdt"] == 80.4
    assert body["misc"]["count"] == 2
    assert body["misc"]["total_usdt"] == pytest.approx(0.4)
    junk = next(row for row in body["holdings"] if row["asset"] == "JUNK")
    assert junk["priced"] is False


def test_overview_misc_empty_when_no_holdings(client, db, demo_user: User):
    from tests.conftest import seed_nav

    seed_nav(db, demo_user.id)
    body = client.get("/api/overview", headers=auth_header(client)).json()
    assert body["current_nav_usdt"] == 11200.0
    assert body["misc"] == {"count": 0, "total_usdt": 0.0}
    assert body["misc_threshold_usdt"] == 50.0
