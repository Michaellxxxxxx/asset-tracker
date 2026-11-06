"""p12b: Solana debt with an uncomputable health factor must not stay silent.

Kamino and Jupiter already publish collateral, borrow, health factor and
liquidation price when the protocol response carries a liquidation threshold.
The remaining gap was a debt position whose threshold/price is missing: the
adapter logged a warning but the alert layer skipped it (`hf is None`), so no
notice ever fired. These tests lock the marker and the warning/recover path.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from app.alerts import evaluate_alerts
from app.auth import hash_password
from app.integrations import lending_jupiter, lending_kamino
from app.models import AlertEvent, User, WechatPushQueue
from app.positions_core import upsert_positions

FIXTURES = Path(__file__).parent / "fixtures" / "lending_solana"
ADDR = "So1anaWa11et11111111111111111111111111111111"
NOW = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _user(db, username: str) -> User:
    user = User(username=username, hashed_password=hash_password("pass-word"))
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _events(db, user_id: int) -> list[AlertEvent]:
    return list(
        db.scalars(
            select(AlertEvent)
            .where(AlertEvent.user_id == user_id)
            .order_by(AlertEvent.id.asc())
        ).all()
    )


def _queue(db, user_id: int) -> list[WechatPushQueue]:
    return list(
        db.scalars(
            select(WechatPushQueue)
            .where(WechatPushQueue.user_id == user_id)
            .order_by(WechatPushQueue.id.asc())
        ).all()
    )


def _kamino_without_threshold() -> dict:
    payload = _load("kamino_user.json")
    loan = payload["markets"][0]["loans"][0]
    loan["loanInfo"]["liquidationLtv"] = "0"
    loan["loanInfo"]["collateral"]["deposits"][0]["liquidationLtv"] = "0"
    return payload


def test_kamino_debt_without_threshold_is_marked_hf_unavailable() -> None:
    rows = lending_kamino.normalize(ADDR, _kamino_without_threshold())
    assert len(rows) == 1
    details = rows[0]["details"]
    assert details["health_factor"] is None
    assert details["liq_threshold"] is None
    assert details["liq_price"] is None
    assert details["hf_unavailable"] is True
    # The debt is real, so this is not a supply-only row.
    assert float(rows[0]["debt_usdt"]) > 0


def test_jupiter_borrow_without_threshold_is_marked_hf_unavailable() -> None:
    payload = _load("jupiter_user.json")
    payload["borrow"][0]["vault"]["liquidationThreshold"] = 0
    rows = lending_jupiter.normalize(ADDR, payload)
    borrow_rows = [
        row for row in rows if row["market_ref"].startswith("jupiter-lend-borrow-")
    ]
    assert len(borrow_rows) == 1
    details = borrow_rows[0]["details"]
    assert details["health_factor"] is None
    assert details["liq_price"] is None
    assert details["hf_unavailable"] is True
    assert float(borrow_rows[0]["debt_usdt"]) > 0


def test_kamino_debt_without_hf_fires_once_then_recovers(db) -> None:
    user = _user(db, "p12b-kamino-hf-gap")
    written = upsert_positions(
        db, user.id, lending_kamino.normalize(ADDR, _kamino_without_threshold())
    )
    db.commit()
    assert len(written) == 1

    first = evaluate_alerts(db, user.id, now=NOW)
    assert first.fired == 1
    assert first.skipped == 1  # no context_token, but the event is persisted
    events = _events(db, user.id)
    assert len(events) == 1
    event = events[0]
    assert event.level == "warn"
    assert event.event_kind == "hf_missing"
    assert event.hf is None
    assert event.position_id == written[0].id
    assert event.channel_result == "skipped:no_token"
    # The notice is queued with an explicit "cannot compute" message.
    assert len(_queue(db, user.id)) == 1
    assert "健康度数据缺失" in _queue(db, user.id)[0].text
    assert "无法计算健康度" in _queue(db, user.id)[0].text

    # Same data gap within the cooldown is suppressed, not re-sent every sync.
    second = evaluate_alerts(db, user.id, now=NOW + timedelta(minutes=10))
    assert second.fired == 0
    assert second.suppressed == 1
    assert len(_events(db, user.id)) == 1


def test_hf_recovery_after_gap_is_not_silent(db) -> None:
    user = _user(db, "p12b-kamino-hf-recover")
    written = upsert_positions(
        db, user.id, lending_kamino.normalize(ADDR, _kamino_without_threshold())
    )
    db.commit()
    evaluate_alerts(db, user.id, now=NOW)

    # The next successful read restores a safe HF and removes the marker.
    position = written[0]
    details = dict(position.details)
    details["health_factor"] = 2.0
    details.pop("hf_unavailable", None)
    position.details = details
    db.commit()

    recovered = evaluate_alerts(db, user.id, now=NOW + timedelta(minutes=20))
    assert recovered.fired == 1
    events = _events(db, user.id)
    assert [row.level for row in events] == ["warn", "recover"]
    assert events[1].event_kind == "hf"
    assert float(events[1].hf) == 2.0
    assert "健康度恢复" in _queue(db, user.id)[-1].text

    again = evaluate_alerts(db, user.id, now=NOW + timedelta(minutes=30))
    assert again.fired == 0
    assert again.suppressed == 1


def test_supply_only_row_does_not_fire_hf_missing(db) -> None:
    payload = _kamino_without_threshold()
    payload["markets"][0]["loans"][0]["loanInfo"]["debt"]["borrows"] = []
    user = _user(db, "p12b-supply-only")
    upsert_positions(db, user.id, lending_kamino.normalize(ADDR, payload))
    db.commit()

    result = evaluate_alerts(db, user.id, now=NOW)
    assert result.fired == 0
    assert _events(db, user.id) == []
