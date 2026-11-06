"""p22: Solana HF basis, alert end-to-end coverage, and HF escalation.

All network/WeChat boundaries are mocked. These tests never touch the live
database, sync, or push providers.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from app.alerts import evaluate_alerts, evaluate_data_freshness
from app.auth import hash_password
from app.integrations import lending_jupiter, lending_kamino, lending_morpho
from app.integrations.wechat.client import MockWechatClient
from app.models import AlertEvent, User, WechatContextToken
from app.positions_core import upsert_positions
from tests.conftest import auth_header

FIXTURES = Path(__file__).parent / "fixtures" / "lending_solana"
MORPHO_FIXTURES = Path(__file__).parent / "fixtures" / "morpho"
ADDR = "So1anaWa11et11111111111111111111111111111111"
NOW = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)
PASSWORD = "pass-word"


@pytest.fixture(autouse=True)
def _offline_pendle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Morpho PT collateral must not call the Pendle quote endpoint."""
    monkeypatch.setattr(lending_morpho, "pendle_implied_apy", lambda *_a, **_k: None)


def _load(name: str, directory: Path = FIXTURES) -> dict:
    return json.loads((directory / name).read_text(encoding="utf-8"))


def _user(db, username: str) -> User:
    user = User(username=username, hashed_password=hash_password(PASSWORD))
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


def _hf_events(db, user_id: int) -> list[AlertEvent]:
    return [row for row in _events(db, user_id) if row.event_kind == "hf"]


def _lending(hf: float, protocol: str = "aave", market: str = "mkt") -> dict:
    return {
        "kind": "lending",
        "protocol": protocol,
        "chain": "ethereum",
        "account_ref": "0xabc",
        "market_ref": market,
        "label": f"{protocol} {market}",
        "status": "open",
        "net_value_usdt": "100",
        "collateral_usdt": "200",
        "debt_usdt": "100",
        "details": {"health_factor": hf},
    }


# -- Kamino HF basis --------------------------------------------------------


def test_kamino_protocol_stats_basis_wins() -> None:
    row = lending_kamino.normalize(ADDR, _load("kamino_protocol_stats.json"))[0]
    details = row["details"]
    # 1500 liquidation limit / 2000 adjusted debt = 0.75, not 5000*.8/2000.
    assert details["health_factor"] == pytest.approx(0.75)
    assert details["hf_basis"] == "protocol_stats"
    assert details["liq_threshold"] == pytest.approx(0.8)


def test_kamino_reserve_borrow_factor_percent_basis() -> None:
    row = lending_kamino.normalize(ADDR, _load("kamino_borrow_factor.json"))[0]
    details = row["details"]
    # Reserve publishes 150 (percent) -> 1.5. 5000*.8/(2000*1.5) = 1.3333.
    assert details["health_factor"] == pytest.approx(1.3333333333)
    assert details["hf_basis"] == "borrow_factor"
    assert details["borrow"][0]["borrowFactor"] == pytest.approx(1.5)


def test_kamino_ltv_formula_basis_and_existing_fixture_unchanged() -> None:
    new_row = lending_kamino.normalize(ADDR, _load("kamino_ltv_formula.json"))[0]
    assert new_row["details"]["health_factor"] == pytest.approx(2.0)
    assert new_row["details"]["hf_basis"] == "ltv_formula"

    old_row = lending_kamino.normalize(ADDR, _load("kamino_user.json"))[0]
    assert old_row["details"]["health_factor"] == pytest.approx(2.0)
    assert old_row["details"]["liq_price"] == pytest.approx(50)
    assert old_row["details"]["hf_basis"] == "ltv_formula"


def test_kamino_emode_ignores_reserve_borrow_factor() -> None:
    payload = _load("kamino_borrow_factor.json")
    loan = payload["markets"][0]["loans"][0]
    loan["elevationGroup"] = 2
    loan["loanInfo"]["liquidationLtv"] = "0.8"
    row = lending_kamino.normalize(ADDR, payload)[0]
    details = row["details"]
    assert details["elevation_group"] == 2
    # eMode forces borrowFactor=1: 5000*.8/2000 = 2.0.
    assert details["health_factor"] == pytest.approx(2.0)
    assert details["hf_basis"] == "ltv_formula"
    assert "borrowFactor" not in details["borrow"][0]


# -- Jupiter HF basis -------------------------------------------------------


