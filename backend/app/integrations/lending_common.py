"""Shared helpers for EVM lending readers (Aave / Morpho / Pendle).

Pure normalization. No DB writes and no sync hook. Logs never include an
API key or a URL query string.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

# Protocols whose latest fetch hit a transport or parse error. A clean empty
# wallet does not land here. The sync hook reads this so an outage does not
# close the open rows of a protocol we failed to read. Thread-local: a live
# sync and a test can run in different threads without sharing the set.
_failed_local = threading.local()

# Public retry policy for the lending readers. Three retries after the first
# try; a 429/5xx/timeout waits 0.5s, 1.5s, then 3s unless Retry-After is larger.
RETRY_WAITS: tuple[float, ...] = (0.5, 1.5, 3.0)
RETRY_CAP_SECONDS = 8.0
RETRY_HEADERS = {"accept": "application/json", "user-agent": "asset-tracker"}


def note_failure(protocol: str) -> None:
    """Remember that ``protocol`` failed on this thread. Does not log."""
    failed = getattr(_failed_local, "protocols", None)
    if failed is None:
        failed = set()
        _failed_local.protocols = failed
    failed.add(protocol)


def note_source_failure(protocol: str, source: str) -> None:
    """Remember one source (earn/borrow/perps or a market) that failed."""
    by_protocol = getattr(_failed_local, "sources", None)
    if by_protocol is None:
        by_protocol = {}
        _failed_local.sources = by_protocol
    by_protocol.setdefault(protocol, set()).add(str(source))


def take_source_failures() -> dict[str, list[str]]:
    """Return and clear per-source failures as ``{protocol: [source, ...]}``."""
    by_protocol = getattr(_failed_local, "sources", None)
    _failed_local.sources = {}
    return {
        protocol: sorted(sources)
        for protocol, sources in (by_protocol or {}).items()
    }


def take_failures() -> set[str]:
    """Return and clear the protocols that failed since the last take.

    Source-level notes are cleared too so a test or a later sync cannot see a
    stale ``jupiter:earn`` entry. Read ``take_source_failures`` first if both
    are needed.
    """
    failed = getattr(_failed_local, "protocols", None)
    _failed_local.protocols = set()
    _failed_local.sources = {}
    return set(failed or ())

# Aave reports "no debt" as uint256 max. Anything above this is treated as infinite.
_HF_SENTINEL = Decimal("1e18")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def asof_iso(moment: datetime | None = None) -> str:
    value = moment or utcnow()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def norm_address(value: object) -> str:
    return str(value or "").strip().lower()


def dec(value: object, default: str = "0") -> Decimal:
    if value is None or value == "":
        return Decimal(default)
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(default)


def fnum(value: object, places: str | None = None) -> float:
    """JSON-native number. ``places`` quantizes before the float cast."""
    number = dec(value)
    if places is not None:
        number = number.quantize(Decimal(places))
    return float(number)


def money(value: object) -> str:
    """8 dp string, matching the positions fixture style for USDT notionals."""
    return f"{dec(value).quantize(Decimal('0.00000001')):f}"


def log_failure(
    protocol: str,
    chain: str,
    exc: BaseException,
    *,
    source: str | None = None,
    fatal: bool = True,
) -> None:
    """One line, no key and no URL query. Address middles are masked by the log filter.

    ``source`` adds a per-source failure note (``earn``, ``borrow``,
    ``market:<pubkey>``). ``fatal=False`` is for an optional feed whose loss
    does not make the whole protocol unreadable.
    """
    host = ""
    response = getattr(exc, "response", None)
    url = getattr(getattr(exc, "request", None), "url", None)
    if url is None and response is not None:
        url = getattr(getattr(response, "request", None), "url", None)
    if url is not None:
        parts = urlsplit(str(url))
        host = parts.netloc
    # Status is diagnostic and safe: it never contains a key or query string.
    status_code = getattr(response, "status_code", None)
    if source:
        note_source_failure(protocol, source)
    if fatal:
        note_failure(protocol)
    logger.warning(
        "lending %s on %s failed (%s)%s%s",
        protocol,
        chain,
        type(exc).__name__,
        f" host={host}" if host else "",
        f" status={status_code}" if status_code is not None else "",
    )


def mark_hf_unavailable(
    details: dict[str, Any],
    *,
    debt_usd: Decimal,
    hf: object,
    has_debt: bool | None = None,
) -> bool:
    """Flag a row whose debt makes health factor required but uncomputable.

    Adapters publish this marker so the alert layer can distinguish a genuine
    no-debt row (HF is legitimately ``None``) from a debt row whose liquidation
    threshold/price was missing. Without it the latter was completely silent.
    """
    debt_present = debt_usd > 0 if has_debt is None else has_debt
    if debt_present and hf is None:
        details["hf_unavailable"] = True
        return True
    return False


def warn_debt_without_hf(
    protocol: str,
    market_ref: str,
    *,
    debt_usd: Decimal,
    hf: object,
    has_debt: bool | None = None,
) -> None:
    """Warn when debt is present but no health factor could be computed.

    ``has_debt`` lets a caller count a debt leg whose price is missing (USD is
    zero but the token quantity is positive). No wallet address, key or token
    amount is logged; the market/obligation reference locates the row.
    """
    debt_present = debt_usd > 0 if has_debt is None else has_debt
    if debt_present and hf is None:
        logger.warning(
            "lending %s has debt but no health factor (market=%s)",
            protocol,
            market_ref,
        )


def health_factor(
    reported: object,
    *,
    collateral_usd: Decimal,
    debt_usd: Decimal,
    liq_threshold: Decimal | None,
) -> float | None:
    """Prefer the protocol value. Fall back to collateral * liq_threshold / debt.

    Aave's no-debt sentinel (uint256 max, already scaled) becomes ``None``.
    """
    raw = dec(reported, default="nan")
    if raw.is_finite() and raw > 0 and raw < _HF_SENTINEL:
        return float(raw)
    if debt_usd > 0 and liq_threshold is not None and liq_threshold > 0:
        return float((collateral_usd * liq_threshold) / debt_usd)
    if debt_usd <= 0 and collateral_usd > 0:
        return None
    return None


def retry_after_seconds(response: httpx.Response, attempt: int) -> float:
    """Seconds to wait before the next try: Retry-After (s or HTTP-date), else backoff."""
    header = response.headers.get("Retry-After") or response.headers.get("retry-after")
    if header:
        seconds = 0.0
        try:
            seconds = float(header)
        except (TypeError, ValueError):
            try:
                when = parsedate_to_datetime(header)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                seconds = (when - utcnow()).total_seconds()
            except (TypeError, ValueError, OverflowError):
                seconds = 0.0
        if seconds > 0:
            return min(seconds, RETRY_CAP_SECONDS)
    return RETRY_WAITS[min(attempt, len(RETRY_WAITS) - 1)]


def get_json_with_retry(
    http: httpx.Client,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> Any:
    """GET JSON with 429/5xx/timeout backoff, then raise the last error.

    A non-retryable 4xx (other than 429) raises immediately: it is a real
    client error, not a blip. The caller logs it without the query string.
    """
    pause = sleep or time.sleep
    last: Exception | None = None
    for attempt in range(len(RETRY_WAITS) + 1):
        try:
            response = http.get(url, headers=headers or RETRY_HEADERS)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last = exc
            if attempt == len(RETRY_WAITS):
                raise
            pause(RETRY_WAITS[attempt])
            continue
        if response.status_code == 429 or 500 <= response.status_code < 600:
            last = httpx.HTTPStatusError(
                f"HTTP {response.status_code}",
                request=response.request,
                response=response,
            )
            if attempt == len(RETRY_WAITS):
                raise last
            pause(retry_after_seconds(response, attempt))
            continue
        response.raise_for_status()
        return response.json()
    if last is not None:
        raise last
    raise RuntimeError("lending get produced no response")


def net_apy(supply_usd_apy: list[tuple[Decimal, Decimal]], borrow_usd_apy: list[tuple[Decimal, Decimal]], net_usd: Decimal) -> float | None:
    """Value-weighted (supply yield − borrow cost) / net equity. None when net is 0."""
    if net_usd == 0:
        return None
    earned = sum((usd * apy for usd, apy in supply_usd_apy), Decimal("0"))
    paid = sum((usd * apy for usd, apy in borrow_usd_apy), Decimal("0"))
    return float((earned - paid) / net_usd)


def leg(
    *,
    symbol: str,
    amount: object,
    usd: object,
    contract: str = "",
    apy_supply: object | None = None,
    apy_borrow: object | None = None,
    lowercase_contract: bool = True,
) -> dict[str, Any]:
    contract_text = str(contract or "").strip()
    row: dict[str, Any] = {
        "symbol": symbol,
        "amount": fnum(amount),
        "usd": fnum(usd),
        "contract": norm_address(contract_text) if lowercase_contract else contract_text,
    }
    if apy_supply is not None:
        row["apy_supply"] = fnum(apy_supply)
    if apy_borrow is not None:
        row["apy_borrow"] = fnum(apy_borrow)
    return row


def position_row(
    *,
    protocol: str,
    chain: str,
    account_ref: str,
    market_ref: str,
    label: str,
    collateral_usd: Decimal,
    debt_usd: Decimal,
    details: dict[str, Any],
    opened_at: str | None = None,
    asof: str | None = None,
    lowercase_account: bool = True,
) -> dict[str, Any]:
    net = collateral_usd - debt_usd
    ref = str(account_ref or "").strip()
    return {
        "kind": "lending",
        "protocol": protocol,
        "chain": chain,
        "account_ref": norm_address(ref) if lowercase_account else ref,
        "market_ref": market_ref,
        "label": label,
        "status": "open",
        "net_value_usdt": money(net),
        "collateral_usdt": money(collateral_usd),
        "debt_usdt": money(debt_usd),
        "opened_at": opened_at,
        "asof": asof or asof_iso(),
        "details": details,
    }


def empty_details(**overrides: Any) -> dict[str, Any]:
    details: dict[str, Any] = {
        "collateral": [],
        "borrow": [],
        "health_factor": None,
        "ltv": None,
        "liq_threshold": None,
        "liq_price": None,
        "net_apy": None,
        "interest_paid_usd": None,
        "interest_earned_usd": None,
        "pnl_usd": None,
        "pnl_apr": None,
    }
    details.update(overrides)
    return details
