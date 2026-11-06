"""P13: demo/fixture/live provenance is explicit, independent of ``source``.

Mock and fixture writes must never be reported as live. Historical rows keep
NULL provenance until a later write sets it; nothing is backfilled.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import create_engine, inspect, select, text

from app.integrations.prices.service import PriceService
from app.migrate import apply_phase2_migrations
from app.models import Position, PositionSnapshot, User
from app.positions_core import load_mock_positions, upsert_positions
from app.provenance import classify_provenance
from app.schemas import UserOut
from app.snapshots import select_nav_snapshots, write_sync_snapshot
from app.sync.positions_hook import sync_positions_for_user
from tests.conftest import auth_header


def _prices() -> PriceService:
    return PriceService(use_mock=True)


def test_demo_mock_snapshot_is_demo_and_not_live(client, db, demo_user: User):
    moment = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
    row = write_sync_snapshot(db, demo_user.id, mode="mock", ts=moment, prices=_prices())
    db.commit()

    assert row.source == "sync"
    assert row.provenance == "demo"
    assert row.live is False
    payload = json.loads(row.breakdown)
    assert payload["holdings"]
    assert all(item["live"] is False for item in payload["holdings"])

    _, nav_source = select_nav_snapshots(db, demo_user.id)
    assert nav_source == "demo"

    headers = auth_header(client)
    overview = client.get("/api/overview", headers=headers).json()
    assert overview["nav_source"] == "demo"
    assert overview["provenance"] == "demo"
    assert overview["live"] is False
    assert overview["series"][-1]["provenance"] == "demo"
    assert overview["series"][-1]["live"] is False

    returns = client.get("/api/returns?period=day", headers=headers).json()
    assert returns["nav_source"] == "demo"
    assert returns["provenance"] == "demo"
    assert returns["live"] is False


def test_non_demo_mock_snapshot_is_fixture_and_not_live(db, demo_user: User):
    alice = User(username="alice", hashed_password="x", is_admin=False)
    db.add(alice)
    db.commit()
    db.refresh(alice)

    moment = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
    row = write_sync_snapshot(db, alice.id, mode="mock", ts=moment, prices=_prices())
    db.commit()

    assert row.source == "sync"
    assert row.provenance == "fixture"
    assert row.live is False
    payload = json.loads(row.breakdown)
    assert all(item["live"] is False for item in payload["holdings"])

    _, nav_source = select_nav_snapshots(db, alice.id)
    assert nav_source == "fixture"


def test_mock_positions_are_demo_and_not_live_in_api(client, db, demo_user: User):
    written = sync_positions_for_user(db, demo_user.id, None, use_mock=True)
    assert written > 0

    rows = db.scalars(select(Position).where(Position.user_id == demo_user.id)).all()
    assert rows
    assert all(item.provenance == "demo" and item.live is False for item in rows)
    snapshots = db.scalars(
        select(PositionSnapshot).join(Position).where(Position.user_id == demo_user.id)
    ).all()
    assert snapshots
    assert all(item.provenance == "demo" and item.live is False for item in snapshots)

    listed = client.get("/api/positions", headers=auth_header(client)).json()
    assert listed
    assert all(item["provenance"] == "demo" and item["live"] is False for item in listed)


def test_fixture_positions_for_non_demo_are_fixture_and_not_live(db):
    alice = User(username="alice", hashed_password="x", is_admin=False)
    db.add(alice)
    db.commit()
    db.refresh(alice)

    sync_positions_for_user(db, alice.id, None, use_mock=True)
    rows = db.scalars(select(Position).where(Position.user_id == alice.id)).all()
    assert rows
    assert all(item.provenance == "fixture" and item.live is False for item in rows)


def test_live_position_write_keeps_live_true(db):
    alice = User(username="alice", hashed_password="x", is_admin=False)
    db.add(alice)
    db.commit()
    db.refresh(alice)

    upsert_positions(
        db,
        alice.id,
        load_mock_positions(),
        provenance="live",
        live=True,
    )
    rows = db.scalars(select(Position).where(Position.user_id == alice.id)).all()
    assert rows
    assert all(item.provenance == "live" and item.live is True for item in rows)


def test_provenance_classification_keeps_demo_fixture_and_live_separate():
    demo = User(
        id=1,
        username="demo",
        hashed_password="x",
        is_admin=False,
        created_at=datetime.now(timezone.utc),
    )
    alice = User(
        id=2,
        username="alice",
        hashed_password="x",
        is_admin=False,
        created_at=datetime.now(timezone.utc),
    )
    assert UserOut.model_validate(demo).is_demo is True
    assert UserOut.model_validate(alice).is_demo is False
    assert classify_provenance(mode="mock", use_live=False, user=demo, has_fixture=True) == "demo"
    assert classify_provenance(mode="auto", use_live=True, user=demo, has_fixture=True) == "demo"
    assert classify_provenance(mode="live", use_live=True, user=demo, has_fixture=False) == "live"
    assert classify_provenance(mode="mock", use_live=False, user=alice, has_fixture=True) == "fixture"
    assert classify_provenance(mode="auto", use_live=True, user=alice, has_fixture=True) == "fixture"
    assert classify_provenance(mode="live", use_live=True, user=alice, has_fixture=False) == "live"


def test_sqlite_migration_adds_nullable_provenance_without_backfill(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'p13-old.db'}", future=True)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR(64))"))
        conn.execute(text("INSERT INTO users (id, username) VALUES (1, 'u1')"))
        conn.execute(
            text(
                "CREATE TABLE snapshots ("
                "id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, ts TIMESTAMP NOT NULL, "
                "nav_usdt NUMERIC(38, 8) NOT NULL, source VARCHAR(16) NOT NULL DEFAULT 'seed', "
                "breakdown TEXT)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO snapshots (id, user_id, ts, nav_usdt, source) "
                "VALUES (1, 1, '2026-10-01 00:00:00', 1000, 'sync')"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE positions ("
                "id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, kind VARCHAR(16) NOT NULL, "
                "protocol VARCHAR(16) NOT NULL, chain VARCHAR(32), "
                "account_ref VARCHAR(128) NOT NULL, market_ref VARCHAR(255) NOT NULL, "
                "label VARCHAR(160), status VARCHAR(16) NOT NULL DEFAULT 'open', "
                "net_value_usdt NUMERIC(38, 8) NOT NULL DEFAULT 0, "
                "collateral_usdt NUMERIC(38, 8) NOT NULL DEFAULT 0, "
                "debt_usdt NUMERIC(38, 8) NOT NULL DEFAULT 0, details JSON, "
                "opened_at TIMESTAMP, asof TIMESTAMP)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO positions "
                "(id, user_id, kind, protocol, account_ref, market_ref) "
                "VALUES (1, 1, 'lending', 'aave', '0x1', 'pool')"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE position_snapshots ("
                "id INTEGER PRIMARY KEY, position_id INTEGER NOT NULL, ts TIMESTAMP NOT NULL, "
                "net_value_usdt NUMERIC(38, 8) NOT NULL, "
                "collateral_usdt NUMERIC(38, 8) NOT NULL DEFAULT 0, "
                "debt_usdt NUMERIC(38, 8) NOT NULL DEFAULT 0)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO position_snapshots (id, position_id, ts, net_value_usdt) "
                "VALUES (1, 1, '2026-10-01 00:00:00', 500)"
            )
        )

    apply_phase2_migrations(engine)
    apply_phase2_migrations(engine)  # idempotent second run

    inspector = inspect(engine)
    assert {"provenance", "live"} <= {col["name"] for col in inspector.get_columns("snapshots")}
    assert {"provenance", "live"} <= {col["name"] for col in inspector.get_columns("positions")}
    assert {"provenance", "live"} <= {
        col["name"] for col in inspector.get_columns("position_snapshots")
    }

    with engine.connect() as conn:
        assert conn.execute(text("SELECT provenance FROM snapshots WHERE id = 1")).scalar_one() is None
        assert conn.execute(text("SELECT live FROM snapshots WHERE id = 1")).scalar_one() is None
        assert conn.execute(text("SELECT provenance FROM positions WHERE id = 1")).scalar_one() is None
        assert (
            conn.execute(text("SELECT live FROM position_snapshots WHERE id = 1")).scalar_one()
            is None
        )
    engine.dispose()
