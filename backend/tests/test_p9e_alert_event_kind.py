"""p9e: explicit ``alert_events.event_kind`` with a read-only legacy fallback.

New HF/A6/A7 rows carry ``event_kind`` and keep the old negative-hf marker so
pre-p9e code still routes them. Rows written before p9e stay NULL; readers fall
back to "hf<0 / position_id IS NULL" but must never rewrite those rows.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select

from app.alerts import (
    evaluate_alerts,
    evaluate_data_freshness,
)
from app.auth import hash_password
from app.models import AlertEvent, Snapshot, User
from app.positions_core import upsert_positions

NOW = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)


def _user(db, username: str, *, admin: bool = False) -> User:
    user = User(
        username=username,
        hashed_password=hash_password("pass-word"),
        is_admin=admin,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _lending(
    hf: float = 3.0,
    *,
    liq_price: float | None = None,
    current_price: float = 100.0,
    amount: float = 2.0,
    market: str = "mkt",
) -> dict:
    row = {
        "kind": "lending",
        "protocol": "aave",
        "chain": "ethereum",
        "account_ref": "0xabc",
        "market_ref": market,
        "label": f"aave {market}",
        "status": "open",
        "net_value_usdt": "100",
        "collateral_usdt": "200",
        "debt_usdt": "100",
        "details": {"health_factor": hf},
    }
    if liq_price is not None:
        usd = current_price * amount
        row["collateral_usdt"] = f"{usd:.2f}"
        row["net_value_usdt"] = f"{usd - 100:.2f}"
        row["details"]["liq_price"] = liq_price
        row["details"]["collateral"] = [
            {"symbol": "ETH", "amount": amount, "usd": usd, "contract": "0xeth"}
        ]
        row["details"]["borrow"] = [
            {"symbol": "USDC", "amount": 100, "usd": 100, "contract": "0xusdc"}
        ]
    return row


def _nav(db, user_id: int, ts: datetime, nav: float) -> Snapshot:
    row = Snapshot(
        user_id=user_id,
        ts=ts,
        nav_usdt=Decimal(str(nav)),
        source="sync",
        breakdown=None,
    )
    db.add(row)
    db.commit()
    return row


def _legacy_event(
    db,
    user_id: int,
    *,
    level: str,
    hf: float | None,
    position_id: int | None = None,
    created_at: datetime = NOW,
    channel_result: str = "skipped:no_token",
) -> AlertEvent:
    """Pre-p9e row: ``event_kind`` stays NULL, identity comes from the marker."""
    row = AlertEvent(
        user_id=user_id,
        rule_id=None,
        position_id=position_id,
        level=level,
        hf=Decimal(str(hf)) if hf is not None else None,
        created_at=created_at,
        sent_at=None,
        channel_result=channel_result,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    assert row.event_kind is None
    return row


def _events(db, user_id: int) -> list[AlertEvent]:
    return list(
        db.scalars(
            select(AlertEvent).where(AlertEvent.user_id == user_id).order_by(AlertEvent.id.asc())
        ).all()
    )


def _snapshot_columns(row: AlertEvent) -> dict[str, object]:
    return {
        column.name: getattr(row, column.name) for column in AlertEvent.__table__.columns
    }


def test_new_hf_liq_and_drawdown_events_carry_event_kind(db):
    user = _user(db, "p9e-new-kinds")
    written = upsert_positions(
        db,
        user.id,
        [_lending(1.4, liq_price=95, market="p9e-mixed")],
    )
    db.commit()

    result = evaluate_alerts(db, user.id, now=NOW, liq_alert_distance_pct=10)
    assert result.fired == 2  # one health warn + one A6 warn on the same row
    rows = _events(db, user.id)
    by_kind = {row.event_kind: row for row in rows}
    assert set(by_kind) == {"hf", "liq_distance"}
    assert by_kind["hf"].level == "warn"
    assert by_kind["hf"].hf is not None and by_kind["hf"].hf >= 0
    assert by_kind["liq_distance"].level == "warn"
    # The negative marker is kept so pre-p9e readers keep ignoring this row.
    assert by_kind["liq_distance"].hf is not None
    assert by_kind["liq_distance"].hf < 0
    assert by_kind["liq_distance"].position_id == written[0].id

    admin = _user(db, "p9e-new-drawdown", admin=True)
    _nav(db, admin.id, NOW - timedelta(days=6), 100000)
    _nav(db, admin.id, NOW, 90000)
    nav_result = evaluate_alerts(db, admin.id, now=NOW, nav_drawdown_pct=8)
    assert nav_result.fired == 1
    nav_event = _events(db, admin.id)[-1]
    assert nav_event.event_kind == "drawdown"
    assert nav_event.position_id is None
    assert nav_event.hf is not None and nav_event.hf < 0

    # A4 data-source notices keep the existing mechanism: no kind, no hf.
    state = evaluate_data_freshness(
        db,
        user.id,
        failed_sources=["lending:kamino"],
        previous_state={
            "tracked_sources": {
                "lending:kamino": {
                    "first_seen": (NOW - timedelta(hours=7)).isoformat(),
                    "event_id": None,
                    "stale_count": 0,
                }
            }
        },
        now=NOW,
    )
    assert state["fired"]
    freshness = _events(db, user.id)[-1]
    assert freshness.event_kind is None
    assert freshness.hf is None


def test_legacy_health_row_still_drives_cooldown_then_recover(db):
    user = _user(db, "p9e-legacy-hf")
    written = upsert_positions(db, user.id, [_lending(1.4, market="p9e-legacy-hf")])
    db.commit()
    legacy = _legacy_event(db, user.id, level="warn", hf=1.4, position_id=written[0].id)

    cooled = evaluate_alerts(db, user.id, now=NOW + timedelta(minutes=10))
    assert cooled.fired == 0 and cooled.suppressed == 1
    assert len(_events(db, user.id)) == 1

    # A healthy reading recovers the legacy warn through the fallback lookup.
    written[0].details = {"health_factor": 1.8}
    db.commit()
    recovered = evaluate_alerts(db, user.id, now=NOW + timedelta(minutes=20))
    assert recovered.fired == 1
    new_event = _events(db, user.id)[-1]
    assert new_event.event_kind == "hf"
    assert new_event.level == "recover"

    # The newly tagged recover is itself read through event_kind.
    again = evaluate_alerts(db, user.id, now=NOW + timedelta(minutes=30))
    assert again.fired == 0 and again.suppressed == 1
    assert _events(db, user.id)[0].id == legacy.id


def test_legacy_liq_and_drawdown_rows_drive_cooldown_then_recover(db):
    user = _user(db, "p9e-legacy-liq")
    written = upsert_positions(
        db, user.id, [_lending(3.0, liq_price=95, market="p9e-legacy-liq")]
    )
    db.commit()
    _legacy_event(db, user.id, level="warn", hf=-6, position_id=written[0].id)

    cooled = evaluate_alerts(db, user.id, now=NOW + timedelta(minutes=10), liq_alert_distance_pct=10)
    assert cooled.fired == 0 and cooled.suppressed == 1
    assert len(_events(db, user.id)) == 1

    # 20% away from liquidation: the legacy warn closes with one recover.
    written[0].details = _lending(3.0, liq_price=80, market="p9e-legacy-liq")["details"]
    db.commit()
    recovered = evaluate_alerts(
        db, user.id, now=NOW + timedelta(minutes=20), liq_alert_distance_pct=10
    )
    assert recovered.fired == 1
    event = _events(db, user.id)[-1]
    assert event.event_kind == "liq_distance"
    assert event.level == "recover"

    again = evaluate_alerts(
        db, user.id, now=NOW + timedelta(minutes=30), liq_alert_distance_pct=10
    )
    assert again.fired == 0 and again.suppressed == 1

    admin = _user(db, "p9e-legacy-nav", admin=True)
    _nav(db, admin.id, NOW - timedelta(days=6), 100000)
    _nav(db, admin.id, NOW, 90000)
    legacy_nav = _legacy_event(db, admin.id, level="warn", hf=-11)

    nav_cooled = evaluate_alerts(db, admin.id, now=NOW + timedelta(minutes=10), nav_drawdown_pct=8)
    assert nav_cooled.fired == 0 and nav_cooled.suppressed == 1
    assert [row.id for row in _events(db, admin.id)] == [legacy_nav.id]

    # NAV back at the old peak: the legacy drawdown warn recovers.
    _nav(db, admin.id, NOW + timedelta(hours=2), 100000)
    nav_recovered = evaluate_alerts(
        db, admin.id, now=NOW + timedelta(hours=2), nav_drawdown_pct=8
    )
    assert nav_recovered.fired == 1
    nav_event = _events(db, admin.id)[-1]
    assert nav_event.event_kind == "drawdown"
    assert nav_event.level == "recover"
    assert nav_event.position_id is None


def test_evaluation_never_rewrites_legacy_rows(db):
    user = _user(db, "p9e-immutable")
    written = upsert_positions(
        db, user.id, [_lending(1.4, liq_price=95, market="p9e-immutable")]
    )
    db.commit()
    legacy_hf = _legacy_event(db, user.id, level="warn", hf=1.4, position_id=written[0].id)
    legacy_liq = _legacy_event(db, user.id, level="warn", hf=-6, position_id=written[0].id)
    before = {legacy_hf.id: _snapshot_columns(legacy_hf), legacy_liq.id: _snapshot_columns(legacy_liq)}

    # Inside both cooldowns: nothing fires and nothing may be rewritten.
    cooled = evaluate_alerts(db, user.id, now=NOW + timedelta(minutes=10), liq_alert_distance_pct=10)
    assert cooled.fired == 0 and cooled.suppressed == 2

    # Moves both metrics to safe: two recovers are appended, old rows stay put.
    written[0].details = _lending(1.8, liq_price=80, market="p9e-immutable")["details"]
    db.commit()
    recovered = evaluate_alerts(
        db, user.id, now=NOW + timedelta(minutes=20), liq_alert_distance_pct=10
    )
    assert recovered.fired == 2

    db.expire_all()
    for event_id, expected in before.items():
        row = db.get(AlertEvent, event_id)
        assert row is not None
        assert _snapshot_columns(row) == expected
        assert row.event_kind is None
    assert len(_events(db, user.id)) == 4  # two legacy + two new recover rows


def test_event_list_exposes_kind_with_read_only_legacy_fallback(client, db):
    from tests.conftest import auth_header

    user = _user(db, "p9e-api")
    headers = auth_header(client, username="p9e-api", password="pass-word")
    written = upsert_positions(db, user.id, [_lending(1.4, market="p9e-api")])
    db.commit()
    position_id = written[0].id

    new_hf = _legacy_event(db, user.id, level="warn", hf=1.4, position_id=position_id)
    new_hf.event_kind = "hf"
    legacy_liq = _legacy_event(db, user.id, level="warn", hf=-6, position_id=position_id)
    legacy_nav = _legacy_event(db, user.id, level="warn", hf=-9.5)
    legacy_hf = _legacy_event(db, user.id, level="warn", hf=1.3, position_id=position_id)
    source_row = _legacy_event(db, user.id, level="recover", hf=None, channel_result="sent")
    tagged_liq = _legacy_event(db, user.id, level="warn", hf=-4, position_id=position_id)
    tagged_liq.event_kind = "liq_distance"
    db.commit()

    body = client.get("/api/alerts/events", headers=headers).json()
    kinds = {row["id"]: row["event_kind"] for row in body}
    assert kinds[new_hf.id] == "hf"
    assert kinds[legacy_liq.id] == "liq_distance"
    assert kinds[legacy_nav.id] == "drawdown"
    assert kinds[legacy_hf.id] == "hf"
    assert kinds[source_row.id] is None
    assert kinds[tagged_liq.id] == "liq_distance"

    # The fallback is read-only: legacy rows still have a NULL column.
    db.expire_all()
    for event_id in (legacy_liq.id, legacy_nav.id, legacy_hf.id, source_row.id):
        assert db.get(AlertEvent, event_id).event_kind is None
