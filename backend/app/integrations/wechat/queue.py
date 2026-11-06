"""Enqueue pending rows and flush them once a context_token exists.

P7a delivery policy
-------------------
* Every failed attempt is scheduled with exponential backoff
  (30s × 2^(n-1), capped at 1h) through ``next_attempt_at``; the flush worker
  only picks rows whose gate has elapsed, so a bad token no longer burns five
  attempts in 2.5 minutes.
* A missing/expired token (``ret=-2`` and friends) is *not* a terminal error.
  The row stays ``queued`` and waits for the next inbound message to refresh
  the token. A non-token error still fails after ``_MAX_ATTEMPTS``; a token
  error only fails after ``_MAX_TOKEN_WAIT`` / ``_MAX_TOKEN_ATTEMPTS``.
* A token refresh resets backoff for rows that are still ``queued``. Rows that
  already reached ``failed`` (the six pre-P7a pushes) are never touched.
* ``ok=True`` is not by itself a delivery: a row becomes ``sent`` only when
  ``ret`` is 0/None and the error text is not a token error. iLink can return
  HTTP 200 with ``ret=-2``; such an attempt must stay queued/failed.

P11e send dedup and unverified ret=-2
-------------------------------------
* ``flush_user_queue`` leases one row at a time with an atomic ``UPDATE``
  (``next_attempt_at`` = now + 120s, ``last_error`` = ``sending:<uuid>``),
  commits, sends, then records the outcome. A second concurrent flush skips
  the active lease, so the same row cannot be sent twice. A crash leaves the
  row queued until the lease expires.
* A ret=-2 on an HTTP-ok response (``ok=True`` or 2xx ``http_status``) with no
  textual token marker is *unverified*: it may already have been delivered.
  After ``WECHAT_UNVERIFIED_MAX_ATTEMPTS`` (default 3) the row becomes
  ``failed`` with an ``unverified:`` prefix and is never auto-resent. Token
  errors keep the P7a 60-attempt/7-day policy.
* ret/errcode/errmsg are stored in ``last_error`` and logged only after
  ``redact_send_text``: URLs, token/key parameters and secret-looking blobs
  are removed and the result is capped at 200 characters.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from ...models import (
    AlertEvent,
    PendingConfirmation,
    WechatContextToken,
    WechatPushQueue,
)
from ...nature import (
    NatureContext,
    build_context,
    classify_nature,
    format_nature_tag,
)
from .client import (
    SendResult,
    WechatClient,
    get_wechat_client,
    redact_send_text,
)
from .health import (
    HEALTH_ALERT_EVENT_KIND,
    HEALTH_ALERT_HOLD,
    HEALTH_ALERT_HOLD_UNTIL,
    record_send_failure,
    record_send_success,
    send_blocked_for,
)
from .status import token_status
from ...config import settings as _settings

logger = logging.getLogger(__name__)

_MAX_ATTEMPTS = 5
_ERROR_LIMIT = 200


def _positive_int_env(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(int(os.getenv(name, str(default))), minimum)
    except (TypeError, ValueError):
        return default


def _non_negative_float_env(name: str, default: float) -> float:
    try:
        return max(float(os.getenv(name, str(default))), 0.0)
    except (TypeError, ValueError):
        return default


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().casefold() in ("1", "true", "yes", "on")


# p11e: a ret=-2 answer on an HTTP-ok response (ok=True or 2xx http_status) is
# "unverified" -- iLink might have delivered it. Never retry it forever:
# N attempts, then terminal failed with an ``unverified:`` last_error for a
# human to check. Token-class errors keep the P7a policy (60 attempts / 7 days).
UNVERIFIED_MAX_ATTEMPTS = _positive_int_env("WECHAT_UNVERIFIED_MAX_ATTEMPTS", 3)
UNVERIFIED_PREFIX = "unverified:"

# p12a: iLink answers ret=-2 errmsg="prepare failed" when it could not prepare
# the outbound message at all (normally an expired/invalidated context_token).
# That is a definitive pre-delivery failure, not the p11e "maybe delivered"
# shape. It is terminal after the first attempt: waiting 60 attempts / 7 days
# only produces a retry storm, and a later token refresh creates fresh alerts.
# Set WECHAT_TERMINAL_PREPARE_FAILED=0 to fall back to the P7a token-wait
# policy (the row then stays queued until a token refresh).
TERMINAL_PREPARE_FAILED = _bool_env("WECHAT_TERMINAL_PREPARE_FAILED", True)
PREPARE_FAILED_PREFIX = "prepare_failed:"
_PREPARE_FAILED_MARKERS = ("prepare failed", "prepare_failed")

# Lease/claim. A single send is capped at 20s in client.py; the lease must be
# comfortably longer so a healthy sender cannot expire mid-flight, while a
# crashed process releases the row after at most this long.
SEND_CLAIM_LEASE_SECONDS = _positive_int_env("WECHAT_SEND_LEASE_SECONDS", 120, minimum=30)
SEND_CLAIM_LEASE = timedelta(seconds=SEND_CLAIM_LEASE_SECONDS)
_CLAIM_PREFIX = "sending:"

# p11f: token-refresh/inbound flushes must not dump a backlog in one burst.
# At most FLUSH_BATCH_LIMIT real send attempts per flush call, at least
# FLUSH_MIN_INTERVAL_SECONDS between them, and at most
# FLUSH_PER_MINUTE_LIMIT across all flushes in a rolling minute.  Rows over a
# limit stay queued and are picked up by the next flush.
FLUSH_BATCH_LIMIT = _positive_int_env("WECHAT_FLUSH_BATCH_LIMIT", 5)
FLUSH_MIN_INTERVAL_SECONDS = _non_negative_float_env(
    "WECHAT_FLUSH_MIN_INTERVAL_SECONDS", 1.0
)
FLUSH_PER_MINUTE_LIMIT = _positive_int_env("WECHAT_FLUSH_PER_MINUTE_LIMIT", 20)
_RATE_WINDOW_SECONDS = 60.0

# P7a backoff and waiting limits. All values are constants so tests and ops can
# reason about the worst case: 30s, 60s, 120s, ... 3600s.
_BACKOFF_BASE_SECONDS = 30
_BACKOFF_MAX_SECONDS = 60 * 60
_TOKEN_WAIT_LIMIT = timedelta(days=7)
_TOKEN_ATTEMPT_LIMIT = 60

# iLink returns ret=-2 for a missing/expired context token. The string markers
# cover older callers that only kept the error text.
_TOKEN_ERROR_MARKERS = (
    "ret=-2",
    "ret -2",
    "context_token",
    "context token",
    "token expired",
    "token invalid",
    "invalid token",
)

# Textual token markers only. A bare ret=-2 on an HTTP-ok response is treated
# as *unverified*, not as a token refresh request, because the message may in
# fact have been delivered. "prepare failed" is excluded: see the p12a note
# above, it is a definitive pre-delivery token failure.
_TOKEN_TEXT_MARKERS = (
    "context_token",
    "context token",
    "token expired",
    "token invalid",
    "invalid token",
)

# Predicate of the partial unique index in models.WechatPushQueue. Keeping the
# exact text here lets SQLite/Postgres infer the index for ON CONFLICT.
_ACTIVE_ALERT_INDEX_WHERE = text("alert_event_id IS NOT NULL AND status <> 'failed'")


@dataclass(frozen=True)
class FlushStats:
    sent: int = 0
    skipped: int = 0
    failed: int = 0


@dataclass(frozen=True)
class AlertReconcileStats:
    """Rows inspected and historical ``alert_events`` rows corrected."""

    scanned: int = 0
    updated: int = 0


_SHANGHAI = ZoneInfo("Asia/Shanghai")


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)



def _min_id_clause():
    """p29: rows with id <= WECHAT_FLUSH_MIN_ID are never claimed or reset."""
    return WechatPushQueue.id > int(_settings.wechat_flush_min_id or 0)

def backoff_delay(attempts: int) -> timedelta:
    """Delay after the ``attempts``-th failure: 30s, 60s, ... capped at 1h."""
    exponent = max(int(attempts) - 1, 0)
    seconds = _BACKOFF_BASE_SECONDS * (2 ** min(exponent, 20))
    return timedelta(seconds=min(seconds, _BACKOFF_MAX_SECONDS))


@dataclass
class _SendRateState:
    """Per-engine pacing state (reset automatically when the test engine dies)."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    last_sent_at: float | None = None
    recent: deque[float] = field(default_factory=deque)


