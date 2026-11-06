"""Kamino Lend positions for bound Solana wallets.

Read-only. Public API ``https://api.kamino.finance`` (no key):

* ``GET /v2/kamino-market`` lists every market. ``/kamino-market`` returns only
  the primary one, so isolated markets (xStocks, OnRe, Figure) would be missed.
* ``GET /kamino-market/{market}/users/{wallet}/obligations`` returns the
  obligation addresses owned by the wallet. Each entry also carries
  ``refreshedStats`` (the protocol's ``borrowLiquidationLimit`` and
  ``userTotalBorrowBorrowFactorAdjusted``); this list is the only place those
  stats appear.
* ``GET /klend/loans/{obligation}`` is the human-readable loan (collateral,
  debt, LTV, liquidation LTV). It has no ``refreshedStats``; the fetch merges
  the obligation's stats onto the loan so the protocol HF basis can apply.
* ``GET /kamino-market/{market}/reserves/metrics`` supplies supply/borrow APY
  keyed by mint. One market's metrics failure does not drop the positions.
* ``GET /v2/kamino-market/{market}/obligations/{obligation}/pnl`` and the
  interest-fees / interest-paid routes fill pnl and interest. A missing
  history leaves those fields null; the position itself still lands.

Multiply / leverage obligations are the same loan object. A wallet with no
obligation is a clean empty read. A market that errors is recorded with its
name and its old rows stay open, while the other markets still land. A total
enumeration failure returns ``[]`` and notes the protocol.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable

import httpx

from .http import quiet_client
from .lending_common import (
    asof_iso,
    dec,
    empty_details,
    fnum,
    get_json_with_retry,
    health_factor,
    leg,
    log_failure,
    mark_hf_unavailable,
    net_apy,
    note_source_failure,
    position_row,
    warn_debt_without_hf,
)
from .yield_sources import (
    clear_cache as clear_yield_cache,
    jlp_collateral_apy,
    onyc_collateral_apy,
)

logger = logging.getLogger(__name__)

PROTOCOL = "kamino"
API_URL = "https://api.kamino.finance"
CHAIN = "solana"

# JLP's reserve does not report a usable native supply APY. Jupiter's public
# jlp-info endpoint publishes the official one; the collateral mint identifies
# the asset exactly (Solana addresses are case-sensitive).
JLP_MINT = "27G8MtK7VtTcCHkpASjSDdkWWYfoqT6ggEuKidVJidD4"

# ONyc (OnRe tokenized reinsurance) also reports a zero native supply APY on
# Kamino. DefiLlama's dedicated ``onre`` pool publishes the real yield; the
# collateral mint identifies the asset exactly. Solana mints are case-sensitive.
ONYC_MINT = "5Y8NV33Vv7WbnLfq3zBcKSdYPrk7g2KoiQoe7M2tcxp5"

# Dust below one cent is not a position. Keeps the empty-wallet fixture empty
# even if a reserve reports a rounding leftover.
_MIN_USD = Decimal("0.01")

# Estimated APYs (collateral price return, not protocol-reported supply/borrow
# rates) above this absolute fraction are hidden from ``net_apy``: volatile
# collateral such as JLP can print 136%+ from a short price window.
_APY_ESTIMATE_OUTLIER = 1.0

# Market discovery: the v2 list is ~50 markets, so cache it briefly. A market
# that was probed for this wallet and had no obligation is skipped for a while;
# the summary always wins, so a new earn position is seen immediately.
_MARKET_LIST_TTL = 300.0
_PROBE_TTL = 300.0
# Two dead markets in a row is an outage, not one isolated market. Stop the
# scan so a live sync does not wait on every market in the list.
_MAX_CONSECUTIVE_MARKET_FAILURES = 2
_market_list_cache: dict[str, tuple[float, list[str]]] = {}
_probe_cache: dict[tuple[str, str], float] = {}


def reset_caches() -> None:
    """Drop process-local discovery and yield caches (tests/manual refresh)."""
    _market_list_cache.clear()
    _probe_cache.clear()
    clear_yield_cache()


def fetch_positions(
    address: str,
    *,
    client: httpx.Client | None = None,
    use_mock: bool = False,
    sleep: Callable[[float], None] | None = None,
) -> list[dict]:
    """Normalized Kamino rows for one Solana wallet.

    A market that fails is recorded but does not discard the markets that were
    read; the hook keeps the failed market's old row open. A clean empty read
    is still an empty list and may close the wallet's old rows.
    """
    if use_mock:
        import json
        from pathlib import Path

        path = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "lending_solana" / "kamino_user.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        return normalize(address, payload)

    owner = _sol_address(address)
    if not owner:
        return []
    own_client = client is None
    http = client or quiet_client(timeout=20.0)
    try:
        summary_markets: list[str] = []
        summary_error: Exception | None = None
        try:
            summary_markets = _summary_markets(http, owner, sleep=sleep)
        except Exception as exc:  # noqa: BLE001 — the full list is authoritative
            summary_error = exc
            logger.info(
                "kamino summary unavailable (%s); using the full market list",
                type(exc).__name__,
            )
        all_markets: list[str] = []
        markets_error: Exception | None = None
        try:
            all_markets = _all_markets_cached(http, sleep=sleep)
        except Exception as exc:  # noqa: BLE001 — summary markets remain usable
            markets_error = exc
            # Discovery is incomplete even when the summary named some markets:
            # a borrow-only obligation in an unlisted market would be missed,
            # so the protocol must stay failed (no rows are closed).
            log_failure(PROTOCOL, CHAIN, exc, source="markets")
        markets = _merge_markets(owner, summary_markets, all_markets)
        if not markets and (summary_error or markets_error):
            # Nothing could be enumerated, so the wallet was not read. Raising
            # makes the outer handler note the protocol and keep old rows.
            raise summary_error or markets_error

        rows: list[dict] = []
        streak = 0
        for market in markets:
            try:
                market_rows, had_obligations = _fetch_market_with_meta(
                    http, owner, market, sleep=sleep
                )
            except Exception as exc:  # noqa: BLE001 — one market must not sink the rest
                log_failure(PROTOCOL, CHAIN, exc, source=f"market:{market}")
                streak += 1
                if streak >= _MAX_CONSECUTIVE_MARKET_FAILURES:
                    note_source_failure(PROTOCOL, "scan_aborted")
                    logger.warning(
                        "kamino scan stopped after %s consecutive market failures",
                        streak,
                    )
                    break
                continue
            streak = 0
            rows.extend(market_rows)
            key = (owner, market)
            if had_obligations:
                _probe_cache.pop(key, None)
            else:
                _probe_cache[key] = time.monotonic() + _PROBE_TTL
        return rows
    except Exception as exc:  # noqa: BLE001
        log_failure(PROTOCOL, CHAIN, exc)
        return []
    finally:
        if own_client:
            http.close()


def normalize(address: str, payload: dict[str, Any]) -> list[dict]:
    """Turn a recorded Kamino bundle into positions rows.

    ``payload`` is ``{"markets": [{"market": ..., "obligations": [...],
    "loans": [...], "reserves": [...], "pnl": {...}, "interest_earned": {...},
    "interest_paid": {...}}]}``. One loan with collateral or debt becomes one
    row. A loan that fails to parse is skipped.
    """
    owner = _sol_address(address)
    markets = payload.get("markets") if isinstance(payload, dict) else None
    if not isinstance(markets, list):
        return []
    rows: list[dict] = []
    moment = asof_iso()
    for market in markets:
        if not isinstance(market, dict):
            continue
        market_id = str(market.get("market") or "")
        rates = _rates_by_mint(market.get("reserves"))
        extras = {
            "pnl": market.get("pnl") if isinstance(market.get("pnl"), dict) else {},
            "earned": market.get("interest_earned") if isinstance(market.get("interest_earned"), dict) else {},
            "paid": market.get("interest_paid") if isinstance(market.get("interest_paid"), dict) else {},
        }
        loans = market.get("loans")
        if not isinstance(loans, list):
            continue
        for loan in loans:
            if not isinstance(loan, dict):
                continue
            try:
                built = _loan_row(owner, market_id, loan, rates, extras, moment)
            except Exception as exc:  # noqa: BLE001 — one obligation must not sink the rest
                log_failure(
                    PROTOCOL,
                    CHAIN,
                    exc,
                    source=f"market:{market_id}" if market_id else None,
                )
                continue
            if built is not None:
                rows.append(built)
    return rows


def _fetch_market(
    http: httpx.Client,
    owner: str,
    market: str,
    *,
    sleep: Callable[[float], None] | None = None,
) -> list[dict]:
    """Rows for one market (list-only compatibility wrapper)."""
    rows, _had_obligations = _fetch_market_with_meta(
        http, owner, market, sleep=sleep
    )
    return rows


# The obligations list has appeared with camelCase and snake_case stats keys.
_OBLIGATION_STATS_KEYS = (
    "refreshedStats",
    "refreshed_stats",
    "obligationStats",
    "stats",
)


def _obligation_stats(item: dict[str, Any]) -> dict[str, Any] | None:
    """Per-obligation protocol stats block from the obligations list."""
    for key in _OBLIGATION_STATS_KEYS:
        block = item.get(key)
        if isinstance(block, dict) and block:
            return block
    return None


def _usable_hf_stats(block: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return ``block`` only when it holds both fields the HF pair needs.

    Delegates key/alias handling to ``_protocol_hf_stats`` so the fetch and the
    normalizer cannot drift. A partial block is not copied: without both
    values it cannot change the basis, so the existing formula stays in charge.
    """
    if not isinstance(block, dict) or not block:
        return None
    return block if _protocol_hf_stats({"refreshedStats": block}, {}) is not None else None


