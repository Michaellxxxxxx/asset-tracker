from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, timezone


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc


def _str_env(name: str, default: str = "") -> str:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip()


def _csv_env(name: str) -> tuple[str, ...]:
    raw = os.getenv(name, "")
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def parse_sync_since(raw: str | None = None) -> datetime:
    """SYNC_SINCE is a calendar date (default 2026-01-01) interpreted as UTC midnight."""
    value = (raw if raw is not None else _str_env("SYNC_SINCE", "2026-01-01")) or "2026-01-01"
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise RuntimeError("SYNC_SINCE must be an ISO date (YYYY-MM-DD)") from exc
    return datetime(parsed.year, parsed.month, parsed.day, tzinfo=timezone.utc)


# 仅用于开发/演示。生产必须更换；启动时 warn_if_default_secrets 会按这两个值告警。
DEV_JWT_SECRET_DEFAULT = "demo-jwt-secret-change-me-in-prod-32b"
DEV_MASTER_KEY_DEFAULT = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"


def resolve_sync_mode(explicit: str | None = None) -> str:
    """auto|mock|live. auto falls back to mock when no live credentials are configured."""
    mode = (explicit if explicit is not None else _str_env("SYNC_MODE", "auto")).lower() or "auto"
    if mode not in {"auto", "mock", "live"}:
        raise RuntimeError("SYNC_MODE must be one of auto, mock, live")
    return mode


def resolve_wechat_push_mode(
    explicit: str | None = None,
    token: str | None = None,
) -> str:
    """mock|live. auto (default) is live only when WECHAT_BOT_TOKEN is non-empty.

    Explicit mock stays mock even if a token is present. Explicit live with an
    empty token is resolved as live here; the client factory still degrades to
    mock so the process can boot without a token.
    """
    mode = (explicit if explicit is not None else _str_env("WECHAT_PUSH_MODE", "auto")).lower() or "auto"
    if mode not in {"auto", "mock", "live"}:
        raise RuntimeError("WECHAT_PUSH_MODE must be one of auto, mock, live")
    if mode == "mock":
        return "mock"
    if mode == "live":
        return "live"
    present = token if token is not None else _str_env("WECHAT_BOT_TOKEN")
    return "live" if present.strip() else "mock"


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number") from exc