_RATE_FALLBACK = _SendRateState()


def _rate_state(db: Session) -> _SendRateState:
    # Engine has no ``.info`` dict; attach our state to the Engine object so
    # separate test engines (and app processes) never share a rate window.
    bind = db.get_bind()
    engine = getattr(bind, "engine", bind)
    state = getattr(engine, "_wechat_send_rate", None)
    if state is None:
        state = _SendRateState()
        try:
            setattr(engine, "_wechat_send_rate", state)
        except (AttributeError, TypeError):  # pragma: no cover - defensive
            return _RATE_FALLBACK
    return state


def _acquire_send_slot(
    db: Session,
    *,
    clock,
    sleep,
    min_interval: float,
    per_minute: int,
) -> bool:
    """Reserve one paced send slot, or return False when this minute is full.

    Holding the lock during the interval sleep serialises concurrent flushes, so
    two callers cannot both send inside the same 1s window.  The minute window
    is a rolling 60s deque, independent of the ``now`` used for DB backoff.
    """
    state = _rate_state(db)
    with state.lock:
        moment = clock()
        cutoff = moment - _RATE_WINDOW_SECONDS
        while state.recent and state.recent[0] <= cutoff:
            state.recent.popleft()
        if len(state.recent) >= per_minute:
            return False
        if state.last_sent_at is not None and min_interval > 0:
            wait = min_interval - (moment - state.last_sent_at)
            if wait > 0:
                sleep(wait)
                # Advance at least to the promised spacing even if a fake clock
                # does not move during the injected sleep.
                moment = max(clock(), state.last_sent_at + min_interval)
                cutoff = moment - _RATE_WINDOW_SECONDS
                while state.recent and state.recent[0] <= cutoff:
                    state.recent.popleft()
                if len(state.recent) >= per_minute:
                    return False
        state.last_sent_at = moment
        state.recent.append(moment)
        return True


