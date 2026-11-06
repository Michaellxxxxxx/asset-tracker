from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import create_access_token, get_current_user, verify_password
from ..db import get_db
from ..models import User
from ..schemas import LoginIn, TokenOut, UserOut

router = APIRouter(prefix="/auth", tags=["auth"])

REGISTER_CLOSED_DETAIL = "注册已关闭，请联系管理员开号"


@router.post("/register", include_in_schema=False)
def register_closed() -> None:
    """P18: public self-service registration is closed.

    Accounts are created only by an admin through ``POST /api/admin/users``.
    The route stays registered so old clients get an explicit 403 instead of a
    confusing 404/405, and it validates no body before refusing the request.
    """
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=REGISTER_CLOSED_DETAIL,
    )


@router.post("/login", response_model=TokenOut)
def login(payload: LoginIn, db: Session = Depends(get_db)) -> TokenOut:
    user = db.scalar(select(User).where(User.username == payload.username))
    if user is None or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="账号已停用",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return TokenOut(
        access_token=create_access_token(user),
        token_type="bearer",
        user=UserOut.model_validate(user),
    )


@router.get("/me", response_model=UserOut)
def me(current_user: User = Depends(get_current_user)) -> User:
    return current_user
