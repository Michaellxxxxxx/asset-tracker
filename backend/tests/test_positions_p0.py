"""Phase-4 P0: positions tables, upsert, summaries, alert-rule skeleton, coverage hook."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import inspect, select

from app.auth import hash_password
from app.migrate import apply_phase2_migrations
from app.models import AlertEvent, AlertRule, Position, PositionSnapshot, SyncRun, User, UserWallet
from app.positions_core import (
    load_mock_positions,
    mark_covered_by_position,
    positions_net_value,
    upsert_positions,
)
from app.sync.positions_hook import sync_positions_for_user
from tests.conftest import auth_header

AAVE_WETH = "0x4d5F47FA6A74757f35C14fD3a6Ef8E3C9BC514E8"
AAVE_USDC = "0x98C23E9d8f34FEFb1B7BD6a91B7FF122F4e16F5c"
AAVE_DEBT = "0x6df1C1E379bC5a00a7b4C6e67A203333772f45A8"
MORPHO_COLLAT = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


def _user(db, username: str) -> User:
    user = User(username=username, hashed_password=hash_password("pass-word"))
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_models_create_tables_and_constraints(engine):
    names = set(inspect(engine).get_table_names())
    assert {"positions", "position_snapshots", "alert_rules", "alert_events"} <= names
    pos_indexes = {idx["name"] for idx in inspect(engine).get_indexes("positions")}
    snap_indexes = {idx["name"] for idx in inspect(engine).get_indexes("position_snapshots")}
    assert "ix_positions_user_kind" in pos_indexes
    assert "ix_position_snapshots_position_ts" in snap_indexes
    unique = {item["name"] for item in inspect(engine).get_unique_constraints("positions")}
    assert "uq_positions_user_protocol_account_market" in unique


def test_migration_positions_idempotent_sqlite(engine):
    apply_phase2_migrations(engine)
    apply_phase2_migrations(engine)
    assert inspect(engine).has_table("positions")
    assert inspect(engine).has_table("alert_events")


def test_upsert_idempotent_and_closes_missing(db):
    user = _user(db, "pos-user")
    rows = load_mock_positions()
    first = upsert_positions(db, user.id, rows, sync_id=None)
    assert len(first) == 5
    snaps = db.scalars(select(PositionSnapshot)).all()
    assert len(snaps) == 5

    again = upsert_positions(db, user.id, rows, sync_id=None)
    assert len(again) == 5
    ids = db.scalars(select(Position.id).where(Position.user_id == user.id)).all()
    assert len(ids) == 5
    assert len(db.scalars(select(PositionSnapshot)).all()) == 10

    # A second Aave market is synced, then dropped. Aave was in the batch, so the
    # missing market closes. Morpho / Kamino stay open because they were present.
    extra = {
        "kind": "lending",
        "protocol": "aave",
        "chain": "ethereum",
        "account_ref": "0x1111111111111111111111111111111111111111",
        "market_ref": "aave-v3-ethereum-extra",
        "status": "open",
        "net_value_usdt": "0",
    }
    upsert_positions(db, user.id, [*rows, extra], sync_id=None)
    trimmed = [row for row in rows if row["market_ref"] != "aave-v3-ethereum"]
    trimmed.append(extra)
    upsert_positions(db, user.id, trimmed, sync_id=None)
    aave = db.scalar(select(Position).where(Position.market_ref == "aave-v3-ethereum"))
    morpho = db.scalar(select(Position).where(Position.market_ref == "morpho-weth-usdc"))
    kamino = db.scalar(select(Position).where(Position.protocol == "kamino"))
    assert aave is not None and aave.status == "closed"
    assert morpho is not None and morpho.status == "open"
    assert kamino is not None and kamino.status == "open"
    # Closed rows do not add NAV. Open mock net = 4500+2000+2500-400 = 8600.
    assert positions_net_value(db, user.id) == Decimal("8600.00000000")


def test_upsert_closes_only_seen_protocols_and_skips_failed(db):
    """Batch with A but not B keeps B open; a missing A market closes; failed A stays open."""
    user = _user(db, "scope-user")
    a_keep = {
        "kind": "lending",
        "protocol": "aave",
        "account_ref": "0xaaa",
        "market_ref": "aave-keep",
        "status": "open",
        "net_value_usdt": "100",
    }
    a_gone = {**a_keep, "market_ref": "aave-gone", "net_value_usdt": "50"}
    b_old = {
        "kind": "lending",
        "protocol": "morpho",
        "account_ref": "0xbbb",
        "market_ref": "morpho-old",
        "status": "open",
        "net_value_usdt": "200",
    }
    upsert_positions(db, user.id, [a_keep, a_gone, b_old])

    # Batch contains protocol A only. B was not synced, so its open row stays.
    upsert_positions(db, user.id, [a_keep])
    aave_keep = db.scalar(select(Position).where(Position.market_ref == "aave-keep"))
    aave_gone = db.scalar(select(Position).where(Position.market_ref == "aave-gone"))
    morpho = db.scalar(select(Position).where(Position.market_ref == "morpho-old"))
    assert aave_keep is not None and aave_keep.status == "open"
    assert aave_gone is not None and aave_gone.status == "closed"
    assert morpho is not None and morpho.status == "open"

    # Reopen the disappeared A market, then fail the A adapter: old A rows stay open.
    upsert_positions(db, user.id, [a_keep, a_gone])
    upsert_positions(db, user.id, [], failed_protocols=["aave"])
    aave_keep = db.scalar(select(Position).where(Position.market_ref == "aave-keep"))
    aave_gone = db.scalar(select(Position).where(Position.market_ref == "aave-gone"))
    morpho = db.scalar(select(Position).where(Position.market_ref == "morpho-old"))
    assert aave_keep is not None and aave_keep.status == "open"
    assert aave_gone is not None and aave_gone.status == "open"
    assert morpho is not None and morpho.status == "open"

    # A row present in the batch still must not close sibling A markets when A failed.
    upsert_positions(db, user.id, [a_keep], failed_protocols=["aave"])
    aave_gone = db.scalar(select(Position).where(Position.market_ref == "aave-gone"))
    assert aave_gone is not None and aave_gone.status == "open"


def test_upsert_optional_source_failure_still_closes_and_main_source_blocks(db):
    """Optional enrichment failure cannot block close; an unread market does."""
    user = _user(db, "failure-scope-user")
    row = {
        "kind": "lending",
        "protocol": "kamino",
        "chain": "solana",
        "account_ref": "So1ana",
        "market_ref": "kamino-old",
        "status": "open",
        "net_value_usdt": "10",
    }
    upsert_positions(db, user.id, [row])

    # pnl/interest are optional: main obligation data was read, so a repaid
    # position may close even if that source failed.
    upsert_positions(
        db,
        user.id,
        [],
        seen_protocols=["kamino"],
        failed_protocols=["kamino:optional:pnl"],
    )
    old = db.scalar(select(Position).where(Position.market_ref == "kamino-old"))
    assert old is not None and old.status == "closed"

    # Reopen, then fail one market/source: this is main data, so no close.
    upsert_positions(db, user.id, [row])
    upsert_positions(
        db,
        user.id,
        [],
        seen_protocols=["kamino"],
        failed_protocols=["kamino:market:MktBad"],
    )
    old = db.scalar(select(Position).where(Position.market_ref == "kamino-old"))
    assert old is not None and old.status == "open"


def test_upsert_legacy_market_ref_keeps_old_identity(db):
    """Kamino's old obligation ref must match the new market ref without a split."""
    user = _user(db, "legacy-ref-user")
    old_row = {
        "kind": "lending",
        "protocol": "kamino",
        "chain": "solana",
        "account_ref": "So1anaLegacy",
        "market_ref": "kamino-OblLegacy",
        "status": "open",
        "net_value_usdt": "100",
        "details": {"obligation": "OblLegacy"},
    }
    upsert_positions(db, user.id, [old_row])
    original = db.scalar(select(Position).where(Position.market_ref == "kamino-OblLegacy"))
    assert original is not None

    new_row = {
        **old_row,
        "market_ref": "kamino-MarketReal",
        "net_value_usdt": "120",
        "details": {"obligation": "OblLegacy", "market": "MarketReal"},
        "legacy_market_refs": ["kamino-OblLegacy"],
    }
    written = upsert_positions(db, user.id, [new_row])
    assert len(written) == 1 and written[0].id == original.id
    assert written[0].market_ref == "kamino-OblLegacy"
    assert written[0].net_value_usdt == Decimal("120")
    # No duplicate open row and no rewritten identity; history stays on one row.
    rows = db.scalars(select(Position).where(Position.user_id == user.id)).all()
    assert len(rows) == 1 and rows[0].status == "open"
    snapshots = db.scalars(
        select(PositionSnapshot).where(PositionSnapshot.position_id == original.id)
    ).all()
    assert len(snapshots) == 2