def is_token_error(error: str | None, ret: int | None = None) -> bool:
    """True when the failure is a stale/missing context token, not a bad message."""
    if ret == -2:
        return True
    text = (error or "").casefold()
    return any(marker in text for marker in _TOKEN_ERROR_MARKERS)


def is_prepare_failed(result: SendResult) -> bool:
    """True for iLink's ret=-2 ``errmsg=prepare failed`` shape (p12a).

    This is a definitive failure before delivery: the platform never prepared
    the message, so it cannot have reached the user. It is not the p11e
    "delivered but unconfirmed" case and must not burn the unverified retries.
    """
    if getattr(result, "ret", None) != -2:
        return False
    text = " ".join(
        str(value or "")
        for value in (
            getattr(result, "error", None),
            getattr(result, "errmsg", None),
        )
    ).casefold()
    return any(marker in text for marker in _PREPARE_FAILED_MARKERS)


def is_unverified_ret_minus_two(result: SendResult) -> bool:
    """True for the p11e shape: HTTP-ok response but iLink answered ret=-2.

    "HTTP ok" means either ``ok=True`` or a 2xx ``http_status``. The live
    client returns ``ok=False`` for any non-zero application code even when the
    HTTP response was 200, so checking ``ok`` alone would never catch a real
    delivered-but-unconfirmed send.

    A bare ``ok=False`` + ret=-2 with no HTTP status is the legacy/P7a
    stale-token shape; it keeps the token-wait policy. Any textual token marker
    does the same. Unverified rows get N bounded attempts, then a terminal
    ``unverified:`` failure that is never auto-resent.
    """
    if getattr(result, "ret", None) != -2:
        return False
    http_status = getattr(result, "http_status", None)
    http_ok = bool(getattr(result, "ok", False)) or (
        isinstance(http_status, int) and 200 <= http_status < 300
    )
    if not http_ok:
        return False
    text = " ".join(
        str(value or "")
        for value in (getattr(result, "error", None), getattr(result, "errmsg", None))
    ).casefold()
    if any(marker in text for marker in _PREPARE_FAILED_MARKERS):
        return False
    return not any(marker in text for marker in _TOKEN_TEXT_MARKERS)


def is_delivery_success(result: SendResult) -> bool:
    """True only for an explicit success response.

    ``ok`` alone is not enough: iLink can answer HTTP 200 with ``ret=-2``
    (stale context token), and an empty body used to be parsed as ``ok=True``.
    A row may be marked ``sent`` only when ``ret`` is 0/None *and* the text is
    not a token error; everything else must go through the retry/backoff path.
    """
    if not result.ok:
        return False
    ret = getattr(result, "ret", None)
    if ret not in (None, 0):
        return False
    return not is_token_error(getattr(result, "error", None), ret)


def _send_failure_text(error: str | None, ret: int | None) -> str:
    """Keep a truthful reason when ``ok`` was true but ``ret`` was not."""
    if error:
        return redact_send_text(error, _ERROR_LIMIT)
    if ret not in (None, 0):
        return f"ret={ret}"[:_ERROR_LIMIT]
    return "send_failed"


def _send_detail(result: SendResult, error: str | None = None) -> str:
    """Short, redacted ret/errcode/errmsg summary for ``last_error`` and logs.

    Legacy callers (and most tests) only return ``error``; keep that exact text
    so the existing ret=-2 strings do not change. When the client supplies a
    real errcode/errmsg, include all three fields, truncated to 200 chars.
    """
    ret = getattr(result, "ret", None)
    errcode = getattr(result, "errcode", None)
    errmsg = getattr(result, "errmsg", None)
    if errcode is None and not errmsg:
        return _send_failure_text(error, ret)
    parts: list[str] = []
    if ret is not None:
        parts.append(f"ret={ret}")
    if errcode is not None:
        parts.append(f"errcode={errcode}")
    if errmsg:
        parts.append(f"errmsg={redact_send_text(errmsg, _ERROR_LIMIT)}")
    return redact_send_text(" ".join(parts) or "send_failed", _ERROR_LIMIT)


def format_occurred_at(value: datetime | None) -> str:
    """Chain/execution time in Beijing time. Naive values are treated as UTC."""
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(_SHANGHAI).strftime("%Y-%m-%d %H:%M")


