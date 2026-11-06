"""Retry ccxt calls on transient network errors. No new dependencies."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TypeVar

from .errors import is_non_retryable

logger = logging.getLogger("app.exchanges")


class RetryableError(Exception):
    """Raised by tests to force a retry without importing ccxt."""

T = TypeVar("T")

# Network / rate-limit style failures. Import-time ccxt errors are matched by
# name so this module imports even when ccxt itself is unhappy.
# ExchangeNotAvailable is NOT here: Binance answers HTTP 451 (restricted
# location) with that class, and retrying it four times only hides the body.
_RETRYABLE_NAMES = frozenset(
    {
        "NetworkError",
        "RequestTimeout",
        "DDoSProtection",
        "RateLimitExceeded",
        "Timeout",
        "ConnectionError",
    }
)


def _retryable(exc: BaseException) -> bool:
    if is_non_retryable(exc):
        return False
    if isinstance(exc, RetryableError):
        return True
    return type(exc).__name__ in _RETRYABLE_NAMES or isinstance(exc, (TimeoutError, ConnectionError))


def call_with_retry(
    fn: Callable[[], T],
    *,
    attempts: int = 4,
    base_delay: float = 0.25,
    sleep: Callable[[float], None] | None = None,
) -> T:
    """Call ``fn`` up to ``attempts`` times. Sleeps ``base_delay * 2**n`` between tries.

    HTTP 451 / 401 / 403, AuthenticationError, PermissionDenied, Binance codes
    -2015/-2014/-1022 and Gate FORBIDDEN/INVALID_KEY/IP/READ_ONLY labels are
    raised immediately. Nothing here adds a proxy or changes the host.
    """
    last: BaseException | None = None
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — classified below, then re-raised
            last = exc
            if not _retryable(exc) or attempt == attempts - 1:
                raise
            delay = base_delay * (2**attempt)
            logger.warning(
                "exchange call failed (%s), retry %s/%s in %.2fs",
                type(exc).__name__,
                attempt + 1,
                attempts - 1,
                delay,
            )
            (sleep or time.sleep)(delay)
    assert last is not None
    raise last