def test_summaries_match_fixtures(client, db):
    user = _user(db, "sum-user")
    upsert_positions(db, user.id, load_mock_positions())
    headers = auth_header(client, username="sum-user", password="pass-word")

    lending = client.get("/api/lending/summary", headers=headers)
    assert lending.status_code == 200, lending.text
    body = lending.json()
    assert body["total_collateral_usdt"] == 21000.0
    assert body["total_debt_usdt"] == 7500.0
    assert body["net_value_usdt"] == 13500.0
    assert body["min_health_factor"] == 1.40
    assert {row["protocol"] for row in body["positions"]} == {"aave", "morpho", "kamino"}

    options = client.get("/api/options/summary", headers=headers)
    assert options.status_code == 200, options.text
    obody = options.json()
    # 2500 + (-400); premium 900+120; pnl 1600+(-280)
    assert obody["total_mark_value_usdt"] == 2100.0
    assert obody["total_premium_usdt"] == 1020.0
    assert obody["unrealized_pnl_usdt"] == 1320.0
    shorts = [row for row in obody["positions"] if row["protocol"] == "binance"]
    assert shorts[0]["details"]["mark_value_usd"] == -400.0
    assert shorts[0]["details"]["size"] == -1.0
    assert shorts[0]["net_value_usdt"] == -400.0

    listed = client.get("/api/positions", headers=headers)
    assert listed.status_code == 200 and len(listed.json()) == 5
    only_lending = client.get("/api/positions?kind=lending&protocol=aave", headers=headers)
    assert only_lending.status_code == 200
    assert [row["protocol"] for row in only_lending.json()] == ["aave"]