def _fetch_market_with_meta(
    http: httpx.Client,
    owner: str,
    market: str,
    *,
    sleep: Callable[[float], None] | None = None,
) -> tuple[list[dict], bool]:
    """Return ``(rows, had_obligations)`` for one market.

    ``had_obligations`` lets the caller keep the market in future scans even
    when every loan is dust, while a genuinely empty market is cached as a
    negative probe.

    The obligations list is the only payload that publishes ``refreshedStats``;
    ``/klend/loans/{obligation}`` omits it. Copy a usable stats block onto the
    loan so ``_protocol_hf_stats`` can prefer the protocol's own HF.
    """
    obligations = _get_json(
        http, f"/kamino-market/{market}/users/{owner}/obligations", sleep=sleep
    )
    if not isinstance(obligations, list):
        raise ValueError("kamino obligations not a list")
    if not obligations:
        return [], False
    loans: list[dict] = []
    stats_merged = 0
    for item in obligations:
        if not isinstance(item, dict):
            continue
        obligation = str(item.get("obligationAddress") or "")
        if not obligation:
            continue
        stats = _obligation_stats(item)
        loan = _get_json(http, f"/klend/loans/{obligation}", sleep=sleep)
        if not isinstance(loan, dict):
            continue
        info = loan.get("loanInfo") if isinstance(loan.get("loanInfo"), dict) else {}
        if stats is not None and _protocol_hf_stats(loan, info) is None:
            usable = _usable_hf_stats(stats)
            if usable is not None:
                # Top-level loan is the first container _protocol_hf_stats reads.
                loan["refreshedStats"] = usable
                stats_merged += 1
        loans.append(loan)
    if stats_merged:
        # One line per market, not per obligation, so a scan stays quiet.
        logger.info(
            "kamino merged obligation refreshedStats into %s loan(s) (market=%s)",
            stats_merged,
            market,
        )
    reserves: Any = []
    try:
        reserves = _get_json(
            http, f"/kamino-market/{market}/reserves/metrics", sleep=sleep
        )
    except Exception as exc:  # noqa: BLE001 — APY is optional
        _warn_optional("reserves", market, exc)
        reserves = []
    extras = _extras(http, market, loans, sleep=sleep)
    rows = normalize(
        owner,
        {"markets": [{
            "market": market,
            "loans": loans,
            "reserves": reserves if isinstance(reserves, list) else [],
            **extras,
        }]},
    )
    return rows, True