def _jupiter_borrow_row(payload: dict) -> dict:
    rows = lending_jupiter.normalize(ADDR, payload)
    return next(
        row
        for row in rows
        if row["market_ref"].startswith("jupiter-lend-borrow-")
    )


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("oraclePriceLiquidate", "oracle_price_liquidate"),
        ("oraclePrice", "oracle_price"),
    ],
)
def test_jupiter_borrow_records_oracle_basis_without_changing_numbers(
    field: str, expected: str
) -> None:
    payload = copy.deepcopy(_load("jupiter_user.json"))
    vault = payload["borrow"][0]["vault"]
    vault[field] = "123.45"
    details = _jupiter_borrow_row(payload)["details"]
    assert details["hf_basis"] == expected
    # The p22 contract is additive only: threshold/HF arithmetic is unchanged.
    assert details["health_factor"] == pytest.approx(0.2125)
    assert details["liq_threshold"] == pytest.approx(0.85)


def test_jupiter_borrow_without_oracle_fields_marks_ltv_basis() -> None:
    details = _jupiter_borrow_row(_load("jupiter_user.json"))["details"]
    assert details["hf_basis"] == "liquidation_threshold"
    assert details["health_factor"] == pytest.approx(0.2125)


# -- End-to-end: adapter -> positions -> alerts -> API ----------------------