def test_user_isolation(client, db):
    owner = _user(db, "owner-pos")
    other = _user(db, "other-pos")
    upsert_positions(db, owner.id, load_mock_positions())
    other_headers = auth_header(client, username="other-pos", password="pass-word")
    assert client.get("/api/positions", headers=other_headers).json() == []
    summary = client.get("/api/lending/summary", headers=other_headers).json()
    assert summary["positions"] == []
    assert summary["min_health_factor"] is None
    assert summary["net_value_usdt"] == 0.0
    assert positions_net_value(db, other.id) == Decimal("0")
    assert positions_net_value(db, owner.id) == Decimal("15600.00000000")


def test_alert_rules_default_and_put(client, db):
    _user(db, "alert-user")
    headers = auth_header(client, username="alert-user", password="pass-word")
    listed = client.get("/api/alerts/rules", headers=headers)
    assert listed.status_code == 200, listed.text
    rules = listed.json()
    assert len(rules) == 1
    assert rules[0]["protocol"] is None
    assert rules[0]["warn_hf"] == 1.5
    assert rules[0]["danger_hf"] == 1.2
    assert rules[0]["enabled"] is True
    assert rules[0]["cooldown_min"] == 360

    updated = client.put(
        "/api/alerts/rules",
        headers=headers,
        json={"warn_hf": 1.8, "danger_hf": 1.1, "enabled": False, "cooldown_min": 60},
    )
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert body["warn_hf"] == 1.8
    assert body["danger_hf"] == 1.1
    assert body["enabled"] is False
    assert body["cooldown_min"] == 60
    assert body["id"] == rules[0]["id"]

    scoped = client.put(
        "/api/alerts/rules",
        headers=headers,
        json={"protocol": "aave", "warn_hf": 1.6, "danger_hf": 1.25, "enabled": True, "cooldown_min": 120},
    )
    assert scoped.status_code == 200
    again = client.get("/api/alerts/rules", headers=headers).json()
    assert len(again) == 2
    assert client.get("/api/alerts/events", headers=headers).json() == []

    # Events of another user stay hidden. Skeleton only — no evaluation.
    other = _user(db, "alert-other")
    rule = db.scalar(select(AlertRule).where(AlertRule.user_id == other.id)) or AlertRule(
        user_id=other.id
    )
    if rule.id is None:
        db.add(rule)
        db.commit()
        db.refresh(rule)
    db.add(
        AlertEvent(
            user_id=other.id,
            rule_id=rule.id,
            level="warn",
            hf=Decimal("1.4"),
            sent_at=datetime(2026, 10, 23, tzinfo=timezone.utc),
            channel_result="queued",
        )
    )
    db.commit()
    assert client.get("/api/alerts/events", headers=headers).json() == []
    other_headers = auth_header(client, username="alert-other", password="pass-word")
    events = client.get("/api/alerts/events", headers=other_headers).json()
    assert len(events) == 1 and events[0]["level"] == "warn" and events[0]["hf"] == 1.4