@dataclass(frozen=True)
class Settings:
    app_name: str = "Asset Tracker"
    app_version: str = "0.2.0"
    database_url: str = os.getenv(
        "DATABASE_URL",
        "postgresql+psycopg://asset:asset_pass@postgres:5432/asset_tracker",
    )
    redis_url: str = os.getenv("REDIS_URL", "redis://redis:6379/0")
    jwt_secret: str = os.getenv("JWT_SECRET", DEV_JWT_SECRET_DEFAULT)
    master_key: str = os.getenv("MASTER_KEY", DEV_MASTER_KEY_DEFAULT)
    access_token_expire_minutes: int = _int_env("ACCESS_TOKEN_EXPIRE_MINUTES", 1440)

    binance_api_key: str = _str_env("BINANCE_API_KEY")
    binance_api_secret: str = _str_env("BINANCE_API_SECRET")
    bitget_api_key: str = _str_env("BITGET_API_KEY")
    bitget_api_secret: str = _str_env("BITGET_API_SECRET")
    bitget_api_passphrase: str = _str_env("BITGET_API_PASSPHRASE")
    bybit_api_key: str = _str_env("BYBIT_API_KEY")
    bybit_api_secret: str = _str_env("BYBIT_API_SECRET")
    kraken_api_key: str = _str_env("KRAKEN_API_KEY")
    kraken_api_secret: str = _str_env("KRAKEN_API_SECRET")
    gate_api_key: str = _str_env("GATE_API_KEY")
    gate_api_secret: str = _str_env("GATE_API_SECRET")

    zerion_api_key: str = _str_env("ZERION_API_KEY")
    alchemy_api_key: str = _str_env("ALCHEMY_API_KEY")
    helius_api_key: str = _str_env("HELIUS_API_KEY")
    coingecko_demo_api_key: str = _str_env("COINGECKO_DEMO_API_KEY")

    # CoinStats Open API (Binance proxy). The key itself lives per user, not here.
    coinstats_base_url: str = _str_env("COINSTATS_BASE_URL", "https://api.coinstats.app") or "https://api.coinstats.app"
    coinstats_timeout_seconds: int = _int_env("COINSTATS_TIMEOUT_SECONDS", 20)
    coinstats_sync_interval_seconds: int = _int_env("COINSTATS_SYNC_INTERVAL_SECONDS", 21600)
    coinstats_max_pages: int = _int_env("COINSTATS_MAX_PAGES", 10)

    evm_addresses: tuple[str, ...] = _csv_env("EVM_ADDRESSES")
    solana_addresses: tuple[str, ...] = _csv_env("SOLANA_ADDRESSES")
    # env 里的地址在启动时幂等归属给该用户名；缺省 admin。空用户没有钱包，只能走 mock。
    wallet_owner_username: str = _str_env("WALLET_OWNER_USERNAME", "admin") or "admin"
    # 交易所 key 只属于该用户。缺省 admin；空串表示没有任何用户拥有（全部走 mock）。
    exchange_owner_username: str = _str_env("EXCHANGE_OWNER_USERNAME", "admin")
    sync_mode: str = resolve_sync_mode()
    sync_since: datetime = parse_sync_since()
    sync_interval_seconds: int = _int_env("SYNC_INTERVAL_SECONDS", 1800)

    # WeChat ClawBot. Token stays in the environment only; never written to the DB.
    wechat_bot_token: str = _str_env("WECHAT_BOT_TOKEN")
    wechat_push_mode: str = resolve_wechat_push_mode()
    wechat_poll_enabled: bool = _bool_env("WECHAT_POLL_ENABLED", False)
    wechat_flush_interval_seconds: int = _int_env("WECHAT_FLUSH_INTERVAL_SECONDS", 30)
    # p29: only rows with id > this are flushed/reset/released (0 = all rows).
    wechat_flush_min_id: int = _int_env("WECHAT_FLUSH_MIN_ID", 0)
    # iLink endpoint and the scanned WeChat user / bot ids (env only, never logged).
    wechat_ilink_baseurl: str = _str_env("WECHAT_ILINK_BASEURL")
    wechat_ilink_user_id: str = _str_env("WECHAT_ILINK_USER_ID")
    wechat_ilink_bot_id: str = _str_env("WECHAT_ILINK_BOT_ID")
    # Skeleton: inbound messages bind to this username until a binding table exists.
    wechat_default_username: str = _str_env("WECHAT_DEFAULT_USERNAME", "admin") or "admin"
    wechat_default_user_id: int | None = (
        _int_env("WECHAT_DEFAULT_USER_ID", 0) or None
    )

    # Holdings below this USDT value (after same-symbol aggregation) fold into 杂项.
    misc_threshold_usdt: float = _float_env("MISC_THRESHOLD_USDT", 50)


settings = Settings()


def warn_if_default_secrets(logger=None) -> list[str]:
    """Warn when JWT_SECRET or MASTER_KEY is still the development default.

    Returns the names that still match. Never logs the secret values.
    """
    import logging

    log = logger if logger is not None else logging.getLogger(__name__)
    hits: list[str] = []
    if settings.jwt_secret == DEV_JWT_SECRET_DEFAULT:
        log.warning("JWT_SECRET 仍为开发默认，请轮换后再用于生产")
        hits.append("JWT_SECRET")
    if settings.master_key == DEV_MASTER_KEY_DEFAULT:
        log.warning("MASTER_KEY 仍为开发默认，请轮换后再用于生产")
        hits.append("MASTER_KEY")
    return hits


def exchange_credentials() -> dict[str, dict[str, str]]:
    """Return only exchanges whose required secrets are all non-empty. Values stay in memory."""
    configured: dict[str, dict[str, str]] = {}
    pairs = {
        "binance": {"apiKey": settings.binance_api_key, "secret": settings.binance_api_secret},
        "bitget": {
            "apiKey": settings.bitget_api_key,
            "secret": settings.bitget_api_secret,
            "password": settings.bitget_api_passphrase,
        },
        "bybit": {"apiKey": settings.bybit_api_key, "secret": settings.bybit_api_secret},
        "kraken": {"apiKey": settings.kraken_api_key, "secret": settings.kraken_api_secret},
        "gate": {"apiKey": settings.gate_api_key, "secret": settings.gate_api_secret},
    }
    for name, creds in pairs.items():
        if all(creds.values()):
            configured[name] = creds
    return configured


def has_live_credentials() -> bool:
    """Process-wide live signal for chain keys and prices.

    Exchange keys are per-user (see ``user_exchange_credentials``) and must not
    flip another user — demo in particular — into live mode.
    """
    if settings.evm_addresses and (settings.zerion_api_key or settings.alchemy_api_key):
        return True
    if settings.solana_addresses and settings.helius_api_key:
        return True
    if settings.coingecko_demo_api_key:
        return True
    return False
