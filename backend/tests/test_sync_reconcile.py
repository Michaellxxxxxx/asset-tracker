"""Phase-6 auto reconcile: tiers, failed sources, isolation, alert dedupe.

Read-only: the only writes are ``sync_reconcile_log`` and (on fail) one
``alert_events`` / ``wechat_push_queue`` row. Snapshots, positions and the
ledger must not change.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import hash_password
from app.models import (
    AlertEvent,
    LedgerEntry,
    Position,
    Snapshot,
    SyncReconcileLog,
    User,
    WechatPushQueue,
)
from app.sync.reconcile import build_reconcile, record_sync_reconcile, reconcile_tolerance

from .conftest import auth_header

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


def _snapshot(
    db: Session,
    user_id: int,
    ts: datetime,
    nav: str,
    holdings: list[dict],
    *,
    folded: bool = True,
) -> Snapshot:
    payload = {"positions_included": folded, "holdings": holdings}
    row = Snapshot(
        user_id=user_id,
        ts=ts,
        nav_usdt=Decimal(nav),
        source="sync",
        breakdown=json.dumps(payload),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _position(db: Session, user_id: int, net: str, *, symbol: str = "mUSDC") -> Position:
    row = Position(
        user_id=user_id,
        kind="lending",
        protocol="morpho",
        chain="base",
        account_ref="0xabc",
        market_ref="usdc-market",
        status="open",
        net_value_usdt=Decimal(net),
        collateral_usdt=Decimal("800"),
        debt_usdt=Decimal("0"),
        details={"collateral": [{"symbol": symbol, "contract": "0xreceipt"}]},
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _count(db: Session, model) -> int:
    return int(db.scalar(select(func.count()).select_from(model)) or 0)


def test_ok_identity_and_read_only(db: Session, demo_user: User) -> None:
    _position(db, demo_user.id, "500")
    snap = _snapshot(
        db,
        demo_user.id,
        NOW,
        "700.00",
        [
            {"asset": "USDC", "value_usdt": "200.00"},
            {"asset": "mUSDC", "value_usdt": "800.00", "covered_by_position": True},
        ],
    )
    before_snapshot = (snap.nav_usdt, snap.breakdown)
    before_ledger = _count(db, LedgerEntry)
    before_positions = _count(db, Position)

    row = record_sync_reconcile(db, user_id=demo_user.id, snapshot=snap, now=NOW)

    assert row.status == "ok"
    assert row.residual_usdt == Decimal("0.00")
    assert row.flat_total_usdt == Decimal("1000.00")
    assert row.covered_total_usdt == Decimal("800.00")
    assert row.positions_net_usdt == Decimal("500.00")
    assert row.detail["checks"]["covered_accounted"] is True
    # Nothing else moved.
    db.refresh(snap)
    assert (snap.nav_usdt, snap.breakdown) == before_snapshot
    assert _count(db, LedgerEntry) == before_ledger
    assert _count(db, Position) == before_positions
    assert _count(db, AlertEvent) == 0
    assert _count(db, WechatPushQueue) == 0


def test_warn_is_logged_but_not_pushed(db: Session, demo_user: User) -> None:
    snap = _snapshot(db, demo_user.id, NOW, "1003.00", [{"asset": "USDC", "value_usdt": "1000.00"}])
    assert reconcile_tolerance(Decimal("1003")) == Decimal("1.00")

    row = record_sync_reconcile(db, user_id=demo_user.id, snapshot=snap, now=NOW)

    assert row.status == "warn"
    assert row.residual_usdt == Decimal("3.00")
    assert row.detail["reason"] == "residual_above_threshold"
    assert row.detail["alert"]["status"] == "not_needed"
    assert _count(db, AlertEvent) == 0
    assert _count(db, WechatPushQueue) == 0


def test_fail_is_logged_and_queued_once_per_cooldown(db: Session, demo_user: User) -> None:
    first = _snapshot(db, demo_user.id, NOW, "1100.00", [{"asset": "USDC", "value_usdt": "1000.00"}])
    row = record_sync_reconcile(db, user_id=demo_user.id, snapshot=first, now=NOW)

    assert row.status == "fail"
    assert row.detail["reason"] == "residual_above_fail_threshold"
    assert row.detail["alert"]["status"] == "queued"
    assert _count(db, AlertEvent) == 1
    assert _count(db, WechatPushQueue) == 1
    queued = db.scalar(select(WechatPushQueue))
    assert queued is not None and queued.status == "queued" and queued.alert_event_id is not None
    assert "对账失败" in queued.text

    second = _snapshot(
        db,
        demo_user.id,
        NOW + timedelta(minutes=1),
        "1100.00",
        [{"asset": "USDC", "value_usdt": "1000.00"}],
    )
    again = record_sync_reconcile(
        db, user_id=demo_user.id, snapshot=second, now=NOW + timedelta(minutes=1)
    )
    assert again.status == "fail"
    assert again.detail["alert"]["status"] == "suppressed_cooldown"
    assert _count(db, AlertEvent) == 1
    assert _count(db, WechatPushQueue) == 1


def test_failed_sources_do_not_trigger_fail(db: Session, demo_user: User) -> None:
    snap = _snapshot(db, demo_user.id, NOW, "1000.00", [{"asset": "USDC", "value_usdt": "1000.00"}])
    row = record_sync_reconcile(
        db,
        user_id=demo_user.id,
        snapshot=snap,
        failed_sources=["lending:jupiter", "binance:HTTPStatusError"],
        now=NOW,
    )

    assert row.status == "ok"
    assert row.detail["failed_sources"] == ["lending:jupiter", "binance:HTTPStatusError"]
    assert row.detail["carry_forward"] is True
    assert row.detail["carry_forward_affects_nav"] is False
    assert "沿用上次值" in row.detail["carry_forward_nav_note"]
    assert _count(db, AlertEvent) == 0


def test_partial_marks_stale_carry_forward(db: Session, demo_user: User) -> None:
    position = _position(db, demo_user.id, "500")
    position.details = {
        **position.details,
        "stale": True,
        "stale_since": "2026-10-31T03:00:00+00:00",
    }
    db.commit()
    snap = _snapshot(
        db,
        demo_user.id,
        NOW,
        "700.00",
        [
            {"asset": "USDC", "value_usdt": "200.00"},
            {"asset": "mUSDC", "value_usdt": "800.00", "covered_by_position": True},
        ],
    )
    row = record_sync_reconcile(
        db,
        user_id=demo_user.id,
        snapshot=snap,
        failed_sources=["lending:kamino:market:MktBad"],
        now=NOW,
    )

    assert row.status == "ok"
    assert row.detail["partial"] is True
    assert row.detail["failed_sources"] == ["lending:kamino:market:MktBad"]
    assert row.detail["stale_count"] == 1
    stale = row.detail["stale_positions"][0]
    assert stale["protocol"] == "morpho"
    assert stale["market_ref"] == "usdc-market"
    assert stale["stale_since"] == "2026-10-31T03:00:00+00:00"
    # The partial read itself keeps the reconcile identity; the carried-forward
    # morpho row is old enough to raise one A4 stale-data notice.
    events = list(db.scalars(select(AlertEvent)).all())
    assert len(events) == 1
    assert events[0].level == "warn"
    assert events[0].position_id is None
    freshness = row.detail["data_freshness"]
    assert [item["source"] for item in freshness["fired"]] == ["stale:morpho"]
    assert "data_freshness" in row.detail


def test_data_freshness_recovers_on_next_clean_log(db: Session, demo_user: User) -> None:
    """A4 state carried in the log detail is what lets the next run recover."""
    position = _position(db, demo_user.id, "500")
    position.details = {
        **position.details,
        "stale": True,
        "stale_since": (NOW - timedelta(hours=7)).isoformat(),
    }
    db.commit()
    holdings = [
        {"asset": "USDC", "value_usdt": "200.00"},
        {"asset": "mUSDC", "value_usdt": "800.00", "covered_by_position": True},
    ]
    first = record_sync_reconcile(
        db,
        user_id=demo_user.id,
        snapshot=_snapshot(db, demo_user.id, NOW, "700.00", holdings),
        now=NOW,
    )
    assert [item["source"] for item in first.detail["data_freshness"]["fired"]] == ["stale:morpho"]

    position.details = {
        key: value
        for key, value in position.details.items()
        if key not in ("stale", "stale_since")
    }
    db.commit()
    second = record_sync_reconcile(
        db,
        user_id=demo_user.id,
        snapshot=_snapshot(db, demo_user.id, NOW + timedelta(hours=1), "700.00", holdings),
        now=NOW + timedelta(hours=1),
    )
    recovered = second.detail["data_freshness"]["recovered"]
    assert [item["source"] for item in recovered] == ["stale:morpho"]
    assert [row.level for row in db.scalars(select(AlertEvent).order_by(AlertEvent.id)).all()] == [
        "warn",
        "recover",
    ]


def test_covered_holding_without_position_fails(db: Session, demo_user: User) -> None:
    # Residual is zero (NAV = uncovered = 0), so only the accounting check can catch this.
    snap = _snapshot(
        db,
        demo_user.id,
        NOW,
        "0.00",
        [{"asset": "aUSDC", "contract": "0xreceipt", "value_usdt": "500.00", "covered_by_position": True}],
    )
    row = record_sync_reconcile(db, user_id=demo_user.id, snapshot=snap, now=NOW)
    assert row.status == "fail"
    assert row.detail["reason"] == "covered_holding_without_position"
    assert row.detail["checks"]["covered_orphans"][0]["asset"] == "aUSDC"


def test_debt_token_left_in_flat_holdings_fails(db: Session, demo_user: User) -> None:
    snap = _snapshot(
        db,
        demo_user.id,
        NOW,
        "500.00",
        [
            {"asset": "USDC", "value_usdt": "1000.00"},
            {"asset": "variableDebtUSDC", "value_usdt": "500.00"},
        ],
    )
    # expected = 1000 + 500 - 0; nav 500 → residual -1000, but the debt check also fires.
    row = record_sync_reconcile(db, user_id=demo_user.id, snapshot=snap, now=NOW)
    assert row.status == "fail"
    assert row.detail["checks"]["debt_accounted"] is False
    assert row.detail["checks"]["uncovered_debt_tokens"][0]["asset"] == "variableDebtUSDC"


def test_api_is_user_isolated(client: TestClient, db: Session, demo_user: User) -> None:
    other = User(username="other", hashed_password=hash_password("other123"), is_admin=False)
    db.add(other)
    db.commit()
    db.refresh(other)

    demo_snap = _snapshot(
        db, demo_user.id, NOW, "1000.00", [{"asset": "USDC", "value_usdt": "1000.00"}]
    )
    record_sync_reconcile(db, user_id=demo_user.id, snapshot=demo_snap, now=NOW)
    other_snap = _snapshot(
        db, other.id, NOW, "900.00", [{"asset": "USDC", "value_usdt": "1000.00"}]
    )
    record_sync_reconcile(db, user_id=other.id, snapshot=other_snap, now=NOW)

    response = client.get("/api/sync/reconcile", headers=auth_header(client))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert body["latest"]["id"] == db.scalar(
        select(SyncReconcileLog.id).where(SyncReconcileLog.user_id == demo_user.id)
    )
    assert len(body["items"]) == 1
    assert all(item["snapshot_id"] == demo_snap.id for item in body["items"])

    # Empty list shape is stable for a user who never synced.
    other_headers = auth_header(client, "other", "other123")
    other_body = client.get("/api/sync/reconcile", headers=other_headers).json()
    assert other_body["status"] == "fail"
    assert len(other_body["items"]) == 1


def test_overview_reconcile_status_is_additive(client: TestClient, db: Session, demo_user: User) -> None:
    snap = _snapshot(db, demo_user.id, NOW, "1000.00", [{"asset": "USDC", "value_usdt": "1000.00"}])
    record_sync_reconcile(db, user_id=demo_user.id, snapshot=snap, now=NOW)

    body = client.get("/api/overview", headers=auth_header(client)).json()
    assert body["reconcile_status"] == "ok"
    # Old contract still present.
    assert "current_nav_usdt" in body and "series" in body


def test_build_reconcile_tiers_are_threshold_based() -> None:
    breakdown = {"positions_included": True, "holdings": [{"value_usdt": "10000.00"}]}
    ok = build_reconcile(nav_usdt="10005.00", breakdown=breakdown, positions=[])
    warn = build_reconcile(nav_usdt="10006.00", breakdown=breakdown, positions=[])
    fail = build_reconcile(nav_usdt="10031.00", breakdown=breakdown, positions=[])
    # tolerance = 5 USDT, fail band = 25 USDT.
    assert ok["status"] == "ok"
    assert warn["status"] == "warn"
    assert fail["status"] == "fail"


def test_run_sync_writes_one_reconcile_row(db: Session, demo_user: User) -> None:
    """The hook sits after the snapshot commit, so every sync leaves a log row."""
    from app.sync.service import run_sync

    run_sync(demo_user.id, "mock", db=db)

    rows = list(
        db.scalars(
            select(SyncReconcileLog).where(SyncReconcileLog.user_id == demo_user.id)
        ).all()
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.snapshot_id is not None
    assert row.status in ("ok", "warn", "fail")
    assert row.detail["failed_sources"] == []
    # Mock snapshot is folded over the same mock positions, so the identity holds.
    assert row.status == "ok"
    assert row.residual_usdt == Decimal("0.00")


def test_zero_positions_snapshot_is_checkable_even_without_fold_flag(db: Session, demo_user: User) -> None:
    """Demo has no positions; ``_fold_positions`` leaves the flag unset, but NAV = flat."""
    snap = _snapshot(
        db,
        demo_user.id,
        NOW,
        "4869.43",
        [{"asset": "USDC", "value_usdt": "4869.43"}],
        folded=False,
    )
    row = record_sync_reconcile(db, user_id=demo_user.id, snapshot=snap, now=NOW)
    assert row.status == "ok"
    assert row.detail["reason"] == "within_tolerance"
    assert _count(db, AlertEvent) == 0
