from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import models  # noqa: F401 - register ORM models before create_all
from .config import settings, warn_if_default_secrets
from .db import Base, SessionLocal, engine, get_db, wait_for_db
from .integrations import http as _quiet_http  # noqa: F401 - silence httpx INFO logs
from .integrations.wechat.health import send_health_status
from .integrations.wechat.status import token_status
from .logging_redact import install_redact_filter
from .migrate import apply_phase2_migrations, seed_wallet_owner
from .routers import admin, auth, coinstats, overview, pending, positions, returns, sync, wallets
from .routers import alerts as alerts_router
from .routers import portfolio as portfolio_router
from .seed import seed_database

install_redact_filter()
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parents[1]
FRONTEND_DIR = BASE_DIR / "frontend"
FRONTEND_STATIC_DIR = FRONTEND_DIR / "static"
INDEX_FILE = FRONTEND_DIR / "index.html"


@asynccontextmanager
async def lifespan(_: FastAPI):
    warn_if_default_secrets(logger)
    wait_for_db()
    Base.metadata.create_all(bind=engine)
    apply_phase2_migrations(engine)
    with SessionLocal() as db:
        seed_database(db)
        seed_wallet_owner(db)
    logger.info("Asset Tracker API is ready")
    yield


app = FastAPI(
    title="Asset Tracker API",
    version=settings.app_version,
    description="资产追踪：演示数据、JWT 鉴权、交易所/链上同步、归因队列与 TWR 收益。",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url=None,
    openapi_url="/api/openapi.json",
)

app.include_router(auth.router, prefix="/api")
app.include_router(overview.router, prefix="/api")
app.include_router(returns.router, prefix="/api")
app.include_router(pending.router, prefix="/api")
app.include_router(admin.router, prefix="/api")
app.include_router(sync.router, prefix="/api")
app.include_router(coinstats.router, prefix="/api")
app.include_router(wallets.router, prefix="/api")
app.include_router(alerts_router.router, prefix="/api")
app.include_router(positions.router, prefix="/api")
app.include_router(portfolio_router.router, prefix="/api")


@app.get("/api/health", tags=["health"])
def health(db=Depends(get_db)) -> dict[str, object]:
    """Liveness plus read-only WeChat token/send freshness (global; no user details)."""
    try:
        status = token_status(db)
        stale = status.stale
        age_hours = status.age_hours
    except Exception:  # noqa: BLE001 - a health probe must not fail on this field
        logger.exception("health: wechat token status unavailable")
        stale, age_hours = True, None
    try:
        send = send_health_status(db)
        send_stale = send.stale
        last_send_error = send.last_send_error
        last_success_at = (
            send.last_success_at.isoformat() if send.last_success_at else None
        )
    except Exception:  # noqa: BLE001 - a health probe must not fail on this field
        logger.exception("health: wechat send health unavailable")
        send_stale, last_send_error, last_success_at = True, "health_unavailable", None
    return {
        # p15b: W1 becomes a consumer-visible health signal.  Token age alone
        # stays advisory (recoverable by one inbound message); a stale send
        # path means alerts are not being delivered, so probes must see it.
        "status": "degraded" if send_stale else "ok",
        "wechat_token_stale": stale,
        "wechat_token_age_hours": age_hours,
        "wechat_send_stale": send_stale,
        "last_send_error": last_send_error,
        "last_success_at": last_success_at,
    }


if FRONTEND_STATIC_DIR.is_dir():
    app.mount(
        "/static",
        StaticFiles(directory=FRONTEND_STATIC_DIR),
        name="static",
    )


@app.get("/{full_path:path}", include_in_schema=False)
def spa_fallback(full_path: str) -> FileResponse:
    """Serve the SPA shell for client-side routes; API paths still 404."""
    if full_path == "api" or full_path.startswith("api/"):
        raise HTTPException(status_code=404, detail="Not found")
    if not INDEX_FILE.is_file():
        raise HTTPException(status_code=404, detail="Frontend build not found")
    return FileResponse(INDEX_FILE)
