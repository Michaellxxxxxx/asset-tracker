"""Phase-4 P0 position and alert skeleton. Evaluation of rules is lane E."""

from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..db import get_db
from ..loop_metrics import compute_loop_metrics
from ..models import AlertEvent, AlertRule, Position, PositionSnapshot, User
from ..portfolio_class import resolve_position
from ..portfolio_service import load_overrides
from ..schemas import (
    AlertEventOut,
    AlertRuleIn,
    AlertRuleOut,
    LendingSummaryOut,
    OptionsSummaryOut,
    PositionOut,
)

router = APIRouter(tags=["positions"])

_KINDS = {"lending", "option"}
_PROTOCOLS = {"deribit", "binance", "aave", "morpho", "pendle", "kamino", "jupiter"}


def _positions_query(
    db: Session,
    user_id: int,
    *,
    kind: str | None = None,
    protocol: str | None = None,
    status_filter: str | None = "open",
):
    stmt = select(Position).where(Position.user_id == user_id)
    if status_filter is not None:
        stmt = stmt.where(Position.status == status_filter)
    if kind is not None:
        stmt = stmt.where(Position.kind == kind)
    if protocol is not None:
        stmt = stmt.where(Position.protocol == protocol)
    return list(db.scalars(stmt.order_by(Position.id.asc())).all())


def _as_float(value: object) -> float:
    return float(Decimal(str(value or 0)))


def _detail_float(details: dict | None, key: str) -> float | None:
    if not isinstance(details, dict) or details.get(key) is None:
        return None
    return float(details[key])


def _position_payload(row: Position, overrides: dict) -> dict:
    """PositionOut plus the phase-5 read-only classification fields."""
    payload = PositionOut.model_validate(row).model_dump()
    asset_class, risk, reason, overridden = resolve_position(row, overrides)
    payload.update(
        asset_class=asset_class,
        risk_level=risk,
        risk_reason=reason,
        overridden=overridden,
    )
    return payload


@router.get("/positions", response_model=list[PositionOut])
def list_positions(
    kind: str | None = Query(default=None),
    protocol: str | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    if kind is not None and kind not in _KINDS:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="kind 无效")
    if protocol is not None and protocol not in _PROTOCOLS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="protocol 无效"
        )
    rows = _positions_query(db, current_user.id, kind=kind, protocol=protocol)
    overrides = load_overrides(db, current_user.id)
    return [_position_payload(row, overrides) for row in rows]


@router.get("/lending/summary", response_model=LendingSummaryOut)
def lending_summary(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> LendingSummaryOut:
    rows = _positions_query(db, current_user.id, kind="lending")
    overrides = load_overrides(db, current_user.id)
    factors = [
        factor
        for row in rows
        if (factor := _detail_float(row.details, "health_factor")) is not None
    ]
    return LendingSummaryOut(
        total_collateral_usdt=sum(_as_float(row.collateral_usdt) for row in rows),
        total_debt_usdt=sum(_as_float(row.debt_usdt) for row in rows),
        net_value_usdt=sum(_as_float(row.net_value_usdt) for row in rows),
        min_health_factor=min(factors) if factors else None,
        positions=[_position_payload(row, overrides) for row in rows],
    )


@router.get("/lending/positions/{position_id}/loop")
def lending_loop(
    position_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """p25 read-only loop metrics for one lending position.

    Historical snapshots are only read; the p25 fields start accumulating from
    the first snapshot written with split loan legs.
    """
    row = db.scalar(
        select(Position).where(
            Position.id == position_id,
            Position.user_id == current_user.id,
            Position.kind == "lending",
            Position.status == "open",
        )
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="借贷仓位不存在")
    snapshots = list(
        db.scalars(
            select(PositionSnapshot)
            .where(PositionSnapshot.position_id == row.id)
            .order_by(PositionSnapshot.ts.asc())
        ).all()
    )
    return compute_loop_metrics(row, snapshots)


@router.get("/options/summary", response_model=OptionsSummaryOut)
def options_summary(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> OptionsSummaryOut:
    rows = _positions_query(db, current_user.id, kind="option")
    overrides = load_overrides(db, current_user.id)
    mark = 0.0
    premium = 0.0
    pnl = 0.0
    for row in rows:
        details = row.details if isinstance(row.details, dict) else {}
        mark += float(details.get("mark_value_usd") or 0)
        premium += float(details.get("premium_usd") or 0)
        pnl += float(details.get("unrealized_pnl_usd") or 0)
    return OptionsSummaryOut(
        total_mark_value_usdt=mark,
        total_premium_usdt=premium,
        unrealized_pnl_usdt=pnl,
        positions=[_position_payload(row, overrides) for row in rows],
    )


def _default_rule(db: Session, user_id: int) -> AlertRule:
    """The all-protocols row (protocol IS NULL). Created on first read."""
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
            select(AlertRule)
            .where(AlertRule.user_id == current_user.id)
            .order_by(AlertRule.id.asc())
        ).all()
    )


@router.put("/alerts/rules", response_model=AlertRuleOut)
def upsert_alert_rule(
    payload: AlertRuleIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AlertRule:
    if payload.danger_hf <= 0 or payload.warn_hf <= 0:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="阈值须为正数")
    if payload.cooldown_min < 0:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="cooldown_min 无效")
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


@router.get("/alerts/events", response_model=list[AlertEventOut])
def list_alert_events(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[AlertEvent]:
    return list(
        db.scalars(
            select(AlertEvent)
            .where(AlertEvent.user_id == current_user.id)
            .order_by(AlertEvent.created_at.desc(), AlertEvent.id.desc())
        ).all()
    )