def test_unauthenticated_positions(client):
    assert client.get("/api/positions").status_code == 401
    assert client.get("/api/lending/summary").status_code == 401
    assert client.get("/api/alerts/rules").status_code == 401


def test_mock_hook_noop_without_flag(db, caplog):
    user = _user(db, "hook-user")
    run = SyncRun(
        started_at=datetime(2026, 10, 23, tzinfo=timezone.utc),
        status="running",
        trigger="manual",
    )
    db.add(run)
    db.commit()
    with caplog.at_level("INFO"):
        written = sync_positions_for_user(db, user.id, run.id, use_mock=False)
    assert written == 0
    assert db.scalars(select(Position).where(Position.user_id == user.id)).all() == []
    assert "positions: no evm wallet, skip live lending" in caplog.text
    written = sync_positions_for_user(db, user.id, run.id, use_mock=True)
    assert written == 5
    stored = db.scalar(select(Position).where(Position.user_id == user.id, Position.protocol == "aave"))
    assert stored is not None and stored.sync_id == run.id


def _wallet(db, user: User, address: str = "0xabc0000000000000000000000000000000000abc") -> None:
    db.add(UserWallet(user_id=user.id, chain="evm", address=address))
    db.commit()


def _open_row(protocol: str, market: str, net: str = "100") -> dict:
    return {
        "kind": "lending",
        "protocol": protocol,
        "chain": "ethereum",
        "account_ref": "0xabc0000000000000000000000000000000000abc",
        "market_ref": market,
        "status": "open",
        "net_value_usdt": net,
        "collateral_usdt": net,
        "debt_usdt": "0",
        "details": {"health_factor": 2.0},
    }


def test_live_hook_calls_adapters_and_upserts(db, monkeypatch):
    """Bound EVM wallet: each live adapter is called, rows land open."""
    user = _user(db, "hook-live")
    _wallet(db, user)
    calls: list[str] = []

    def _aave(address, **_kwargs):
        calls.append(f"aave:{address}")
        return [_open_row("aave", "aave-core", "1500")]

    def _morpho(address, **_kwargs):
        calls.append(f"morpho:{address}")
        return [_open_row("morpho", "morpho-1", "400")]

    def _pendle(address, **_kwargs):
        calls.append(f"pendle:{address}")
        return []

    monkeypatch.setattr("app.sync.positions_hook.fetch_aave", _aave)
    monkeypatch.setattr("app.sync.positions_hook.fetch_morpho", _morpho)
    monkeypatch.setattr("app.sync.positions_hook.fetch_pendle", _pendle)
    # This user has no Solana wallet, so Kamino / Jupiter must not be called.
    def _no_solana(*_args, **_kwargs):
        raise AssertionError("solana adapter called without a solana wallet")

    monkeypatch.setattr("app.sync.positions_hook.fetch_kamino", _no_solana)
    monkeypatch.setattr("app.sync.positions_hook.fetch_jupiter", _no_solana)

    written = sync_positions_for_user(db, user.id, None, use_mock=False)
    assert written == 2
    assert [item.split(":")[0] for item in calls] == ["aave", "morpho", "pendle"]
    stored = {
        row.protocol: row.status
        for row in db.scalars(select(Position).where(Position.user_id == user.id))
    }
    assert stored == {"aave": "open", "morpho": "open"}


def test_live_hook_adapter_error_does_not_close_old_rows(db, monkeypatch):
    """A raising adapter and a swallowed transport error both keep old rows open.

    A clean empty read (Pendle here) is not a failure: that protocol was seen,
    so a position it no longer returns is closed.
    """
    from app.integrations.lending_common import note_failure

    user = _user(db, "hook-fail")
    _wallet(db, user)
    upsert_positions(
        db,
        user.id,
        [
            _open_row("aave", "aave-old"),
            _open_row("morpho", "morpho-old"),
            _open_row("pendle", "pendle-old"),
        ],
    )

    def _boom(_address, **_kwargs):
        raise RuntimeError("aave down")

    def _morpho_swallowed(_address, **_kwargs):
        # Same shape as the real adapter: log the failure, return no rows.
        note_failure("morpho")
        return []

    def _pendle_empty(_address, **_kwargs):
        return []

    monkeypatch.setattr("app.sync.positions_hook.fetch_aave", _boom)
    monkeypatch.setattr("app.sync.positions_hook.fetch_morpho", _morpho_swallowed)
    monkeypatch.setattr("app.sync.positions_hook.fetch_pendle", _pendle_empty)
    def _no_solana(*_args, **_kwargs):
        raise AssertionError("solana adapter called without a solana wallet")

    monkeypatch.setattr("app.sync.positions_hook.fetch_kamino", _no_solana)
    monkeypatch.setattr("app.sync.positions_hook.fetch_jupiter", _no_solana)

    written = sync_positions_for_user(db, user.id, None, use_mock=False)
    assert written == 0
    status = {
        row.protocol: row.status
        for row in db.scalars(select(Position).where(Position.user_id == user.id))
    }
    assert status["aave"] == "open"
    assert status["morpho"] == "open"
    assert status["pendle"] == "closed"