def _assert_api_has_hf_events(client, user: User, events: list[AlertEvent]) -> None:
    headers = auth_header(client, username=user.username, password=PASSWORD)
    response = client.get("/api/alerts/events", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    api_hf = [item for item in body if item["event_kind"] == "hf"]
    assert {(item["position_id"], item["level"]) for item in api_hf} >= {
        (event.position_id, event.level) for event in events
    }
    assert all(item["position_id"] is not None for item in api_hf)


def test_jupiter_hf_alerts_reach_events_endpoint(client, db) -> None:
    user = _user(db, "p22-jupiter-e2e")
    db.add(WechatContextToken(user_id=user.id, context_token="ctx-p22-jupiter"))
    rows = lending_jupiter.normalize(ADDR, _load("jupiter_user.json"))
    written = upsert_positions(db, user.id, rows)
    db.commit()

    sender = MockWechatClient()
    result = evaluate_alerts(db, user.id, now=NOW, client=sender)
    assert result.fired >= 2

    hf_events = _hf_events(db, user.id)
    assert {event.level for event in hf_events} == {"warn", "danger"}
    assert all(event.position_id is not None for event in hf_events)
    assert len(sender.sent) >= 2
    _assert_api_has_hf_events(client, user, hf_events)

    # The written positions are the ones the events point at.
    written_ids = {row.id for row in written}
    assert {event.position_id for event in hf_events} <= written_ids


def test_kamino_hf_alert_reaches_events_endpoint(client, db) -> None:
    user = _user(db, "p22-kamino-e2e")
    db.add(WechatContextToken(user_id=user.id, context_token="ctx-p22-kamino"))
    rows = lending_kamino.normalize(ADDR, _load("kamino_protocol_stats.json"))
    written = upsert_positions(db, user.id, rows)
    db.commit()

    result = evaluate_alerts(db, user.id, now=NOW, client=MockWechatClient())
    assert result.fired >= 1
    hf_events = _hf_events(db, user.id)
    assert len(hf_events) == 1
    assert hf_events[0].level == "danger"
    assert hf_events[0].position_id == written[0].id
    _assert_api_has_hf_events(client, user, hf_events)


def test_morpho_hf_alert_reaches_events_endpoint(client, db) -> None:
    user = _user(db, "p22-morpho-e2e")
    db.add(WechatContextToken(user_id=user.id, context_token="ctx-p22-morpho"))
    rows = lending_morpho.normalize(
        "0x1111111111111111111111111111111111111111",
        _load("morpho_pt_oracle.json", MORPHO_FIXTURES),
    )
    written = upsert_positions(db, user.id, rows)
    db.commit()

    result = evaluate_alerts(db, user.id, now=NOW, client=MockWechatClient())
    assert result.fired >= 1
    hf_events = _hf_events(db, user.id)
    assert len(hf_events) == 1
    assert hf_events[0].level == "danger"
    assert hf_events[0].position_id == written[0].id
    _assert_api_has_hf_events(client, user, hf_events)


# -- HF escalation inside the same-level cooldown ---------------------------


def _context_token(db, user: User, token: str) -> None:
    db.add(WechatContextToken(user_id=user.id, context_token=token))
    db.commit()


def test_hf_drop_005_inside_cooldown_is_suppressed(db) -> None:
    user = _user(db, "p22-escalate-small")
    _context_token(db, user, "ctx-p22-small")
    written = upsert_positions(db, user.id, [_lending(1.40)])
    db.commit()
    sender = MockWechatClient()
    assert evaluate_alerts(db, user.id, now=NOW, client=sender).fired == 1

    written[0].details = {"health_factor": 1.35}
    db.commit()
    second = evaluate_alerts(db, user.id, now=NOW + timedelta(minutes=10), client=sender)
    assert second.fired == 0
    assert second.suppressed == 1
    assert len(_events(db, user.id)) == 1
    assert len(sender.sent) == 1


def test_hf_drop_012_inside_cooldown_escalates(db) -> None:
    user = _user(db, "p22-escalate-big")
    _context_token(db, user, "ctx-p22-big")
    written = upsert_positions(db, user.id, [_lending(1.45)])
    db.commit()
    sender = MockWechatClient()
    assert evaluate_alerts(db, user.id, now=NOW, client=sender).fired == 1

    written[0].details = {"health_factor": 1.33}
    db.commit()
    second = evaluate_alerts(db, user.id, now=NOW + timedelta(minutes=10), client=sender)
    assert second.fired == 1
    assert second.sent == 1
    events = _events(db, user.id)
    assert [row.level for row in events] == ["warn", "warn"]
    assert events[-1].event_kind == "hf"
    assert float(events[-1].hf) == pytest.approx(1.33)
    assert "继续下降" in sender.sent[-1]["text"]
    assert len(sender.sent) == 2


def test_hf_drop_outside_cooldown_still_sends_normally(db) -> None:
    user = _user(db, "p22-escalate-outside")
    _context_token(db, user, "ctx-p22-outside")
    written = upsert_positions(db, user.id, [_lending(1.40)])
    db.commit()
    sender = MockWechatClient()
    assert evaluate_alerts(db, user.id, now=NOW, client=sender).fired == 1

    written[0].details = {"health_factor": 1.39}
    db.commit()
    second = evaluate_alerts(db, user.id, now=NOW + timedelta(hours=7), client=sender)
    assert second.fired == 1
    assert second.sent == 1
    assert len(_events(db, user.id)) == 2
    assert "继续下降" not in sender.sent[-1]["text"]


def test_hf_escalation_delta_is_env_overridable(db, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALERT_HF_ESCALATE_DELTA", "0.01")
    user = _user(db, "p22-escalate-env")
    _context_token(db, user, "ctx-p22-env")
    written = upsert_positions(db, user.id, [_lending(1.40)])
    db.commit()
    sender = MockWechatClient()
    assert evaluate_alerts(db, user.id, now=NOW, client=sender).fired == 1

    written[0].details = {"health_factor": 1.35}
    db.commit()
    second = evaluate_alerts(db, user.id, now=NOW + timedelta(minutes=10), client=sender)
    assert second.fired == 1
    assert "继续下降" in sender.sent[-1]["text"]


# -- Stale positions: skip HF, and A4 cooldown remains independent ----------


def test_stale_low_hf_skips_hf_and_a4_notice_keeps_its_cooldown(db) -> None:
    user = _user(db, "p22-stale-a4")
    written = upsert_positions(db, user.id, [_lending(1.05)])
    db.commit()
    stale_since = (NOW - timedelta(hours=8)).isoformat()
    written[0].details = {
        "health_factor": 1.05,
        "stale": True,
        "stale_since": stale_since,
    }
    db.commit()

    stale = [{"protocol": "aave", "stale_since": stale_since}]
    first_a4 = evaluate_data_freshness(db, user.id, stale_positions=stale, now=NOW)
    assert len(first_a4["fired"]) == 1
    a4_event = _events(db, user.id)[0]
    assert a4_event.position_id is None
    assert a4_event.hf is None

    # The A4 notice is inside its own cooldown...
    second_a4 = evaluate_data_freshness(
        db,
        user.id,
        stale_positions=stale,
        previous_state=first_a4,
        now=NOW + timedelta(minutes=10),
    )
    assert second_a4["fired"] == []
    assert second_a4["suppressed"][0]["reason"] == "cooldown"

    # ...and the stale position's last-known danger HF emits only a degraded
    # hf_stale notice, never a real hf event; the A4 cooldown is untouched.
    result = evaluate_alerts(db, user.id, now=NOW + timedelta(minutes=20))
    assert result.fired == 1
    events = _events(db, user.id)
    assert events[0].id == a4_event.id
    assert events[0].event_kind is None
    assert len(events) == 2
    assert events[-1].event_kind == "hf_stale"
    assert events[-1].position_id == written[0].id
    assert events[-1].level == "danger"
