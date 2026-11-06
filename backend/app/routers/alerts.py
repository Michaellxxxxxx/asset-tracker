"""Health-factor alert thresholds. Evaluation lives in app.alerts."""

from __future__ import annotations

import math
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import get_current_admin, get_current_user
from ..db import get_db
from ..models import AlertEvent, AlertRule, SyncReconcileLog, User, WechatContextToken
from ..schemas import AlertEventOut, AlertRuleIn, AlertRuleOut

router = APIRouter(tags=["alerts"])


def _validate_rule_payload(payload: AlertRuleIn) -> None:
    """A10: reject an impossible band with an explicit Chinese 422.

    ``danger_hf < warn_hf`` is strict: equal lines make a danger row
    impossible to distinguish from a warn row. Both lines must be finite and
    positive; a negative cooldown would put the clock in the future.
    """
    warn = payload.warn_hf
    danger = payload.danger_hf
    if not math.isfinite(warn) or not math.isfinite(danger):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="warn_hf 与 danger_hf 必须是有限数值",
        )
    if warn <= 0 or danger <= 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "warn_hf 与 danger_hf 必须大于 0"
                f"（当前 warn_hf={warn:g}，danger_hf={danger:g}）"
            ),
        )
    if danger >= warn:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "danger_hf 必须小于 warn_hf"
                f"（当前 danger_hf={danger:g}，warn_hf={warn:g}）"
            ),
        )
    if payload.cooldown_min < 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"cooldown_min 不能为负数（当前 {payload.cooldown_min}）",
        )


def _default_rule(db: Session, user_id: int) -> AlertRule:
    existing = db.scalar(
        select(AlertRule).where(AlertRule.user_id == user_id, AlertRule.protocol.is_(None))
    )
    if existing is not None:
        return existing
    row = AlertRule(user_id=user_id)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.get("/alerts/rules", response_model=list[AlertRuleOut])
def list_alert_rules(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[AlertRule]:
    _default_rule(db, current_user.id)
    return list(
        db.scalars(
            select(AlertRule).where(AlertRule.user_id == current_user.id).order_by(AlertRule.id.asc())
        ).all()
    )


@router.put("/alerts/rules", response_model=AlertRuleOut)
def upsert_alert_rule(
    payload: AlertRuleIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AlertRule:
    _validate_rule_payload(payload)
    stmt = select(AlertRule).where(AlertRule.user_id == current_user.id)
    if payload.protocol is None:
        stmt = stmt.where(AlertRule.protocol.is_(None))
    else:
        stmt = stmt.where(AlertRule.protocol == payload.protocol)
    row = db.scalar(stmt)
    if row is None:
        row = AlertRule(user_id=current_user.id, protocol=payload.protocol)
        db.add(row)
    row.warn_hf = Decimal(str(payload.warn_hf))
    row.danger_hf = Decimal(str(payload.danger_hf))
    row.enabled = payload.enabled
    row.cooldown_min = payload.cooldown_min
    db.commit()
    db.refresh(row)
    return row


@router.delete("/alerts/rules", status_code=status.HTTP_204_NO_CONTENT)
def delete_alert_rule(
    protocol: str | None = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """p8e2: remove one protocol override so it falls back to the global rule.

    The global row is the fallback itself and deliberately cannot be deleted.
    Deleting a protocol row that is already gone is a no-op (204), which keeps
    the settings action idempotent if two tabs race.
    """
    key = (protocol or "").strip().lower()
    if not key or key == "global":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="全局规则不可删除",
        )
    row = db.scalar(
        select(AlertRule).where(
            AlertRule.user_id == current_user.id,
            AlertRule.protocol == key,
        )
    )
    if row is not None:
        db.delete(row)
        db.commit()
    return None


@router.get("/alerts/health")
def alert_health(
    _admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> dict[str, int | str]:
    """A9: read-only admin view of users who cannot receive WeChat pushes.

    Only counts are exposed; no context token, username, or message text leaves
    this endpoint. A blank token is treated the same as a missing row.
    """
    total = int(db.scalar(select(func.count(User.id))) or 0)
    token_users = select(WechatContextToken.user_id).where(
        func.trim(WechatContextToken.context_token) != ""
    )
    with_token = int(
        db.scalar(select(func.count(User.id)).where(User.id.in_(token_users))) or 0
    )
    return {
        "status": "ok",
        "users_total": total,
        "users_with_token": with_token,
        "users_without_wechat_token": total - with_token,
    }


def _alert_source_kind(source: str) -> str:
    return "stale" if source.strip().lower().startswith("stale:") else "sync"


def _event_kind(row: AlertEvent) -> str | None:
    """p9e: explicit ``event_kind`` wins; legacy rows fall back read-only.

    Rows written before p9e have the column NULL. A6 used a negative ``hf``
    with a position, A7 a negative ``hf`` without one, and health-factor rows a
    non-negative ``hf``. Data-source notices keep ``hf`` NULL and stay kindless.
    This only classifies the API response; it never writes the inferred value.
    """
    if row.event_kind:
        return row.event_kind
    if row.hf is None:
        return None
    if row.hf < 0:
        return "liq_distance" if row.position_id is not None else "drawdown"
    return "hf"


def _alert_event_sources(db: Session, user_id: int) -> dict[int, str]:
    """Join A4 source identity back onto ``alert_events``.

    ``evaluate_data_freshness`` intentionally keeps the source on the reconcile
    log (``data_freshness.fired/recovered/tracked_sources``) so the queue can
    rewrite ``channel_result`` without losing it. Read the recent logs and build
    event_id -> source. ``from_event_id`` also names the stale event being
    recovered, which keeps the last stale row readable after a recovery.
    """
    rows = db.scalars(
        select(SyncReconcileLog)
        .where(SyncReconcileLog.user_id == user_id)
        .order_by(SyncReconcileLog.ts.desc(), SyncReconcileLog.id.desc())
        .limit(200)
    ).all()
    sources: dict[int, str] = {}

    def add(event_id: object, source: object) -> None:
        try:
            integer = int(event_id)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return
        text = str(source or "").strip()
        if integer > 0 and text and integer not in sources:
            sources[integer] = text

    for row in rows:
        detail = row.detail if isinstance(row.detail, dict) else {}
        fresh = detail.get("data_freshness")
        if not isinstance(fresh, dict):
            continue
        for key in ("fired", "recovered"):
            entries = fresh.get(key)
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                add(entry.get("event_id"), entry.get("source"))
                add(entry.get("from_event_id"), entry.get("source"))
        tracked = fresh.get("tracked_sources")
        if isinstance(tracked, dict):
            for source, entry in tracked.items():
                if isinstance(entry, dict):
                    add(entry.get("event_id"), source)
    return sources


@router.get("/alerts/events", response_model=list[AlertEventOut])
def list_alert_events(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[AlertEventOut]:
    rows = list(
        db.scalars(
            select(AlertEvent)
            .where(AlertEvent.user_id == current_user.id)
            .order_by(AlertEvent.created_at.desc(), AlertEvent.id.desc())
        ).all()
    )
    sources = _alert_event_sources(db, current_user.id)
    out: list[AlertEventOut] = []
    for row in rows:
        item = AlertEventOut.model_validate(row)
        item.event_kind = _event_kind(row)
        source = sources.get(row.id)
        if source:
            item.source_name = source
            item.source_kind = _alert_source_kind(source)  # type: ignore[assignment]
        out.append(item)
    return out
