"""Jupiter Lend (Earn + Borrow) and Jupiter Perps leverage positions.

Read-only public APIs, no key:

* Earn  ``GET https://api.jup.ag/lend/v1/earn/positions?users=``
  Supply-only receipts (``jl*``). ``totalRate`` is the supply APR in bps.
* Borrow ``GET https://api.jup.ag/lend/v1/borrow/positions?users=``
  NFT positions. Amounts are base units. ``supplyRate`` / ``borrowRate`` on
  the vault are APR in bps. ``liquidationThreshold`` is the LTV (in 1/1000)
  at which the position is liquidated, so health factor is
  ``collateral * threshold / debt``.
* Perps ``GET https://perps-api.jup.ag/v1/positions?walletAddress=``
  Leveraged longs/shorts. ``collateralUsd`` is the margin and
  ``pnlAfterFeesUsd`` is the unrealized pnl. Equity is
  ``collateral + pnl`` (the same formula for long and short; a short with a
  loss just has a negative pnl). ``sizeUsd`` is the notional, not debt: the
  borrowed amount is already reflected in the pnl, so borrowing it again would
  double-count the loss. The row therefore reports debt 0 while equity is
  positive, and ``max(-equity, 0)`` only in the rare bad-debt case where a loss
  exceeds the margin. ``sizeUsd`` is kept in ``details.notional_usd``. A
  zero-size row is closed and skipped.

One wallet, one source. A source that errors is recorded with its name
(``earn`` / ``borrow`` / ``perps``); the other sources still land as a partial
read. The adapter also notes the protocol so the hook keeps the open rows of
the failed source and marks them stale. A clean empty list is not a failure.

A source whose shape no longer matches the documented contract (a required
field missing or renamed, a non-numeric amount) is failed as a whole, not
silently normalized to zero: see :class:`JupiterContractError`. The failed
source keeps its old rows; a sibling source that did parse still updates.

The 429 cooldown is keyed by ``(wallet, source)``, not by wallet. A rate limit
on ``earn`` must not freeze ``borrow``/``perps`` for the same wallet: the
cooldown scope matches the failure scope, and only the cooling source is
reported as failed/stale. A per-wallet cooldown would suppress healthy
sources and delay their refresh for no benefit.

A quantity with no price is not dust. Earn receipts and Borrow legs whose
token price is missing or zero are kept, valued from whatever is available
(Earn falls back to ``underlyingBalance``) and flagged with ``price_missing``
plus ``price_missing_legs``, the same contract Borrow uses. Only amount-free
rows are dropped.

``api.jup.ag`` returns 429 under a tight burst (a live sync hits earn, borrow,
and perps for every Solana wallet). 429, 5xx and timeouts are retried with
backoff and ``Retry-After`` (lending_common policy) before a source is failed.
Once a source survives all retries with 429 it enters a cooldown for that
wallet+source only; the next sync retries the other sources.

The cooldown deadline is also written to Redis as an epoch second with a TTL,
under a SHA-256 hash of the wallet (never the plaintext address), so the app
and worker containers share the same cooldown and a restart does not hammer a
rate-limited endpoint again. Redis is best-effort: a missing or failing Redis
silently falls back to the process-local dict and is never allowed to raise
into the sync. Tests inject fakeredis (or a stub) and never dial a real server.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import random
import time
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

import httpx

from .http import quiet_client
from .lending_common import (
    RETRY_HEADERS,
    RETRY_WAITS,
    RETRY_CAP_SECONDS,
    asof_iso,
    dec,
    empty_details,
    fnum,
    health_factor,
    leg,
    log_failure,
    mark_hf_unavailable,
    net_apy,
    position_row,
    retry_after_seconds,
    warn_debt_without_hf,
)

logger = logging.getLogger(__name__)

PROTOCOL = "jupiter"
CHAIN = "solana"
LEND_URL = "https://api.jup.ag/lend/v1"
PERPS_URL = "https://perps-api.jup.ag/v1"

# Jupiter rates are integer basis points (435 = 4.35%). Liquidation threshold
# and collateral factor on borrow vaults are in thousandths (850 = 0.85).
_BPS = Decimal("10000")
_MILLI = Decimal("1000")
_USD_SCALE = Decimal("1000000")  # perps USD fields are USDC atomic (6 dp)
_MIN_USD = Decimal("0.01")

# Accepted spellings for optional pnl / interest fields. Jupiter's current
# Earn/Borrow docs list none of these; the sets keep a future field addition
# usable without inventing a value from collateral or deposit size.
_PNL_KEYS = ("pnlUsd", "pnl_usd", "unrealizedPnlUsd", "unrealized_pnl_usd", "pnl")
_INTEREST_PAID_KEYS = (
    "interestPaidUsd",
    "interest_paid_usd",
    "interestPaid",
    "borrowInterestUsd",
    "borrow_interest_usd",
)
_INTEREST_EARNED_KEYS = (
    "interestEarnedUsd",
    "interest_earned_usd",
    "interestEarned",
    "earnedInterestUsd",
    "earned_interest_usd",
)
# 429 / 5xx / timeout policy: four tries, capped Retry-After, exponential
# backoff plus jitter. A (wallet, source) pair that exhausts 429s enters a
# short cooldown (persisted to Redis when available) so the next sync does not
# hammer that endpoint.
RATE_LIMIT_COOLDOWN_SECONDS = 60.0
_JITTER_SECONDS = 0.25
_MAX_RETRY_SLEEP = 12.0
# (wallet, source) -> monotonic deadline. Deliberately not keyed by wallet:
# a 429 on one endpoint must not stop the other two endpoints from refreshing.
_COOLDOWN_UNTIL: dict[tuple[str, str], float] = {}
# Redis is an optional shared store for the same deadline. Keys carry a hashed
# wallet so the address is not kept in plaintext; values are epoch seconds and
# the key expires with the cooldown.
_COOLDOWN_REDIS_PREFIX = "asset-tracker:jupiter:cooldown"
_COOLDOWN_REDIS_TIMEOUT_SECONDS = 1.0
# After a Redis error, skip further attempts for a while instead of paying the
# connect timeout once per source per wallet.
_COOLDOWN_REDIS_RETRY_SECONDS = 30.0
_COOLDOWN_REDIS: Any | None = None
_COOLDOWN_REDIS_RETRY_AT = 0.0


class JupiterContractError(ValueError):
    """A source payload no longer matches the Jupiter response contract.

    Raised before a missing/renamed field can be coerced to ``0``. The message
    names only the source and field, never a wallet, key or amount.
    """

    def __init__(self, source: str, detail: str) -> None:
        super().__init__(f"{source} contract: {detail}")
        self.source = source


def configure_cooldown_redis(client: Any | None) -> None:
    """Inject the Redis client used for cooldown persistence.

    ``None`` restores the default: create a client lazily from
    ``settings.redis_url`` only when ``REDIS_URL`` is explicitly set, otherwise
    stay process-local. Tests inject fakeredis or a stub so they never dial a
    real Redis server.
    """
    global _COOLDOWN_REDIS, _COOLDOWN_REDIS_RETRY_AT
    _COOLDOWN_REDIS = client
    _COOLDOWN_REDIS_RETRY_AT = 0.0


def reset_cooldowns(client: Any | None = None) -> None:
    """Clear the process-local 429 cooldowns (tests / manual recovery).

    Passing a Redis client also deletes the persisted keys. The default does
    not touch Redis, so unit tests and callers without Redis stay exactly as
    before.
    """
    _COOLDOWN_UNTIL.clear()
    if client is None:
        return
    try:
        for key in client.scan_iter(match=f"{_COOLDOWN_REDIS_PREFIX}:*"):
            client.delete(key)
    except Exception as exc:  # noqa: BLE001 — recovery must not raise
        logger.debug(
            "jupiter cooldown Redis reset skipped (%s)", type(exc).__name__
        )


def _cooldown_key(owner: str, source: str) -> str:
    """Hashed wallet + source. The address is never stored in the key."""
    digest = hashlib.sha256(owner.encode("utf-8")).hexdigest()
    return f"{_COOLDOWN_REDIS_PREFIX}:{digest}:{source}"


def _cooldown_redis() -> Any | None:
    """Lazily build the shared client, or ``None`` when persistence is off."""
    global _COOLDOWN_REDIS, _COOLDOWN_REDIS_RETRY_AT
    if time.monotonic() < _COOLDOWN_REDIS_RETRY_AT:
        return None
    if _COOLDOWN_REDIS is not None:
        return _COOLDOWN_REDIS
    # Only use Redis when the deployment opted in (compose sets REDIS_URL for
    # both app and worker). The default pytest run has no such env var and must
    # not try to reach a real server.
    if not os.getenv("REDIS_URL", "").strip():
        return None
    try:
        from redis import Redis

        from ..config import settings

        _COOLDOWN_REDIS = Redis.from_url(
            settings.redis_url,
            socket_connect_timeout=_COOLDOWN_REDIS_TIMEOUT_SECONDS,
            socket_timeout=_COOLDOWN_REDIS_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 — optional dependency
        _mark_redis_unavailable(exc)
        return None
    return _COOLDOWN_REDIS


def _mark_redis_unavailable(exc: BaseException) -> None:
    global _COOLDOWN_REDIS_RETRY_AT
    _COOLDOWN_REDIS_RETRY_AT = time.monotonic() + _COOLDOWN_REDIS_RETRY_SECONDS
    logger.debug(
        "jupiter cooldown Redis unavailable, using in-process only (%s)",
        type(exc).__name__,
    )


def _redis_cooldown_remaining(owner: str, source: str) -> float:
    """Remaining cooldown from the shared store; 0 on any failure (fail-open)."""
    client = _cooldown_redis()
    if client is None:
        return 0.0
    key = _cooldown_key(owner, source)
    try:
        raw = client.get(key)
    except Exception as exc:  # noqa: BLE001 — never break the sync
        _mark_redis_unavailable(exc)
        return 0.0
    if raw is None:
        return 0.0
    try:
        until = float(raw)
    except (TypeError, ValueError):
        return 0.0
    remaining = until - time.time()
    if remaining <= 0:
        # The value is authoritative; the TTL would have removed it shortly.
        try:
            client.delete(key)
        except Exception as exc:  # noqa: BLE001
            _mark_redis_unavailable(exc)
        return 0.0
    return remaining


def _persist_cooldown(owner: str, source: str) -> None:
    """Store the epoch deadline with a TTL; failures are logged at debug only."""
    client = _cooldown_redis()
    if client is None:
        return
    try:
        client.set(
            _cooldown_key(owner, source),
            int(time.time() + RATE_LIMIT_COOLDOWN_SECONDS),
            ex=max(1, int(math.ceil(RATE_LIMIT_COOLDOWN_SECONDS))),
        )
    except Exception as exc:  # noqa: BLE001 — the local deadline still works
        _mark_redis_unavailable(exc)


def _cooldown_remaining(owner: str, source: str) -> float:
    key = (owner, source)
    until = _COOLDOWN_UNTIL.get(key)
    if until is not None:
        remaining = until - time.monotonic()
        if remaining > 0:
            return remaining
        _COOLDOWN_UNTIL.pop(key, None)
    # The local dict is empty after a restart, so the shared deadline is the
    # only thing that prevents an immediate re-hit. Cache it locally too.
    remaining = _redis_cooldown_remaining(owner, source)
    if remaining > 0:
        _COOLDOWN_UNTIL[key] = time.monotonic() + remaining
    return remaining


def _cool_down(owner: str, source: str) -> None:
    _COOLDOWN_UNTIL[(owner, source)] = time.monotonic() + RATE_LIMIT_COOLDOWN_SECONDS
    _persist_cooldown(owner, source)


def _backoff(attempt: int) -> float:
    return 0.5 * (2 ** attempt)


def fetch_positions(
    address: str,
    *,
    client: httpx.Client | None = None,
    use_mock: bool = False,
    sleep: Callable[[float], None] | None = None,
) -> list[dict]:
    """Normalized Jupiter lending/leverage rows. Empty on a total failure."""
    if use_mock:
        import json
        from pathlib import Path

        path = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "lending_solana" / "jupiter_user.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        return normalize(address, payload)

    owner = _sol_address(address)
    if not owner:
        return []
    own_client = client is None
    http = client or quiet_client(timeout=20.0)
    bundle: dict[str, Any] = {"earn": [], "borrow": [], "perps": []}
    try:
        for key, path in (
            ("earn", f"{LEND_URL}/earn/positions?users={owner}"),
            ("borrow", f"{LEND_URL}/borrow/positions?users={owner}"),
            ("perps", f"{PERPS_URL}/positions?walletAddress={owner}"),
        ):
            remaining = _cooldown_remaining(owner, key)
            if remaining > 0:
                # Only this endpoint is skipped. Its old rows stay open (the
                # protocol is noted failed and marked stale); borrow/perps are
                # still requested for this same wallet.
                log_failure(
                    PROTOCOL,
                    CHAIN,
                    RuntimeError(f"jupiter {key} 429 cooldown active for {remaining:.0f}s"),
                    source=key,
                )
                bundle[key] = []
                continue
            try:
                bundle[key] = _get_json(
                    http, path, sleep=sleep, owner=owner, source=key
                )
            except Exception as exc:  # noqa: BLE001 — one source must not sink the others
                log_failure(PROTOCOL, CHAIN, exc, source=key)
                bundle[key] = []
        # Successful sources still land. The failed source's old rows stay
        # open because the protocol is noted; the hook marks them stale.
        return normalize(owner, bundle)
    finally:
        if own_client:
            http.close()


def normalize(address: str, payload: dict[str, Any]) -> list[dict]:
    """Turn an earn + borrow + perps bundle into positions rows.

    Each source is normalized independently. A contract error fails only that
    source (and is recorded in ``failed_sources``); the other sources still
    return rows.
    """
    owner = _sol_address(address)
    if not isinstance(payload, dict):
        return []
    moment = asof_iso()
    rows: list[dict] = []
    for source, build in (
        ("earn", _earn_rows),
        ("borrow", _borrow_rows),
        ("perps", _perps_rows),
    ):
        try:
            rows.extend(build(owner, payload.get(source), moment))
        except JupiterContractError as exc:
            # Do not let one renamed field poison the source it belongs to,
            # and never turn it into a zero-valued row.
            logger.warning("lending %s contract mismatch: %s", PROTOCOL, exc)
            log_failure(PROTOCOL, CHAIN, exc, source=source)
    return rows


def _source_items(payload: Any, source: str) -> list[dict[str, Any]]:
    """Validate the top-level array shape, or fail the source explicitly.

    ``None`` means the caller did not request this source (unit tests and old
    callers); it is not a contract failure. A dict, string or list of non-object
    items is a wire-contract change and must not be read as "no positions".
    """
    if payload is None:
        return []
    if not isinstance(payload, list):
        raise JupiterContractError(
            source, f"expected a JSON array, got {type(payload).__name__}"
        )
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise JupiterContractError(source, f"item {index} is not an object")
    return payload


def _contract_number(item: dict[str, Any], key: str, source: str) -> Decimal:
    """Required numeric field. Missing/renamed/blank is a contract error."""
    if key not in item:
        raise JupiterContractError(source, f"missing field {key}")
    value = item[key]
    if value is None or isinstance(value, (bool, dict, list)):
        raise JupiterContractError(source, f"field {key} is not numeric")
    if isinstance(value, str) and not value.strip():
        raise JupiterContractError(source, f"field {key} is blank")
    try:
        raw = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise JupiterContractError(source, f"field {key} is not numeric") from exc
    if not raw.is_finite():
        raise JupiterContractError(source, f"field {key} is not finite")
    return raw


def _contract_decimals(
    container: dict[str, Any], source: str, label: str
) -> int | None:
    """Token decimals when present; the caller decides if they are required."""
    if "decimals" not in container:
        return None
    raw = _contract_number(container, "decimals", source)
    if raw != raw.to_integral_value():
        raise JupiterContractError(source, f"field {label}.decimals is not an integer")
    return int(raw)


def _earn_rows(owner: str, payload: Any, moment: str) -> list[dict]:
    items = _source_items(payload, "earn")
    # Group every receipt of this wallet into one supply position. Splitting
    # per token would hide the protocol-level net the lending page sums.
    collaterals: list[dict] = []
    supply_pairs: list[tuple[Decimal, Decimal]] = []
    missing_symbols: list[str] = []
    total = Decimal("0")
    # Earn's documented fields carry no pnl/interest history. Read them when a
    # future API adds them, but never derive them from the deposit value.
    pnl_total = Decimal("0")
    interest_paid_total = Decimal("0")
    interest_earned_total = Decimal("0")
    pnl_seen = interest_paid_seen = interest_earned_seen = False
    for item in items:
        if _sol_address(item.get("ownerAddress")) not in ("", owner):
            continue
        # shares and underlyingAssets are base units of the receipt / deposit.
        # A row with neither has nothing in the protocol: Jupiter still returns
        # a zero-share row for every earn token. Both fields are required so a
        # rename cannot silently turn a real deposit into zero.
        shares = _contract_number(item, "shares", "earn")
        assets_raw = _contract_number(item, "underlyingAssets", "earn")
        if shares <= 0 and assets_raw <= 0:
            continue
        token = item.get("token")
        if not isinstance(token, dict):
            raise JupiterContractError("earn", "missing field token")
        # The live response nests the underlying under ``token.asset``. The
        # fixture flattens price/decimals onto ``token`` itself, so accept both.
        asset = token.get("asset") if isinstance(token.get("asset"), dict) else {}
        if not asset and dec(token.get("price") or 0) > 0:
            asset = token
        decimals_raw = _contract_decimals(asset, "earn", "asset")
        if decimals_raw is None and asset is not token:
            decimals_raw = _contract_decimals(token, "earn", "token")
        if decimals_raw is None:
            raise JupiterContractError("earn", "missing field token.decimals")
        decimals = decimals_raw
        amount = assets_raw / (Decimal(10) ** decimals) if decimals else assets_raw
        price = dec(asset.get("price") or 0)
        symbol = str(token.get("symbol") or asset.get("symbol") or "jl")
        # underlyingBalance is also base units (the published example shows a
        # human number, but the live API returns atomic units). Prefer the
        # deposit's own value; the balance is only a fallback.
        balance = dec(item.get("underlyingBalance"))
        if decimals:
            balance = balance / (Decimal(10) ** decimals)
        usd = amount * price if price > 0 else balance
        if price <= 0 and amount > 0:
            # The quantity is real even when the feed has no price. Do not value
            # it at zero and discard it; keep it with the best available number
            # and let the caller see why the valuation is partial.
            missing_symbols.append(symbol)
        if usd < _MIN_USD and amount <= 0:
            continue
        apy = _bps(token.get("totalRate"))
        collaterals.append(leg(
            symbol=symbol,
            amount=amount,
            usd=usd,
            contract=str(token.get("address") or ""),
            apy_supply=apy,
            lowercase_contract=False,
        ))
        total += usd
        if apy is not None:
            supply_pairs.append((usd, apy))
        pnl = _api_money(item, _PNL_KEYS)
        if pnl is not None:
            pnl_total += pnl
            pnl_seen = True
        paid = _api_money(item, _INTEREST_PAID_KEYS)
        if paid is not None:
            interest_paid_total += paid
            interest_paid_seen = True
        earned = _api_money(item, _INTEREST_EARNED_KEYS)
        if earned is not None:
            interest_earned_total += earned
            interest_earned_seen = True
    if total < _MIN_USD and not missing_symbols:
        return []
    missing_symbols = sorted(set(missing_symbols))
    if missing_symbols:
        logger.warning(
            "lending %s earn has unpriced holdings (market=%s tokens=%s)",
            PROTOCOL,
            "jupiter-lend-earn",
            ",".join(missing_symbols),
        )
    details = empty_details(
        collateral=collaterals,
        borrow=[],
        health_factor=None,
        ltv=None,
        liq_threshold=None,
        liq_price=None,
        net_apy=net_apy(supply_pairs, [], total),
        interest_paid_usd=fnum(interest_paid_total) if interest_paid_seen else None,
        interest_earned_usd=fnum(interest_earned_total) if interest_earned_seen else None,
        pnl_usd=fnum(pnl_total) if pnl_seen else None,
        pnl_apr=None,
        **(
            # Same contract as Borrow: the collateral leg has no usable price.
            {"price_missing": True, "price_missing_legs": ["collateral"]}
            if missing_symbols
            else {}
        ),
        **({"pnl_data_missing": True} if not pnl_seen else {}),
    )
    return [position_row(
        protocol=PROTOCOL,
        chain=CHAIN,
        account_ref=owner,
        market_ref="jupiter-lend-earn",
        label="Jupiter Earn",
        collateral_usd=total,
        debt_usd=Decimal("0"),
        details=details,
        asof=moment,
        lowercase_account=False,
    )]


def _borrow_rows(owner: str, payload: Any, moment: str) -> list[dict]:
    items = _source_items(payload, "borrow")
    rows: list[dict] = []
    for item in items:
        try:
            built = _one_borrow(owner, item, moment)
        except JupiterContractError:
            # A contract break is source-wide, not a row to skip: re-raise so
            # normalize fuses the source, keeps its old rows and marks stale.
            raise
        except Exception as exc:  # noqa: BLE001
            log_failure(PROTOCOL, CHAIN, exc, source="borrow")
            continue
        if built is not None:
            rows.append(built)
    return rows


def _one_borrow(owner: str, item: dict[str, Any], moment: str) -> dict | None:
    if item.get("isLiquidated") is True:
        return None
    vault = item.get("vault")
    if not isinstance(vault, dict):
        raise JupiterContractError("borrow", "missing field vault")
    supply_token = vault.get("supplyToken")
    borrow_token = vault.get("borrowToken")
    if not isinstance(supply_token, dict):
        raise JupiterContractError("borrow", "missing field vault.supplyToken")
    if not isinstance(borrow_token, dict):
        raise JupiterContractError("borrow", "missing field vault.borrowToken")
    supply_raw = _contract_number(item, "supply", "borrow")
    borrow_raw = _contract_number(item, "borrow", "borrow")
    supply_dec = _contract_decimals(supply_token, "borrow", "vault.supplyToken")
    borrow_dec = _contract_decimals(borrow_token, "borrow", "vault.borrowToken")
    if supply_raw != 0 and supply_dec is None:
        raise JupiterContractError("borrow", "missing field vault.supplyToken.decimals")
    if borrow_raw != 0 and borrow_dec is None:
        raise JupiterContractError("borrow", "missing field vault.borrowToken.decimals")
    supply_amt = _base(supply_raw, supply_dec or 0)
    borrow_amt = _base(borrow_raw, borrow_dec or 0)
    supply_px = dec(supply_token.get("price") or 0)
    borrow_px = dec(borrow_token.get("price") or 0)
    collateral_usd = supply_amt * supply_px
    debt_usd = borrow_amt * borrow_px
    price_missing_legs: list[str] = []
    if supply_amt > 0 and supply_px <= 0:
        price_missing_legs.append("collateral")
    if borrow_amt > 0 and borrow_px <= 0:
        price_missing_legs.append("debt")
    # A missing price makes a real quantity look like dust. Keep the row and
    # flag it; only an amount-free row (or a genuinely priced sub-cent row) is
    # dropped as dust.
    if (
        collateral_usd < _MIN_USD
        and debt_usd < _MIN_USD
        and not price_missing_legs
    ):
        return None
    supply_apy = _bps(vault.get("supplyRate"))
    borrow_apy = _bps(vault.get("borrowRate"))
    threshold = _milli(vault.get("liquidationThreshold"))
    ltv_cap = _milli(vault.get("collateralFactor"))
    collaterals = [leg(
        symbol=str(supply_token.get("symbol") or "collateral"),
        amount=supply_amt,
        usd=collateral_usd,
        contract=str(supply_token.get("address") or ""),
        apy_supply=supply_apy,
        lowercase_contract=False,
    )] if collateral_usd >= _MIN_USD or supply_amt > 0 else []
    borrows = [leg(
        symbol=str(borrow_token.get("symbol") or "debt"),
        amount=borrow_amt,
        usd=debt_usd,
        contract=str(borrow_token.get("address") or ""),
        apy_borrow=borrow_apy,
        lowercase_contract=False,
    )] if debt_usd >= _MIN_USD or borrow_amt > 0 else []
    net = collateral_usd - debt_usd
    ltv = fnum(debt_usd / collateral_usd) if collateral_usd > 0 and debt_usd > 0 else None
    hf = health_factor(None, collateral_usd=collateral_usd, debt_usd=debt_usd, liq_threshold=threshold)
    supply_pairs = [(collateral_usd, supply_apy)] if supply_apy is not None else []
    borrow_pairs = [(debt_usd, borrow_apy)] if borrow_apy is not None else []
    position_id = str(item.get("id") or item.get("address") or "")
    if not position_id:
        raise JupiterContractError("borrow", "missing field id/address")
    market_ref = f"jupiter-lend-borrow-{position_id}"
    has_debt = debt_usd > 0 or "debt" in price_missing_legs
    warn_debt_without_hf(
        PROTOCOL,
        market_ref,
        debt_usd=debt_usd,
        hf=hf,
        has_debt=has_debt,
    )
    liq_price = None
    if threshold is not None and threshold > 0 and debt_usd > 0 and supply_amt > 0 and len(collaterals) == 1:
        liq_price = fnum(debt_usd / (supply_amt * threshold))
    details = empty_details(
        collateral=collaterals,
        borrow=borrows,
        health_factor=hf,
        ltv=ltv if ltv is not None else (fnum(ltv_cap) if ltv_cap is not None else None),
        liq_threshold=fnum(threshold) if threshold is not None else None,
        liq_price=liq_price,
        net_apy=net_apy(supply_pairs, borrow_pairs, net),
        interest_paid_usd=_api_money_fnum(item, _INTEREST_PAID_KEYS),
        interest_earned_usd=_api_money_fnum(item, _INTEREST_EARNED_KEYS),
        pnl_usd=_api_money_fnum(item, _PNL_KEYS),
        pnl_apr=None,
        **(
            {"price_missing": True, "price_missing_legs": price_missing_legs}
            if price_missing_legs
            else {}
        ),
        **({"pnl_data_missing": True} if _api_money(item, _PNL_KEYS) is None else {}),
    )
    mark_hf_unavailable(details, debt_usd=debt_usd, hf=hf, has_debt=has_debt)
    # Additive audit field: the current HF arithmetic still uses
    # vault.liquidationThreshold (the documented contract). Some vault
    # payloads additionally expose oracle prices; record which one is present
    # without changing the numbers, whose units are not guaranteed here.
    if vault.get("oraclePriceLiquidate") not in (None, ""):
        details["hf_basis"] = "oracle_price_liquidate"
    elif vault.get("oraclePrice") not in (None, ""):
        details["hf_basis"] = "oracle_price"
    elif vault.get("oracle_price_liquidate") not in (None, ""):
        details["hf_basis"] = "oracle_price_liquidate"
    elif vault.get("oracle_price") not in (None, ""):
        details["hf_basis"] = "oracle_price"
    else:
        details["hf_basis"] = "liquidation_threshold"
    supply_sym = supply_token.get("symbol") or "?"
    borrow_sym = borrow_token.get("symbol") or "?"
    return position_row(
        protocol=PROTOCOL,
        chain=CHAIN,
        account_ref=owner,
        market_ref=market_ref,
        label=f"Jupiter Borrow {supply_sym}/{borrow_sym}",
        collateral_usd=collateral_usd,
        debt_usd=debt_usd,
        details=details,
        asof=moment,
        lowercase_account=False,
    )


def _perps_rows(owner: str, payload: Any, moment: str) -> list[dict]:
    if payload is None:
        items: list[dict[str, Any]] = []
    elif isinstance(payload, dict):
        if not isinstance(payload.get("dataList"), list):
            raise JupiterContractError("perps", "missing field dataList")
        items = _source_items(payload.get("dataList"), "perps")
    elif isinstance(payload, list):
        items = _source_items(payload, "perps")
    else:
        raise JupiterContractError(
            "perps", f"expected a JSON object/array, got {type(payload).__name__}"
        )
    rows: list[dict] = []
    for item in items:
        try:
            built = _one_perp(owner, item, moment)
        except JupiterContractError:
            raise
        except Exception as exc:  # noqa: BLE001
            log_failure(PROTOCOL, CHAIN, exc, source="perps")
            continue
        if built is not None:
            rows.append(built)
    return rows


def _one_perp(owner: str, item: dict[str, Any], moment: str) -> dict | None:
    size = _usd6(_contract_number(item, "sizeUsd", "perps"))
    if size <= 0:
        return None
    margin = _usd6(_contract_number(item, "collateralUsd", "perps"))
    side = str(item.get("side") or "").lower()
    asset = str(item.get("asset") or item.get("marketMint") or "PERP")
    collateral_token = str(item.get("collateralToken") or "USD")
    # Equity = collateral + pnl. A missing/renamed pnl must fail the source,
    # never default to zero: that would understate/overstate equity silently.
    pnl = _usd6(_contract_number(item, "pnlAfterFeesUsd", "perps"))
    # Equity is margin plus unrealized pnl for both sides. The notional is not
    # a second debt: the borrowed part is already inside the pnl.
    equity = margin + (pnl or Decimal("0"))
    collateral_usd = equity if equity > 0 else Decimal("0")
    debt_usd = -equity if equity < 0 else Decimal("0")
    liq = _usd6(item.get("liquidationPriceUsd")) if item.get("liquidationPriceUsd") is not None else None
    # Long liq is below entry, so HF ≈ mark / liq. Short is the mirror.
    mark = _usd6(item.get("markPriceUsd")) if item.get("markPriceUsd") is not None else None
    # A negative equity is already bad debt: liquidation distance no longer
    # describes the position, so leave HF null and let the warning fire.
    hf = None if debt_usd > 0 else _perp_hf(side, mark, liq)
    collaterals = [leg(
        symbol=collateral_token,
        amount=margin,
        usd=collateral_usd,
        contract=str(item.get("collateralMint") or ""),
        lowercase_contract=False,
    )]
    borrows = [leg(
        symbol=f"{asset}-{side or 'perp'}",
        amount=size,
        usd=debt_usd,
        contract=str(item.get("assetMint") or item.get("custody") or ""),
        lowercase_contract=False,
    )] if debt_usd > 0 else []
    # pnl_apr is not derivable from a mark-to-market snapshot.
    details = empty_details(
        collateral=collaterals,
        borrow=borrows,
        health_factor=hf,
        ltv=fnum(debt_usd / collateral_usd) if collateral_usd > 0 and debt_usd > 0 else None,
        liq_threshold=None,
        liq_price=fnum(liq) if liq is not None and liq > 0 else None,
        net_apy=None,
        interest_paid_usd=fnum(_usd6(item.get("borrowFeesUsd"))) if item.get("borrowFeesUsd") is not None else None,
        interest_earned_usd=None,
        pnl_usd=fnum(pnl) if pnl is not None else None,
        pnl_apr=None,
        margin_usd=fnum(margin),
        notional_usd=fnum(size),
        side=side or None,
    )
    pubkey = str(item.get("positionPubkey") or "")
    market_ref = f"jupiter-perps-{pubkey or asset}"
    warn_debt_without_hf(
        PROTOCOL,
        market_ref,
        debt_usd=debt_usd,
        hf=hf,
    )
    return position_row(
        protocol=PROTOCOL,
        chain=CHAIN,
        account_ref=owner,
        market_ref=market_ref,
        label=f"Jupiter Perps {side} {asset}".strip(),
        collateral_usd=collateral_usd,
        debt_usd=debt_usd,
        details=details,
        asof=moment,
        lowercase_account=False,
    )


def _perp_hf(side: str, mark: Decimal | None, liq: Decimal | None) -> float | None:
    if mark is None or liq is None or mark <= 0 or liq <= 0:
        return None
    if side == "short":
        return fnum(liq / mark)
    return fnum(mark / liq)


def _api_money(item: dict[str, Any], keys: tuple[str, ...]) -> Decimal | None:
    """Read a numeric pnl/interest field if this API payload really has it.

    Jupiter's documented Earn/Borrow shapes do not expose pnl or interest
    history. Some deployments put extra values in the borrow ``config`` map, so
    look there too. A missing or non-numeric field returns ``None``; callers
    must not substitute a derived number.
    """
    sources: list[dict[str, Any]] = [item]
    config = item.get("config")
    if isinstance(config, dict):
        sources.append(config)
    for source in sources:
        for key in keys:
            if key not in source:
                continue
            value = source[key]
            if value is None or isinstance(value, (dict, list, bool)):
                continue
            try:
                return Decimal(str(value))
            except (InvalidOperation, ValueError, TypeError):
                continue
    return None


def _api_money_fnum(item: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    value = _api_money(item, keys)
    return fnum(value) if value is not None else None


def _bps(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    return dec(value) / _BPS


def _milli(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    raw = dec(value)
    if raw <= 0:
        return None
    # Vault fields arrive as thousandths (850) or already as a fraction.
    if raw > 1:
        return raw / _MILLI
    return raw


def _base(value: object, decimals: int) -> Decimal:
    raw = dec(value)
    if decimals <= 0:
        return raw
    return raw / (Decimal(10) ** decimals)


def _usd6(value: object) -> Decimal:
    """Perps USD fields are atomic 6-dp integers, sometimes already decimal strings."""
    raw = dec(value)
    if raw == raw.to_integral_value() and abs(raw) >= _USD_SCALE:
        return raw / _USD_SCALE
    return raw


def _get_json(
    http: httpx.Client,
    url: str,
    *,
    sleep: Callable[[float], None] | None = None,
    owner: str | None = None,
    source: str | None = None,
) -> Any:
    """GET JSON with capped Retry-After + exponential backoff + jitter.

    Other 4xx errors raise immediately. Once a 429 survives all four tries the
    ``(wallet, source)`` pair enters a cooldown (shared via Redis when it is
    available); the next sync skips only that endpoint and leaves the failed
    source's old rows stale.
    """
    pause = sleep or time.sleep
    last: Exception | None = None
    last_attempt = len(RETRY_WAITS)
    for attempt in range(last_attempt + 1):
        try:
            response = http.get(url, headers=RETRY_HEADERS)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last = exc
            if attempt == last_attempt:
                raise
            delay = _backoff(attempt) + random.uniform(0, _JITTER_SECONDS)
            pause(min(delay, _MAX_RETRY_SLEEP))
            continue
        if response.status_code == 429 or 500 <= response.status_code < 600:
            last = httpx.HTTPStatusError(
                f"HTTP {response.status_code}",
                request=response.request,
                response=response,
            )
            if attempt == last_attempt:
                if response.status_code == 429 and owner and source:
                    _cool_down(owner, source)
                raise last
            base = (
                min(retry_after_seconds(response, attempt), RETRY_CAP_SECONDS)
                if response.status_code == 429
                else 0.0
            )
            delay = base + _backoff(attempt) + random.uniform(0, _JITTER_SECONDS)
            pause(min(delay, _MAX_RETRY_SLEEP))
            continue
        response.raise_for_status()
        return response.json()
    if last is not None:
        raise last
    raise RuntimeError("jupiter GET failed without a response")


def _sol_address(value: object) -> str:
    return str(value or "").strip()
