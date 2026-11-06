from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..db import get_db
from ..models import User
from ..returns_breakdown import calculate_returns_breakdown
from ..schemas import ReturnsBreakdownOut, ReturnsOut
from ..twr import ReturnPeriod, calculate_returns

router = APIRouter(tags=["returns"])


@router.get("/returns", response_model=ReturnsOut)
def returns(
    period: ReturnPeriod = Query(default="month"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ReturnsOut:
    return calculate_returns(db, current_user.id, period)


@router.get("/returns/breakdown", response_model=ReturnsBreakdownOut)
def returns_breakdown(
    period: ReturnPeriod = Query(default="month"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ReturnsBreakdownOut:
    """p26 additive readout. The existing /returns response is unchanged."""
    return calculate_returns_breakdown(db, current_user.id, period)
