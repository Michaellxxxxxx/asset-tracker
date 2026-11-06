"""p38: degraded ``hf_stale`` notices for carried-forward lending rows.

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
    """Every test starts from the documented escalation defaults."""
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


def _lending(
    hf: float | None,
    *,
    debt: str = "100",
    stale: bool = False,
    hf_unavailable: bool = False,
    protocol: str = "aave",
    market: str = "mkt",
) -> dict:
    details: dict = {}
    if hf is not None:
        details["health_factor"] = hf
    if stale:
        details["stale"] = True
        details["stale_since"] = NOW.isoformat()
    if hf_unavailable:
        details["hf_unavailable"] = True
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
        "debt_usdt": debt,
        "details": details,
    }


def _events(db, user_id: int) -> list[AlertEvent]:
    return list(
        db.scalars(
            select(AlertEvent)
            .where(AlertEvent.user_id == user_id)
            .order_by(AlertEvent.id.asc())
        ).all()
    )


def _seed(db, username: str, token: str, row: dict):
    user = _user(db, username)
    _context_token(db, user, token)
    written = upsert_positions(db, user.id, [row])
    db.commit()
    return user, written[0], MockWechatClient()


def _set(db, position, row: dict) -> None:
    position.details = row["details"]
    position.debt_usdt = row["debt_usdt"]
    db.commit()


def _eval(db, user: User, sender: MockWechatClient, delta: timedelta):
    return evaluate_alerts(db, user.id, now=NOW + delta, client=sender)


def test_stale_debt_with_old_danger_fires_hf_stale(db) -> None:
    user, _position, sender = _seed(
        db, "p38-danger", "ctx-p38-danger", _lending(1.05, stale=True)
    )

    result = _eval(db, user, sender, timedelta(0))

    assert result.fired == 1
    assert result.sent == 1
    events = _events(db, user.id)
    assert len(events) == 1
    assert events[0].level == "danger"
    assert events[0].event_kind == "hf_stale"
    assert float(events[0].hf) == pytest.approx(1.05)
    assert "过期" in sender.sent[-1]["text"]
    assert "沿用旧值 1.05" in sender.sent[-1]["text"]


def test_second_evaluation_inside_cooldown_is_suppressed(db) -> None:
    user, _position, sender = _seed(
        db, "p38-cool", "ctx-p38-cool", _lending(1.05, stale=True)
    )
    _eval(db, user, sender, timedelta(0))

    second = _eval(db, user, sender, timedelta(minutes=10))

    assert second.fired == 0
    assert second.suppressed == 1
    assert len(_events(db, user.id)) == 1
    assert len(sender.sent) == 1


def test_still_stale_after_cooldown_fires_again(db) -> None:
    user, _position, sender = _seed(
        db, "p38-again", "ctx-p38-again", _lending(1.05, stale=True)
    )
    _eval(db, user, sender, timedelta(0))

    later = _eval(db, user, sender, timedelta(hours=7))

    assert later.fired == 1
    events = _events(db, user.id)
    assert [row.event_kind for row in events] == ["hf_stale", "hf_stale"]
    assert len(sender.sent) == 2


def test_stale_safe_old_value_fires_nothing_and_no_recover(db) -> None:
    user, position, sender = _seed(db, "p38-safe", "ctx-p38-safe", _lending(1.05))
    # A real danger first, so a stale "safe" reading could wrongly recover it.
    first = _eval(db, user, sender, timedelta(0))
    assert first.fired == 1
    assert _events(db, user.id)[-1].event_kind == "hf"

    _set(db, position, _lending(2.5, stale=True))
    skipped = _eval(db, user, sender, timedelta(hours=1))

    assert skipped.fired == 0
    assert skipped.suppressed == 0
    assert [row.event_kind for row in _events(db, user.id)] == ["hf"]
    assert [row.level for row in _events(db, user.id)] == ["danger"]
    assert len(sender.sent) == 1


def test_stale_without_debt_fires_nothing(db) -> None:
    user, _position, sender = _seed(
        db, "p38-nodebt", "ctx-p38-nodebt", _lending(1.05, debt="0", stale=True)
    )

    result = _eval(db, user, sender, timedelta(0))

    assert result.fired == 0
    assert result.suppressed == 0
    assert _events(db, user.id) == []
    assert sender.sent == []


def test_hf_stale_does_not_block_real_hf_recover(db) -> None:
    user, position, sender = _seed(db, "p38-recover", "ctx-p38-recover", _lending(1.05))
    first = _eval(db, user, sender, timedelta(0))
    assert first.fired == 1
    assert _events(db, user.id)[-1].event_kind == "hf"

    # The source fails: a stale dangerous HF produces a degraded notice.
    _set(db, position, _lending(1.03, stale=True))
    stale = _eval(db, user, sender, timedelta(minutes=10))
    assert stale.fired == 1
    assert _events(db, user.id)[-1].event_kind == "hf_stale"

    # Fresh data returns safely: the stale notice must not hide the recover.
    _set(db, position, _lending(2.5))
    recovered = _eval(db, user, sender, timedelta(minutes=20))

    assert recovered.fired == 1
    events = _events(db, user.id)
    assert [row.event_kind for row in events] == ["hf", "hf_stale", "hf"]
    assert [row.level for row in events] == ["danger", "danger", "recover"]
    assert "健康度恢复" in sender.sent[-1]["text"]


def test_real_hf_escalation_still_works_inside_cooldown(db) -> None:
    user, position, sender = _seed(db, "p38-p36", "ctx-p38-p36", _lending(1.12))
    assert _eval(db, user, sender, timedelta(0)).fired == 1

    _set(db, position, _lending(1.03))
    second = _eval(db, user, sender, timedelta(minutes=10))

    assert second.fired == 1
    events = _events(db, user.id)
    assert [row.event_kind for row in events] == ["hf", "hf"]
    assert "继续下降" in sender.sent[-1]["text"]


def test_hf_stale_is_not_used_for_escalation_comparison(db) -> None:
    user, position, sender = _seed(db, "p38-iso", "ctx-p38-iso", _lending(1.12))
    assert _eval(db, user, sender, timedelta(0)).fired == 1

    _set(db, position, _lending(1.03, stale=True))
    stale = _eval(db, user, sender, timedelta(minutes=10))
    assert stale.fired == 1
    assert _events(db, user.id)[-1].event_kind == "hf_stale"

    # The comparison must be against the real 1.12 event, not the stale 1.03:
    # 8.04% relative drop escalates. Using the stale value would suppress this.
    _set(db, position, _lending(1.03))
    fresh = _eval(db, user, sender, timedelta(minutes=20))

    assert fresh.fired == 1
    events = _events(db, user.id)
    assert [row.event_kind for row in events] == ["hf", "hf_stale", "hf"]
    assert events[-1].event_kind == "hf"
    assert "继续下降" in sender.sent[-1]["text"]
