"""NAV snapshots: pricing, source selection, and idempotent backfill."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select

from app.integrations.prices.service import PriceService
from app.integrations.types import ChainBalance, NormalizedBalance
from app.models import Snapshot, User
from app.schemas import BackfillOut
from app.snapshots import price_holdings, select_nav_snapshots, upsert_snapshot, write_sync_snapshot
from app.snapshots_backfill import backfill_user, replay_quantities
from tests.conftest import auth_header


def _prices() -> PriceService:
    return PriceService(use_mock=True)


def test_price_holdings_multiplies_balance_by_usdt_quote():
    balances = [
        NormalizedBalance(source="binance", asset="BTC", amount=Decimal("0.015")),
        NormalizedBalance(source="binance", asset="USDT", amount=Decimal("1200.5")),
        ChainBalance(source="zerion", asset="ETH", amount=Decimal("0.25")),
        ChainBalance(source="helius", asset="SOL", amount=Decimal("3.5")),
        ChainBalance(source="zerion", asset="DUST", amount=Decimal("10")),
    ]
    capture = price_holdings(balances, _prices())
    # 0.015 * 65000 = 975; 1200.50; 0.25 * 3200 = 800; 3.5 * 150 = 525; DUST unpriced.
    assert capture.nav_usdt == Decimal("3500.50")
    assert capture.unpriced == ["zerion:DUST"]
    priced = {item.asset: item.value_usdt for item in capture.holdings if item.price_usdt is not None}
    assert priced == {
        "BTC": Decimal("975.00"),
        "USDT": Decimal("1200.50"),
        "ETH": Decimal("800.00"),
        "SOL": Decimal("525.00"),
    }


def test_sync_snapshot_uses_fixture_balances(db, demo_user: User):
    moment = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
    row = write_sync_snapshot(db, demo_user.id, mode="mock", ts=moment, prices=_prices())
    db.commit()
    # Exchanges: binance 975 + 1200.50, bitget 80, bybit 50, kraken 0.4*3200=1280, gate 10.
    # Chains: zerion 0.25*3200 + 400 = 1200, helius 3.5*150 + 25 = 550.
    assert row.nav_usdt == Decimal("5345.50")
    assert row.source == "sync"
    assert "binance" in (row.breakdown or "")
    # No open positions: the raw holding sum is the NAV, and nothing is folded.
    assert "positions_included" not in (row.breakdown or "")

    again, created = upsert_snapshot(db, demo_user.id, moment, price_holdings([], _prices()), source="sync")
    db.commit()
    assert created is False
    assert again.id == row.id
    assert db.scalar(select(func.count()).select_from(Snapshot)) == 1


def test_sync_snapshot_folds_covered_holdings_and_position_net(db, demo_user: User, monkeypatch):
    """NAV = uncovered holdings + open position net. Covered rows stay, flagged.

    The debt receipt is removed from the sum (not added as an asset) and the
    position net already has the debt deducted, so it is counted once.
    """
    from app.integrations.types import ChainBalance
    from app.positions_core import upsert_positions
    from app.snapshots import collect_balances as real_collect

    def _with_receipts(**kwargs):
        balances, skipped, error = real_collect(**kwargs)
        balances = list(balances) + [
            ChainBalance(source="zerion", asset="aEthWETH", amount=Decimal("2.5"), chain="ethereum"),
            ChainBalance(source="zerion", asset="aEthUSDC", amount=Decimal("3000"), chain="ethereum"),
            ChainBalance(
                source="zerion", asset="variableDebtEthUSDT", amount=Decimal("6000"), chain="ethereum"
            ),
            ChainBalance(source="zerion", asset="PT-sUSDx-3DEC2026", amount=Decimal("1"), chain="ethereum"),
        ]
        return balances, skipped, error

    monkeypatch.setattr("app.snapshots.collect_balances", _with_receipts)
    original_quote = PriceService.quote_usdt

    def _quote(self, symbol):
        pinned = {
            "aEthWETH": Decimal("3200"),
            "aEthUSDC": Decimal("1"),
            "variableDebtEthUSDT": Decimal("1"),
            "PT-sUSDx-3DEC2026": Decimal("1"),
        }
        if symbol in pinned:
            return pinned[symbol]
        return original_quote(self, symbol)

    monkeypatch.setattr(PriceService, "quote_usdt", _quote)

    moment = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
    write_sync_snapshot(db, demo_user.id, mode="mock", ts=moment, prices=_prices())
    db.commit()
    bare = db.scalar(select(Snapshot).where(Snapshot.user_id == demo_user.id))
    # 5345.50 plus the three receipts at $1 (aToken price falls back to the
    # symbol quote, which the mock prices as 1 for an unknown symbol? — no).
    # Receipts price on top of the 5345.50 fixture: 2.5*3200 + 3000*1 + 6000*1
    # + the PT spot row at 1. Nothing is folded yet (no open position).
    assert bare.nav_usdt == Decimal("5345.50") + Decimal("8000") + Decimal("3000") + Decimal("6000") + Decimal("1")

    upsert_positions(
        db,
        demo_user.id,
        [
            {
                "kind": "lending",
                "protocol": "aave",
                "chain": "ethereum",
                "account_ref": "0xfold",
                "market_ref": "aave-v3-fold",
                "collateral_usdt": "11000",
                "debt_usdt": "6000",
                "net_value_usdt": "5000",
                "status": "open",
                "details": {
                    "collateral": [
                        {"symbol": "aEthWETH", "amount": "2.5", "usd": "8000.00"},
                        {"symbol": "aEthUSDC", "amount": "3000", "usd": "3000.00"},
                    ],
                    "borrow": [
                        {"symbol": "variableDebtEthUSDT", "amount": "6000", "usd": "6000.00"},
                    ],
                },
            }
        ],
    )
    folded_moment = moment + timedelta(minutes=5)
    folded = write_sync_snapshot(db, demo_user.id, mode="mock", ts=folded_moment, prices=_prices())
    db.commit()

    payload = json.loads(folded.breakdown)
    assert payload["positions_included"] is True
    flags = {item["asset"]: item.get("covered_by_position", False) for item in payload["holdings"]}
    assert flags["aEthWETH"] is True
    assert flags["aEthUSDC"] is True
    assert flags["variableDebtEthUSDT"] is True
    # Pendle spot stays in the sum. A plain wallet asset stays too.
    assert flags["PT-sUSDx-3DEC2026"] is False
    assert flags["ETH"] is False
    # Drops the covered 8000 + 3000 collateral and the 6000 debt receipt, then
    # adds the position net of 5000. The debt is deducted once, inside the net.
    assert folded.nav_usdt == bare.nav_usdt - Decimal("8000") - Decimal("3000") - Decimal("6000") + Decimal("5000")
    assert len(payload["holdings"]) == len(
        json.loads(bare.breakdown)["holdings"]
    )


def test_overview_and_returns_prefer_non_seed(client, db, demo_user: User):
    from tests.conftest import seed_nav

    seed_nav(db, demo_user.id)
    moment = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
    write_sync_snapshot(db, demo_user.id, mode="mock", ts=moment, prices=_prices())
    db.commit()

    headers = auth_header(client)
    overview = client.get("/api/overview", headers=headers)
    assert overview.status_code == 200, overview.text
    body = overview.json()
    assert body["nav_source"] == "demo"
    assert body["provenance"] == "demo"
    assert body["live"] is False
    assert body["latest_snapshot_ts"].startswith("2026-10-21")
    assert len(body["series"]) == 1
    assert body["current_nav_usdt"] == 5345.5

    returns = client.get("/api/returns?period=day", headers=headers)
    assert returns.status_code == 200, returns.text
    assert returns.json()["nav_source"] == "demo"
    assert returns.json()["provenance"] == "demo"
    assert returns.json()["live"] is False
    assert returns.json()["latest_snapshot_ts"].startswith("2026-10-21")


def test_overview_falls_back_to_seed_when_no_real_snapshot(client, db, demo_user: User):
    from tests.conftest import seed_nav

    seed_nav(db, demo_user.id)
    chosen, source = select_nav_snapshots(db, demo_user.id)
    assert source == "seed"
    assert len(chosen) == 2

    body = client.get("/api/overview", headers=auth_header(client)).json()
    assert body["nav_source"] == "seed"
    assert body["current_nav_usdt"] == 11200.0
    assert body["latest_snapshot_ts"].startswith("2026-10-02")


def test_backfill_is_idempotent_and_covers_the_window(db, demo_user: User):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 1, 3, 15, 0, tzinfo=timezone.utc)
    first = backfill_user(db, demo_user.id, mode="mock", since=start, until=end, prices=_prices())
    assert first.created == 3
    assert first.updated == 0

    rows = list(db.scalars(select(Snapshot).where(Snapshot.user_id == demo_user.id).order_by(Snapshot.ts)).all())
    assert [row.source for row in rows] == ["backfill", "backfill", "backfill"]
    # SQLite returns naive datetimes; compare the clock time, not the tzinfo.
    assert rows[0].ts.replace(tzinfo=None) == datetime(2026, 1, 1, 23, 59, 59)
    assert rows[-1].ts.replace(tzinfo=None) == end.replace(tzinfo=None)

    second = backfill_user(db, demo_user.id, mode="mock", since=start, until=end, prices=_prices())
    assert second.created == 0
    assert second.updated == 3
    assert db.scalar(select(func.count()).select_from(Snapshot).where(Snapshot.user_id == demo_user.id)) == 3


def test_replay_undoes_movements_after_the_cutoff():
    current = {("binance", "USDT"): Decimal("100"), ("binance", "BTC"): Decimal("1")}
    movements = [
        (datetime(2026, 1, 2, tzinfo=timezone.utc), "binance", "USDT", Decimal("40")),
        (datetime(2026, 1, 3, tzinfo=timezone.utc), "binance", "BTC", Decimal("0.5")),
    ]
    before = replay_quantities(current, movements, as_of=datetime(2026, 1, 1, 12, tzinfo=timezone.utc))
    # Both movements land after the cutoff, so both are undone.
    assert before[("binance", "USDT", "", "")] == Decimal("60")
    assert before[("binance", "BTC", "", "")] == Decimal("0.5")
    at_deposit = replay_quantities(current, movements, as_of=datetime(2026, 1, 2, tzinfo=timezone.utc))
    assert at_deposit[("binance", "USDT", "", "")] == Decimal("100")


def test_backfill_endpoint_reports_created_and_range(client, db, demo_user: User):
    response = client.post("/api/sync/backfill?mode=mock", headers=auth_header(client))
    assert response.status_code == 200, response.text
    body = response.json()
    parsed = BackfillOut.model_validate(body)
    assert "from" in body and "from_" not in body
    assert parsed.created > 0
    assert parsed.updated == 0
    assert body["from"].startswith("2026-01-01")

    again = client.post("/api/sync/backfill?mode=mock", headers=auth_header(client))
    assert again.json()["created"] == 0
    assert again.json()["updated"] == parsed.created


def test_backfill_then_demo_sync_reports_demo_provenance(client, db, demo_user: User):
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    backfill_user(
        db,
        demo_user.id,
        mode="mock",
        since=start,
        until=start + timedelta(days=1),
        prices=_prices(),
    )
    write_sync_snapshot(
        db,
        demo_user.id,
        mode="mock",
        ts=datetime(2026, 10, 3, 8, 0, tzinfo=timezone.utc),
        prices=_prices(),
    )
    db.commit()
    body = client.get("/api/overview", headers=auth_header(client)).json()
    assert body["nav_source"] == "demo"
    assert body["provenance"] == "demo"
    assert body["live"] is False
    assert len(body["series"]) == 3


def test_overview_prefers_sync_when_later_backfill_exists(client, db, demo_user: User):
    """A same-day (or later) backfill must not cover the latest live sync NAV.

    Rule: if any sync exists, every sync stays and a backfill stays only when
    it is strictly before the earliest sync. Without sync, backfill still wins.
    """
    from app.models import Snapshot

    sync_ts = datetime(2026, 10, 23, 15, 7, 24, tzinfo=timezone.utc)
    # Later than the sync (UTC end-of-day), mirrors production id=590 vs id=615.
    backfill_ts = datetime(2026, 10, 23, 23, 59, 59, tzinfo=timezone.utc)
    older_backfill = datetime(2026, 10, 22, 23, 59, 59, tzinfo=timezone.utc)
    db.add_all(
        [
            Snapshot(
                user_id=demo_user.id,
                ts=older_backfill,
                nav_usdt=Decimal("10000.00"),
                source="backfill",
            ),
            Snapshot(
                user_id=demo_user.id,
                ts=sync_ts,
                nav_usdt=Decimal("16240.80"),
                source="sync",
            ),
            Snapshot(
                user_id=demo_user.id,
                ts=backfill_ts,
                nav_usdt=Decimal("10381.64"),
                source="backfill",
            ),
        ]
    )
    db.commit()

    chosen, source = select_nav_snapshots(db, demo_user.id)
    assert source == "sync"
    assert [row.source for row in chosen] == ["backfill", "sync"]
    assert float(chosen[-1].nav_usdt) == 16240.80

    body = client.get("/api/overview", headers=auth_header(client)).json()
    assert body["nav_source"] == "sync"
    assert body["current_nav_usdt"] == 16240.8
    assert body["latest_snapshot_ts"].startswith("2026-10-23T15:07:24")
    assert len(body["series"]) == 2
    assert body["series"][-1]["nav_usdt"] == 16240.8

    returns = client.get("/api/returns?period=day", headers=auth_header(client)).json()
    assert returns["nav_source"] == "sync"
    assert returns["latest_snapshot_ts"].startswith("2026-10-23T15:07:24")


def test_backfill_between_syncs_is_dropped_pre_sync_kept(db, demo_user: User):
    """A backfill sitting between two syncs is a stale valuation and is dropped.

    Production shape: sync 16121, backfill #590 (2026-10-23 23:59:59, nav
    10381.64), sync 16498. That middle row punched a false dip. A backfill
    strictly before the first sync stays; with no sync, every backfill stays.
    nav_source is unchanged by the filter.
    """
    from app.models import Snapshot

    early_backfill = datetime(2026, 10, 20, 23, 59, 59, tzinfo=timezone.utc)
    first_sync = datetime(2026, 10, 23, 8, 0, 0, tzinfo=timezone.utc)
    sandwiched = datetime(2026, 10, 23, 23, 59, 59, tzinfo=timezone.utc)
    later_sync = datetime(2026, 10, 24, 8, 0, 0, tzinfo=timezone.utc)
    db.add_all(
        [
            Snapshot(
                user_id=demo_user.id,
                ts=early_backfill,
                nav_usdt=Decimal("9000.00"),
                source="backfill",
            ),
            Snapshot(
                user_id=demo_user.id,
                ts=first_sync,
                nav_usdt=Decimal("16121.00"),
                source="sync",
            ),
            Snapshot(
                user_id=demo_user.id,
                ts=sandwiched,
                nav_usdt=Decimal("10381.64"),
                source="backfill",
            ),
            Snapshot(
                user_id=demo_user.id,
                ts=later_sync,
                nav_usdt=Decimal("16498.00"),
                source="sync",
            ),
            Snapshot(
                user_id=demo_user.id,
                ts=datetime(2026, 10, 1, tzinfo=timezone.utc),
                nav_usdt=Decimal("1"),
                source="seed",
            ),
        ]
    )
    db.commit()

    chosen, source = select_nav_snapshots(db, demo_user.id)
    assert source == "sync"
    assert [(row.source, float(row.nav_usdt)) for row in chosen] == [
        ("backfill", 9000.0),
        ("sync", 16121.0),
        ("sync", 16498.0),
    ]
    assert all(float(row.nav_usdt) != 10381.64 for row in chosen)


def test_no_sync_keeps_every_backfill(db, demo_user: User):
    from app.models import Snapshot

    db.add_all(
        [
            Snapshot(
                user_id=demo_user.id,
                ts=datetime(2026, 10, 22, 23, 59, 59, tzinfo=timezone.utc),
                nav_usdt=Decimal("10000.00"),
                source="backfill",
            ),
            Snapshot(
                user_id=demo_user.id,
                ts=datetime(2026, 10, 23, 23, 59, 59, tzinfo=timezone.utc),
                nav_usdt=Decimal("10381.64"),
                source="backfill",
            ),
        ]
    )
    db.commit()

    chosen, source = select_nav_snapshots(db, demo_user.id)
    assert source == "backfill"
    assert [float(row.nav_usdt) for row in chosen] == [10000.0, 10381.64]


def test_overview_falls_back_to_backfill_when_no_sync(client, db, demo_user: User):
    from app.models import Snapshot

    db.add(
        Snapshot(
            user_id=demo_user.id,
            ts=datetime(2026, 10, 23, 23, 59, 59, tzinfo=timezone.utc),
            nav_usdt=Decimal("10381.64"),
            source="backfill",
        )
    )
    db.commit()

    chosen, source = select_nav_snapshots(db, demo_user.id)
    assert source == "backfill"
    assert len(chosen) == 1
    assert float(chosen[-1].nav_usdt) == 10381.64

    body = client.get("/api/overview", headers=auth_header(client)).json()
    assert body["nav_source"] == "backfill"
    assert body["current_nav_usdt"] == 10381.64