def test_mark_covered_by_position_pure():
    positions = load_mock_positions()
    holdings = [
        {"asset": "aEthWETH", "contract": AAVE_WETH, "value_usdt": "8000"},
        {"asset": "aEthUSDC", "contract": AAVE_USDC.upper(), "value_usdt": "2000"},
        {"symbol": "variableDebtEthUSDT", "contract": AAVE_DEBT, "value_usdt": "3000"},
        {"asset": "mwETH", "contract": MORPHO_COLLAT, "value_usdt": "6000"},
        {"asset": "WETH", "contract": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", "value_usdt": "100"},
        {"asset": "PLP-SUSDX-3DEC2026", "contract": "0xpendlelp", "value_usdt": "6009"},
        {"asset": "PT-sUSDx-3DEC2026", "value_usdt": "2486"},
        {"asset": "YT-sUSDx-3DEC2026", "value_usdt": "103"},
        {"asset": "aUSDC", "value_usdt": "10"},
        {"asset": "ARB", "value_usdt": "20"},
    ]
    marked = mark_covered_by_position(holdings, positions)
    flags = {row.get("symbol") or row["asset"]: row["covered_by_position"] for row in marked}
    assert flags["aEthWETH"] is True
    assert flags["aEthUSDC"] is True
    assert flags["variableDebtEthUSDT"] is True
    assert flags["mwETH"] is True
    assert flags["WETH"] is False
    assert flags["PLP-SUSDX-3DEC2026"] is False
    assert flags["PT-sUSDx-3DEC2026"] is False
    assert flags["YT-sUSDx-3DEC2026"] is False
    # No contract on the holding: exact aToken symbol still matches a leg.
    assert flags["aUSDC"] is False  # fixture leg is aEthUSDC, not aUSDC
    assert flags["ARB"] is False
    assert "covered_by_position" not in holdings[0]

    dropped = mark_covered_by_position(holdings, positions, drop_covered=True)
    names = [row.get("symbol") or row["asset"] for row in dropped]
    assert "aEthWETH" not in names
    assert "mwETH" not in names
    assert "WETH" in names
    assert "PLP-SUSDX-3DEC2026" in names

    # Closed positions do not cover anything.
    closed = [{**positions[0], "status": "closed"}]
    only_closed = mark_covered_by_position(holdings[:1], closed)
    assert only_closed[0]["covered_by_position"] is False

    # Symbol fallback when the holding has no contract but the leg names an aToken.
    bare = [{"asset": "aEthWETH", "value_usdt": "1"}]
    assert mark_covered_by_position(bare, positions)[0]["covered_by_position"] is True

    # A live Morpho borrow lists the underlying (USDC), not a share token. The
    # wallet's own USDC shares that contract and must stay in the flat NAV;
    # only the receipt / debt / share symbol is covered.
    underlying = [
        {
            "protocol": "morpho",
            "status": "open",
            "details": {
                "collateral": [{"symbol": "USD3", "contract": "0x056b269eb1f75477a8666ae8c7fe01b64dd55ecc"}],
                "borrow": [{"symbol": "USDC", "contract": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"}],
            },
        }
    ]
    wallet = [
        {"asset": "USDC", "contract": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", "value_usdt": "5431"},
        {"asset": "USD3", "contract": "0x056b269eb1f75477a8666ae8c7fe01b64dd55ecc", "value_usdt": "100"},
        {"asset": "mwETH", "value_usdt": "1"},
    ]
    flags = {
        row["asset"]: row["covered_by_position"]
        for row in mark_covered_by_position(wallet, underlying)
    }
    assert flags == {"USDC": False, "USD3": False, "mwETH": False}
