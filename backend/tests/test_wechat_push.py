"""WeChat push skeleton. Mock only — no ilink traffic."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import resolve_wechat_push_mode
from app.db import Base
from app.integrations.wechat.client import MockWechatClient
from app.integrations.wechat.commands import parse_command
from app.integrations.wechat.poll import handle_inbound_text
from app.integrations.wechat.queue import enqueue_alert, enqueue_pending, flush_user_queue
from app.logging_redact import redact_text
from app.migrate import apply_phase2_migrations
from app.models import (
    AlertEvent,
    PendingConfirmation,
    User,
    WechatContextToken,
    WechatPushQueue,
)


class _FakeLive:
    """Looks like a live client: records calls, refuses to send without being asked to fail."""

    def __init__(self, *, ok: bool = False, error: str = "ret=-2") -> None:
        self.ok = ok
        self.error = error
        self.sent: list[str] = []

    def send_message(self, context_token: str, text: str):
        from app.integrations.wechat.client import SendResult

        self.sent.append(text)
        if self.ok:
            return SendResult(ok=True, ret=0)
        return SendResult(ok=False, ret=-2, error=self.error)


def _pending(db, user_id: int, *, title: str = "币安 充值 10 USDT") -> PendingConfirmation:
    row = PendingConfirmation(
        user_id=user_id,
        title=title,
        detail="建议充值",
        amount_usdt=Decimal("10.00"),
        status="pending",
        suggested_category="deposit",
        suggested_reason="充值",
        ts=datetime(2026, 3, 1, tzinfo=timezone.utc),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_mock_enqueue_and_flush_marks_sent(db, demo_user) -> None:
    pending = _pending(db, demo_user.id)
    queued = enqueue_pending(db, pending)
    db.commit()
    assert queued.status == "queued"

    client = MockWechatClient()
    db.add(WechatContextToken(user_id=demo_user.id, context_token="ctx-demo"))
    db.commit()

    stats = flush_user_queue(db, demo_user.id, client=client)
    assert stats.sent == 1
    assert stats.failed == 0
    db.refresh(queued)
    assert queued.status == "sent"
    assert queued.last_error == "mock"
    assert queued.sent_at is not None
    assert client.sent
    assert f"#{pending.id}" in client.sent[0]["text"]
    assert "0x" not in client.sent[0]["text"]


def test_live_without_context_stays_queued(db, demo_user) -> None:
    pending = _pending(db, demo_user.id)
    queued = enqueue_pending(db, pending)
    db.commit()

    fake = _FakeLive()
    stats = flush_user_queue(db, demo_user.id, client=fake)
    assert stats.skipped == 1
    assert fake.sent == []
    db.refresh(queued)
    assert queued.status == "queued"
    assert queued.attempts == 1
    assert queued.last_error == "no_context_token"


def test_parse_confirm_and_unknown() -> None:
    parsed = parse_command("确认 #1 空投")
    assert parsed.kind == "confirm"
    assert parsed.pending_id == 1
    assert parsed.category == "airdrop"

    assert parse_command("确认 #123 airdrop").category == "airdrop"
    assert parse_command("确认 123 transfer").category == "transfer"
    assert parse_command("#123 确认 充值").category == "deposit"
    assert parse_command("123 空投").category == "airdrop"
    assert parse_command("123 提现").category == "withdraw"
    assert parse_command("8 KOL").category == "kol_reward"

    junk = parse_command("你好呀")
    assert junk.kind == "unknown"
    assert "确认" in junk.reply
    assert junk.pending_id is None


def test_handle_inbound_flushes_then_confirms(db, demo_user) -> None:
    pending = _pending(db, demo_user.id, title="空投示例")
    queued = enqueue_pending(db, pending)
    db.commit()

    client = MockWechatClient()
    reply = handle_inbound_text(
        db,
        demo_user.id,
        f"确认 #{pending.id} 空投",
        "ctx-from-user",
        client,
    )
    assert reply == f"已确认 #{pending.id} → airdrop"

    cached = db.get(WechatContextToken, demo_user.id)
    assert cached is not None
    assert cached.context_token == "ctx-from-user"

    db.refresh(queued)
    assert queued.status == "sent"
    assert client.sent

    db.refresh(pending)
    assert pending.status == "confirmed"
    assert pending.final_category == "airdrop"

    missing = handle_inbound_text(db, demo_user.id, "确认 #999999 充值", "ctx-from-user", client)
    assert "没有找到" in missing


def test_resolve_wechat_push_mode() -> None:
    assert resolve_wechat_push_mode("auto", "") == "mock"
    assert resolve_wechat_push_mode("auto", "tok_present") == "live"
    assert resolve_wechat_push_mode("mock", "tok_present") == "mock"
    assert resolve_wechat_push_mode("live", "") == "live"


def test_redact_wechat_bot_token(monkeypatch) -> None:
    import app.logging_redact as redact_mod

    monkeypatch.setattr(
        redact_mod,
        "_configured_secrets",
        lambda: ["wechat-bot-token-example"],
    )
    cleaned = redact_text("push failed token=wechat-bot-token-example")
    assert "wechat-bot-token-example" not in cleaned
    assert "***" in cleaned


def test_sync_enqueues_wechat_row(db, demo_user) -> None:
    from app.sync.service import run_sync

    since = datetime(2026, 1, 1, tzinfo=timezone.utc)
    result = run_sync(demo_user.id, "mock", db=db, since=since)
    assert result.enqueued > 0
    queued = list(
        db.scalars(
            select(WechatPushQueue).where(
                WechatPushQueue.user_id == demo_user.id,
                WechatPushQueue.pending_id.is_not(None),
            )
        ).all()
    )
    assert len(queued) == result.enqueued
    assert all(row.status == "queued" for row in queued)
    assert all(row.last_error == "no_context_token" for row in queued)


def _review_pending(db, user_id: int) -> PendingConfirmation:
    row = PendingConfirmation(
        user_id=user_id,
        title="zerion 待核实的链上转出 5 USDC",
        detail="链上流出，缺少 operation_type/对手方证据；方向 out",
        amount_usdt=Decimal("5.00"),
        status="pending",
        source="zerion",
        external_id="0x" + "ab" * 32,
        direction="out",
        asset="USDC",
        amount_raw=Decimal("5.00"),
        suggested_category="withdraw",
        suggested_confidence=Decimal("0.55"),
        suggested_reason="链上流出，缺少 operation_type/对手方证据 → 待核实的链上转出",
        ts=datetime(2026, 3, 1, tzinfo=timezone.utc),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_review_row_requires_second_confirmation(db, demo_user) -> None:
    from app.integrations.wechat.service import confirm_pending_item  # noqa: F401

    row = _review_pending(db, demo_user.id)
    client = MockWechatClient()
    reply = handle_inbound_text(db, demo_user.id, f"确认 #{row.id} 提现", "", client)
    assert "尚未入账" in reply
    assert "确认入账" in reply
    db.refresh(row)
    assert row.status == "pending"
    assert row.ledger_entry_id is None


def test_review_row_confirms_after_strong_phrase(db, demo_user) -> None:
    from app.models import LedgerEntry

    row = _review_pending(db, demo_user.id)
    reply = handle_inbound_text(db, demo_user.id, f"确认入账 #{row.id}", "")
    assert reply == f"已确认 #{row.id} → withdraw"
    db.refresh(row)
    assert row.status == "confirmed"
    ledger = db.get(LedgerEntry, row.ledger_entry_id)
    assert ledger is not None and ledger.kind == "withdraw"
    assert ledger.amount_usdt < 0


def test_safe_row_still_confirms_in_one_message(db, demo_user) -> None:
    row = _pending(db, demo_user.id, title="币安 充值 10 USDT")
    reply = handle_inbound_text(db, demo_user.id, f"确认 #{row.id} 充值", "")
    assert reply == f"已确认 #{row.id} → deposit"
    db.refresh(row)
    assert row.status == "confirmed"


def test_parse_inbound_item_list_and_skip_bot_echo():
    from app.integrations.wechat.client import _parse_inbound

    msg = _parse_inbound(
        {
            "from_user_id": "u1",
            "message_type": 1,
            "context_token": "ctx",
            "item_list": [{"type": 1, "text_item": {"text": "你好"}}],
        }
    )
    assert msg is not None and msg.text == "你好" and msg.context_token == "ctx"
    assert _parse_inbound({"message_type": 2, "item_list": []}) is None


def test_greeting_gets_connected_ack(db, demo_user):
    from app.integrations.wechat.poll import handle_inbound_text

    reply = handle_inbound_text(db, demo_user.id, "你好", "")
    assert "已连通" in reply


def test_format_pending_message_includes_beijing_occurred_time() -> None:
    from app.integrations.wechat.queue import format_occurred_at, format_pending_message

    row = PendingConfirmation(
        id=77,
        user_id=1,
        title="币安 充值 10 USDT",
        detail="建议充值",
        amount_usdt=Decimal("10.00"),
        status="pending",
        suggested_category="deposit",
        ts=datetime(2026, 1, 6, 4, 20, tzinfo=timezone.utc),
    )
    text = format_pending_message(row)
    assert "发生时间：2026-01-06 12:20（北京时间）" in text
    assert text.count("\n") == 2

    # Zone-less datetimes coming back from SQLite are interpreted as UTC.
    assert format_occurred_at(datetime(2026, 1, 6, 4, 20)) == "2026-01-06 12:20"
    assert format_occurred_at(None) == ""


def test_format_pending_message_omits_occurred_time_when_ts_missing() -> None:
    from app.integrations.wechat.queue import format_pending_message

    row = PendingConfirmation(
        id=78,
        user_id=1,
        title="无链上时间",
        detail="",
        amount_usdt=Decimal("1.00"),
        status="pending",
        suggested_category="other",
        ts=None,
    )
    text = format_pending_message(row)
    assert "发生时间" not in text
    assert "\n" not in text
    assert text == (
        "待确认 #78 无链上时间，金额 1.00 USDT，建议 other。"
        "性质：⚠️ 请核对（性质待确认）。"
        "该笔性质待核实，回复「确认入账 #78」二次确认后才入账；请先核对原因或打开网页处理。"
    )


def test_format_pending_message_adds_explain_line_for_enriched_row() -> None:
    from app.integrations.wechat.queue import format_pending_message

    label = "合约 · Helper（3Jane USD3 存入助手）"
    explain = f"{label} · 收到 2307.38 USD3"
    row = PendingConfirmation(
        id=79,
        user_id=1,
        title="zerion 协议存入 2666.320174 USDC",
        detail=f"{explain}；方向 out；地址 0x2a66f992bf227d2e50ef19edd21503c3c4f3f682",
        amount_usdt=Decimal("2666.32"),
        status="pending",
        suggested_category="defi_deposit",
        ts=datetime(2026, 1, 6, 4, 20, tzinfo=timezone.utc),
        meta={"counterparty_label": label, "explain": explain},
    )
    text = format_pending_message(row)
    lines = text.splitlines()
    assert lines[1] == "发生时间：2026-01-06 12:20（北京时间）"
    assert lines[2] == f"说明：{explain}"
    assert lines[3] == "回复「确认 #79 类别」归档。"
    assert "0x2a66" not in text


def test_format_pending_message_skips_explain_line_without_explain() -> None:
    from app.integrations.wechat.queue import format_pending_message

    row = PendingConfirmation(
        id=80,
        user_id=1,
        title="只有标签没有说明",
        detail="",
        amount_usdt=Decimal("5.00"),
        status="pending",
        suggested_category="defi_redeem",
        ts=datetime(2026, 1, 6, 4, 20, tzinfo=timezone.utc),
        meta={"counterparty_label": "合约 · Helper"},
    )
    text = format_pending_message(row)
    assert "说明：" not in text
    assert "合约 · Helper" not in text


def test_format_pending_message_marks_exchange_internal_safe() -> None:
    from app.integrations.wechat.queue import format_pending_message

    row = PendingConfirmation(
        id=897,
        user_id=1,
        source="binance",
        title="binance 提现 0.05 USDT",
        detail="交易所提现记录→withdraw；方向 out；原始类型 Roll Out",
        amount_usdt=Decimal("0.05"),
        status="pending",
        suggested_category="withdraw",
        ts=None,
        meta=None,
    )
    text = format_pending_message(row)
    assert "性质：✅ 已识别 · 非外流（交易所内部划转）" in text
    assert "待确认 #897" in text


def test_concurrent_alert_enqueue_creates_one_queue_row(tmp_path):
    """P8a: ON CONFLICT on the partial unique index wins the enqueue race."""
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'queue-race.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
        future=True,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        user = User(username="queue-race", hashed_password="not-used")
        db.add(user)
        db.flush()
        event = AlertEvent(
            user_id=user.id,
            level="danger",
            created_at=datetime(2026, 10, 23, tzinfo=timezone.utc),
            channel_result="skipped:no_token",
        )
        db.add(event)
        db.flush()
        user_id, event_id = user.id, event.id
        db.commit()

    barrier = threading.Barrier(6)

    def _enqueue(index: int) -> int:
        with Session(engine) as db:
            barrier.wait(timeout=15)
            row = enqueue_alert(
                db,
                alert_event_id=event_id,
                user_id=user_id,
                text=f"race {index}",
            )
            db.commit()
            return row.id

    with ThreadPoolExecutor(max_workers=6) as pool:
        row_ids = list(pool.map(_enqueue, range(6)))

    assert len(set(row_ids)) == 1
    with Session(engine) as db:
        rows = list(
            db.scalars(
                select(WechatPushQueue).where(
                    WechatPushQueue.alert_event_id == event_id
                )
            ).all()
        )
        assert len(rows) == 1
        assert rows[0].status == "queued"
        assert rows[0].attempts == 0
    engine.dispose()


def test_queue_migration_dedupes_active_rows_and_keeps_failed_history(tmp_path):
    """P8a: pre-check duplicates, keep one active row, never touch failed."""
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'old-queue.db'}", future=True
    )
    Base.metadata.create_all(engine)
    # Simulate the old schema: the unique index did not exist, so two syncs
    # could insert duplicates for one alert event.
    with engine.begin() as conn:
        conn.execute(text("DROP INDEX uq_wechat_push_queue_alert_event_active"))

    with Session(engine) as db:
        user = User(username="old-queue", hashed_password="not-used")
        db.add(user)
        db.flush()
        first = AlertEvent(
            user_id=user.id,
            level="danger",
            created_at=datetime(2026, 10, 23, tzinfo=timezone.utc),
            channel_result="queued:test",
        )
        second = AlertEvent(
            user_id=user.id,
            level="warn",
            created_at=datetime(2026, 10, 23, tzinfo=timezone.utc),
            channel_result="sent",
        )
        db.add_all([first, second])
        db.flush()
        rows = [
            WechatPushQueue(
                alert_event_id=first.id, user_id=user.id, text="keep queued", status="queued"
            ),
            WechatPushQueue(
                alert_event_id=first.id, user_id=user.id, text="drop queued", status="queued"
            ),
            WechatPushQueue(
                alert_event_id=first.id,
                user_id=user.id,
                text="failed history",
                status="failed",
                attempts=5,
                last_error="ret=-2",
            ),
            WechatPushQueue(
                alert_event_id=second.id, user_id=user.id, text="keep sent", status="sent"
            ),
            WechatPushQueue(
                alert_event_id=second.id, user_id=user.id, text="drop queued 2", status="queued"
            ),
            WechatPushQueue(
                alert_event_id=second.id,
                user_id=user.id,
                text="failed history 2",
                status="failed",
                attempts=5,
                last_error="ret=-2",
            ),
        ]
        db.add_all(rows)
        db.commit()
        user_id = user.id
        failed_ids = [row.id for row in rows if row.status == "failed"]
        keep_ids = [rows[0].id, rows[3].id]
        first_id, second_id = first.id, second.id

    apply_phase2_migrations(engine)
    apply_phase2_migrations(engine)  # idempotent

    with Session(engine) as db:
        remaining = list(
            db.scalars(select(WechatPushQueue).order_by(WechatPushQueue.id.asc())).all()
        )
        assert {row.id for row in remaining} == set(keep_ids + failed_ids)
        failed = [row for row in remaining if row.status == "failed"]
        assert {row.id for row in failed} == set(failed_ids)
        assert all(row.attempts == 5 and row.last_error == "ret=-2" for row in failed)
        assert sum(row.alert_event_id == first_id for row in remaining) == 2
        assert sum(row.alert_event_id == second_id for row in remaining) == 2

    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO wechat_push_queue "
                    "(alert_event_id, user_id, text, status, attempts) "
                    "VALUES (:event_id, :user_id, 'late duplicate', 'queued', 0)"
                ),
                {"event_id": first_id, "user_id": user_id},
            )
    except IntegrityError:
        pass
    else:  # pragma: no cover - must be rejected by the partial unique index
        raise AssertionError("duplicate active queue row was accepted")
    engine.dispose()


def test_enqueue_does_not_revive_terminal_failed_row(db):
    """The six old failed pushes stay terminal even if their event is re-enqueued."""
    user = User(username="queue-failed-terminal", hashed_password="not-used")
    db.add(user)
    db.flush()
    event = AlertEvent(
        user_id=user.id,
        level="danger",
        created_at=datetime(2026, 10, 23, tzinfo=timezone.utc),
        channel_result="failed:ret=-2",
    )
    db.add(event)
    db.flush()
    failed = WechatPushQueue(
        alert_event_id=event.id,
        user_id=user.id,
        text="six old pushes",
        status="failed",
        attempts=5,
        last_error="ret=-2",
    )
    db.add(failed)
    db.commit()

    returned = enqueue_alert(
        db, alert_event_id=event.id, user_id=user.id, text="do not resend"
    )
    assert returned.id == failed.id
    db.commit()
    rows = list(
        db.scalars(
            select(WechatPushQueue).where(WechatPushQueue.alert_event_id == event.id)
        ).all()
    )
    assert len(rows) == 1
    assert rows[0].id == failed.id
    assert rows[0].status == "failed"
    assert rows[0].attempts == 5


def test_enqueue_keeps_preexisting_sent_row_when_failed_history_exists(db):
    """A sent row is audit history; a failed sibling must not make us delete it."""
    user = User(username="queue-sent-and-failed", hashed_password="not-used")
    db.add(user)
    db.flush()
    event = AlertEvent(
        user_id=user.id,
        level="warn",
        created_at=datetime(2026, 10, 23, tzinfo=timezone.utc),
        channel_result="sent",
        sent_at=datetime(2026, 10, 23, tzinfo=timezone.utc),
    )
    db.add(event)
    db.flush()
    sent = WechatPushQueue(
        alert_event_id=event.id,
        user_id=user.id,
        text="already delivered",
        status="sent",
        attempts=1,
    )
    failed = WechatPushQueue(
        alert_event_id=event.id,
        user_id=user.id,
        text="old failure",
        status="failed",
        attempts=5,
        last_error="ret=-2",
    )
    db.add_all([sent, failed])
    db.commit()

    returned = enqueue_alert(
        db, alert_event_id=event.id, user_id=user.id, text="do not duplicate"
    )
    assert returned.id == sent.id
    db.commit()
    rows = list(
        db.scalars(
            select(WechatPushQueue).where(WechatPushQueue.alert_event_id == event.id)
        ).all()
    )
    assert {row.id for row in rows} == {sent.id, failed.id}