def format_pending_message(
    pending: PendingConfirmation,
    ctx: NatureContext | None = None,
) -> str:
    """Short Chinese notice. No address, txid, or secret.

    ``ctx`` is optional so callers that only have a detached row keep the old
    signature.  Queueing passes the user's wallet context so self-transfers
    are recognised; classification itself never writes.
    """
    amount = pending.amount_usdt
    if isinstance(amount, Decimal):
        amount_text = f"{amount:.2f} USDT"
    elif amount is None:
        amount_text = "金额未知"
    else:
        amount_text = f"{amount} USDT"
    category = pending.suggested_category or "待选"
    title = (pending.title or "待确认")[:80]
    occurred = format_occurred_at(pending.ts)
    base = f"待确认 #{pending.id} {title}，金额 {amount_text}，建议 {category}。"
    result = classify_nature(pending, ctx)
    nature_tag = format_nature_tag(result)
    if nature_tag:
        base = f"{base}{nature_tag}。"
    reply = (
        f"该笔性质待核实，回复「确认入账 #{pending.id}」二次确认后才入账；请先核对原因或打开网页处理。"
        if result.safety == "review"
        else f"回复「确认 #{pending.id} 类别」归档。"
    )
    lines = [base]
    if occurred:
        lines.append(f"发生时间：{occurred}（北京时间）")
    # Only enriched rows carry an explain; label-only rows must keep the
    # pre-phase-4 wording. describe_pending is read-only and never does I/O.
    if getattr(pending, "explain", None):
        from ..chain.enrich import describe_pending

        description = describe_pending(pending)
        if description:
            lines.append(f"说明：{description}")
    if len(lines) == 1:
        # Keep the phase-1 one-line wording when there is no chain time and
        # no enrichment line.
        return f"{base}{reply}"
    lines.append(reply)
    return "\n".join(lines)


def enqueue_pending(
    db: Session,
    pending: PendingConfirmation,
    ctx: NatureContext | None = None,
) -> WechatPushQueue:
    if ctx is None:
        ctx = build_context(db, pending.user_id)
    row = WechatPushQueue(
        pending_id=pending.id,
        user_id=pending.user_id,
        text=format_pending_message(pending, ctx),
        status="queued",
        attempts=0,
    )
    db.add(row)
    db.flush()
    return row


def enqueue_pending_ids(db: Session, pending_ids: list[int]) -> list[WechatPushQueue]:
    if not pending_ids:
        return []
    rows = list(
        db.scalars(select(PendingConfirmation).where(PendingConfirmation.id.in_(pending_ids))).all()
    )
    contexts: dict[int, NatureContext] = {}
    queued: list[WechatPushQueue] = []
    for pending in rows:
        ctx = contexts.get(pending.user_id)
        if ctx is None:
            ctx = build_context(db, pending.user_id)
            contexts[pending.user_id] = ctx
        queued.append(enqueue_pending(db, pending, ctx))
    return queued


def enqueue_alert(
    db: Session,
    *,
    alert_event_id: int,
    user_id: int,
    text: str,
) -> WechatPushQueue:
    """Queue one alert notice for retry. Deduped by alert_event_id.

    Alert rows have no pending_id; the queue's pending_id became nullable for
    this path. A failed or no-context push therefore survives the sync that
    created it and is retried on the next flush.

    The insert is an ``ON CONFLICT DO NOTHING`` on the partial unique index
    (active rows only), so two sync workers racing on the same event cannot
    create two pushes. Historical ``failed`` rows are outside that index and
    are never touched, deleted, or reset; re-enqueueing an event that already
    has failed history returns that terminal row instead of reviving it.
    """
    values = {
        "pending_id": None,
        "alert_event_id": alert_event_id,
        "user_id": user_id,
        "text": text,
        "status": "queued",
        "attempts": 0,
        "next_attempt_at": None,
    }
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        stmt = (
            pg_insert(WechatPushQueue)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=["alert_event_id"],
                index_where=_ACTIVE_ALERT_INDEX_WHERE,
            )
        )
    elif dialect == "sqlite":
        stmt = (
            sqlite_insert(WechatPushQueue)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=["alert_event_id"],
                index_where=_ACTIVE_ALERT_INDEX_WHERE,
            )
        )
    else:  # pragma: no cover - only Postgres and SQLite are supported
        row = WechatPushQueue(**values)
        db.add(row)
        db.flush()
        return row

    inserted = db.execute(stmt).rowcount == 1
    db.flush()
    # The insert either won or lost the race; both paths return the one active
    # row that is now visible in this transaction.
    row = db.scalar(
        select(WechatPushQueue)
        .where(
            WechatPushQueue.alert_event_id == alert_event_id,
            WechatPushQueue.status != "failed",
        )
        .order_by(WechatPushQueue.id.asc())
    )
    if row is not None:
        # If this event already has terminal failed history, honour it: remove
        # only the active row we just inserted (never a pre-existing sent row,
        # never the failed row) and return the failed row. This is what keeps
        # the six old pushes from being resent if an old event is enqueued
        # again.
        if inserted:
            failed = db.scalar(
                select(WechatPushQueue)
                .where(
                    WechatPushQueue.alert_event_id == alert_event_id,
                    WechatPushQueue.status == "failed",
                )
                .order_by(WechatPushQueue.id.asc())
            )
            if failed is not None:
                db.delete(row)
                db.flush()
                return failed
        return row
    # Extremely defensive: a concurrent actor changed the winning row to
    # failed between the insert and this read. Return the historical row
    # instead of inserting a duplicate or raising.
    existing = db.scalar(
        select(WechatPushQueue)
        .where(WechatPushQueue.alert_event_id == alert_event_id)
        .order_by(WechatPushQueue.id.asc())
    )
    if existing is not None:
        return existing
    raise RuntimeError("alert queue insert conflicted but no row is visible")


