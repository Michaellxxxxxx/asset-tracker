from __future__ import annotations

from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt.exceptions import InvalidTokenError
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from .models import User

bearer_scheme = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        return bcrypt.checkpw(
            plain_password.encode("utf-8"),
            hashed_password.encode("utf-8"),
        )
    except (TypeError, ValueError):
        return False


def create_access_token(user: User) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user.id),
        "username": user.username,
        "is_admin": user.is_admin,
        # P18: bind the token to the account's current token generation.
        "ver": int(user.token_version or 0),
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_expire_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def _unauthorized(detail: str = "登录已失效，请重新登录") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _unauthorized()

    try:
        payload = jwt.decode(
            credentials.credentials,
            settings.jwt_secret,
            algorithms=["HS256"],
            options={"require": ["exp", "sub"]},
        )
    except InvalidTokenError as exc:
        raise _unauthorized() from exc

    try:
        user_id = int(payload["sub"])
    except (KeyError, TypeError, ValueError) as exc:
        raise _unauthorized() from exc

    user = db.get(User, user_id)
    if user is None:
        raise _unauthorized()
    # P18: an account can be disabled after its token was issued. Re-check the
    # live account state on every request so a demo token dies immediately.
    if not user.is_active:
        raise _unauthorized("账号已停用")
    try:
        token_version = int(payload.get("ver", 0))
    except (TypeError, ValueError) as exc:
        raise _unauthorized() from exc
    # Tokens issued before P18 have no "ver" claim and are treated as version 0.
    # Disabling demo bumps its version, so even a re-enabled account cannot
    # resurrect the old token.
    if token_version != int(user.token_version or 0):
        raise _unauthorized()
    return user


def get_current_admin(current_user: User = Depends(get_current_user)) -> User:
    if not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要管理员权限",
        )
    return current_user
