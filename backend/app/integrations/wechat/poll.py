"""Inbound ClawBot messages: refresh context, flush the queue, maybe confirm.

p11f cursor safety
------------------
The getupdates cursor is committed only after every message in the batch was
handled (and its reply sent).  A mid-batch failure leaves the old cursor in
place, so the next long-poll retries the same batch.  A small progress key in
the existing ``wechat_poll_state`` table records how many messages of the
current ``(cursor, new_buf)`` batch already completed; the retry resumes there
instead of replaying the whole batch.  All exception paths roll back and retire
a broken connection so an INTRANS/PendingRollbackError session never returns to
the shared worker pool.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...config import settings
from ...db import release_failed_session
from ...models import PendingConfirmation, User, WechatContextToken, WechatPollState
from ...nature import build_context, classify_nature
from .client import InboundMessage, WechatClient, redact_send_text
from .commands import parse_command
from .health import record_send_failure, record_send_success
from .queue import (
    flush_user_queue,
    is_delivery_success,
    is_token_error,
    reset_retry_backoff,
)
from .service import confirm_pending_item

logger = logging.getLogger(__name__)

_BUF_KEY = "get_updates_buf"
_PROGRESS_KEY = "get_updates_progress"
_ACK = "已连通，之后有待确认会推给你。"


def upsert_context_token(db: Session, user_id: int, context_token: str) -> bool:
    """Store the token; return True only when its value actually changed.

    A duplicate/replayed inbound message carries the same token.  It still
    proves the token is alive, so ``updated_at`` is refreshed, but it must not
    trigger another token-refresh flush (p11f W4 idempotency).
    """
    if not context_token:
        return False
    now = datetime.now(timezone.utc)
    row = db.get(WechatContextToken, user_id)
    if row is None:
        db.add(
            WechatContextToken(
                user_id=user_id,
                context_token=context_token,
                updated_at=now,
            )
        )
        db.commit()
        return True
    changed = row.context_token != context_token
    row.context_token = context_token
    row.updated_at = now
    db.commit()
    return changed


def resolve_default_user_id(db: Session) -> int | None:
    if settings.wechat_default_user_id is not None:
        return settings.wechat_default_user_id
    username = settings.wechat_default_username or "admin"
    user = db.scalar(select(User).where(User.username == username))
    return user.id if user is not None else None


def handle_inbound_text(
    db: Session,
    user_id: int,
    text: str,
    context_token: str,
    client: WechatClient | None = None,
) -> str:
    if context_token:
        refreshed = upsert_context_token(db, user_id, context_token)
        if refreshed:
            # A refresh clears the stale-token wall: queued notices become due
            # now. Failed rows (the six pre-P7a pushes) are deliberately not
            # reset. A replayed message with the same token must not flush again.
            reset_retry_backoff(db, user_id)
            db.commit()
            flush_user_queue(db, user_id, client=client)

    parsed = parse_command(text)
    if parsed.kind in {"help", "unknown"}:
        return f"{_ACK}\n{parsed.reply}" if parsed.reply else _ACK
    assert parsed.pending_id is not None
    item = db.scalar(
        select(PendingConfirmation).where(
            PendingConfirmation.id == parsed.pending_id,
            PendingConfirmation.user_id == user_id,
        )
    )
    if item is None:
        return f"没有找到 #{parsed.pending_id}，或它不属于当前用户。"
    if item.status != "confirmed":
        result = classify_nature(item, build_context(db, user_id))
        if result.safety == "review" and not parsed.strong:
            # Never one-click a review/unknown row into the ledger.  The human
            # must see the reason and reply with the explicit phrase.
            return (
                f"#{item.id} 尚未入账：{result.explain}。\n"
                f"这是待核实记录，请先核对；如确认无误，回复「确认入账 #{item.id}」二次确认。"
            )
    try:
        item = confirm_pending_item(db, user_id, parsed.pending_id, parsed.category)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else "无法确认"
        if exc.status_code == 404:
            return f"没有找到 #{parsed.pending_id}，或它不属于当前用户。"
        return detail
    category = item.final_category or parsed.category or "未分类"
    return f"已确认 #{item.id} → {category}"


def _load_progress(db: Session) -> dict:
    row = db.get(WechatPollState, _PROGRESS_KEY)
    if row is None or not row.value:
        return {}
    try:
        data = json.loads(row.value)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_progress(
    db: Session,
    cursor: str,
    new_buf: str,
    next_index: int,
) -> None:
    payload = json.dumps(
        {"cursor": cursor, "new_buf": new_buf, "next_index": next_index},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    row = db.get(WechatPollState, _PROGRESS_KEY)
    if row is None:
        db.add(WechatPollState(key=_PROGRESS_KEY, value=payload))
    else:
        row.value = payload
    db.commit()


def _clear_progress(db: Session) -> None:
    row = db.get(WechatPollState, _PROGRESS_KEY)
    if row is not None:
        db.delete(row)


def process_updates(
    db: Session, client: WechatClient, state: WechatPollState | None = None
) -> list[str]:
    """Long-poll once; advance the cursor only after the whole batch succeeds."""
    cursor = state.value if state is not None else _load_buf(db)
    try:
        result = client.get_updates(cursor or "")
    except Exception:
        # The long-poll itself failed (network/timeout). Roll back the read
        # transaction started by _load_buf and retire a broken connection;
        # the cursor is untouched, so the next poll retries the same offset.
        release_failed_session(db, label="wechat poll getupdates")
        raise
    if not result.ok:
        # An error/empty getupdates response must not move the cursor forward.
        logger.warning("wechat getupdates not ok; cursor unchanged")
        return []

    user_id = resolve_default_user_id(db)
    if user_id is None:
        logger.warning("wechat inbound dropped: default user missing; cursor unchanged")
        return []

    start = 0
    progress = _load_progress(db)
    if (
        progress.get("cursor") == cursor
        and progress.get("new_buf") == result.new_buf
        and isinstance(progress.get("next_index"), int)
        and 0 <= progress["next_index"] <= len(result.msgs)
    ):
        start = progress["next_index"]

    replies: list[str] = []
    for index in range(start, len(result.msgs)):
        message = result.msgs[index]
        try:
            reply = handle_inbound_text(
                db,
                user_id,
                message.text,
                message.context_token,
                client,
            )
            if message.context_token:
                sent = client.send_message(message.context_token, reply)
                ret = getattr(sent, "ret", None)
                if is_delivery_success(sent):
                    record_send_success(db, at=datetime.now(timezone.utc))
                else:
                    error = redact_send_text(
                        getattr(sent, "error", None)
                        or (f"ret={ret}" if ret is not None else "send_failed"),
                        200,
                    )
                    record_send_failure(
                        db,
                        error,
                        ret=ret,
                        token_error=is_token_error(error, ret),
                        user_id=user_id,
                    )
                    # Persist the stale/alert state before the batch handler
                    # rolls the session back.
                    db.commit()
                    # The reply did not go out, so this message is not done.
                    # Raise into the batch handler: rollback/retire the session,
                    # keep the old cursor, and retry this message next poll.
                    raise RuntimeError(f"wechat reply send failed: {error}")
        except Exception:
            # Do not advance the cursor. Keep the completed prefix so the
            # retry resumes at this message instead of replaying the batch.
            try:
                release_failed_session(db, label="wechat poll batch")
                _save_progress(db, cursor, result.new_buf, index)
            except Exception:  # noqa: BLE001 - preserve the original exception
                logger.exception("wechat poll: failed to persist batch progress")
            raise
        _save_progress(db, cursor, result.new_buf, index + 1)
        replies.append(reply)

    # The batch is fully handled: commit the new cursor and drop the progress
    # marker in the same transaction.
    _save_buf(db, result.new_buf, clear_progress=True)
    return replies


def message_user_id(db: Session, message: InboundMessage) -> int | None:
    """Skeleton mapping. A binding table can replace this later."""
    del message
    return resolve_default_user_id(db)


def _load_buf(db: Session) -> str:
    row = db.get(WechatPollState, _BUF_KEY)
    return row.value if row is not None else ""


def _save_buf(db: Session, value: str, *, clear_progress: bool = False) -> None:
    row = db.get(WechatPollState, _BUF_KEY)
    if row is None:
        db.add(WechatPollState(key=_BUF_KEY, value=value or ""))
    else:
        row.value = value or ""
    if clear_progress:
        _clear_progress(db)
    db.commit()
