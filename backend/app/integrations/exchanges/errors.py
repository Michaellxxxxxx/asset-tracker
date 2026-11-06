"""Turn a ccxt (or network) exception into a short, redacted, factual line.

The line is what lands in ``sync_runs.error``. It quotes the exchange body when
one is present and never guesses a cause.
"""

from __future__ import annotations

import json
import re

_MAX_LEN = 300
_URL_QUERY = re.compile(r"\?[^\s\"']*")
_HTTP = re.compile(r"\b(?:HTTPError|status(?:\s*code)?)\s*[:=]?\s*(\d{3})\b", re.IGNORECASE)
_HTTP_BARE = re.compile(r"\b([45]\d{2})\b")
_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)

# These are recorded as-is and must not be retried. Matching is on the
# exception class name plus the parsed body, never on a guessed cause.
_NON_RETRY_TYPES = frozenset(
    {
        "AuthenticationError",
        "PermissionDenied",
        "InsufficientFunds",
        "BadSymbol",
        "InvalidOrder",
        "AccountSuspended",
    }
)
_NON_RETRY_HTTP = frozenset({401, 403, 451})
_BINANCE_FATAL_CODES = frozenset({-2015, -2014, -1022})
_GATE_FATAL_LABELS = ("FORBIDDEN", "INVALID_KEY", "INVALID_SIGNATURE", "IP", "READ_ONLY")


def _scrub(text: str, secrets: list[str] | None) -> str:
    cleaned = _URL_QUERY.sub("", text)
    for secret in secrets or []:
        if secret and len(secret) >= 4 and secret in cleaned:
            cleaned = cleaned.replace(secret, "***")
    return cleaned


def _parse_body(message: str) -> tuple[int | None, dict | None]:
    http: int | None = None
    matched = _HTTP.search(message)
    if matched:
        http = int(matched.group(1))
    blob = _JSON_OBJECT.search(message)
    body: dict | None = None
    if blob:
        try:
            parsed = json.loads(blob.group(0))
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            body = parsed
            if http is None:
                for key in ("http", "status", "statusCode"):
                    raw = body.get(key)
                    if isinstance(raw, int) and 100 <= raw < 600:
                        http = raw
                        break
    if http is None:
        bare = _HTTP_BARE.search(message)
        if bare:
            http = int(bare.group(1))
    return http, body


def describe_exchange_error(exc: BaseException, secrets: list[str] | None = None, *, venue: str | None = None) -> str:
    """``binance: ExchangeNotAvailable http=451 code=0 msg=\"...\"``.

    URL query strings (``?timestamp=&signature=``) are stripped and every
    credential value is replaced with ``***``. The result is at most 300 chars.
    """
    user_message = getattr(exc, "user_message", None)
    if isinstance(user_message, str) and user_message:
        text = _scrub(f"{venue}: {user_message}" if venue else user_message, secrets)
        if len(text) > _MAX_LEN:
            text = text[: _MAX_LEN - 3] + "..."
        return text
    message = _scrub(str(exc), secrets)
    http, body = _parse_body(message)
    name = type(exc).__name__
    prefix = f"{venue}: " if venue else ""
    if body is not None:
        code = body.get("code", body.get("label"))
        msg = body.get("msg", body.get("message"))
        parts = [f"{prefix}{name}"]
        if http is not None:
            parts.append(f"http={http}")
        if code is not None:
            parts.append(f"code={code}")
        if msg is not None:
            parts.append(f'msg="{_scrub(str(msg), secrets)}"')
        text = " ".join(parts)
    else:
        text = f"{prefix}{name}: {message}".strip()
    text = _scrub(text, secrets)
    if len(text) > _MAX_LEN:
        text = text[: _MAX_LEN - 3] + "..."
    return text


def is_non_retryable(exc: BaseException) -> bool:
    """Auth, permission, geo-block and explicit IP/key errors are not retried."""
    if type(exc).__name__ in _NON_RETRY_TYPES:
        return True
    http, body = _parse_body(str(exc))
    if http in _NON_RETRY_HTTP:
        return True
    message = str(exc).lower()
    if "restricted location" in message:
        return True
    if body is None:
        return False
    code = body.get("code")
    if isinstance(code, int) and code in _BINANCE_FATAL_CODES:
        return True
    label = str(body.get("label") or "")
    upper = label.upper()
    if any(token in upper for token in _GATE_FATAL_LABELS):
        return True
    return False