def reset_retry_backoff(db: Session, user_id: int) -> int:
    """Token refresh: make queued rows due now again.

    ``failed`` rows are deliberately excluded: the six pre-P7a pushes stay
    failed and must never be reset or resent. p11e adds one more exclusion:

    * a row carrying an active ``sending:`` lease is being handled by another
      flush right now; clearing its lease would let this thread send it again.

    A queued row already marked ``unverified:`` is also skipped defensively.
    Terminal ``unverified:`` rows are ``failed`` and therefore outside the
    query anyway; test_p11e locks both cases in.
    """
    rows = list(
        db.scalars(
            select(WechatPushQueue).where(
                WechatPushQueue.user_id == user_id,
                WechatPushQueue.status == "queued",
                _min_id_clause(),
            )
        ).all()
    )
    reset = 0
    for row in rows:
        error = row.last_error or ""
        if error.startswith((_CLAIM_PREFIX, UNVERIFIED_PREFIX)):
            continue
        row.attempts = 0
        row.next_attempt_at = None
        row.last_error = None
        reset += 1
    return reset


def release_health_alerts(db: Session) -> int:
    """Make held W1 notices due again after the send path recovers.

    Only queued rows carrying the exact hold marker are touched.  A ``failed``
    row (including every historical p7a failure) is outside the query and is
    never revived.  ``reset_retry_backoff`` also releases these rows on a token
    refresh; this hook covers recovery by a real send success.
    """
    rows = list(
        db.scalars(
            select(WechatPushQueue).where(
                WechatPushQueue.status == "queued",
                WechatPushQueue.last_error == HEALTH_ALERT_HOLD,
                _min_id_clause(),
            )
        ).all()
    )
    for row in rows:
        row.next_attempt_at = None
    return len(rows)


def enqueue_and_try_flush(db: Session, pending_ids: list[int], client: WechatClient | None = None) -> FlushStats:
    """Queue rows then try one flush. Safe to call after the pending commit.

    A failure here must not undo the pending rows the caller already committed.
    """
    queued = enqueue_pending_ids(db, pending_ids)
    db.commit()
    if not queued:
        return FlushStats()
    user_ids = {row.user_id for row in queued}
    total = FlushStats()
    for user_id in user_ids:
        stats = flush_user_queue(db, user_id, client=client)
        total = FlushStats(
            sent=total.sent + stats.sent,
            skipped=total.skipped + stats.skipped,
            failed=total.failed + stats.failed,
        )
    return total


def _result_prefix(value: str | None) -> str:
    """Keep the reconcile-failure identity used by the settings history."""
    return "reconcile_fail:" if (value or "").startswith("reconcile") else ""


def _unprefixed_result(value: str | None) -> str:
    text = (value or "").strip()
    for prefix in ("reconcile_fail:", "reconcile:"):
        if text.startswith(prefix):
            return text[len(prefix):]
    return text


def _is_queued_result(value: str | None) -> bool:
    text = _unprefixed_result(value)
    return text == "queued" or text.startswith("queued:")


def _is_failed_result(value: str | None) -> bool:
    text = _unprefixed_result(value)
    return text == "failed" or text.startswith("failed:")


def _failure_reason(error: str | None) -> str:
    reason = (error or "send_failed").strip()
    if reason.casefold().startswith("failed:"):
        reason = reason.split(":", 1)[1].strip()
    return reason or "send_failed"


def _sync_alert_event(
    db: Session,
    row: WechatPushQueue,
    *,
    status: str,
    error: str | None,
    moment: datetime,
    mock: bool = False,
) -> None:
    """Mirror the queue outcome onto ``alert_events``.

    queued  -> channel_result=queued:<reason>, sent_at stays NULL
    sent    -> channel_result=sent|mock, sent_at=delivery time
    failed  -> channel_result=failed:<reason>, sent_at stays NULL

    A retryable queued update never downgrades an already-corrected failure.
    The developer's queue row is still queued (and may later succeed), but the
    history must not flip back to "queued" after a terminal failure has been
    recorded. A real success still overwrites it with sent/mock.
    """
    if row.alert_event_id is None:
        return
    event = db.get(AlertEvent, row.alert_event_id)
    if event is None:  # pragma: no cover - FK cascade normally removes the row
        return
    prefix = _result_prefix(event.channel_result)
    if status == "sent":
        event.channel_result = f"{prefix}{'mock' if mock else 'sent'}"
        event.sent_at = moment
    elif status == "failed":
        event.channel_result = f"{prefix}failed:{_failure_reason(error)}"[:_ERROR_LIMIT]
        # Never keep a stale delivered timestamp on a failed attempt.
        event.sent_at = None
    else:
        if _is_failed_result(event.channel_result):
            # A historical failure was already reconciled. A later success
            # replaces it through the ``sent`` branch above; a retry must not
            # make the settings page look queued again.
            event.sent_at = None
            return
        event.channel_result = f"{prefix}queued:{_failure_reason(error)}"[:_ERROR_LIMIT]


