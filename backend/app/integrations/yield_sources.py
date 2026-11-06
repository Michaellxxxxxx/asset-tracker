"""Public yield feeds for collateral whose protocol rate is not the real yield.

Three tiny, read-only sources:

* DefiLlama ``GET https://yields.llama.fi/chart/<pool>`` for USD3 supplied on
  3Jane (pool ``f8cd444e-d99f-4132-b234-fd3482bf8806``, Ethereum).
* DefiLlama ``GET https://yields.llama.fi/chart/<pool>`` for ONyc supplied on
  Kamino (OnRe tokenized reinsurance, pool
  ``7083d6a5-e3cb-4eeb-8204-f1b735e4ecbb``, Solana). Kamino's own supplyApy is
  zero; OnRe's real yield is on the DefiLlama ``onre`` pool.
* Jupiter ``GET https://perps-api.jup.ag/v1/jlp-info`` for JLP's official APY
  (``jlpApyPct``). JLP's price return over a short window is not a yield.

All return a fraction (0.0628 = 6.28%). A missing feed returns ``None``; this
module never turns a miss into 0 and never estimates a value. Results are
cached briefly, failures for less time than a valid miss.
"""

from __future__ import annotations

import logging
import threading
import time
from decimal import Decimal
from typing import Any, Callable

import httpx

from .http import quiet_client
from .lending_common import dec, log_failure

logger = logging.getLogger(__name__)

DEFILLAMA_BASE = "https://yields.llama.fi"
USD3_POOL_ID = "f8cd444e-d99f-4132-b234-fd3482bf8806"
USD3_SOURCE = "defillama:3jane-lending"
USD3_CHAIN = "ethereum"

JUPITER_JLP_URL = "https://perps-api.jup.ag/v1/jlp-info"
JLP_SOURCE = "jupiter:jlp-info"
JLP_CHAIN = "solana"

REQUEST_TIMEOUT_SECONDS = 8.0
USD3_CACHE_TTL_SECONDS = 3600.0

# ONyc (OnRe tokenized reinsurance) on Solana. DefiLlama's ``onre`` pool carries
# the real yield; the kamino-lend ONyc pool only reports the lending supply
# rate (0), so it must not be used here.
ONYC_POOL_ID = "7083d6a5-e3cb-4eeb-8204-f1b735e4ecbb"
ONYC_SOURCE = "defillama:onre"
ONYC_CHAIN = "solana"
ONYC_CACHE_TTL_SECONDS = 3600.0

JLP_CACHE_TTL_SECONDS = 300.0
FAIL_BACKOFF_SECONDS = 60.0

# key -> (expires monotonic, quote | None). None is a short negative cache.
_cache: dict[str, tuple[float, "YieldQuote | None"]] = {}
_lock = threading.Lock()


class YieldQuote:
    """A reported yield. ``apy`` is a fraction (0.0633 = 6.33%)."""

    __slots__ = ("apy", "source", "asof")

    def __init__(self, *, apy: Decimal, source: str, asof: object = None) -> None:
        self.apy = apy
        self.source = source
        self.asof = asof


def clear_cache() -> None:
    """Drop process-local quotes (tests and a manual refresh)."""
    with _lock:
        _cache.clear()


def usd3_collateral_apy(
    *, client: httpx.Client | None = None
) -> YieldQuote | None:
    """Latest DefiLlama APY for the 3Jane USD3 pool, or ``None``."""
    return _cached(
        "defillama:usd3",
        USD3_CACHE_TTL_SECONDS,
        lambda: _fetch_usd3(client),
    )


def jlp_collateral_apy(
    *, client: httpx.Client | None = None
) -> YieldQuote | None:
    """Jupiter's official JLP APY (``jlpApyPct``), or ``None``."""
    return _cached(
        "jupiter:jlp",
        JLP_CACHE_TTL_SECONDS,
        lambda: _fetch_jlp(client),
    )


def onyc_collateral_apy(
    *, client: httpx.Client | None = None
) -> YieldQuote | None:
    """Latest DefiLlama APY for the OnRe ONyc pool, or ``None``."""
    return _cached(
        "defillama:onyc",
        ONYC_CACHE_TTL_SECONDS,
        lambda: _fetch_onyc(client),
    )


