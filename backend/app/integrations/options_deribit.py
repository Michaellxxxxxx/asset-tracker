"""Deribit option marks from the public market-data API. No key, no orders.

Public methods used (``https://www.deribit.com/api/v2/public/…``):

* ``get_book_summary_by_currency`` — mark, mark IV, underlying.
* ``ticker`` — the same, plus greeks.
* ``get_index_price`` — BTC/ETH (and ``*_usdc``) index, to convert an inverse
  (coin-settled) mark into USD.

Inverse options (BTC/ETH) quote mark as a fraction of one coin, so
``mark_usd = mark × index``. Linear USDC options quote mark in USDC already.
Nothing here calls a ``private/`` method or reads a credential.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..options_calc import dec, fnum
from .http import quiet_client

logger = logging.getLogger(__name__)

DERIBIT_API = "https://www.deribit.com/api/v2/public"
# Currencies whose option book is coin-margined. USDC books are linear.
INVERSE_CURRENCIES = frozenset({"BTC", "ETH"})


class DeribitPublicError(Exception):
    """Public market-data call failed. Message never includes a query string."""


def book_summary(
    currency: str,
    *,
    client: httpx.Client | None = None,
    kind: str = "option",
) -> list[dict[str, Any]]:
    """``get_book_summary_by_currency``. Raises ``DeribitPublicError`` on failure."""
    payload = _rpc(
        "get_book_summary_by_currency",
        {"currency": currency.upper(), "kind": kind},
        client=client,
    )
    if not isinstance(payload, list):
        raise DeribitPublicError("book summary was not a list")
    return [row for row in payload if isinstance(row, dict)]


def ticker(instrument_name: str, *, client: httpx.Client | None = None) -> dict[str, Any]:
    """``public/ticker``. One instrument, including greeks."""
    payload = _rpc("ticker", {"instrument_name": instrument_name}, client=client)
    if not isinstance(payload, dict):
        raise DeribitPublicError("ticker was not an object")
    return payload


def index_price(index_name: str, *, client: httpx.Client | None = None) -> Decimal:
    """``get_index_price``. ``index_name`` is ``btc_usd`` / ``eth_usd`` / ``eth_usdc``."""
    payload = _rpc("get_index_price", {"index_name": index_name}, client=client)
    if not isinstance(payload, dict) or payload.get("index_price") is None:
        raise DeribitPublicError("index price missing")
    return dec(payload["index_price"])


def quote_to_usd(mark_price: object, *, quote_currency: str, index_usd: object) -> Decimal:
    """Coin-quoted mark × index = USD. USDC (linear) marks are already USD."""
    mark = dec(mark_price)
    if quote_currency.upper() in INVERSE_CURRENCIES:
        return mark * dec(index_usd)
    return mark


def normalize_book_row(row: dict[str, Any], *, index_usd: object | None = None) -> dict[str, Any]:
    """One book-summary row → mark in USD plus IV as a fraction (Deribit sends percent).

    Greeks are absent on the book summary; they stay ``None`` until a ticker is merged.
    """
    quote = str(row.get("quote_currency") or row.get("base_currency") or "").upper()
    index = index_usd if index_usd is not None else row.get("underlying_price")
    mark = quote_to_usd(row.get("mark_price"), quote_currency=quote, index_usd=index)
    return {
        "instrument_name": str(row.get("instrument_name") or ""),
        "quote_currency": quote,
        "mark_price": fnum(row.get("mark_price")),
        "mark_price_usd": fnum(mark),
        "mark_iv": fnum(_iv_fraction(row.get("mark_iv"))),
        "underlying_price": fnum(row.get("underlying_price")),
        "index_usd": fnum(index) if index is not None else None,
        "open_interest": fnum(row.get("open_interest")),
        "delta": None,
        "greeks": None,
    }


def normalize_ticker(row: dict[str, Any], *, index_usd: object | None = None) -> dict[str, Any]:
    """Ticker row: book fields plus delta and the greeks object."""
    base = normalize_book_row(row, index_usd=index_usd if index_usd is not None else row.get("index_price"))
    greeks = row.get("greeks") if isinstance(row.get("greeks"), dict) else {}
    base["delta"] = fnum(greeks["delta"]) if greeks.get("delta") is not None else None
    base["greeks"] = {
        "delta": fnum(greeks["delta"]) if greeks.get("delta") is not None else None,
        "gamma": fnum(greeks["gamma"]) if greeks.get("gamma") is not None else None,
        "vega": fnum(greeks["vega"]) if greeks.get("vega") is not None else None,
        "theta": fnum(greeks["theta"]) if greeks.get("theta") is not None else None,
        "rho": fnum(greeks["rho"]) if greeks.get("rho") is not None else None,
    }
    base["mark_iv"] = fnum(_iv_fraction(row.get("mark_iv")))
    return base


def apply_mark(position: dict[str, Any], quote: dict[str, Any]) -> dict[str, Any]:
    """Copy a normalized quote's USD mark / IV / greeks onto a position dict.

    Does not recompute PnL — that is ``options_calc.value_position``. The position
    is shallow-copied; nested details are copied too so the caller is not mutated.
    """
    updated = dict(position)
    updated["mark_price"] = quote.get("mark_price_usd")
    details = dict(updated.get("details") or {})
    details["mark_price_usd"] = quote.get("mark_price_usd")
    details["iv"] = quote.get("mark_iv")
    if quote.get("delta") is not None:
        details["delta"] = quote.get("delta")
    if quote.get("greeks"):
        details["greeks"] = quote.get("greeks")
    updated["details"] = details
    return updated


def _iv_fraction(value: object) -> Decimal:
    """Deribit ``mark_iv`` is a percent (42.74 → 0.4274). Already-fractional values pass."""
    raw = dec(value)
    if abs(raw) > 3:
        return raw / Decimal(100)
    return raw


def _rpc(method: str, params: dict[str, Any], *, client: httpx.Client | None) -> Any:
    own = client is None
    http = client or quiet_client(timeout=20.0)
    url = f"{DERIBIT_API}/{method}"
    try:
        response = http.get(url, params=params)
        response.raise_for_status()
        body = response.json()
    except Exception as exc:  # noqa: BLE001 — one public call, re-raised without the query
        raise DeribitPublicError(_safe_message(method, exc)) from exc
    finally:
        if own:
            http.close()
    if not isinstance(body, dict) or body.get("error"):
        raise DeribitPublicError(f"{method} returned an error")
    return body.get("result")


def _safe_message(method: str, exc: BaseException) -> str:
    host = ""
    url = getattr(getattr(exc, "request", None), "url", None)
    if url is not None:
        host = urlsplit(str(url)).netloc
    where = f" host={host}" if host else ""
    return f"{method} failed ({type(exc).__name__}){where}"


def expiry_iso(instrument_name: str) -> str | None:
    """``BTC-27MAR26-100000-C`` → ``2026-03-27T08:00:00Z`` (Deribit expires 08:00 UTC)."""
    parts = instrument_name.split("-")
    if len(parts) < 2:
        return None
    try:
        parsed = datetime.strptime(parts[1], "%d%b%y").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    parsed = parsed.replace(hour=8)
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")
