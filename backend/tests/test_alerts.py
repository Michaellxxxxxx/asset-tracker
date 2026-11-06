"""Lane E: health / expiry evaluation, cooldown, and a failed WeChat push that stays queued."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select

from app.alerts import (
    evaluate_alerts,
    evaluate_data_freshness,
    evaluate_liq_distance_alerts,
    evaluate_nav_drawdown,
)
from app.auth import hash_password
from app.integrations.wechat.client import MockWechatClient, SendResult
from app.integrations.wechat.queue import flush_user_queue
from app.models import (
    AlertEvent,
    AlertRule,
    Position,
    Snapshot,
    User,
    WechatContextToken,
    WechatPushQueue,
)
from app.positions_core import upsert_positions
from app.sync.service import run_sync


class _Boom:
    """Send always fails. Evaluation must queue the notice instead of raising."""

    def send_message(self, context_token: str, text: str) -> SendResult:
        del context_token, text
        return SendResult(ok=False, error="send_failed")


def _user(db, username: str) -> User:
    user = User(username=username, hashed_password=hash_password("pass-word"))
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


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


def _option(expiry: str, market: str = "BTC-C") -> dict:
    return {
        "kind": "option",
        "protocol": "deribit",
        "chain": None,
        "account_ref": "deribit:demo",
        "market_ref": market,
        "label": "BTC call",
        "status": "open",
        "net_value_usdt": "10",
        "collateral_usdt": "0",
        "debt_usdt": "0",
        "details": {"expiry": expiry, "underlying": "BTC", "type": "call"},
    }


NOW = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)


def _events(db, user_id: int) -> list[AlertEvent]:
    return list(
        db.scalars(
            select(AlertEvent).where(AlertEvent.user_id == user_id).order_by(AlertEvent.id.asc())
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


def test_health_triggers_warn_and_danger(db):
    user = _user(db, "alert-fire")
    upsert_positions(
        db,
        user.id,
        [_lending(1.4, market="warn"), _lending(1.1, protocol="morpho", market="danger")],
    )
    db.commit()

    result = evaluate_alerts(db, user.id, now=NOW)
    assert result.fired == 2
    assert result.skipped == 2  # no context_token → skipped, not a failed send
    assert result.queued == 0
    levels = {row.level for row in _events(db, user.id)}
    assert levels == {"warn", "danger"}
    assert all(row.channel_result == "skipped:no_token" for row in _events(db, user.id))
    # The notices are still persisted for the next token-bearing flush.
    queued_rows = _queue(db, user.id)
    assert len(queued_rows) == 2
    assert all(row.pending_id is None and row.alert_event_id for row in queued_rows)
    assert {row.alert_event_id for row in queued_rows} == {row.id for row in _events(db, user.id)}


def test_healthy_position_does_not_fire(db):
    user = _user(db, "alert-quiet")
    upsert_positions(db, user.id, [_lending(1.85)])
    db.commit()

    result = evaluate_alerts(db, user.id, now=NOW)
    assert result.fired == 0
    assert _events(db, user.id) == []


def test_disabled_rule_does_not_fire(db):
    user = _user(db, "alert-off")
    db.add(AlertRule(user_id=user.id, enabled=False))
    upsert_positions(db, user.id, [_lending(1.05)])
    db.commit()

    result = evaluate_alerts(db, user.id, now=NOW)
    assert result.fired == 0
    assert _events(db, user.id) == []


def test_cooldown_suppresses_same_level(db):
    user = _user(db, "alert-cool")
    written = upsert_positions(db, user.id, [_lending(1.3)])
    db.commit()

    first = evaluate_alerts(db, user.id, now=NOW)
    assert first.fired == 1 and first.suppressed == 0
    second = evaluate_alerts(db, user.id, now=NOW + timedelta(minutes=10))
    assert second.fired == 0 and second.suppressed == 1
    assert len(_events(db, user.id)) == 1

    # A worse level is a different event and is not cooled down by the warn.
    position = written[0]
    position.details = {"health_factor": 1.05}
    db.commit()
    third = evaluate_alerts(db, user.id, now=NOW + timedelta(minutes=20))
    assert third.fired == 1
    assert [row.level for row in _events(db, user.id)] == ["warn", "danger"]


def test_recover_fires_once_then_cools_down(db):
    user = _user(db, "alert-recover")
    written = upsert_positions(db, user.id, [_lending(1.3)])
    db.commit()
    evaluate_alerts(db, user.id, now=NOW)

    written[0].details = {"health_factor": 2.4}
    db.commit()
    recovered = evaluate_alerts(db, user.id, now=NOW + timedelta(hours=1))
    assert recovered.fired == 1
    assert _events(db, user.id)[-1].level == "recover"

    again = evaluate_alerts(db, user.id, now=NOW + timedelta(hours=2))
    # Still safe, and the one recover is inside the cooldown, so nothing new is written.
    assert again.fired == 0 and again.suppressed == 1
    assert [row.level for row in _events(db, user.id)].count("recover") == 1


def test_option_near_expiry_fires_far_expiry_does_not(db):
    user = _user(db, "alert-expiry")
    upsert_positions(
        db,
        user.id,
        [
            _option("2026-10-28T08:00:00Z", market="near"),
            _option("2026-12-25T08:00:00Z", market="far"),
        ],
    )
    db.commit()

    result = evaluate_alerts(db, user.id, now=NOW)
    assert result.fired == 1
    event = _events(db, user.id)[0]
    assert event.level == "warn"
    assert event.hf is None


def test_push_failure_queues_without_raising(db):
    user = _user(db, "alert-queue")
    db.add(WechatContextToken(user_id=user.id, context_token="ctx-present"))
    upsert_positions(db, user.id, [_lending(1.1)])
    db.commit()

    result = evaluate_alerts(db, user.id, now=NOW, client=_Boom())
    assert result.fired == 1
    assert result.queued == 1
    assert result.sent == 0
    event = _events(db, user.id)[0]
    assert event.channel_result == "queued:send_failed"
    queued = _queue(db, user.id)
    assert len(queued) == 1
    assert queued[0].alert_event_id == event.id
    assert queued[0].pending_id is None
    assert queued[0].status == "queued"
    assert "健康度危险" in queued[0].text

    # A later flush with a working client clears the retry row.
    stats = flush_user_queue(db, user.id, client=MockWechatClient())
    assert stats.sent == 1
    db.refresh(queued[0])
    assert queued[0].status == "sent"


def test_alert_queue_write_failure_keeps_alert_event(db, monkeypatch):
    """Queue bookkeeping is best-effort; the alert event must still commit."""
    user = _user(db, "alert-queue-boom")
    db.add(WechatContextToken(user_id=user.id, context_token="ctx-present"))
    upsert_positions(db, user.id, [_lending(1.1)])
    db.commit()

    def _boom(*_args, **_kwargs):
        raise RuntimeError("queue boom")

    monkeypatch.setattr("app.alerts.enqueue_alert", _boom)
    result = evaluate_alerts(db, user.id, now=NOW, client=_Boom())
    assert result.fired == 1 and result.queued == 1
    assert len(_events(db, user.id)) == 1
    assert _queue(db, user.id) == []


def test_alert_queue_without_context_does_not_burn_attempts(db):
    """A quiet bot must not mark alerts failed; they wait for a token."""
    user = _user(db, "alert-quiet-queue")
    upsert_positions(db, user.id, [_lending(1.1)])
    db.commit()

    result = evaluate_alerts(db, user.id, now=NOW, client=_Boom())
    assert result.skipped == 1
    assert result.queued == 0
    queued = _queue(db, user.id)[0]
    assert queued.attempts == 0

    for _ in range(6):  # above the queue's five-attempt cap
        flush_user_queue(db, user.id, client=_Boom())
    db.refresh(queued)
    assert queued.status == "queued"
    assert queued.attempts == 0
    assert queued.last_error == "no_context_token"


def test_no_token_skipped_event_is_delivered_after_token_arrives(db):
    """A9: skipped is truthful, but the notice is not lost."""
    user = _user(db, "alert-skip-then-token")
    upsert_positions(db, user.id, [_lending(1.1)])
    db.commit()

    result = evaluate_alerts(db, user.id, now=NOW)
    assert result.fired == 1 and result.skipped == 1 and result.queued == 0
    event = _events(db, user.id)[0]
    assert event.channel_result == "skipped:no_token"
    assert event.sent_at is None
    row = _queue(db, user.id)[0]
    assert row.alert_event_id == event.id and row.attempts == 0

    db.add(WechatContextToken(user_id=user.id, context_token="ctx-arrived"))
    db.commit()
    stats = flush_user_queue(
        db, user.id, client=MockWechatClient(), now=NOW + timedelta(minutes=1)
    )
    assert stats.sent == 1
    db.refresh(event)
    db.refresh(row)
    assert event.channel_result == "mock"
    assert event.sent_at is not None
    assert row.status == "sent"


def test_admin_health_counts_users_without_wechat_token(client, db):
    from tests.conftest import auth_header

    admin = User(
        username="p8a-admin",
        hashed_password=hash_password("pass-word"),
        is_admin=True,
    )
    with_token = _user(db, "health-token")
    blank_token = _user(db, "health-blank")
    no_row = _user(db, "health-none")
    db.add(admin)
    db.add(WechatContextToken(user_id=with_token.id, context_token="ctx-real"))
    db.add(WechatContextToken(user_id=blank_token.id, context_token="   "))
    db.commit()

    response = client.get(
        "/api/alerts/health",
        headers=auth_header(client, username="p8a-admin", password="pass-word"),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["users_total"] == 4
    assert body["users_with_token"] == 1
    assert body["users_without_wechat_token"] == 3
    assert "ctx-real" not in response.text
    assert no_row.id is not None

    forbidden = client.get(
        "/api/alerts/health",
        headers=auth_header(client, username="health-token", password="pass-word"),
    )
    assert forbidden.status_code == 403


def test_put_alert_rule_rejects_impossible_thresholds(client, db):
    """A10: protocol and global rules share the same band validation."""
    from tests.conftest import auth_header

    _user(db, "rule-validation")
    headers = auth_header(client, username="rule-validation", password="pass-word")
    listed = client.get("/api/alerts/rules", headers=headers)
    assert listed.status_code == 200
    rule_id = listed.json()[0]["id"]

    cases = [
        ({"warn_hf": 1.2, "danger_hf": 1.2}, "danger_hf 必须小于 warn_hf"),
        ({"warn_hf": 1.2, "danger_hf": 1.3}, "danger_hf 必须小于 warn_hf"),
        ({"warn_hf": 0, "danger_hf": -1}, "warn_hf 与 danger_hf 必须大于 0"),
        ({"warn_hf": 1.5, "danger_hf": 0}, "warn_hf 与 danger_hf 必须大于 0"),
        ({"warn_hf": 1.5, "danger_hf": 1.2, "cooldown_min": -1}, "cooldown_min 不能为负数"),
        (
            {"protocol": "morpho", "warn_hf": 1.1, "danger_hf": 1.2},
            "danger_hf 必须小于 warn_hf",
        ),
    ]
    for payload, expected_detail in cases:
        response = client.put("/api/alerts/rules", headers=headers, json=payload)
        assert response.status_code == 422, (payload, response.text)
        assert expected_detail in response.json()["detail"]

    # Rejected writes did not mutate the default row.
    unchanged = client.get("/api/alerts/rules", headers=headers).json()
    assert len(unchanged) == 1
    assert unchanged[0]["id"] == rule_id
    assert unchanged[0]["warn_hf"] == 1.5
    assert unchanged[0]["danger_hf"] == 1.2

    # Boundary values are valid: cooldown 0 and danger just below warn.
    valid = client.put(
        "/api/alerts/rules",
        headers=headers,
        json={
            "protocol": "morpho",
            "warn_hf": 1.05,
            "danger_hf": 1.01,
            "cooldown_min": 0,
        },
    )
    assert valid.status_code == 200, valid.text
    assert valid.json()["cooldown_min"] == 0


def test_sync_survives_alert_failure(db, monkeypatch):
    user = _user(db, "alert-sync")

    def _explode(*_args, **_kwargs):
        raise RuntimeError("alert boom")

    monkeypatch.setattr("app.alerts.evaluate_alerts", _explode)
    result = run_sync(user.id, "mock", db=db, since=datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert result.fetched >= 0
    assert _events(db, user.id) == []


def test_protocol_rule_overrides_default(db):
    user = _user(db, "alert-scope")
    db.add(AlertRule(user_id=user.id, warn_hf=Decimal("1.5"), danger_hf=Decimal("1.2")))
    db.add(
        AlertRule(
            user_id=user.id,
            protocol="aave",
            warn_hf=Decimal("2.2"),
            danger_hf=Decimal("1.5"),
            enabled=True,
        )
    )
    upsert_positions(db, user.id, [_lending(1.85, protocol="aave")])
    db.commit()

    # 1.85 is safe on the default rule (warn 1.5) but a warn on the aave rule (warn 2.2).
    result = evaluate_alerts(db, user.id, now=NOW)
    assert result.fired == 1
    assert _events(db, user.id)[0].level == "warn"


def test_protocol_rule_isolated_from_global_rule(db):
    """A5: USD3/morpho uses its 1.05-specific rule; the global 1.5/1.2 still
    governs aave and never leaks across protocols."""
    user = _user(db, "alert-protocol-isolation")
    global_rule = AlertRule(
        user_id=user.id, warn_hf=Decimal("1.5"), danger_hf=Decimal("1.2")
    )
    morpho_rule = AlertRule(
        user_id=user.id,
        protocol="morpho",
        warn_hf=Decimal("1.2"),
        danger_hf=Decimal("1.05"),
    )
    db.add_all([global_rule, morpho_rule])
    written = upsert_positions(
        db,
        user.id,
        [
            _lending(1.4, protocol="morpho", market="usd3-usdc"),
            _lending(1.4, protocol="aave", market="global"),
        ],
    )
    db.commit()

    # 1.4 is a warn on the global 1.5 line, but safe on the morpho 1.2 line.
    first = evaluate_alerts(db, user.id, now=NOW)
    assert first.fired == 1
    first_events = _events(db, user.id)
    assert len(first_events) == 1
    assert first_events[0].level == "warn"
    assert first_events[0].rule_id == global_rule.id
    assert first_events[0].position_id == next(
        row.id for row in written if row.protocol == "aave"
    )

    morpho_position = next(row for row in written if row.protocol == "morpho")
    morpho_position.details = {"health_factor": 1.05}
    db.commit()
    second = evaluate_alerts(db, user.id, now=NOW + timedelta(minutes=10))
    assert second.fired == 1
    morpho_event = _events(db, user.id)[-1]
    assert morpho_event.level == "danger"
    assert morpho_event.rule_id == morpho_rule.id
    assert morpho_event.position_id == morpho_position.id
    assert morpho_event.hf == Decimal("1.05")


def test_morpho_hf_1_05_is_danger_and_cooldown_holds(db):
    """Live Morpho USD3/USDC HF ~1.05 is danger on the default 1.2 line.

    A second evaluation inside cooldown writes nothing (no spam). The push
    client is a mock: nothing is sent to WeChat.
    """
    from app.integrations.wechat.client import MockWechatClient

    user = _user(db, "alert-morpho-hf")
    db.add(AlertRule(user_id=user.id, warn_hf=Decimal("1.5"), danger_hf=Decimal("1.2"), cooldown_min=360))
    db.add(WechatContextToken(user_id=user.id, context_token="ctx-test-not-a-real-token"))
    upsert_positions(
        db,
        user.id,
        [_lending(1.05046258, protocol="morpho", market="usd3-usdc")],
    )
    db.commit()

    sender = MockWechatClient()
    first = evaluate_alerts(db, user.id, now=NOW, client=sender)
    assert first.fired == 1
    assert first.sent == 1
    assert first.queued == 0
    event = _events(db, user.id)[0]
    assert event.level == "danger"
    assert event.hf == Decimal("1.05046258")
    assert event.channel_result == "mock"
    assert len(sender.sent) == 1
    assert "健康度危险" in sender.sent[0]["text"]
    assert "ctx-test" not in sender.sent[0]["text"]

    again = evaluate_alerts(db, user.id, now=NOW + timedelta(minutes=30), client=sender)
    assert again.fired == 0 and again.suppressed == 1
    assert len(_events(db, user.id)) == 1
    assert len(sender.sent) == 1  # cooldown: no second message


def test_position_row_written(db):
    """The event points at the position that crossed the line."""
    user = _user(db, "alert-pos")
    written = upsert_positions(db, user.id, [_lending(1.05)])
    db.commit()
    evaluate_alerts(db, user.id, now=NOW)
    event = _events(db, user.id)[0]
    assert event.position_id == written[0].id
    assert event.hf == Decimal("1.05")
    assert db.get(Position, event.position_id) is not None


# -- A4: stale data / sync failure notice + recovery -------------------------


def test_stale_position_alerts_after_threshold_and_cools_down(db):
    user = _user(db, "alert-stale-pos")
    stale = {
        "protocol": "morpho",
        "label": "morpho usdc",
        "market_ref": "usdc-market",
        "stale_since": (NOW - timedelta(hours=7)).isoformat(),
    }

    first = evaluate_data_freshness(db, user.id, stale_positions=[stale], now=NOW)
    assert len(first["fired"]) == 1
    assert first["fired"][0]["source"] == "stale:morpho"
    assert first["suppressed"] == []
    event = _events(db, user.id)[0]
    assert event.level == "warn"
    assert event.position_id is None
    assert event.hf is None
    queued = _queue(db, user.id)[0]
    assert "数据过期" in queued.text
    assert "morpho" in queued.text

    # Inside the six-hour cooldown the same source is silent.
    cooled = evaluate_data_freshness(
        db,
        user.id,
        stale_positions=[stale],
        previous_state=first,
        now=NOW + timedelta(hours=1),
    )
    assert cooled["fired"] == []
    assert cooled["suppressed"] == [{"source": "stale:morpho", "reason": "cooldown"}]
    assert len(_events(db, user.id)) == 1

    # Still stale after the cooldown: notify again.
    again = evaluate_data_freshness(
        db,
        user.id,
        stale_positions=[stale],
        previous_state=cooled,
        now=NOW + timedelta(hours=6, minutes=1),
    )
    assert len(again["fired"]) == 1
    assert len(_events(db, user.id)) == 2


def test_failed_source_waits_for_threshold_then_alerts(db):
    user = _user(db, "alert-stale-src")

    waiting = evaluate_data_freshness(
        db, user.id, failed_sources=["lending:jupiter:perps"], now=NOW
    )
    assert waiting["fired"] == []
    assert waiting["suppressed"] == [
        {"source": "lending:jupiter:perps", "reason": "waiting_threshold"}
    ]
    assert _events(db, user.id) == []

    fired = evaluate_data_freshness(
        db,
        user.id,
        failed_sources=["lending:jupiter:perps"],
        previous_state=waiting,
        now=NOW + timedelta(hours=6, minutes=1),
    )
    assert len(fired["fired"]) == 1
    assert fired["fired"][0]["source"] == "lending:jupiter:perps"
    event = _events(db, user.id)[0]
    assert event.level == "warn" and event.position_id is None


def test_failed_source_recovery_fires_once(db):
    user = _user(db, "alert-stale-recover")
    state = evaluate_data_freshness(
        db,
        user.id,
        failed_sources=["binance:HTTPStatusError"],
        previous_state={
            "tracked_sources": {
                "binance:HTTPStatusError": {
                    "first_seen": (NOW - timedelta(hours=7)).isoformat(),
                    "event_id": None,
                    "stale_count": 0,
                }
            }
        },
        now=NOW,
    )
    assert len(state["fired"]) == 1

    recovered = evaluate_data_freshness(
        db, user.id, previous_state=state, now=NOW + timedelta(hours=1)
    )
    assert len(recovered["recovered"]) == 1
    assert recovered["recovered"][0]["source"] == "binance:HTTPStatusError"
    event = _events(db, user.id)[-1]
    assert event.level == "recover"
    assert "数据恢复" in _queue(db, user.id)[-1].text

    # The recovered source is no longer tracked, so it cannot recover twice.
    again = evaluate_data_freshness(
        db, user.id, previous_state=recovered, now=NOW + timedelta(hours=2)
    )
    assert again["recovered"] == []
    assert [row.level for row in _events(db, user.id)] == ["warn", "recover"]


def test_terminal_failed_delivery_does_not_start_cooldown(db):
    user = _user(db, "alert-stale-retry")
    stale = {
        "protocol": "aave",
        "label": "aave eth",
        "market_ref": "eth-market",
        "stale_since": (NOW - timedelta(hours=7)).isoformat(),
    }
    state = evaluate_data_freshness(db, user.id, stale_positions=[stale], now=NOW)
    event = _events(db, user.id)[0]
    # p7a: only a terminal failed:* delivery releases the cooldown.
    event.channel_result = "failed:bad_request"
    db.commit()

    retried = evaluate_data_freshness(
        db,
        user.id,
        stale_positions=[stale],
        previous_state=state,
        now=NOW + timedelta(minutes=10),
    )
    assert len(retried["fired"]) == 1
    assert len(_events(db, user.id)) == 2


# -- A8: closed position sends one recover ----------------------------------


def test_closed_position_sends_one_recover_and_dedupes(db):
    user = _user(db, "alert-closed")
    written = upsert_positions(db, user.id, [_lending(1.05)])
    db.commit()
    evaluate_alerts(db, user.id, now=NOW)
    assert _events(db, user.id)[-1].level == "danger"

    written[0].status = "closed"
    db.commit()
    closed = evaluate_alerts(db, user.id, now=NOW + timedelta(hours=1))
    assert closed.fired == 1
    event = _events(db, user.id)[-1]
    assert event.level == "recover"
    assert event.position_id == written[0].id
    assert event.hf is None
    assert "已关闭或已还清" in _queue(db, user.id)[-1].text

    again = evaluate_alerts(db, user.id, now=NOW + timedelta(hours=2))
    assert again.fired == 0 and again.suppressed == 1
    assert [row.level for row in _events(db, user.id)].count("recover") == 1


# -- O1: a carried-forward position is not scored ---------------------------


def test_stale_position_does_not_trigger_or_recover_hf_alert(db):
    user = _user(db, "alert-stale-o1")
    written = upsert_positions(db, user.id, [_lending(1.05)])
    db.commit()
    evaluate_alerts(db, user.id, now=NOW)
    assert _events(db, user.id)[-1].level == "danger"

    # The source failed: the old 1.05 row is carried forward with stale=true,
    # even if a later (unused) value would look healthy.
    written[0].details = {"health_factor": 2.5, "stale": True, "stale_since": NOW.isoformat()}
    db.commit()
    skipped = evaluate_alerts(db, user.id, now=NOW + timedelta(hours=1))
    assert skipped.fired == 0 and skipped.suppressed == 0
    assert [row.level for row in _events(db, user.id)] == ["danger"]

    # A stale low HF still must not become a real hf event, but p38 emits a
    # degraded hf_stale reminder when the row carries debt.
    written[0].details = {"health_factor": 1.05, "stale": True}
    db.commit()
    stale_low = evaluate_alerts(db, user.id, now=NOW + timedelta(hours=2))
    assert stale_low.fired == 1
    assert stale_low.skipped == 1
    events = _events(db, user.id)
    assert [row.level for row in events] == ["danger", "danger"]
    assert [row.event_kind for row in events] == ["hf", "hf_stale"]
    assert events[-1].event_kind != "hf"
    assert "过期" in _queue(db, user.id)[-1].text
    assert "沿用旧值 1.05" in _queue(db, user.id)[-1].text


# -- A6: liquidation-price distance -----------------------------------------


def _lending_liq(
    liq_price: float,
    *,
    current_price: float = 100.0,
    amount: float = 2.0,
    estimated: bool = False,
    include_collateral: bool = True,
    stale: bool = False,
    market: str = "liq-mkt",
) -> dict:
    row = _lending(3.0, market=market)
    usd = current_price * amount
    row["collateral_usdt"] = f"{usd:.2f}"
    row["net_value_usdt"] = f"{usd - 100:.2f}"
    details = {"health_factor": 3.0, "liq_price": liq_price}
    if include_collateral:
        details["collateral"] = [
            {"symbol": "ETH", "amount": amount, "usd": usd, "contract": "0xeth"}
        ]
        details["borrow"] = [
            {"symbol": "USDC", "amount": 100, "usd": 100, "contract": "0xusdc"}
        ]
    if estimated:
        details["liq_price_estimated"] = True
    if stale:
        details["stale"] = True
    row["details"] = details
    return row


def test_liq_distance_warn_cooldown_danger_and_recover(db):
    user = _user(db, "a6-path")
    written = upsert_positions(
        db, user.id, [_lending_liq(95, estimated=True, market="a6")]
    )
    db.commit()

    first = evaluate_alerts(db, user.id, now=NOW, liq_alert_distance_pct=10)
    assert first.fired == 1
    event = _events(db, user.id)[-1]
    assert event.level == "warn"
    assert event.position_id == written[0].id
    # A6 uses a negative marker so it cannot be mistaken for a health alert.
    assert event.hf is not None and event.hf < 0
    text = _queue(db, user.id)[-1].text
    assert "清算价预警" in text
    assert "估算" in text
    assert "5.00%" in text

    cooled = evaluate_alerts(
        db, user.id, now=NOW + timedelta(minutes=10), liq_alert_distance_pct=10
    )
    assert cooled.fired == 0 and cooled.suppressed == 1
    assert len(_events(db, user.id)) == 1

    # 100 vs 102: price has crossed the liquidation level.
    written[0].details = _lending_liq(102, estimated=True, market="a6")["details"]
    db.commit()
    danger = evaluate_alerts(
        db, user.id, now=NOW + timedelta(minutes=20), liq_alert_distance_pct=10
    )
    assert danger.fired == 1
    assert _events(db, user.id)[-1].level == "danger"
    assert "已跌破清算价" in _queue(db, user.id)[-1].text

    # 100 vs 80: back above the 10% line -> one recover.
    written[0].details = _lending_liq(80, estimated=True, market="a6")["details"]
    db.commit()
    recovered = evaluate_alerts(
        db, user.id, now=NOW + timedelta(minutes=30), liq_alert_distance_pct=10
    )
    assert recovered.fired == 1
    assert _events(db, user.id)[-1].level == "recover"
    assert "已回到警戒线" in _queue(db, user.id)[-1].text

    again = evaluate_alerts(
        db, user.id, now=NOW + timedelta(minutes=40), liq_alert_distance_pct=10
    )
    assert again.fired == 0 and again.suppressed == 1
    assert [row.level for row in _events(db, user.id)] == [
        "warn",
        "danger",
        "recover",
    ]


def test_liq_distance_safe_missing_or_stale_data_does_not_fire(db):
    user = _user(db, "a6-quiet")
    no_debt = _lending_liq(90, market="a6-no-debt")
    no_debt["debt_usdt"] = "0"
    upsert_positions(
        db,
        user.id,
        [
            _lending_liq(80, market="a6-safe"),
            _lending_liq(90, include_collateral=False, market="a6-no-price"),
            _lending_liq(90, stale=True, market="a6-stale"),
            no_debt,
        ],
    )
    db.commit()

    result = evaluate_alerts(db, user.id, now=NOW, liq_alert_distance_pct=10)
    assert result.fired == 0 and result.suppressed == 0
    assert _events(db, user.id) == []
    assert _queue(db, user.id) == []


# -- A7: NAV drawdown -------------------------------------------------------


def _admin(db, username: str) -> User:
    user = User(
        username=username,
        hashed_password=hash_password("pass-word"),
        is_admin=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _nav(
    db,
    user_id: int,
    ts: datetime,
    nav: float,
    *,
    source: str = "sync",
    breakdown: str | None = None,
) -> Snapshot:
    row = Snapshot(
        user_id=user_id,
        ts=ts,
        nav_usdt=Decimal(str(nav)),
        source=source,
        breakdown=breakdown,
    )
    db.add(row)
    db.commit()
    return row


def test_nav_drawdown_fires_once_recovers_and_new_breach_alerts(db):
    admin = _admin(db, "a7-path")
    _nav(db, admin.id, NOW - timedelta(days=6), 100000)
    _nav(db, admin.id, NOW - timedelta(days=5), 105000)
    _nav(db, admin.id, NOW, 95000)

    first = evaluate_alerts(db, admin.id, now=NOW, nav_drawdown_pct=8)
    assert first.fired == 1
    event = _events(db, admin.id)[-1]
    assert event.level == "warn"
    assert event.position_id is None
    assert event.hf is not None and event.hf < 0
    text = _queue(db, admin.id)[-1].text
    assert "净值回撤预警" in text
    assert "9.52%" in text
    assert "105000.00" in text and "95000.00" in text

    # Still above the threshold: one alert per episode, not one per sync.
    cooled = evaluate_alerts(
        db, admin.id, now=NOW + timedelta(minutes=10), nav_drawdown_pct=8
    )
    assert cooled.fired == 0 and cooled.suppressed == 1
    assert len(_events(db, admin.id)) == 1

    _nav(db, admin.id, NOW + timedelta(hours=2), 100000)
    recovered = evaluate_alerts(
        db, admin.id, now=NOW + timedelta(hours=2), nav_drawdown_pct=8
    )
    assert recovered.fired == 1
    assert _events(db, admin.id)[-1].level == "recover"
    assert "净值回撤恢复" in _queue(db, admin.id)[-1].text

    # A new breach after recovery starts a new episode.
    _nav(db, admin.id, NOW + timedelta(hours=3), 90000)
    breach = evaluate_alerts(
        db, admin.id, now=NOW + timedelta(hours=3), nav_drawdown_pct=8
    )
    assert breach.fired == 1
    assert [row.level for row in _events(db, admin.id)] == [
        "warn",
        "recover",
        "warn",
    ]

    rows = list(
        db.scalars(
            select(Snapshot)
            .where(Snapshot.user_id == admin.id)
            .order_by(Snapshot.ts.asc())
        ).all()
    )
    assert len(rows) == 5
    assert [row.nav_usdt for row in rows] == [
        Decimal("100000.00"),
        Decimal("105000.00"),
        Decimal("95000.00"),
        Decimal("100000.00"),
        Decimal("90000.00"),
    ]


def test_nav_drawdown_skips_non_admin_missing_stale_and_seed(db):
    normal = _user(db, "a7-normal")
    _nav(db, normal.id, NOW - timedelta(days=1), 100000)
    _nav(db, normal.id, NOW, 90000)
    skipped = evaluate_nav_drawdown(db, normal.id, now=NOW)
    assert skipped.evaluated is False
    assert skipped.reason == "not_admin"
    assert skipped.fired == 0

    empty = _admin(db, "a7-empty")
    missing = evaluate_nav_drawdown(db, empty.id, now=NOW)
    assert missing.evaluated is False
    assert missing.reason == "no_snapshot"

    old = _admin(db, "a7-old")
    _nav(db, old.id, NOW - timedelta(days=4), 100000)
    _nav(db, old.id, NOW - timedelta(days=3), 90000)
    stale = evaluate_nav_drawdown(
        db, old.id, now=NOW, stale_after=timedelta(hours=48)
    )
    assert stale.evaluated is True
    assert stale.reason == "stale_snapshot"
    assert stale.fired == 0

    carried = _admin(db, "a7-carried")
    _nav(db, carried.id, NOW - timedelta(days=1), 100000)
    _nav(
        db,
        carried.id,
        NOW,
        90000,
        breakdown='{"holdings":[{"source":"binance","stale":true}]}',
    )
    stale_breakdown = evaluate_nav_drawdown(db, carried.id, now=NOW)
    assert stale_breakdown.reason == "stale_snapshot"
    assert stale_breakdown.fired == 0

    seed = _admin(db, "a7-seed")
    _nav(db, seed.id, NOW, 90000, source="seed")
    no_real = evaluate_nav_drawdown(db, seed.id, now=NOW)
    assert no_real.reason == "no_snapshot"
    assert no_real.fired == 0

    assert _events(db, normal.id) == []
    assert _events(db, empty.id) == []
    assert _events(db, old.id) == []
    assert _events(db, carried.id) == []
    assert _events(db, seed.id) == []


def test_liq_distance_multi_collateral_estimate_uses_weighted_price(db):
    user = _user(db, "a6-multi")
    row = _lending_liq(240, estimated=True, market="a6-multi")
    row["details"]["collateral"] = [
        {"symbol": "ETH", "amount": 1, "usd": 100, "contract": "0xeth"},
        {"symbol": "WBTC", "amount": 1, "usd": 300, "contract": "0xbtc"},
    ]
    upsert_positions(db, user.id, [row])
    db.commit()

    result = evaluate_liq_distance_alerts(
        db, user.id, now=NOW, threshold_pct=10
    )
    assert result.fired == 1
    event = _events(db, user.id)[-1]
    assert event.level == "warn"
    assert event.hf is not None and event.hf < 0
    text = _queue(db, user.id)[-1].text
    # (250 - 240) / 250 = 4.00%, and the source was flagged as an estimate.
    assert "4.00%" in text and "估算" in text


def test_liq_distance_perps_uses_health_factor_with_zero_debt(db):
    user = _user(db, "a6-perp")
    row = _lending_liq(80, market="a6-perp")
    row["debt_usdt"] = "0"
    row["collateral_usdt"] = "50"
    row["net_value_usdt"] = "50"
    row["details"] = {
        "health_factor": 1.05,
        "liq_price": 80,
        "side": "long",
        "margin_usd": 50,
        "notional_usd": 500,
    }
    upsert_positions(db, user.id, [row])
    db.commit()

    result = evaluate_liq_distance_alerts(
        db, user.id, now=NOW, threshold_pct=10
    )
    assert result.fired == 1
    event = _events(db, user.id)[-1]
    assert event.level == "warn"
    # mark = 80 * 1.05 = 84; (84 - 80) / 84 = 4.76%.
    assert "4.76%" in _queue(db, user.id)[-1].text


def _reconcile_log(user_id: int, freshness: dict) -> "SyncReconcileLog":
    from app.models import SyncReconcileLog

    return SyncReconcileLog(
        user_id=user_id,
        ts=NOW,
        nav_usdt=Decimal("0"),
        flat_total_usdt=Decimal("0"),
        covered_total_usdt=Decimal("0"),
        positions_net_usdt=Decimal("0"),
        residual_usdt=Decimal("0"),
        tolerance_usdt=Decimal("0"),
        status="ok",
        detail={"data_freshness": freshness},
    )


def test_alert_events_join_source_name_from_reconcile_log(client, db):
    """p8e2 ②: NULL-position rows carry the provider title, not a generic one."""
    from tests.conftest import auth_header

    _user(db, "p8e2-source")
    headers = auth_header(client, username="p8e2-source", password="pass-word")
    user_id = db.scalar(select(User.id).where(User.username == "p8e2-source"))
    assert user_id is not None

    state = evaluate_data_freshness(
        db,
        user_id,
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
    db.add(_reconcile_log(user_id, state))
    db.commit()

    body = client.get("/api/alerts/events", headers=headers).json()
    assert body[0]["source_name"] == "lending:kamino"
    assert body[0]["source_kind"] == "sync"

    # A recovery names both itself and the stale event it closes.
    recovered = evaluate_data_freshness(
        db,
        user_id,
        previous_state=state,
        now=NOW + timedelta(hours=1),
    )
    db.add(_reconcile_log(user_id, recovered))
    db.commit()

    body = client.get("/api/alerts/events", headers=headers).json()
    assert len(body) == 2
    assert {row["source_name"] for row in body} == {"lending:kamino"}
    assert {row["source_kind"] for row in body} == {"sync"}


def test_alert_events_mark_stale_carry_forward_source(client, db):
    from tests.conftest import auth_header

    user = _user(db, "p8e2-stale-source")
    headers = auth_header(client, username="p8e2-stale-source", password="pass-word")
    state = evaluate_data_freshness(
        db,
        user.id,
        stale_positions=[
            {
                "protocol": "morpho",
                "stale_since": (NOW - timedelta(hours=7)).isoformat(),
            }
        ],
        now=NOW,
    )
    db.add(_reconcile_log(user.id, state))
    db.commit()

    body = client.get("/api/alerts/events", headers=headers).json()
    assert len(body) == 1
    assert body[0]["position_id"] is None
    assert body[0]["source_name"] == "stale:morpho"
    assert body[0]["source_kind"] == "stale"


def test_delete_protocol_rule_falls_back_to_global_and_keeps_global(client, db):
    """p8e2 ④: delete one protocol override; the global fallback is immutable."""
    from tests.conftest import auth_header

    _user(db, "p8e2-delete-rule")
    headers = auth_header(client, username="p8e2-delete-rule", password="pass-word")

    global_rule = client.get("/api/alerts/rules", headers=headers).json()[0]
    added = client.put(
        "/api/alerts/rules",
        headers=headers,
        json={"protocol": "morpho", "warn_hf": 1.3, "danger_hf": 1.05, "cooldown_min": 60},
    )
    assert added.status_code == 200, added.text
    assert len(client.get("/api/alerts/rules", headers=headers).json()) == 2

    removed = client.delete("/api/alerts/rules?protocol=morpho", headers=headers)
    assert removed.status_code == 204, removed.text
    left = client.get("/api/alerts/rules", headers=headers).json()
    assert [row["id"] for row in left] == [global_rule["id"]]
    assert left[0]["protocol"] is None

    # Idempotent for a rule that is already gone.
    assert client.delete("/api/alerts/rules?protocol=morpho", headers=headers).status_code == 204

    # No global delete, and no unnamed delete.
    assert client.delete("/api/alerts/rules?protocol=global", headers=headers).status_code == 422
    assert client.delete("/api/alerts/rules", headers=headers).status_code == 422
    still_there = client.get("/api/alerts/rules", headers=headers).json()
    assert [row["id"] for row in still_there] == [global_rule["id"]]
