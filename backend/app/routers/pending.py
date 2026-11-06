from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..address_labels import UNKNOWN_LABEL, get_cached_label
from ..auth import get_current_user
from ..db import get_db
from ..integrations.wechat.service import confirm_pending_item
from ..models import PendingConfirmation, User
from ..nature import (
    NATURE_LABELS,
    NatureContext,
    build_context,
    classify_exchange_internal,
    classify_nature,
)
from ..schemas import ConfirmIn, PendingOut

router = APIRouter(prefix="/pending", tags=["pending"])


def _internal_transfer_wording(text: str | None) -> str | None:
    """Read-time wording for a row already proven to be an internal move."""
    if not text:
        return text
    corrected = text.replace("提现", "内部划转").replace("充值", "内部划转")
    return (
        corrected.replace("→withdraw", "→transfer")
        .replace("→deposit", "→transfer")
    )


def _serialize_pending(
    row: PendingConfirmation,
    ctx: NatureContext,
    db: Session | None = None,
) -> PendingOut:
    """Attach read-only nature/safety/explain without mutating the ORM row."""
    result = classify_nature(row, ctx)
    # Inflows are not an outflow risk, so classify_nature stops at "unknown".
    # A historical Roll In row still needs the same read-time wording fix.
    if result.nature == "unknown":
        internal = classify_exchange_internal(row)
        if internal is not None:
            result = internal
    payload = PendingOut.model_validate(row)
    # Old rows may only carry the address in meta; when the sync/backfill has
    # already cached a label, expose it even if the row's own label is stale.
    if db is not None and payload.counterparty_address and payload.counterparty_chain:
        stale = not payload.counterparty_label or payload.counterparty_label in {
            UNKNOWN_LABEL,
            "未识别合约",
        }
        if stale or payload.counterparty_label_source in {None, "fallback"}:
            cached = get_cached_label(
                db, payload.counterparty_chain, payload.counterparty_address
            )
            if cached is not None:
                payload = payload.model_copy(
                    update={
                        "counterparty_label": cached.label,
                        "counterparty_kind": cached.kind,
                        "counterparty_label_source": cached.source,
                        "counterparty_chain": cached.chain,
                        "counterparty_address": cached.address,
                    }
                )
    title = payload.title
    confidence = payload.suggested_confidence
    # Old rows were attributed before the provider's Roll/Earn/transfer type was
    # honoured: they still say withdraw/deposit with high confidence.  Nature is
    # already proven read-only, so correct the outward-facing wording without
    # touching the stored row.
    if (
        result.nature == "exchange_internal"
        and (payload.suggested_category or "").strip().casefold()
        in {"withdraw", "deposit"}
    ):
        payload = payload.model_copy(
            update={
                "suggested_category": "transfer",
                "title": _internal_transfer_wording(payload.title),
                "suggested_reason": _internal_transfer_wording(
                    payload.suggested_reason
                ),
            }
        )
        title = payload.title
        confidence = payload.suggested_confidence
    # Historical rows are never rewritten.  A low-confidence outflow title that
    # still says "提现" is corrected while reading, matching the new wording.
    if (
        title
        and confidence is not None
        and float(confidence) < 0.8
        and (payload.suggested_category or "") == "withdraw"
    ):
        # Prefer what read-time nature already proved (protocol deposit, swap,
        # …); only genuinely unexplained outflows use the generic wording.
        replacement = NATURE_LABELS.get(result.nature or "", "待核实的链上转出")
        if result.nature in {None, "unknown", "test_data"}:
            replacement = "待核实的链上转出"
        title = title.replace("提现", replacement)
    return payload.model_copy(
        update={
            "title": title,
            "nature": result.nature,
            "safety": result.safety,
            "explain": result.explain,
        }
    )


@router.get("", response_model=list[PendingOut])
def list_pending(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[PendingOut]:
    ctx = build_context(db, current_user.id)
    rows = list(
        db.scalars(
            select(PendingConfirmation)
            .where(PendingConfirmation.user_id == current_user.id)
            .order_by(PendingConfirmation.created_at.desc())
        ).all()
    )
    return [_serialize_pending(row, ctx, db) for row in rows]


@router.post("/{pending_id}/confirm", response_model=PendingOut)
def confirm_pending(
    pending_id: int,
    payload: ConfirmIn | None = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PendingConfirmation:
    """Confirm a row. Body is optional: phase-1 rows confirm with no body and no ledger write.

    Phase-2 rows (those carrying a suggestion or an explicit category) write one
    LedgerEntry. Repeating confirm is idempotent and does not insert a second row.
    """
    requested = (payload.category if payload is not None else None) or None
    row = confirm_pending_item(db, current_user.id, pending_id, requested)
    return _serialize_pending(row, build_context(db, current_user.id), db)