def _extras(
    http: httpx.Client,
    market: str,
    loans: list[dict],
    *,
    sleep: Callable[[float], None] | None = None,
) -> dict[str, dict]:
    """PnL and interest keyed by obligation. Each call is best-effort."""
    pnl: dict[str, Any] = {}
    earned: dict[str, Any] = {}
    paid: dict[str, Any] = {}
    for loan in loans:
        obligation = str(loan.get("loanId") or "")
        if not obligation:
            continue
        pnl[obligation] = _optional(
            http, f"/v2/kamino-market/{market}/obligations/{obligation}/pnl", sleep=sleep
        )
        earned[obligation] = _optional(
            http,
            f"/v2/kamino-market/{market}/obligations/{obligation}/interest-fees",
            sleep=sleep,
        )
        paid[obligation] = _optional(
            http,
            f"/v2/kamino-market/{market}/obligations/{obligation}/interest-paid",
            sleep=sleep,
        )
    return {"pnl": pnl, "interest_earned": earned, "interest_paid": paid}


def _optional(
    http: httpx.Client, path: str, *, sleep: Callable[[float], None] | None = None
) -> Any:
    try:
        return _get_json(http, path, sleep=sleep)
    except Exception as exc:  # noqa: BLE001 — history is optional
        _warn_optional("history", path.rsplit("/", 1)[-1], exc)
        return None


def _warn_optional(kind: str, ref: str, exc: BaseException) -> None:
    """Optional feed lost: warn, but do not fail the protocol or its positions."""
    logger.warning(
        "kamino optional %s failed (%s, ref=%s)", kind, type(exc).__name__, ref
    )


