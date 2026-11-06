"""P8d: sync provenance, orphaned wallets, and honest Jupiter Earn/Borrow pnl."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy import create_engine

from app.auth import hash_password
from app.integrations import lending_jupiter
from app.migrate import _migrate_positions_sqlite
from app.models import Position, PositionSnapshot, SyncRun, User, UserWallet
from app.sync.positions_hook import sync_positions_for_user
from app.sync.scheduler import record_run
from app.sync.service import run_sync

FIXTURES = Path(__file__).parent / "fixtures" / "lending_solana"
SOLANA_ADDR = "So1anaWa11et11111111111111111111111111111111"
EVM_ADDR = "0xabc0000000000000000000000000000000000abc"


def _user(db, username: str) -> User:
    user = User(username=username, hashed_password=hash_password("pass-word"))
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _wallet(db, user: User, chain: str, address: str) -> UserWallet:
    row = UserWallet(user_id=user.id, chain=chain, address=address)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _position(
    db,
    user: User,
    *,
    chain: str,
    protocol: str,
    account_ref: str,
    market_ref: str,
) -> Position:
    row = Position(
        user_id=user.id,
        kind="lending",
        protocol=protocol,
        chain=chain,
        account_ref=account_ref,
        market_ref=market_ref,
        status="open",
        net_value_usdt=Decimal("100"),
        collateral_usdt=Decimal("100"),
        debt_usdt=Decimal("0"),
        details={"health_factor": 2.0},
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _fail_all_adapters(monkeypatch) -> None:
    def _boom(*_args, **_kwargs):
        raise RuntimeError("adapter offline in test")

    for name in ("fetch_aave", "fetch_morpho", "fetch_pendle", "fetch_kamino", "fetch_jupiter"):
        monkeypatch.setattr(f"app.sync.positions_hook.{name}", _boom)


def test_record_run_stamps_positions_and_snapshots_with_sync_id(db):
    user = _user(db, "p8d-sync-id")

    record_run(
        db,
        trigger="manual",
        action=lambda: run_sync(user.id, "mock", db=db),
    )

    runs = db.scalars(select(SyncRun)).all()
    assert len(runs) == 1
    run = runs[0]
    assert run.status == "ok"

    positions = db.scalars(select(Position).where(Position.user_id == user.id)).all()
    assert positions
    assert {position.sync_id for position in positions} == {run.id}

    snapshots = db.scalars(
        select(PositionSnapshot).where(
            PositionSnapshot.position_id.in_([position.id for position in positions])
        )
    ).all()
    assert snapshots
    assert {snapshot.sync_id for snapshot in snapshots} == {run.id}


def test_unbound_solana_wallets_close_rows_as_orphaned(db, monkeypatch):
    user = _user(db, "p8d-orphan")
    _wallet(db, user, "evm", EVM_ADDR)
    solana_wallet = _wallet(db, user, "solana", SOLANA_ADDR)
    run = SyncRun(started_at=datetime(2026, 11, 2, tzinfo=timezone.utc), status="running")
    db.add(run)
    db.commit()
    db.refresh(run)
    solana_row = _position(
        db,
        user,
        chain="solana",
        protocol="jupiter",
        account_ref=SOLANA_ADDR,
        market_ref="jupiter-lend-earn",
    )
    evm_row = _position(
        db,
        user,
        chain="ethereum",
        protocol="aave",
        account_ref=EVM_ADDR,
        market_ref="aave-weth",
    )
    _fail_all_adapters(monkeypatch)

    db.delete(solana_wallet)
    db.commit()

    sync_positions_for_user(db, user.id, run.id)

    db.refresh(solana_row)
    assert solana_row.status == "closed"
    assert solana_row.close_reason == "orphaned"
    assert solana_row.details["close_reason"] == "orphaned"
    assert solana_row.details["closed_at"]
    assert solana_row.sync_id == run.id

    # The EVM chain still has a wallet: its open row is not orphaned.
    db.refresh(evm_row)
    assert evm_row.status == "open"
    assert evm_row.close_reason is None


def test_chain_with_another_wallet_is_not_orphaned(db, monkeypatch):
    user = _user(db, "p8d-other-wallet")
    _wallet(db, user, "evm", EVM_ADDR)
    first = _wallet(db, user, "solana", SOLANA_ADDR)
    _wallet(db, user, "solana", "So1anaWa11et22222222222222222222222222222222")
    run = SyncRun(started_at=datetime(2026, 11, 2, tzinfo=timezone.utc), status="running")
    db.add(run)
    db.commit()
    db.refresh(run)
    row = _position(
        db,
        user,
        chain="solana",
        protocol="jupiter",
        account_ref=SOLANA_ADDR,
        market_ref="jupiter-lend-earn",
    )
    _fail_all_adapters(monkeypatch)

    db.delete(first)
    db.commit()

    sync_positions_for_user(db, user.id, run.id)

    db.refresh(row)
    assert row.status == "open"
    assert row.close_reason is None


def _load_fixture() -> dict:
    return json.loads((FIXTURES / "jupiter_user.json").read_text(encoding="utf-8"))


def test_jupiter_earn_borrow_missing_pnl_is_flagged_not_invented():
    rows = lending_jupiter.normalize(SOLANA_ADDR, _load_fixture())
    by_market = {row["market_ref"]: row for row in rows}

    earn = by_market["jupiter-lend-earn"]["details"]
    assert earn["pnl_usd"] is None
    assert earn["interest_paid_usd"] is None
    assert earn["interest_earned_usd"] is None
    assert earn["pnl_data_missing"] is True

    borrow = by_market["jupiter-lend-borrow-3518"]["details"]
    assert borrow["pnl_usd"] is None
    assert borrow["interest_paid_usd"] is None
    assert borrow["interest_earned_usd"] is None
    assert borrow["pnl_data_missing"] is True


def test_jupiter_earn_borrow_reads_pnl_when_api_provides_it():
    payload = _load_fixture()
    payload["earn"][0]["pnlUsd"] = "12.5"
    payload["earn"][0]["interestEarnedUsd"] = "1.25"
    payload["borrow"][0]["config"] = {
        "pnlUsd": "-3.0",
        "interestPaidUsd": "0.75",
        "interestEarnedUsd": "0.0",
    }

    rows = lending_jupiter.normalize(SOLANA_ADDR, payload)
    by_market = {row["market_ref"]: row for row in rows}

    earn = by_market["jupiter-lend-earn"]["details"]
    assert earn["pnl_usd"] == pytest.approx(12.5)
    assert earn["interest_earned_usd"] == pytest.approx(1.25)
    assert earn["interest_paid_usd"] is None
    assert "pnl_data_missing" not in earn

    borrow = by_market["jupiter-lend-borrow-3518"]["details"]
    assert borrow["pnl_usd"] == pytest.approx(-3.0)
    assert borrow["interest_paid_usd"] == pytest.approx(0.75)
    assert borrow["interest_earned_usd"] == pytest.approx(0.0)
    assert "pnl_data_missing" not in borrow


def test_old_sqlite_tables_gain_columns_and_keep_null(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}", future=True)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY)"))
        conn.execute(text("CREATE TABLE sync_runs (id INTEGER PRIMARY KEY)"))
        conn.execute(
            text(
                "CREATE TABLE positions ("
                "id INTEGER PRIMARY KEY, "
                "user_id INTEGER NOT NULL, "
                "kind VARCHAR(16) NOT NULL, "
                "protocol VARCHAR(16) NOT NULL, "
                "chain VARCHAR(32), "
                "account_ref VARCHAR(128) NOT NULL, "
                "market_ref VARCHAR(255) NOT NULL, "
                "label VARCHAR(160), "
                "status VARCHAR(16) NOT NULL DEFAULT 'open', "
                "net_value_usdt NUMERIC(38, 8) NOT NULL DEFAULT 0, "
                "collateral_usdt NUMERIC(38, 8) NOT NULL DEFAULT 0, "
                "debt_usdt NUMERIC(38, 8) NOT NULL DEFAULT 0, "
                "details JSON, "
                "opened_at TIMESTAMP, "
                "asof TIMESTAMP"
                ")"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE position_snapshots ("
                "id INTEGER PRIMARY KEY, "
                "position_id INTEGER NOT NULL, "
                "ts TIMESTAMP NOT NULL, "
                "net_value_usdt NUMERIC(38, 8) NOT NULL, "
                "collateral_usdt NUMERIC(38, 8) NOT NULL DEFAULT 0, "
                "debt_usdt NUMERIC(38, 8) NOT NULL DEFAULT 0, "
                "health_factor NUMERIC(38, 8), "
                "apy_net NUMERIC(38, 8), "
                "details JSON"
                ")"
            )
        )
        conn.execute(
            text(
                "INSERT INTO positions "
                "(id, user_id, kind, protocol, chain, account_ref, market_ref, status) "
                "VALUES (1, 1, 'lending', 'aave', 'ethereum', '0xabc', 'old', 'open')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO position_snapshots "
                "(id, position_id, ts, net_value_usdt) VALUES (1, 1, '2026-01-01', 100)"
            )
        )

    with engine.begin() as conn:
        _migrate_positions_sqlite(conn, inspect(engine))

    columns = {column["name"] for column in inspect(engine).get_columns("positions")}
    assert {"sync_id", "close_reason"} <= columns
    snapshot_columns = {
        column["name"] for column in inspect(engine).get_columns("position_snapshots")
    }
    assert "sync_id" in snapshot_columns

    with engine.connect() as conn:
        position = conn.execute(
            text("SELECT sync_id, close_reason FROM positions WHERE id = 1")
        ).one()
        snapshot_sync_id = conn.execute(
            text("SELECT sync_id FROM position_snapshots WHERE id = 1")
        ).scalar_one()
    assert position == (None, None)
    assert snapshot_sync_id is None
