"""Confirm a pending row. Shared by the HTTP router and the WeChat poller."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import EXTERNAL_FLOW_KINDS, LEDGER_KINDS, LedgerEntry, PendingConfirmation

# Suggestion-only categories that still need a concrete ledger kind when a human
# explicitly confirms them.  "chain_out" must not become an invalid ledger row.
_LEDGER_ALIASES = {"chain_out": "withdraw"}


def confirm_pending_item(
    db: Session,
    user_id: int,
    pending_id: int,
    category: str | None = None,
) -> PendingConfirmation:
    """Same rules as POST /pending/{id}/confirm. Idempotent; one ledger row."""
    item = db.scalar(
        select(PendingConfirmation).where(
            PendingConfirmation.id == pending_id,
            PendingConfirmation.user_id == user_id,
        )
    )
    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="待确认事项不存在",
        )

    requested = category or None
    if (
        requested is not None
        and requested not in LEDGER_KINDS
        and requested not in _LEDGER_ALIASES
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="不支持的分类",
        )

    if item.status == "confirmed":
        return item

    chosen = requested or item.suggested_category
    if chosen is not None:
        item.final_category = chosen
        ledger_kind = _LEDGER_ALIASES.get(chosen, chosen)
        if item.ledger_entry_id is None and ledger_kind in LEDGER_KINDS:
            entry = ledger_from_pending(item, ledger_kind)
            db.add(entry)
            db.flush()
            item.ledger_entry_id = entry.id

    item.status = "confirmed"
    item.confirmed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(item)
    return item


def ledger_from_pending(item: PendingConfirmation, category: str) -> LedgerEntry:
    amount = item.amount_usdt if item.amount_usdt is not None else Decimal("0")
    if category in EXTERNAL_FLOW_KINDS and category != "deposit":
        if category == "withdraw":
            amount = -abs(Decimal(amount))
        elif category == "transfer":
            amount = Decimal(amount) if item.direction == "in" else -abs(Decimal(amount))
        else:
            amount = abs(Decimal(amount))
    else:
        amount = abs(Decimal(amount))
    when = item.ts or item.created_at or datetime.now(timezone.utc)
    note = item.suggested_reason or item.title
    return LedgerEntry(
        user_id=item.user_id,
        ts=when,
        amount_usdt=amount,
        kind=category,
        note=(note or "")[:255],
    )
