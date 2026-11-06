"""Phase-5 portfolio structure, item detail and manual classification overrides."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..db import get_db
from ..models import AssetOverride, User
from ..portfolio_class import ASSET_CLASSES, RISK_LEVELS
from ..portfolio_service import portfolio_context, portfolio_items, structure
from ..schemas import (
    OverrideDeleteOut,
    OverrideIn,
    OverrideOut,
    PortfolioItemsOut,
    StructureOut,
)

router = APIRouter(tags=["portfolio"])


@router.get("/portfolio/structure", response_model=StructureOut)
def portfolio_structure(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    return structure(portfolio_context(db, current_user.id))


@router.get("/portfolio/items", response_model=PortfolioItemsOut)
def portfolio_item_list(
    asset_class: str | None = Query(default=None, alias="class"),
    risk: str | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    if asset_class is not None and asset_class not in ASSET_CLASSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"asset_class 无效，可选 {', '.join(ASSET_CLASSES)}",
        )
    if risk is not None and risk not in RISK_LEVELS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"risk 无效，可选 {', '.join(RISK_LEVELS)}",
        )
    context = portfolio_context(db, current_user.id)
    items = portfolio_items(context)
    if asset_class is not None:
        items = [item for item in items if item["asset_class"] == asset_class]
    if risk is not None:
        items = [item for item in items if item["risk_level"] == risk]
    items.sort(key=lambda item: item["value_usdt"], reverse=True)
    return {"total_usdt": round(context["current_nav"], 8), "items": items}


@router.put("/portfolio/override", response_model=OverrideOut)
def put_override(
    payload: OverrideIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> OverrideOut:
    key = payload.key.strip()
    if not key:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="key 不能为空")
    if payload.asset_class is None and payload.risk_level is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="asset_class 与 risk_level 至少提供一个",
        )
    if payload.asset_class is not None and payload.asset_class not in ASSET_CLASSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"asset_class 无效，可选 {', '.join(ASSET_CLASSES)}",
        )
    if payload.risk_level is not None and payload.risk_level not in RISK_LEVELS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"risk_level 无效，可选 {', '.join(RISK_LEVELS)}",
        )

    row = db.scalar(
        select(AssetOverride).where(
            AssetOverride.user_id == current_user.id, AssetOverride.key == key
        )
    )
    if row is None:
        row = AssetOverride(user_id=current_user.id, key=key)
        db.add(row)
    if payload.asset_class is not None:
        row.asset_class = payload.asset_class
    if payload.risk_level is not None:
        row.risk_level = payload.risk_level
    if payload.reason.strip():
        row.reason = payload.reason.strip()
    elif row.reason is None:
        row.reason = ""
    row.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(row)
    return OverrideOut(
        key=row.key,
        asset_class=row.asset_class,
        risk_level=row.risk_level,
        reason=row.reason,
        updated_at=row.updated_at,
    )


@router.delete("/portfolio/override/{key}", response_model=OverrideDeleteOut)
def delete_override(
    key: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> OverrideDeleteOut:
    row = db.scalar(
        select(AssetOverride).where(
            AssetOverride.user_id == current_user.id, AssetOverride.key == key
        )
    )
    if row is None:
        return OverrideDeleteOut(deleted=False, key=key)
    db.delete(row)
    db.commit()
    return OverrideDeleteOut(deleted=True, key=key)
