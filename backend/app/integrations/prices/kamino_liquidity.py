"""Kamino Liquidity (``KJLP-*``) share price from the strategy metrics feed.

A ``KJLP-*`` token is a Kamino Liquidity vault share (a kToken), not a lending
receipt. Its USD value is the strategy's per-share net asset value, which the
public metrics endpoint reports as ``sharePrice``:

    GET https://api.kamino.finance/strategies/{strategy}/metrics?env=mainnet-beta

Only the two allow-listed share mints are ever requested. DefiLlama, Jupiter
and Kamino's ``/prices`` endpoint do not quote these mints, so a miss is a miss:
this module never invents a price and never scans the full strategy list.
"""

from __future__ import annotations

import logging
import threading
import time
from decimal import Decimal, InvalidOperation

import httpx

from ..http import quiet_client
from ..lending_common import log_failure

logger = logging.getLogger(__name__)

KAMINO_METRICS_BASE = "https://api.kamino.finance/strategies"
KAMINO_ENV = "mainnet-beta"
KAMINO_PRICE_SOURCE = "kamino_strategy_share"

REQUEST_TIMEOUT_SECONDS = 8.0
CACHE_TTL_SECONDS = 300.0
FAIL_BACKOFF_SECONDS = 60.0

# canonical shareMint (kToken) -> Kamino strategy. Solana mint addresses are
# case-sensitive, so the canonical spelling is kept and only used verbatim in
# the request URL. The lower-case map only normalises caller input.
_SHARE_MINTS: dict[str, str] = {
    "J5o36oSuGS4nQ7cfZiAuvmaN2bggcoMoqp7BB7zATtwE": (
        "DvK76xvo2dnAEkDBSVNFWzfJ8WC49rGkRkkAvnQbYTxS"
    ),
    "FVk7hoHvqAkdpNY5Zxr19WzVxd35jgHcaG4FNr1UUVn9": (
        "E5A71YnEizRMxuYWf9JQN3Cf5yrcPB5Y6QdC23Sc4icr"
    ),
}
_SHARE_MINTS_LOWER = {mint.lower(): mint for mint in _SHARE_MINTS}

# canonical mint -> (expires monotonic, price | None). None is a short negative
# cache so a failing feed is not retried for every balance in the snapshot.
_cache: dict[str, tuple[float, Decimal | None]] = {}
_lock = threading.Lock()


def clear_cache() -> None:
    """Drop process-local share prices (tests and a manual refresh)."""
    with _lock:
        _cache.clear()


def canonical_mint(mint: str | None) -> str | None:
    """Return the allow-listed spelling of ``mint``, or ``None``.

    Input is stripped. An exact match wins; the lower-case map additionally
    accepts the lower-cased ``solana:<mint>`` keys used by the contract cache.
    """
    if not mint:
        return None
    candidate = str(mint).strip()
    if not candidate:
        return None
    if candidate in _SHARE_MINTS:
        return candidate
    return _SHARE_MINTS_LOWER.get(candidate.lower())


def strategy_for(mint: str | None) -> str | None:
    """Kamino strategy for an allow-listed share mint, else ``None``."""
    canonical = canonical_mint(mint)
    return _SHARE_MINTS.get(canonical) if canonical else None


def quote_share_price(
    mint: str | None,
    *,
    client: httpx.Client | None = None,
) -> Decimal | None:
    """USD net asset value per Kamino Liquidity share, or ``None``.

    A non-allow-listed mint, a missing/non-positive ``sharePrice``, or any HTTP
    failure returns ``None``. Results are cached for five minutes; misses for
    one minute. The caller must leave the holding unpriced on ``None``.
    """
    canonical = canonical_mint(mint)
    if canonical is None:
        return None
    moment = time.monotonic()
    with _lock:
        entry = _cache.get(canonical)
        if entry is not None and moment < entry[0]:
            return entry[1]
    price = _fetch_share_price(canonical, client)
    ttl = CACHE_TTL_SECONDS if price is not None else FAIL_BACKOFF_SECONDS
    with _lock:
        _cache[canonical] = (time.monotonic() + ttl, price)
    return price


def _fetch_share_price(mint: str, client: httpx.Client | None) -> Decimal | None:
    strategy = _SHARE_MINTS[mint]
    url = f"{KAMINO_METRICS_BASE}/{strategy}/metrics"
    own_client = client is None
    http = client or quiet_client(timeout=REQUEST_TIMEOUT_SECONDS)
    try:
        response = http.get(url, params={"env": KAMINO_ENV}, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # noqa: BLE001 - an optional price feed must not sink a snapshot
        # fatal=False: the protocol reader is unaffected, only this quote is lost.
        log_failure("kamino_liquidity", "solana", exc, fatal=False)
        return None
    finally:
        if own_client:
            http.close()
    if not isinstance(payload, dict):
        logger.warning("kamino liquidity metrics payload is not an object")
        return None
    raw = payload.get("sharePrice")
    if raw in (None, ""):
        logger.warning("kamino liquidity metrics has no sharePrice")
        return None
    try:
        price = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        logger.warning("kamino liquidity sharePrice is not a number")
        return None
    if not price.is_finite() or price <= 0:
        logger.warning("kamino liquidity sharePrice is not positive")
        return None
    return price