def _cached(
    key: str,
    ttl: float,
    fetch: Callable[[], "YieldQuote | None"],
) -> YieldQuote | None:
    moment = time.monotonic()
    with _lock:
        entry = _cache.get(key)
        if entry is not None and moment < entry[0]:
            return entry[1]
    quote = fetch()
    expires = time.monotonic() + (ttl if quote is not None else FAIL_BACKOFF_SECONDS)
    with _lock:
        _cache[key] = (expires, quote)
    return quote


def _fetch_usd3(client: httpx.Client | None) -> YieldQuote | None:
    url = f"{DEFILLAMA_BASE}/chart/{USD3_POOL_ID}"
    payload = _get_json("defillama", USD3_CHAIN, url, client)
    if payload is None:
        return None
    quote = _usd3_from_chart(payload)
    if quote is None:
        logger.warning("defillama USD3 chart has no usable APY")
    return quote


def _fetch_jlp(client: httpx.Client | None) -> YieldQuote | None:
    payload = _get_json("jupiter_jlp", JLP_CHAIN, JUPITER_JLP_URL, client)
    if payload is None:
        return None
    quote = _jlp_from_info(payload)
    if quote is None:
        logger.warning("jupiter jlp-info has no usable jlpApyPct")
    return quote


def _fetch_onyc(client: httpx.Client | None) -> YieldQuote | None:
    url = f"{DEFILLAMA_BASE}/chart/{ONYC_POOL_ID}"
    payload = _get_json("defillama", ONYC_CHAIN, url, client)
    if payload is None:
        return None
    quote = _onyc_from_chart(payload)
    if quote is None:
        logger.warning("defillama ONyc chart has no usable APY")
    return quote


def _get_json(
    protocol: str,
    chain: str,
    url: str,
    client: httpx.Client | None,
) -> dict[str, Any] | None:
    own_client = client is None
    http = client or quiet_client(timeout=REQUEST_TIMEOUT_SECONDS)
    try:
        response = http.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # noqa: BLE001 — one yield feed must not sink a position
        # fatal=False: this is an optional feed, not the protocol reader.
        log_failure(protocol, chain, exc, fatal=False)
        return None
    finally:
        if own_client:
            http.close()
    if not isinstance(payload, dict):
        logger.warning("%s yield payload is not an object", protocol)
        return None
    return payload


def _usd3_from_chart(payload: dict[str, Any]) -> YieldQuote | None:
    """Last chart point's APY for USD3 (DefiLlama percent -> fraction)."""
    return _chart_quote(payload, USD3_SOURCE)


def _onyc_from_chart(payload: dict[str, Any]) -> YieldQuote | None:
    """Last chart point's APY for ONyc (DefiLlama percent -> fraction)."""
    return _chart_quote(payload, ONYC_SOURCE)


def _chart_quote(payload: dict[str, Any], source: str) -> YieldQuote | None:
    """Last chart point's APY. DefiLlama reports percent; convert to a fraction."""
    data = payload.get("data")
    if not isinstance(data, list):
        return None
    for point in reversed(data):
        if not isinstance(point, dict):
            continue
        # ``apy`` already includes rewards. ``apyBase`` is only a fallback for
        # payloads that omit the total; neither is ever synthesised.
        for key in ("apy", "apyBase"):
            raw = point.get(key)
            if raw in (None, ""):
                continue
            value = dec(raw, default="nan")
            if not value.is_finite():
                continue
            return YieldQuote(
                apy=value / Decimal("100"),
                source=source,
                asof=point.get("timestamp"),
            )
    return None


def _jlp_from_info(payload: dict[str, Any]) -> YieldQuote | None:
    """Official JLP APY. A missing ``jlpApyPct`` is a miss, never an estimate."""
    raw = payload.get("jlpApyPct")
    if raw in (None, ""):
        return None
    value = dec(raw, default="nan")
    if not value.is_finite():
        return None
    return YieldQuote(
        apy=value / Decimal("100"),
        source=JLP_SOURCE,
        asof=payload.get("jlpAprLastUpdatedTimestamp"),
    )