def _first_present(mapping: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value is not None and value != "":
            return value
    return None


def _group_id(value: object) -> int:
    """Kamino elevation group id. Anything missing/invalid means base mode (0)."""
    try:
        group = int(dec(value))
    except (ValueError, TypeError, OverflowError):
        return 0
    return group if group > 0 else 0


def _borrow_factor(value: object) -> Decimal | None:
    """Normalize a Kamino borrow factor to a fraction.

    The public payload has appeared both as a fraction (``0.9``) and as a
    percentage (``90``). A factor above 1 can only be the percentage form;
    values at or below 1 are already fractions. Invalid/non-positive values are
    treated as absent so they cannot silently improve a health factor.
    """
    if value is None or value == "":
        return None
    factor = dec(value, default="nan")
    if not factor.is_finite() or factor <= 0:
        return None
    if factor > 1:
        factor = factor / Decimal("100")
    return factor


def _protocol_hf_stats(
    loan: dict[str, Any], info: dict[str, Any]
) -> tuple[Decimal, Decimal] | None:
    """Return ``(borrowLiquidationLimit, borrowFactorAdjustedDebt)`` if present.

    Kamino's richer obligation responses publish this pair under
    ``refreshedStats`` (sometimes nested in ``loanInfo`` or ``debt``). The two
    values are in the same unit, so their ratio is the protocol's own HF.
    Accept common camelCase/snake_case aliases instead of guessing from one
    response shape.
    """
    containers: list[dict[str, Any]] = [loan, info]
    debt = info.get("debt") if isinstance(info.get("debt"), dict) else None
    if debt is not None:
        containers.append(debt)
    for base in (loan, info, debt):
        if not isinstance(base, dict):
            continue
        for key in ("refreshedStats", "refreshed_stats", "stats", "obligationStats"):
            block = base.get(key)
            if isinstance(block, dict):
                containers.append(block)
    limit_keys = (
        "borrowLiquidationLimit",
        "borrow_liquidation_limit",
        "borrowLiquidationLimitUsd",
        "borrow_liquidation_limit_usd",
        "liquidationLimit",
        "liquidation_limit",
    )
    debt_keys = (
        "userTotalBorrowBorrowFactorAdjusted",
        "userTotalBorrowBorrowFactorAdjustedDebt",
        "user_total_borrow_borrow_factor_adjusted",
        "borrowFactorAdjustedDebt",
        "borrow_factor_adjusted_debt",
        "totalBorrowBorrowFactorAdjusted",
        "borrowFactorAdjustedBorrow",
    )
    for block in containers:
        limit_raw = _first_present(block, limit_keys)
        debt_raw = _first_present(block, debt_keys)
        if limit_raw is None or debt_raw is None:
            continue
        limit = dec(limit_raw, default="nan")
        adjusted = dec(debt_raw, default="nan")
        if limit.is_finite() and adjusted.is_finite() and limit > 0 and adjusted > 0:
            return limit, adjusted
    return None


def _elevation_configs(raw: Any) -> dict[int, dict[str, Decimal]]:
    """Normalize a reserve's elevation-group block to ``{group: {ltv...}}``.

    Kamino's public loans response currently carries the effective per-deposit
    LTV directly and exposes no elevation-group table. Some deployments/reserve
    payloads do include one (``elevationGroups`` / ``elevationGroupConfigs``);
    accept list, single-object and ``{"2": {...}}`` map shapes so a synthetic or
    a future API payload is handled without guessing field names.
    """
    configs: dict[int, dict[str, Decimal]] = {}
    items: list[dict[str, Any]] = []
    if isinstance(raw, list):
        items = [item for item in raw if isinstance(item, dict)]
    elif isinstance(raw, dict):
        if _first_present(raw, ("elevationGroup", "elevation_group", "group", "id")) is not None:
            items = [raw]
        else:
            for key, value in raw.items():
                if isinstance(value, dict):
                    entry = dict(value)
                    entry.setdefault("elevationGroup", key)
                    items.append(entry)
    for item in items:
        group = _group_id(_first_present(item, ("elevationGroup", "elevation_group", "group", "id")))
        if group <= 0:
            continue
        liquidation = _first_present(
            item,
            (
                "liquidationLtv",
                "liquidationLTV",
                "liquidation_ltv",
                "liquidationThreshold",
                "liquidation_threshold",
                "liqLtv",
            ),
        )
        max_ltv = _first_present(item, ("maxLtv", "maxLTV", "max_ltv", "ltv"))
        configs[group] = {
            "liquidation_ltv": dec(liquidation) if liquidation is not None else Decimal("0"),
            "max_ltv": dec(max_ltv) if max_ltv is not None else Decimal("0"),
        }
    return configs


def _reserve_elevations(item: dict[str, Any]) -> dict[int, dict[str, Decimal]]:
    """Elevation-group LTVs on one reserve row, if that API payload has them."""
    raw = _first_present(
        item,
        (
            "elevationGroups",
            "elevationGroupConfigs",
            "elevation_group_configs",
            "elevation_groups",
        ),
    )
    configs = _elevation_configs(raw)
    # A flat reserve row can describe a single group itself.
    flat_group = _group_id(_first_present(item, ("elevationGroup", "elevation_group")))
    if flat_group > 0:
        configs.update(_elevation_configs({**item, "elevationGroup": flat_group}))
    return configs


def _leg_elevation_ltv(
    item: dict[str, Any],
    group: int,
    reserve_configs: dict[int, dict[str, Decimal]],
) -> tuple[Decimal, str]:
    """Effective liquidation LTV for one collateral leg and where it came from.

    With ``group == 0`` the previous base-mode behaviour is untouched. In eMode
    prefer an explicit effective value on the leg, then a reserve elevation
    table, then the leg's own ``liquidationLtv`` (which the loans API publishes
    as the effective reserve threshold). The caller can additionally fall back
    to the loan-level weighted average when no leg exposes an eMode value.

    Plain ``liquidationThreshold`` is deliberately not treated as explicit: in
    the real Kamino loans payload it is a base-mode field, while the eMode
    value is published as ``liquidationLtv``. Preferring the base field would
    silently understate the threshold for an active elevation group.
    """
    base = dec(item.get("liquidationLtv"))
    if group <= 0:
        return base, "base"
    explicit = _first_present(
        item,
        (
            "elevationLiquidationLtv",
            "elevationLiquidationLTV",
            "elevation_liquidation_ltv",
            "eModeLiquidationLtv",
            "eModeLiquidationThreshold",
        ),
    )
    if explicit is not None and dec(explicit) > 0:
        return dec(explicit), "leg_elevation"
    config = reserve_configs.get(group)
    if config and config.get("liquidation_ltv", Decimal("0")) > 0:
        return config["liquidation_ltv"], "reserve_elevation"
    return base, "leg_base"


def _jlp_supply_quote():
    """Official JLP APY, or ``None`` when Jupiter's feed is unavailable."""
    try:
        return jlp_collateral_apy()
    except Exception as exc:  # noqa: BLE001 — an optional yield feed
        log_failure("jupiter_jlp", CHAIN, exc, fatal=False)
        return None


def _onyc_supply_quote():
    """Official ONyc APY, or ``None`` when DefiLlama's onre pool is unavailable."""
    try:
        return onyc_collateral_apy()
    except Exception as exc:  # noqa: BLE001 — an optional yield feed
        log_failure("defillama_onre", CHAIN, exc, fatal=False)
        return None


def _loan_row(
    owner: str,
    market_id: str,
    loan: dict[str, Any],
    rates: dict[str, dict[str, Decimal]],
    extras: dict[str, dict],
    moment: str,
) -> dict | None:
    info = loan.get("loanInfo") if isinstance(loan.get("loanInfo"), dict) else {}
    collateral_in = ((info.get("collateral") or {}).get("deposits") if isinstance(info.get("collateral"), dict) else None) or []
    borrow_in = ((info.get("debt") or {}).get("borrows") if isinstance(info.get("debt"), dict) else None) or []
    if not isinstance(collateral_in, list) or not isinstance(borrow_in, list):
        raise ValueError("kamino loan legs not lists")

    collaterals: list[dict] = []
    borrows: list[dict] = []
    supply_pairs: list[tuple[Decimal, Decimal]] = []
    borrow_pairs: list[tuple[Decimal, Decimal]] = []
    collateral_usd = Decimal("0")
    debt_usd = Decimal("0")
    borrow_factor_debt_usd = Decimal("0")
    borrow_factor_used = False
    threshold_usd = Decimal("0")
    price_missing_legs: list[str] = []
    elevation_raw = _first_present(loan, ("elevationGroup", "elevation_group"))
    if elevation_raw is None:
        elevation_raw = _first_present(info, ("elevationGroup", "elevation_group"))
    elevation_group = _group_id(elevation_raw)
    # Distinguish "group 0" from "field renamed/absent". Both mean base mode
    # for the arithmetic, but the marker lets callers spot a schema drift
    # instead of trusting a silently-defaulted 0.
    elevation_field_present = elevation_raw is not None
    elevation_leg_hits = 0
    elevation_hit_sources: set[str] = set()
    elevation_missing_legs = 0
    collateral_legs = 0
    jlp_collateral_present = False
    onyc_collateral_present = False

    for item in collateral_in:
        if not isinstance(item, dict):
            continue
        usd = dec(item.get("tokenValue"))
        amount = dec(item.get("tokenAmount"))
        if usd < _MIN_USD and amount <= 0:
            continue
        if amount > 0 and usd <= 0:
            price_missing_legs.append("collateral")
        mint = str(item.get("tokenMint") or "")
        apy = rates.get(mint, {}).get("supply")
        apy_source = None
        if mint == JLP_MINT:
            jlp_collateral_present = True
            # JLP's net carry must use Jupiter's reported APY. If the feed is
            # unavailable the leg stays unknown; the reserve's 0 / price return
            # is not a fallback.
            quote = _jlp_supply_quote()
            apy = quote.apy if quote is not None else None
            apy_source = quote.source if quote is not None else None
        elif mint == ONYC_MINT:
            onyc_collateral_present = True
            # ONyc's real yield is on DefiLlama's onre pool, not the reserve's
            # zero supplyApy. A feed miss leaves the leg unknown: the zero rate
            # and the price-return estimate are not publishing fallbacks.
            quote = _onyc_supply_quote()
            apy = quote.apy if quote is not None else None
            apy_source = quote.source if quote is not None else None
        reserve_configs = rates.get(mint, {}).get("elevation") or {}
        raw_ltv = dec(item.get("liquidationLtv"))
        effective_ltv, ltv_source = _leg_elevation_ltv(item, elevation_group, reserve_configs)
        if ltv_source in ("leg_elevation", "reserve_elevation"):
            elevation_leg_hits += 1
            elevation_hit_sources.add(ltv_source)
        if elevation_group > 0 and effective_ltv <= 0:
            elevation_missing_legs += 1
        collateral_legs += 1
        c_leg = leg(
            symbol=str(item.get("tokenName") or mint[:6] or "UNKNOWN"),
            amount=item.get("tokenAmount"),
            usd=usd,
            contract=mint,
            apy_supply=apy,
            lowercase_contract=False,
        )
        if apy_source:
            c_leg["apy_source"] = apy_source
        c_leg["maxLtv"] = fnum(item.get("maxLtv")) if item.get("maxLtv") is not None else None
        c_leg["liquidationLtv"] = fnum(raw_ltv) if item.get("liquidationLtv") is not None else None
        if elevation_group > 0 and effective_ltv > 0 and effective_ltv != raw_ltv:
            c_leg["effectiveLiquidationLtv"] = fnum(effective_ltv)
        if item.get("tokenPrice") is not None:
            c_leg["price"] = fnum(item.get("tokenPrice"))
        collaterals.append(c_leg)
        collateral_usd += usd
        if effective_ltv > 0:
            threshold_usd += usd * effective_ltv
        if apy is not None:
            supply_pairs.append((usd, apy))

    for item in borrow_in:
        if not isinstance(item, dict):
            continue
        usd = dec(item.get("tokenValue"))
        amount = dec(item.get("tokenAmount"))
        if usd < _MIN_USD and amount <= 0:
            continue
        if amount > 0 and usd <= 0:
            price_missing_legs.append("debt")
        mint = str(item.get("tokenMint") or "")
        reserve = rates.get(mint, {})
        apy = reserve.get("borrow")
        raw_leg_factor = _first_present(
            item,
            (
                "borrowFactor",
                "borrow_factor",
                "borrowFactorPct",
                "borrow_factor_pct",
            ),
        )
        raw_reserve_factor = reserve.get("borrow_factor")
        leg_factor = _borrow_factor(raw_leg_factor)
        reserve_factor = _borrow_factor(raw_reserve_factor)
        effective_factor = leg_factor if leg_factor is not None else reserve_factor
        # eMode replaces the borrow factor with 1: the elevation group's
        # liquidation threshold already encodes the risk. Base mode multiplies
        # each debt leg by its own/reserve factor when one is published.
        if elevation_group > 0 or effective_factor is None:
            effective_factor = Decimal("1")
        b_leg = leg(
            symbol=str(item.get("tokenName") or mint[:6] or "UNKNOWN"),
            amount=item.get("tokenAmount"),
            usd=usd,
            contract=mint,
            apy_borrow=apy,
            lowercase_contract=False,
        )
        # LTV belongs to the collateral reserve; debt carries only its borrow
        # factor. Keep the keys present on every leg so the shape is uniform.
        b_leg["maxLtv"] = None
        b_leg["liquidationLtv"] = None
        if raw_leg_factor is not None:
            b_leg["borrowFactor"] = fnum(raw_leg_factor)
        elif raw_reserve_factor is not None and elevation_group <= 0:
            b_leg["borrowFactor"] = fnum(effective_factor)
        if item.get("tokenPrice") is not None:
            b_leg["price"] = fnum(item.get("tokenPrice"))
        borrows.append(b_leg)
        debt_usd += usd
        borrow_factor_debt_usd += usd * effective_factor
        if effective_factor != 1:
            borrow_factor_used = True
        if apy is not None:
            borrow_pairs.append((usd, apy))

    if (
        collateral_usd < _MIN_USD
        and debt_usd < _MIN_USD
        and not price_missing_legs
    ):
        return None

    # eMode: the loans API does not publish a reserve elevation-group table, so
    # use a leg/reserve value when present and otherwise the loan-level weighted
    # liquidationLtv (which the protocol reports after applying the group).
    elevation_source = "leg_base" if elevation_group > 0 else "base"
    reported_liq = dec(info.get("liquidationLtv"))
    liq_threshold: Decimal | None = None
    if collateral_usd > 0 and threshold_usd > 0:
        liq_threshold = threshold_usd / collateral_usd
        elevation_source = "leg_base" if elevation_group > 0 else "base"
    if elevation_group > 0 and elevation_leg_hits == 0:
        if reported_liq > 0:
            liq_threshold = reported_liq
            elevation_source = "loan_elevation"
        elif liq_threshold is not None:
            elevation_source = "leg_fallback"
    elif elevation_group > 0 and elevation_leg_hits > 0:
        if elevation_leg_hits < collateral_legs:
            elevation_source = "leg_mixed"
        elif elevation_hit_sources == {"reserve_elevation"}:
            elevation_source = "reserve_elevation"
        elif elevation_hit_sources == {"leg_elevation"}:
            elevation_source = "leg_elevation"
        else:
            elevation_source = "leg_mixed"
    elevation_degraded = False
    if elevation_group > 0 and collateral_usd > 0 and (
        liq_threshold is None or (reported_liq <= 0 and elevation_missing_legs > 0)
    ):
        # Group is active but no effective eMode LTV survived the payload
        # (field renamed/removed). Refuse to blend base-mode legs: skip HF and
        # flag the row instead of silently understating liquidation risk.
        liq_threshold = None
        elevation_source = "elevation_missing"
        elevation_degraded = True
    reported_ltv = info.get("currentLtv")
    ltv = fnum(reported_ltv) if reported_ltv is not None and dec(reported_ltv) > 0 else (
        fnum(debt_usd / collateral_usd) if collateral_usd > 0 and debt_usd > 0 else None
    )
    # Kamino's richer obligation payloads can carry the protocol's own
    # liquidation limit and borrow-factor-adjusted debt. Prefer that ratio;
    # otherwise reproduce it from borrow factors when base mode publishes one.
    # The final fallback is the legacy collateral * liquidationLtv / debt.
    hf_basis = "ltv_formula"
    hf: float | None = None
    stats = _protocol_hf_stats(loan, info)
    if stats is not None:
        limit, adjusted_debt = stats
        hf = float(limit / adjusted_debt)
        hf_basis = "protocol_stats"
    else:
        hf_debt_usd = borrow_factor_debt_usd if borrow_factor_used else debt_usd
        hf = health_factor(
            None,
            collateral_usd=collateral_usd,
            debt_usd=hf_debt_usd,
            liq_threshold=liq_threshold,
        )
        if borrow_factor_used:
            hf_basis = "borrow_factor"
    obligation = str(loan.get("loanId") or "")
    pnl_payload = extras["pnl"].get(obligation)
    pnl = _pnl_usd(pnl_payload)
    earned = _fee_usd(extras["earned"].get(obligation), "totalFeesEarnedObligation")
    paid = _fee_usd(extras["paid"].get(obligation), "totalFeesPaidObligation")
    net = collateral_usd - debt_usd
    days = _holding_days(
        extras["earned"].get(obligation),
        extras["paid"].get(obligation),
        moment=moment,
    )
    invested = _invested_usd(pnl_payload)
    pnl_apr = (
        float(Decimal(str(pnl)) / invested * Decimal("365") / Decimal(str(days)))
        if pnl is not None and invested is not None and invested > 0 and days
        else None
    )
    apy_value, apy_estimated, apy_basis, yield_missing, apy_estimate_raw = _yield_metrics(
        collaterals=collaterals,
        borrows=borrows,
        supply_pairs=supply_pairs,
        borrow_pairs=borrow_pairs,
        collateral_usd=collateral_usd,
        debt_usd=debt_usd,
        net=net,
        pnl=pnl,
        paid=paid,
        earned=earned,
        days=days,
        invested=invested,
        audit_price_estimate=jlp_collateral_present or onyc_collateral_present,
    )
    # A price-return estimate can explode on volatile collateral (JLP: 136%+).
    # Keep the raw figure for audit but stop publishing it as a net APY.
    apy_outlier = (
        apy_estimated
        and apy_value is not None
        and abs(apy_value) > _APY_ESTIMATE_OUTLIER
    )
    if apy_outlier:
        apy_value = None
    liq_price, liq_price_estimated, liq_price_note = _liq_price(
        collaterals, borrows, liq_threshold, debt_usd
    )
    details = empty_details(
        collateral=collaterals,
        borrow=borrows,
        health_factor=hf,
        ltv=ltv,
        liq_threshold=fnum(liq_threshold) if liq_threshold is not None else None,
        liq_price=liq_price,
        net_apy=apy_value,
        apy_estimated=apy_estimated,
        apy_basis=apy_basis,
        yield_data_missing=yield_missing,
        interest_paid_usd=paid,
        interest_earned_usd=earned,
        pnl_usd=pnl,
        pnl_apr=pnl_apr,
        **(
            {
                "apy_estimate_raw": apy_estimate_raw,
                **(
                    {"apy_outlier": True, "apy_note": "估算值，仅供参考"}
                    if apy_outlier
                    else {}
                ),
            }
            # The legacy price-return estimate stays in the audit field even
            # when the official JLP / ONyc APY replaced it. An ordinary ONyc
            # estimate is only exposed when it hit the outlier guard, matching
            # the previous shape.
            if apy_estimate_raw is not None
            and (
                apy_outlier
                or jlp_collateral_present
                or onyc_collateral_present
            )
            else {}
        ),
        **(
            {"price_missing": True, "price_missing_legs": price_missing_legs}
            if price_missing_legs
            else {}
        ),
    )
    # Keep the per-leg LTV inputs alongside the aggregated figures so the UI
    # can explain multi-collateral risk without re-fetching the loan payload.
    details["legs"] = [
        {**item, "side": "collateral"} for item in collaterals
    ] + [{**item, "side": "debt"} for item in borrows]
    details["elevation_group"] = elevation_group
    details["elevation_ltv_source"] = elevation_source
    details["elevation_group_field_present"] = elevation_field_present
    details["hf_basis"] = hf_basis
    if elevation_degraded:
        details["elevation_degraded"] = True
        details["elevation_note"] = (
            "elevationGroup 生效但响应缺少可用的 eMode 生效 LTV；已跳过 HF，"
            "避免按 base 阈值静默算错。"
        )
    if liq_price_estimated:
        details["liq_price_estimated"] = True
        details["liq_price_note"] = liq_price_note
    label_bits = [row["symbol"] for row in collaterals[:2]] or [row["symbol"] for row in borrows[:1]]
    label = "Kamino " + "/".join(label_bits) if label_bits else "Kamino"
    # Key the row by the real market, not the obligation. The obligation is
    # still exposed for debugging and is the legacy market_ref alias.
    market_key = str(market_id or loan.get("marketId") or obligation or "")
    market_ref = f"kamino-{market_key or obligation}"
    details["market"] = market_key or None
    details["obligation"] = obligation or None
    legacy_market_refs = (
        [f"kamino-{obligation}"]
        if obligation and f"kamino-{obligation}" != market_ref
        else []
    )
    has_debt = debt_usd > 0 or "debt" in price_missing_legs
    mark_hf_unavailable(details, debt_usd=debt_usd, hf=hf, has_debt=has_debt)
    warn_debt_without_hf(
        PROTOCOL,
        market_ref,
        debt_usd=debt_usd,
        hf=hf,
        has_debt=has_debt,
    )
    row = position_row(
        protocol=PROTOCOL,
        chain=CHAIN,
        account_ref=owner,
        market_ref=market_ref,
        label=label,
        collateral_usd=collateral_usd,
        debt_usd=debt_usd,
        details=details,
        asof=moment,
        lowercase_account=False,
    )
    if legacy_market_refs:
        row["legacy_market_refs"] = legacy_market_refs
    return row


def _invested_usd(payload: Any) -> Decimal | None:
    """Initial net equity reported by the Kamino pnl endpoint (USD)."""
    if not isinstance(payload, dict):
        return None
    invested = payload.get("invested")
    if not isinstance(invested, dict):
        return None
    raw = invested.get("usd")
    if raw is None or raw == "":
        return None
    value = dec(raw)
    return value if value > 0 else None


def _history_start(*payloads: Any) -> datetime | None:
    """Earliest daily fee timestamp across the obligation's history payloads.

    Kamino's interest endpoints return ``historicalFeesObligation``: one list
    per obligation of daily entries. Its first timestamp approximates when the
    position's accounting started, which is the best holding period available
    without reading transaction history.
    """
    starts: list[int] = []
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        block = payload.get("historicalFeesObligation")
        if not isinstance(block, dict):
            continue
        for entries in block.values():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                # Live payloads key each daily entry by reserve mint:
                # ``{"<mint>": {"ts": ..., "usdFees": ...}}``. Older/recorded
                # shapes may nest it under ``reserve`` or put ``ts`` directly.
                candidates = [entry]
                if isinstance(entry.get("reserve"), dict):
                    candidates.append(entry["reserve"])
                candidates.extend(v for v in entry.values() if isinstance(v, dict))
                for candidate in candidates:
                    raw = candidate.get("ts")
                    try:
                        starts.append(int(raw))
                    except (TypeError, ValueError):
                        continue
    if not starts:
        return None
    return datetime.fromtimestamp(min(starts) / 1000, tz=timezone.utc)


def _holding_days(*payloads: Any, moment: str) -> float | None:
    """Days from the earliest fee-history entry to ``moment`` (>= 1 day)."""
    start = _history_start(*payloads)
    if start is None:
        return None
    try:
        end = datetime.fromisoformat(str(moment).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    days = (end - start).total_seconds() / 86400.0
    return days if days >= 1.0 else None


def _weighted_rate(pairs: list[tuple[Decimal, Decimal]], total_usd: Decimal) -> Decimal | None:
    if total_usd <= 0 or not pairs:
        return None
    return sum((usd * rate for usd, rate in pairs), Decimal("0")) / total_usd


def _price_return_estimate(
    *,
    collaterals_usd: Decimal,
    debt_usd: Decimal,
    net: Decimal,
    pnl: float | None,
    paid: float | None,
    earned: float | None,
    days: float | None,
    invested: Decimal | None,
    borrows: list[dict],
    borrow_pairs: list[tuple[Decimal, Decimal]],
) -> tuple[float | None, str | None]:
    """Legacy collateral price-return APY estimate, or ``(None, None)``.

    This is not a protocol-reported yield. It is kept for carry analysis and
    as an audit value; the caller decides whether it may be published.
    """
    price_data = (
        pnl is not None
        and paid is not None
        and earned is not None
        and days is not None
        and collaterals_usd > 0
        and net > 0
    )
    if not price_data:
        return None, None
    gross_gain = Decimal(str(pnl)) + Decimal(str(paid)) - Decimal(str(earned))
    price_apr = gross_gain / collaterals_usd * Decimal("365") / Decimal(str(days))
    borrow_complete = all(leg.get("apy_borrow") is not None for leg in borrows)
    reported_borrow = _weighted_rate(borrow_pairs, debt_usd) if borrows else Decimal("0")
    if reported_borrow is None or not borrow_complete:
        # Missing/partial borrow rates: fall back to the pnl endpoint's
        # return on invested equity, which is already net of interest.
        if invested is None:
            return None, None
        value = float(
            Decimal(str(pnl)) / invested * Decimal("365") / Decimal(str(days))
        )
        return value, "pnl_total_return"
    value = float(
        (collaterals_usd * price_apr - debt_usd * reported_borrow) / net
    )
    return value, "pnl_collateral_price_yield"


def _yield_metrics(
    *,
    collaterals: list[dict],
    borrows: list[dict],
    supply_pairs: list[tuple[Decimal, Decimal]],
    borrow_pairs: list[tuple[Decimal, Decimal]],
    collateral_usd: Decimal,
    debt_usd: Decimal,
    net: Decimal,
    pnl: float | None,
    paid: float | None,
    earned: float | None,
    days: float | None,
    invested: Decimal | None,
    audit_price_estimate: bool = False,
) -> tuple[float | None, bool, str, bool, float | None]:
    """Net APY and its data basis for one Kamino obligation.

    Reported rates are used when every leg carries one. JLP gets its official
    Jupiter APY and ONyc gets DefiLlama's ``defillama:onre`` APY on the
    collateral leg, so both land here as reported data. ONyc no longer falls
    into the zero-supply price-return publish path. When another leveraged
    obligation's collateral reports a zero/missing supply APY, the old
    carry-only figure showed the borrow cost as if the collateral earned
    nothing. For those rows the collateral's price return is estimated as
    ``(pnl + interest paid - interest earned) / collateral`` annualized over
    the fee-history window, then netted against the borrow cost.
    ``apy_estimated`` marks that estimate and ``apy_basis`` records which
    method was used.

    The fifth return value is the raw estimate (None for ordinary reported
    rates) so callers can suppress an outlier while keeping it in details. With
    ``audit_price_estimate`` the raw price-return figure is also returned for
    a reported-rate row (used to audit JLP / ONyc while publishing the official
    feed APY).

    If neither reported rates nor a complete pnl/interest/history window are
    available, the APY is None and ``yield_data_missing`` is true: no
    borrow-cost-only number is emitted.
    """
    supply_complete = bool(collaterals) and all(
        leg.get("apy_supply") is not None for leg in collaterals
    )
    borrow_complete = all(leg.get("apy_borrow") is not None for leg in borrows)
    reported_supply = sum(
        (usd * apy for usd, apy in supply_pairs), Decimal("0")
    )
    has_debt = debt_usd > _MIN_USD

    # Zero/missing supply carry: only a price-return estimate can avoid showing
    # the borrow leg alone. JLP no longer lands here (jupiter:jlp-info) and ONyc
    # no longer lands here either (defillama:onre fills its supply rate before
    # this call).
    if has_debt and net > 0 and supply_complete and reported_supply <= 0:
        value, basis = _price_return_estimate(
            collaterals_usd=collateral_usd,
            debt_usd=debt_usd,
            net=net,
            pnl=pnl,
            paid=paid,
            earned=earned,
            days=days,
            invested=invested,
            borrows=borrows,
            borrow_pairs=borrow_pairs,
        )
        if value is None:
            return None, True, "insufficient", True, None
        return value, True, basis or "insufficient", False, value

    if supply_complete and borrow_complete:
        value = net_apy(supply_pairs, borrow_pairs, net)
        audit = None
        if audit_price_estimate:
            audit, _basis = _price_return_estimate(
                collaterals_usd=collateral_usd,
                debt_usd=debt_usd,
                net=net,
                pnl=pnl,
                paid=paid,
                earned=earned,
                days=days,
                invested=invested,
                borrows=borrows,
                borrow_pairs=borrow_pairs,
            )
        if value is None:
            # Zero net equity: no meaningful APY, but the data was present.
            return None, False, "reported_rates", False, audit
        return value, False, "reported_rates", False, audit

    return None, False, "insufficient", True, None


def _liq_price(
    collaterals: list[dict],
    borrows: list[dict],
    liq_threshold: Decimal | None,
    debt_usd: Decimal,
) -> tuple[float | None, bool, str | None]:
    """Price at which collateral * liq_threshold == debt.

    Single collateral keeps the original direct formula. With several
    collateral legs we cannot know which one the protocol will liquidate first,
    so publish an explicit estimate: assume all collateral prices move together,
    use their USD-weighted average price as the reference, and solve for the
    common move that makes the (already liquidationLtv-weighted) threshold
    equal to debt. For one leg this algebraically equals the direct formula.
    """
    if liq_threshold is None or liq_threshold <= 0 or debt_usd <= 0 or not collaterals:
        return None, False, None
    if len(collaterals) == 1:
        amount = dec(collaterals[0].get("amount"))
        if amount <= 0:
            return None, False, None
        return fnum(debt_usd / (amount * liq_threshold)), False, None

    total_usd = Decimal("0")
    weighted_price = Decimal("0")
    for item in collaterals:
        usd = dec(item.get("usd"))
        amount = dec(item.get("amount"))
        price = dec(item.get("price"))
        if price <= 0 and amount > 0 and usd > 0:
            price = usd / amount
        if usd <= 0 or price <= 0:
            return None, False, None
        total_usd += usd
        weighted_price += price * usd
    if total_usd <= 0:
        return None, False, None
    reference_price = weighted_price / total_usd
    factor = debt_usd / (liq_threshold * total_usd)
    if reference_price <= 0 or factor <= 0:
        return None, False, None
    note = (
        "多抵押估算：以各抵押腿 USD 占比为权重、按各腿 liquidationLtv 加权得到组合清算阈值；"
        "假设各抵押品价格同比例变动，以 USD 加权均价为基准折算清算价。"
    )
    return fnum(reference_price * factor), True, note


def _pnl_usd(payload: Any) -> float | None:
    if not isinstance(payload, dict):
        return None
    raw = payload.get("usd")
    if raw is None or raw == "":
        return None
    return fnum(raw)


def _fee_usd(payload: Any, key: str) -> float | None:
    """Sum ``usdFees`` across the obligation's fee map. None when absent."""
    if not isinstance(payload, dict):
        return None
    block = payload.get(key)
    if not isinstance(block, dict) or not block:
        return None
    total = Decimal("0")
    found = False
    for item in block.values():
        if isinstance(item, dict) and item.get("usdFees") is not None:
            total += dec(item.get("usdFees"))
            found = True
    return fnum(total) if found else None


def _rates_by_mint(reserves: Any) -> dict[str, dict[str, Any]]:
    """Last reserve wins when a mint is listed more than once (main pool last)."""
    rates: dict[str, dict[str, Any]] = {}
    if not isinstance(reserves, list):
        return rates
    for item in reserves:
        if not isinstance(item, dict):
            continue
        mint = str(item.get("liquidityTokenMint") or "")
        if not mint:
            continue
        supply = item.get("supplyApy")
        borrow = item.get("borrowApy")
        rates[mint] = {
            "supply": dec(supply) if supply is not None and supply != "" else None,
            "borrow": dec(borrow) if borrow is not None and borrow != "" else None,
            # Base-mode debt risk input; some payloads publish it only here,
            # not on each loan's borrow leg. Kept raw and normalized at use.
            "borrow_factor": _first_present(
                item,
                (
                    "borrowFactor",
                    "borrow_factor",
                    "borrowFactorPct",
                    "borrow_factor_pct",
                ),
            ),
            # Effective eMode thresholds may be published per reserve; the
            # public loans payload currently puts them on each deposit leg.
            "elevation": _reserve_elevations(item),
        }
    return rates


def _summary_markets(
    http: httpx.Client, owner: str, *, sleep: Callable[[float], None] | None = None
) -> list[str]:
    """Markets named by the earn summary (fast path)."""
    summary = _get_json(
        http, f"/v3/earn/users/{owner}/summary", sleep=sleep
    )
    found: list[str] = []
    tokens = summary.get("tokens") if isinstance(summary, dict) else None
    for token in tokens or []:
        if not isinstance(token, dict):
            continue
        for position in token.get("positions") or []:
            if not isinstance(position, dict):
                continue
            market = str(position.get("market") or "")
            if market and market not in found:
                found.append(market)
    return found


def _markets_for(
    http: httpx.Client,
    owner: str,
    *,
    sleep: Callable[[float], None] | None = None,
) -> list[str]:
    """Compatibility helper: summary markets plus the full list.

    ``fetch_positions`` splits the two calls so one failing endpoint is
    recorded without losing the other. This wrapper raises if either fails.
    """
    summary_markets = _summary_markets(http, owner, sleep=sleep)
    all_markets = _all_markets_cached(http, sleep=sleep)
    return _merge_markets(owner, summary_markets, all_markets)


def _merge_markets(
    owner: str, summary_markets: list[str], all_markets: list[str]
) -> list[str]:
    """Summary markets first, then the full list minus recently empty probes.

    A borrow-only/debt-only obligation can live in a market the earn summary
    does not mention, so a summary hit no longer ends discovery. Empty probes
    are cached per wallet so the ~50-market list stays cheap on later syncs.
    """
    now = time.monotonic()
    markets = list(summary_markets)
    for market in all_markets:
        if market in markets:
            continue
        until = _probe_cache.get((owner, market))
        if until is not None and until > now:
            continue
        markets.append(market)
    return markets


def _all_markets_cached(
    http: httpx.Client, *, sleep: Callable[[float], None] | None = None
) -> list[str]:
    cached = _market_list_cache.get(API_URL)
    if cached is not None and cached[0] > time.monotonic():
        return list(cached[1])
    markets = _all_markets(http, sleep=sleep)
    _market_list_cache[API_URL] = (time.monotonic() + _MARKET_LIST_TTL, markets)
    return markets


def _all_markets(
    http: httpx.Client, *, sleep: Callable[[float], None] | None = None
) -> list[str]:
    payload = _get_json(http, "/v2/kamino-market", sleep=sleep)
    if isinstance(payload, dict):
        payload = payload.get("markets") or payload.get("data") or []
    if not isinstance(payload, list):
        raise ValueError("kamino markets not a list")
    markets: list[str] = []
    for item in payload:
        if isinstance(item, dict):
            market = str(item.get("lendingMarket") or item.get("market") or "")
        else:
            market = str(item or "")
        if market:
            markets.append(market)
    return markets


def _get_json(
    http: httpx.Client, path: str, *, sleep: Callable[[float], None] | None = None
) -> Any:
    return get_json_with_retry(http, API_URL + path, sleep=sleep)


def _sol_address(value: object) -> str:
    """Solana addresses are case-sensitive base58. Do not lowercase them."""
    return str(value or "").strip()