def _terminal_failure_reason(
    db: Session,
    row: WechatPushQueue,
    moment: datetime,
    *,
    include_stale_token: bool,
) -> str | None:
    """Return the failure reason when a queue row is terminal for history.

    ``failed`` is always terminal.  Legacy P7a-era rows can still be ``queued``
    after the old five-attempt cap; those are treated as exhausted.  A token
    error is only terminal for this read-only reconciliation when the cached
    token has also been stale for ``TOKEN_STALE_AFTER`` -- the queue itself may
    still be waiting for the documented P7a seven-day window, but the event is
    already undeliverable and must not keep claiming ``queued``.
    """
    if row.status == "failed":
        return row.last_error or "send_failed"
    if not include_stale_token or row.sent_at is not None:
        return None
    if not row.last_error or row.attempts < _MAX_ATTEMPTS:
        return None
    if not is_token_error(row.last_error):
        # A non-token error should have failed at _MAX_ATTEMPTS. Older rows may
        # have missed that branch; history is still truthful as failed.
        return row.last_error
    if token_status(db, user_id=row.user_id, now=moment).stale:
        return row.last_error
    return None


def reconcile_alert_event_results(
    db: Session,
    *,
    user_id: int | None = None,
    now: datetime | None = None,
    include_stale_token: bool = True,
    commit: bool = True,
) -> AlertReconcileStats:
    """One-off/idempotent repair of ``alert_events.channel_result``.

    Reads terminal queue rows and rewrites only history rows that still say
    ``queued:*``.  Queue rows are never updated, deleted, reset, or resent:
    ``status``, ``attempts``, ``text``/payload, ``last_error``, and ``sent_at``
    are strictly read-only inputs.  Running it twice updates nothing.
    """
    moment = _aware(now or datetime.now(timezone.utc))
    stmt = select(WechatPushQueue).where(
        WechatPushQueue.alert_event_id.is_not(None),
        WechatPushQueue.status.in_(("queued", "failed")),
    )
    if user_id is not None:
        stmt = stmt.where(WechatPushQueue.user_id == user_id)
    rows = list(db.scalars(stmt.order_by(WechatPushQueue.id.asc())).all())

    updated = 0
    corrected_events: set[int] = set()
    for row in rows:
        if row.alert_event_id in corrected_events:
            continue
        event = db.get(AlertEvent, row.alert_event_id)
        if event is None or not _is_queued_result(event.channel_result):
            continue
        reason = _terminal_failure_reason(
            db, row, moment, include_stale_token=include_stale_token
        )
        if reason is None:
            continue
        event.channel_result = (
            f"{_result_prefix(event.channel_result)}failed:{_failure_reason(reason)}"
        )[:_ERROR_LIMIT]
        event.sent_at = None
        corrected_events.add(row.alert_event_id)
        updated += 1

    if updated and commit:
        db.commit()
    return AlertReconcileStats(scanned=len(rows), updated=updated)


def _claim_next_row(
    db: Session, user_id: int, moment: datetime
) -> WechatPushQueue | None:
    """Atomically lease the oldest due row for one send.

    The claim is a single ``UPDATE ... WHERE id = (SELECT ... LIMIT 1) AND
    status='queued' AND (next_attempt_at IS NULL OR next_attempt_at <= now)``
    committed *before* the network call. Two concurrent flushes therefore
    cannot lease the same row:

    * Postgres re-checks the outer predicate after the competing UPDATE
      commits (READ COMMITTED), so the loser matches zero rows.
    * SQLite serialises writers; the loser sees the committed future lease.

    No new status/column is used. ``next_attempt_at`` carries the lease expiry
    and ``last_error`` carries a unique ``sending:<uuid>`` marker so this
    session can find exactly the rows it leased. A crash simply leaves the row
    queued until the lease expires. ``reset_retry_backoff`` skips the marker.
    """
    due = or_(
        WechatPushQueue.next_attempt_at.is_(None),
        WechatPushQueue.next_attempt_at <= moment,
    )
    # Base the lease on real wall-clock time as well as the caller's ``now``:
    # a long flush keeps a fixed ``moment`` while later rows are claimed
    # minutes after start. Using only ``moment`` would hand those rows an
    # already-expired lease before their send even begins.
    lease_base = max(moment, _aware(datetime.now(timezone.utc)))
    lease_until = lease_base + SEND_CLAIM_LEASE
    marker = f"{_CLAIM_PREFIX}{uuid.uuid4().hex}"
    next_id = (
        select(WechatPushQueue.id)
        .where(
            WechatPushQueue.user_id == user_id,
            WechatPushQueue.status == "queued",
            due,
            _min_id_clause(),
        )
        .order_by(WechatPushQueue.id.asc())
        .limit(1)
        .scalar_subquery()
    )
    claimed = db.execute(
        update(WechatPushQueue)
        .where(
            WechatPushQueue.id == next_id,
            WechatPushQueue.user_id == user_id,
            WechatPushQueue.status == "queued",
            due,
            _min_id_clause(),
        )
        .values(next_attempt_at=lease_until, last_error=marker)
        .execution_options(synchronize_session=False)
    )
    db.commit()
    if claimed.rowcount != 1:
        return None
    # ``populate_existing`` is required: the core UPDATE above deliberately
    # bypasses the ORM, so the identity-map object still holds the pre-claim
    # last_error/next_attempt_at. Without this refresh, writing last_error=None
    # after a successful send would be a no-op and the lease marker would stay
    # in the database.
    return db.scalar(
        select(WechatPushQueue)
        .where(
            WechatPushQueue.user_id == user_id,
            WechatPushQueue.status == "queued",
            WechatPushQueue.last_error == marker,
        )
        .execution_options(populate_existing=True)
    )


