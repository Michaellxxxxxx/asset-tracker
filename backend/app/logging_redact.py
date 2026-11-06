"""Redact API keys and secrets from log records. Never log credential material."""

from __future__ import annotations

import logging
import re

# Common secret shapes. Replacement keeps length hidden.
_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)(bearer\s+)(\S+)"),
    re.compile(r"(?i)(basic\s+)(\S+)"),
    re.compile(
        r"(?i)((?:api[_-]?key|api[_-]?secret|secret|passphrase|password|token|"
        r"authorization|x-api-key)\s*[=:]\s*)(\S+)"
    ),
    re.compile(r"(?i)(key=)([^&\s]+)"),
    # ccxt signs private GETs as ``?timestamp=...&signature=...``. Drop both
    # values so a logged request line cannot be replayed.
    re.compile(r"(?i)((?:signature|timestamp)=)([^&\s]+)"),
    # Alchemy embeds the key in the URL path: https://eth-mainnet.g.alchemy.com/v2/<KEY>
    re.compile(r"(?i)(/v2/)([^/?\s]+)"),
    # Wallet addresses: keep the first 6 and last 4, hide the middle. Applied
    # after the key patterns so an Alchemy URL still redacts the key first.
    re.compile(r"0x[a-fA-F0-9]{40}"),
)

_REDACTED = "***"


def redact_text(value: str) -> str:
    redacted = value
    for secret in _configured_secrets():
        if secret and secret in redacted:
            redacted = redacted.replace(secret, _REDACTED)
    for pattern in _PATTERNS[:-1]:
        redacted = pattern.sub(lambda match: f"{match.group(1)}{_REDACTED}", redacted)
    return _PATTERNS[-1].sub(lambda match: _mask_address(match.group(0)), redacted)


def _configured_secrets() -> list[str]:
    """Exact key/secret values currently in settings. Never logged, only matched."""
    from .config import settings

    names = (
        "binance_api_key",
        "binance_api_secret",
        "bitget_api_key",
        "bitget_api_secret",
        "bitget_api_passphrase",
        "bybit_api_key",
        "bybit_api_secret",
        "kraken_api_key",
        "kraken_api_secret",
        "gate_api_key",
        "gate_api_secret",
        "zerion_api_key",
        "alchemy_api_key",
        "helius_api_key",
        "coingecko_demo_api_key",
        "wechat_bot_token",
        "wechat_ilink_user_id",
        "wechat_ilink_bot_id",
    )
    found: list[str] = []
    for name in names:
        raw = getattr(settings, name, "") or ""
        if len(raw) >= 6:
            found.append(raw)
    return found


def _mask_address(address: str) -> str:
    """First 6 and last 4. A full wallet address must not reach a log line."""
    if len(address) <= 10:
        return address
    return f"{address[:6]}…{address[-4:]}"


class RedactFilter(logging.Filter):
    """Strip credential-looking substrings from log messages and exception text."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_text(record.msg)
        if record.args:
            record.args = _redact_args(record.args)
        if record.exc_info and record.exc_info[1] is not None:
            # Leave the exception object intact; redact only the rendered message cache.
            record.exc_text = None
        return True


def _redact_args(args: tuple | dict) -> tuple | dict:
    if isinstance(args, dict):
        return {key: _redact_value(value) for key, value in args.items()}
    return tuple(_redact_value(value) for value in args)


def _redact_value(value: object) -> object:
    if isinstance(value, str):
        return redact_text(value)
    return value


def install_redact_filter(target: logging.Logger | None = None) -> RedactFilter:
    filt = RedactFilter()
    logger = target or logging.getLogger()
    if not any(isinstance(existing, RedactFilter) for existing in logger.filters):
        logger.addFilter(filt)
    return filt
