"""Pendle implied APY for a PT used as Morpho collateral.

Morpho's own ``supplyApy`` is the loan-asset rate, not the PT's locked yield.
This module looks the PT up by contract (``GET /v1/{chainId}/markets?pt=``)
and returns ``impliedApy`` only. It does not fall back to ``underlyingApy``
or ``aggregatedApy``, and it does not read Pendle's own looping positions
(those stay in ``lending_pendle``).

No API key. Logs never include a URL query string.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from .http import quiet_client
from .lending_common import log_failure

logger = logging.getLogger(__name__)

API_BASE = "https://api-v2.pendle.finance/core"
SOURCE = "pendle_implied"

# Pendle chain ids we can query. Unknown chains return None without a request.
CHAIN_ID_BY_NAME = {
    "ethereum": 1,
    "base": 8453,
    "arbitrum": 42161,
}

CACHE_TTL_SECONDS = 300.0
FAIL_BACKOFF_SECONDS = 30.0
REQUEST_TIMEOUT_SECONDS = 8.0

# Process cache. Value is (expires_monotonic, quote | None). None is a negative
# cache (total=0 or a short failure backoff), never a long "API is down" entry.
_cache: dict[tuple[int, str], tuple[float, "PendleImpliedQuote | None"]] = {}
_lock = threading.Lock()
# After a 429, skip further lookups until this monotonic instant (this round).
_rate_limited_until = 0.0


class PendleImpliedQuote:
    """Structured implied-APY result. ``implied_apy`` is a fraction (0.15 = 15%)."""

    __slots__ = ("implied_apy", "source", "expired", "expiry", "is_active", "market")

    def __init__(
        self,
        *,
        implied_apy: float,
        expired: bool = False,
        expiry: str | None = None,
        is_active: bool | None = None,
        market: str | None = None,
    ) -> None:
        self.implied_apy = float(implied_apy)
        self.source = SOURCE
        self.expired = expired
        self.expiry = expiry
        self.is_active = is_active
        self.market = market


def clear_cache() -> None:
    """Drop the process cache and any 429 pause. Tests call this between cases."""
    global _rate_limited_until
    with _lock:
        _cache.clear()
        _rate_limited_until = 0.0


def pendle_implied_apy(
    chain: str | int,
    address: str,
    *,
    client: httpx.Client | None = None,
    now: datetime | None = None,
) -> PendleImpliedQuote | None:
    """Look up implied APY for a PT contract. ``None`` on miss, unknown chain, or failure.

    ``total=0`` is cached for 300s. Transport / 5xx / 429 / bad JSON return
    ``None`` and are cached for at most 30s so one blip does not hide the next
    sync. A 429 also pauses further lookups in this process until the reset
    header (or 30s if the header is missing).
    """
    chain_id = _chain_id(chain)
    pt = str(address or "").strip().lower()
    if chain_id is None or not pt.startswith("0x") or len(pt) != 42:
        return None

    key = (chain_id, pt)
    moment = time.monotonic()
    cached = _cache_get(key, moment)
    if cached is not _MISS:
        return cached  # type: ignore[return-value]

    with _lock:
        if moment < _rate_limited_until:
            return None

    own_client = client is None
    http = client or quiet_client(timeout=REQUEST_TIMEOUT_SECONDS)
    try:
        quote, outcome = _fetch(http, chain_id, pt, now=now)
    except Exception as exc:  # noqa: BLE001 — one PT must not sink the Morpho sync
        log_failure("pendle_implied", str(chain_id), exc)
        _cache_put(key, None, FAIL_BACKOFF_SECONDS)
        return None
    finally:
        if own_client:
            http.close()

    if outcome == "rate_limited":
        return None
    if outcome == "hit" or outcome == "miss":
        _cache_put(key, quote, CACHE_TTL_SECONDS)
        return quote
    # failure: short backoff only
    _cache_put(key, None, FAIL_BACKOFF_SECONDS)
    return None


_MISS = object()


def _chain_id(chain: str | int) -> int | None:
    if isinstance(chain, int):
        return chain if chain in CHAIN_ID_BY_NAME.values() else None
    return CHAIN_ID_BY_NAME.get(str(chain or "").strip().lower())


def _cache_get(key: tuple[int, str], moment: float) -> PendleImpliedQuote | None | object:
    with _lock:
        entry = _cache.get(key)
        if entry is None:
            return _MISS
        expires, quote = entry
        if moment >= expires:
            _cache.pop(key, None)
            return _MISS
        return quote


def _cache_put(key: tuple[int, str], quote: PendleImpliedQuote | None, ttl: float) -> None:
    with _lock:
        _cache[key] = (time.monotonic() + ttl, quote)


def _fetch(
    http: httpx.Client,
    chain_id: int,
    pt: str,
    *,
    now: datetime | None,
) -> tuple[PendleImpliedQuote | None, str]:
    """Return ``(quote, outcome)`` where outcome is hit / miss / failure / rate_limited."""
    url = f"{API_BASE}/v1/{chain_id}/markets"
    try:
        response = http.get(url, params={"pt": pt}, timeout=REQUEST_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001
        log_failure("pendle_implied", str(chain_id), exc)
        return None, "failure"

    if response.status_code == 429:
        _note_rate_limit(response)
        log_failure("pendle_implied", str(chain_id), httpx.HTTPStatusError("429", request=response.request, response=response))
        return None, "rate_limited"
    if response.status_code >= 400:
        log_failure(
            "pendle_implied",
            str(chain_id),
            httpx.HTTPStatusError(str(response.status_code), request=response.request, response=response),
        )
        return None, "failure"
    try:
        payload = response.json()
    except Exception as exc:  # noqa: BLE001
        log_failure("pendle_implied", str(chain_id), exc)
        return None, "failure"
    if not isinstance(payload, dict):
        return None, "failure"

    total = payload.get("total")
    results = payload.get("results")
    if total == 0 or not isinstance(results, list) or not results:
        return None, "miss"
    market = results[0] if isinstance(results[0], dict) else None
    if market is None:
        return None, "failure"
    quote = _quote_from_market(market, now=now)
    if quote is None:
        # Live market but impliedApy missing / non-finite: treat as failure, not a long miss.
        return None, "failure"
    return quote, "hit"


def _note_rate_limit(response: httpx.Response) -> None:
    """Pause further lookups until ``x-ratelimit-reset`` (unix seconds), else 30s."""
    global _rate_limited_until
    pause = FAIL_BACKOFF_SECONDS
    raw = response.headers.get("x-ratelimit-reset")
    if raw:
        try:
            reset_at = float(raw)
            # Header is unix epoch seconds, not a delta.
            wait = reset_at - time.time()
            if wait > 0:
                pause = wait
        except (TypeError, ValueError):
            pause = FAIL_BACKOFF_SECONDS
    with _lock:
        _rate_limited_until = max(_rate_limited_until, time.monotonic() + pause)


def _quote_from_market(market: dict[str, Any], *, now: datetime | None) -> PendleImpliedQuote | None:
    expiry = market.get("expiry")
    if expiry is None:
        pt = market.get("pt") if isinstance(market.get("pt"), dict) else {}
        expiry = pt.get("expiry")
    expiry_text = str(expiry) if expiry else None
    is_active = market.get("isActive")
    active = bool(is_active) if is_active is not None else None
    raw_apy = market.get("impliedApy")
    expired = _is_expired(expiry_text, now=now) or (raw_apy == 0 and active is False)
    if expired:
        # A matured PT redeems 1:1. impliedApy=0 is "no term left", not a failure.
        return PendleImpliedQuote(
            implied_apy=0.0,
            expired=True,
            expiry=expiry_text,
            is_active=active,
            market=str(market.get("address") or "") or None,
        )
    apy = _finite(raw_apy)
    if apy is None:
        return None
    return PendleImpliedQuote(
        implied_apy=apy,
        expired=False,
        expiry=expiry_text,
        is_active=active,
        market=str(market.get("address") or "") or None,
    )


def _is_expired(expiry: str | None, *, now: datetime | None) -> bool:
    if not expiry:
        return False
    text = expiry.replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return moment <= current.astimezone(timezone.utc)


def _finite(raw: object) -> float | None:
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        number = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number