def flush_user_queue(
    db: Session,
    user_id: int,
    client: WechatClient | None = None,
    now: datetime | None = None,
    *,
    clock=None,
    sleep=None,
) -> FlushStats:
    moment = _aware(now or datetime.now(timezone.utc))
    clock = clock or time.monotonic
    sleep = sleep or time.sleep
    token_row = db.get(WechatContextToken, user_id)
    context = token_row.context_token if token_row is not None else ""
    sender = client or get_wechat_client()
    mock_mode = type(sender).__name__ == "MockWechatClient"

    sent = 0
    failed = 0
    skipped = 0
    send_attempts = 0
    while send_attempts < FLUSH_BATCH_LIMIT:
        # Lease exactly one row, commit, then send. The commit releases the
        # SQLite/Postgres write lock before the 20s HTTP call, and the lease
        # keeps every other flush away from this row until we finish or crash.
        row = _claim_next_row(db, user_id, moment)
        if row is None:
            break

        # p15b: W1 notices are queued but must not be retried while the
        # owner's cached token is older than the failure.  Keep the row
        # queued and parked; a token refresh or a real send success releases
        # it.  No attempt is spent and the alert event stays queued.
        if row.alert_event_id is not None:
            event_kind = db.scalar(
                select(AlertEvent.event_kind).where(
                    AlertEvent.id == row.alert_event_id
                )
            )
            if event_kind == HEALTH_ALERT_EVENT_KIND and send_blocked_for(
                db, user_id
            ):
                row.last_error = HEALTH_ALERT_HOLD
                row.next_attempt_at = HEALTH_ALERT_HOLD_UNTIL
                db.commit()
                skipped += 1
                continue

        if not context:
            row.last_error = "no_context_token"
            if row.pending_id is not None:
                # A human is waiting on a confirmation, so keep the legacy cap.
                row.attempts += 1
                if not mock_mode and row.attempts >= _MAX_ATTEMPTS:
                    row.status = "failed"
                    row.next_attempt_at = None
            else:
                # An alert waits for a token without spending retries.
                row.next_attempt_at = moment + timedelta(seconds=_BACKOFF_BASE_SECONDS)
                _sync_alert_event(
                    db, row, status="queued", error="no_context_token", moment=moment
                )
            db.commit()
            skipped += 1
            continue

        if not _acquire_send_slot(
            db,
            clock=clock,
            sleep=sleep,
            min_interval=FLUSH_MIN_INTERVAL_SECONDS,
            per_minute=FLUSH_PER_MINUTE_LIMIT,
        ):
            # The rolling-minute cap is full. Release the lease we just took
            # without spending an attempt; the next flush will pick it up.
            row.next_attempt_at = moment
            row.last_error = None
            db.commit()
            break
        send_attempts += 1

        if mock_mode:
            logger.info(
                "wechat mock send row=%s pending_id=%s alert_event_id=%s",
                row.id,
                row.pending_id,
                row.alert_event_id,
            )
            try:
                sender.send_message(context, row.text)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "wechat mock send failed row=%s detail=%s",
                    row.id,
                    redact_send_text(f"{type(exc).__name__}: {exc}", _ERROR_LIMIT),
                )
                row.attempts += 1
                row.last_error = redact_send_text(
                    f"{type(exc).__name__}: {exc}", _ERROR_LIMIT
                )
                row.next_attempt_at = moment + backoff_delay(row.attempts)
                db.commit()
                failed += 1
                continue
            row.status = "sent"
            row.last_error = "mock"
            row.sent_at = moment
            row.next_attempt_at = None
            _sync_alert_event(
                db, row, status="sent", error=None, moment=moment, mock=True
            )
            db.commit()
            sent += 1
            continue

        try:
            result = sender.send_message(context, row.text)
        except Exception as exc:  # noqa: BLE001 - one bad send must not abort the batch
            error = redact_send_text(
                f"{type(exc).__name__}: {exc}", _ERROR_LIMIT
            )
            detail = error
            ret = None
            unverified = False
            prepare_failed = False
            token_error = is_token_error(error, ret)
            logger.warning(
                "wechat send failed row=%s pending_id=%s alert_event_id=%s ok=0 detail=%s",
                row.id,
                row.pending_id,
                row.alert_event_id,
                detail,
            )
        else:
            ret = getattr(result, "ret", None)
            if is_delivery_success(result):
                logger.info(
                    "wechat send ok row=%s pending_id=%s alert_event_id=%s ret=%s",
                    row.id,
                    row.pending_id,
                    row.alert_event_id,
                    ret,
                )
                row.attempts += 1
                row.status = "sent"
                row.last_error = None
                row.sent_at = moment
                row.next_attempt_at = None
                record_send_success(db, at=moment)
                _sync_alert_event(
                    db, row, status="sent", error=None, moment=moment
                )
                db.commit()
                sent += 1
                continue
            # Any HTTP-ok ret=-2 (or token error) is not a delivery.
            error = _send_failure_text(getattr(result, "error", None), ret)
            detail = _send_detail(result, error)
            prepare_failed = is_prepare_failed(result)
            unverified = is_unverified_ret_minus_two(result) and not prepare_failed
            token_error = (is_token_error(error, ret) or prepare_failed) and not unverified
            logger.warning(
                "wechat send failed row=%s pending_id=%s alert_event_id=%s ok=%s "
                "unverified=%s token=%s prepare_failed=%s detail=%s",
                row.id,
                row.pending_id,
                row.alert_event_id,
                getattr(result, "ok", False),
                unverified,
                token_error,
                prepare_failed,
                detail,
            )

        row.attempts += 1
        row.last_error = detail

        if prepare_failed and TERMINAL_PREPARE_FAILED:
            # p12a: iLink could not prepare the message, so nothing was
            # delivered. Retrying the same expired context_token only creates
            # a failed-row backlog; terminate on the first attempt. A later
            # token refresh generates fresh alerts with a valid token.
            row.status = "failed"
            row.next_attempt_at = None
            row.last_error = f"{PREPARE_FAILED_PREFIX} {detail}"[:_ERROR_LIMIT]
            _sync_alert_event(
                db, row, status="failed", error=row.last_error, moment=moment
            )
        elif unverified:
            # HTTP succeeded but iLink answered ret=-2: the message may have
            # arrived, so bounded retries only, then terminal for a human.
            if row.attempts >= UNVERIFIED_MAX_ATTEMPTS:
                row.status = "failed"
                row.next_attempt_at = None
                row.last_error = f"{UNVERIFIED_PREFIX} {detail}"[:_ERROR_LIMIT]
                _sync_alert_event(
                    db, row, status="failed", error=row.last_error, moment=moment
                )
            else:
                row.status = "queued"
                row.next_attempt_at = moment + backoff_delay(row.attempts)
                _sync_alert_event(
                    db, row, status="queued", error=detail, moment=moment
                )
        elif token_error:
            waited = moment - _aware(row.created_at)
            if row.attempts >= _TOKEN_ATTEMPT_LIMIT or waited >= _TOKEN_WAIT_LIMIT:
                row.status = "failed"
                row.next_attempt_at = None
                row.last_error = f"token_wait_exhausted: {detail}"[:_ERROR_LIMIT]
                _sync_alert_event(
                    db, row, status="failed", error=row.last_error, moment=moment
                )
            else:
                # Stay pending: the next inbound message refreshes the token.
                row.status = "queued"
                row.next_attempt_at = moment + backoff_delay(row.attempts)
                _sync_alert_event(
                    db, row, status="queued", error=detail, moment=moment
                )
        elif row.attempts >= _MAX_ATTEMPTS:
            row.status = "failed"
            row.next_attempt_at = None
            _sync_alert_event(db, row, status="failed", error=detail, moment=moment)
        else:
            row.status = "queued"
            row.next_attempt_at = moment + backoff_delay(row.attempts)
            _sync_alert_event(db, row, status="queued", error=detail, moment=moment)
        record_send_failure(
            db,
            row.last_error,
            ret=ret,
            token_error=token_error,
            at=moment,
            user_id=row.user_id,
        )
        db.commit()
        failed += 1

    return FlushStats(sent=sent, skipped=skipped, failed=failed)


def flush_all_queued(db: Session, client: WechatClient | None = None) -> FlushStats:
    # Repair historical queued:* rows before the worker selects users. This is
    # read-only with respect to wechat_push_queue and never revives a failed row.
    reconcile_alert_event_results(db)
    user_ids = list(
        db.scalars(
            select(WechatPushQueue.user_id)
            .where(WechatPushQueue.status == "queued", _min_id_clause())
            .distinct()
        ).all()
    )
    total = FlushStats()
    for user_id in user_ids:
        stats = flush_user_queue(db, user_id, client=client)
        total = FlushStats(
            sent=total.sent + stats.sent,
            skipped=total.skipped + stats.skipped,
            failed=total.failed + stats.failed,
        )
    return total
