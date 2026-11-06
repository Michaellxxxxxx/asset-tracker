"""Per-user on-chain wallet CRUD. No private keys — addresses only."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..db import get_db
from ..models import User, UserWallet
from ..schemas import WalletIn, WalletOut

router = APIRouter(prefix="/wallets", tags=["wallets"])


@router.get("", response_model=list[WalletOut])
def list_wallets(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[UserWallet]:
    return list(
        db.scalars(
            select(UserWallet)
            .where(UserWallet.user_id == current_user.id)
            .order_by(UserWallet.id.asc())
        ).all()
    )


@router.post("", response_model=WalletOut, status_code=status.HTTP_201_CREATED)
def add_wallet(
    payload: WalletIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UserWallet:
    try:
        chain, address = payload.normalized()
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    existing = db.scalar(
        select(UserWallet).where(
            UserWallet.user_id == current_user.id,
            UserWallet.chain == chain,
            UserWallet.address == address,
        )
    )
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="该地址已添加")

    row = UserWallet(user_id=current_user.id, chain=chain, address=address)
    db.add(row)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="该地址已添加") from exc
    db.refresh(row)
    return row


@router.delete("/{wallet_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
def delete_wallet(
    wallet_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    row = db.get(UserWallet, wallet_id)
    if row is None or row.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="钱包不存在")
    db.delete(row)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
