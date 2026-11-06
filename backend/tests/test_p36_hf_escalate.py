"""p36: same-level HF escalation by absolute or relative worsening.

Network/WeChat boundaries are mocked. These tests never touch the live
database, sync, or push providers.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.alerts import evaluate_alerts
from app.auth import hash_password
from app.integrations.wechat.client import MockWechatClient
from app.models import AlertEvent, User, WechatContextToken
from app.positions_core import upsert_positions

NOW = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)
PASSWORD = "pass-word"


@pytest.fixture(autouse=True)
def _clean_escalation_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts from the documented defaults."""
    monkeypatch.delenv("ALERT_HF_ESCALATE_DELTA", raising=False)
    monkeypatch.delenv("ALERT_HF_ESCALATE_PCT", raising=False)


def _user(db, username: str) -> User:
    user = User(username=username, hashed_password=hash_password(PASSWORD))
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _context_token(db, user: User, token: str) -> None:
    db.add(WechatContextToken(user_id=user.id, context_token=token))
    db.commit()


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


def _events(db, user_id: int) -> list[AlertEvent]:
    return list(
        db.scalars(
            select(AlertEvent)
            .where(AlertEvent.user_id == user_id)
            .order_by(AlertEvent.id.asc())
        ).all()
    )


def _seed(db, username: str, token: str, hf: float):
    """Create one open position whose first reading fires a normal alert."""
    user = _user(db, username)
    _context_token(db, user, token)
    written = upsert_positions(db, user.id, [_lending(hf)])
    db.commit()
    sender = MockWechatClient()
    first = evaluate_alerts(db, user.id, now=NOW, client=sender)
    assert first.fired == 1
    assert first.sent == 1
    return user, written[0], sender


def _set_hf(db, position, hf: float) -> None:
    position.details = {"health_factor": hf}
    db.commit()


def _second_eval(db, user: User, sender: MockWechatClient):
    return evaluate_alerts(db, user.id, now=NOW + timedelta(minutes=10), client=sender)


def test_absolute_drop_inside_cooldown_escalates(db, monkeypatch: pytest.MonkeyPatch) -> None:
    # Isolate the absolute criterion; the relative one is covered separately.
    monkeypatch.setenv("ALERT_HF_ESCALATE_PCT", "0")
    user, position, sender = _seed(db, "p36-abs-big", "ctx-p36-abs-big", 1.45)
    _set_hf(db, position, 1.33)

    second = _second_eval(db, user, sender)
    assert second.fired == 1
    assert second.sent == 1
    events = _events(db, user.id)
    assert [row.level for row in events] == ["warn", "warn"]
    assert events[-1].event_kind == "hf"
    assert float(events[-1].hf) == pytest.approx(1.33)
    assert "继续下降" in sender.sent[-1]["text"]


def test_small_drop_inside_cooldown_is_suppressed(db) -> None:
    user, position, sender = _seed(db, "p36-small", "ctx-p36-small", 1.40)
    _set_hf(db, position, 1.35)

    second = _second_eval(db, user, sender)
    assert second.fired == 0
    assert second.suppressed == 1
    assert len(_events(db, user.id)) == 1
    assert len(sender.sent) == 1


@pytest.mark.parametrize(
    ("previous_hf", "current_hf", "expected_fired"),
    [
        # abs drop 0.09 < 0.10, relative drop 8.04% >= 8% -> escalate.
        (1.12, 1.03, True),
        # abs drop 0.04 and relative drop 3.6% -> suppressed.
        (1.12, 1.08, False),
    ],
)
def test_relative_drop_can_escalate_alone(
    db, previous_hf: float, current_hf: float, expected_fired: bool
) -> None:
    user, position, sender = _seed(
        db, f"p36-rel-{current_hf}", f"ctx-p36-rel-{current_hf}", previous_hf
    )
    _set_hf(db, position, current_hf)

    second = _second_eval(db, user, sender)
    if expected_fired:
        assert second.fired == 1
        assert second.sent == 1
        assert "继续下降" in sender.sent[-1]["text"]
        assert len(_events(db, user.id)) == 2
    else:
        assert second.fired == 0
        assert second.suppressed == 1
        assert len(_events(db, user.id)) == 1
        assert len(sender.sent) == 1


def test_pct_env_override(db, monkeypatch: pytest.MonkeyPatch) -> None:
    # 1.12 -> 1.06: abs 0.06 < 0.10, relative 5.36% >= 5% only via env.
    monkeypatch.setenv("ALERT_HF_ESCALATE_PCT", "5")
    user, position, sender = _seed(db, "p36-pct-env", "ctx-p36-pct-env", 1.12)
    _set_hf(db, position, 1.06)

    second = _second_eval(db, user, sender)
    assert second.fired == 1
    assert second.sent == 1
    assert "继续下降" in sender.sent[-1]["text"]


def test_zero_delta_keeps_relative_criterion(db, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALERT_HF_ESCALATE_DELTA", "0")
    user, position, sender = _seed(db, "p36-delta-zero", "ctx-p36-delta-zero", 1.12)
    _set_hf(db, position, 1.03)

    second = _second_eval(db, user, sender)
    assert second.fired == 1
    assert second.sent == 1
    assert "继续下降" in sender.sent[-1]["text"]


def test_both_thresholds_zero_disables_escalation(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALERT_HF_ESCALATE_DELTA", "0")
    monkeypatch.setenv("ALERT_HF_ESCALATE_PCT", "0")
    user, position, sender = _seed(db, "p36-both-zero", "ctx-p36-both-zero", 1.12)
    _set_hf(db, position, 1.03)

    second = _second_eval(db, user, sender)
    assert second.fired == 0
    assert second.suppressed == 1
    assert len(_events(db, user.id)) == 1
    assert len(sender.sent) == 1


def test_negative_env_falls_back_to_defaults(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Negative values are invalid and must fall back to 0.10 / 8, not disable.
    monkeypatch.setenv("ALERT_HF_ESCALATE_DELTA", "-1")
    monkeypatch.setenv("ALERT_HF_ESCALATE_PCT", "-1")
    user, position, sender = _seed(db, "p36-negative", "ctx-p36-negative", 1.45)
    _set_hf(db, position, 1.33)

    second = _second_eval(db, user, sender)
    assert second.fired == 1
    assert second.sent == 1
    assert "继续下降" in sender.sent[-1]["text"]
