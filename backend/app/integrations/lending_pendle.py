"""Pendle lending / leveraged positions only.

Pendle itself is a yield-tokenization AMM (PT / YT / LP). Those spot holdings
already live in the flat wallet list (PLP- / PT- / YT-, including sUSDx PT)
and must **not** become positions — see the reconciliation note in
``/workspace/p4b_result.md``.

A Pendle PT used as collateral on Aave or Morpho is that protocol's position
and is returned by ``lending_aave`` / ``lending_morpho``, never here.

This module returns rows only from Pendle's own PT-looping boxes
(``/v1/pt-looping/loop/positions``: collateral, debt, health factor). The
dashboard snapshot (``/v1/dashboard/positions/database/{user}``) is PT / YT /
LP / SY and is not read. A payload that only carries those spot types
normalizes to ``[]``.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

import httpx

from .http import quiet_client
from .lending_common import (
    asof_iso,
    dec,
    empty_details,
    fnum,
    health_factor,
    leg,
    log_failure,
    net_apy,
    norm_address,
    position_row,
)

logger = logging.getLogger(__name__)

PROTOCOL = "pendle"
# Pendle's own leveraged boxes (PT posted as collateral, debt in a money market).
# Not the dashboard, which is spot PT/YT/LP/SY.
API_URL = "https://api-v2.pendle.finance/core/v1/pt-looping/loop/positions"

# Spot product types Pendle already accounts for as holdings. Never positions.
_SPOT_TYPES = {"pt", "yt", "lp", "sy", "plp"}
_LENDING_KINDS = {"lending", "leverage", "loop", "borrow"}


def fetch_positions(
    address: str,
    *,
    client: httpx.Client | None = None,
    use_mock: bool = False,
    chain_id: int = 1,
) -> list[dict]:
    """Lending/leverage rows only. Spot PT/YT/LP and any error become ``[]``."""
    if use_mock:
        import json
        from pathlib import Path

        path = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "lending_evm" / "pendle_user.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        return normalize(address, payload, chain="ethereum")

    owner = norm_address(address)
    if not owner:
        return []
    own_client = client is None
    http = client or quiet_client(timeout=20.0)
    try:
        response = http.get(API_URL, params={"chainId": chain_id, "owner": owner})
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, list):
            payload = {"positions": payload}
        if not isinstance(payload, dict):
            raise ValueError("pendle response not an object")
        chain = {1: "ethereum", 42161: "arbitrum", 8453: "base"}.get(chain_id, "ethereum")
        return normalize(owner, payload, chain=chain)
    except Exception as exc:  # noqa: BLE001
        log_failure("pendle", "ethereum" if chain_id == 1 else str(chain_id), exc)
        return []
    finally:
        if own_client:
            http.close()


def normalize(address: str, payload: dict[str, Any], *, chain: str) -> list[dict]:
    """Keep only rows tagged lending/leverage. PT/YT/LP positions are dropped."""
    items = _items(payload)
    rows: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            if not _is_lending(item):
                continue
            built = _one(address, chain, item)
        except Exception as exc:  # noqa: BLE001
            log_failure("pendle", chain, exc)
            continue
        if built is not None:
            rows.append(built)
    return rows


def _items(payload: dict[str, Any]) -> list:
    for key in ("positions", "data", "results"):
        raw = payload.get(key)
        if isinstance(raw, list):
            return raw
        if isinstance(raw, dict):
            nested = raw.get("positions") or raw.get("items")
            if isinstance(nested, list):
                return nested
    return []


def _is_lending(item: dict[str, Any]) -> bool:
    # Live PT-looping rows. Valuation is collateral minus debt; spot PT/YT/LP
    # never carry this object, so they cannot be mistaken for a borrow.
    valuation = item.get("valuation")
    if isinstance(valuation, dict) and (
        valuation.get("debtValueUsd") is not None or valuation.get("collateralValueUsd") is not None
    ):
        return True
    kind = str(item.get("kind") or item.get("category") or item.get("type") or "").strip().lower()
    if kind in _SPOT_TYPES:
        return False
    if kind in _LENDING_KINDS:
        return True
    # A nested open borrow is the only other signal we accept.
    borrow = item.get("borrow")
    return isinstance(borrow, list) and any(isinstance(row, dict) and dec(row.get("usd")) > 0 for row in borrow)


def _one(address: str, chain: str, item: dict[str, Any]) -> dict | None:
    valuation = item.get("valuation") if isinstance(item.get("valuation"), dict) else None
    if valuation is not None and valuation.get("collateralValueUsd") is not None:
        return _from_loop(address, chain, item, valuation)
    collateral = _legs(item.get("collateral"), side="collateral")
    borrow = _legs(item.get("borrow"), side="borrow")
    coll_usd = sum((dec(row["usd"]) for row in collateral), Decimal("0"))
    debt_usd = sum((dec(row["usd"]) for row in borrow), Decimal("0"))
    if coll_usd <= 0 and debt_usd <= 0:
        # Some payloads only carry a single notional. That is still a spot
        # balance unless a borrow leg exists — and we already required one.
        return None
    liq = dec(item.get("liq_threshold") or item.get("lltv") or "0") or None
    reported = item.get("health_factor") if item.get("health_factor") is not None else item.get("healthFactor")
    hf = health_factor(reported, collateral_usd=coll_usd, debt_usd=debt_usd, liq_threshold=liq)
    ltv = (debt_usd / coll_usd) if coll_usd > 0 else None
    supply_pairs = [(dec(row["usd"]), dec(row.get("apy_supply"))) for row in collateral]
    borrow_pairs = [(dec(row["usd"]), dec(row.get("apy_borrow"))) for row in borrow]
    net = coll_usd - debt_usd
    market = str(item.get("market") or item.get("market_ref") or item.get("id") or "pendle-lending")
    details = empty_details(
        collateral=collateral,
        borrow=borrow,
        health_factor=hf,
        ltv=fnum(ltv) if ltv is not None else None,
        liq_threshold=fnum(liq) if liq is not None else None,
        liq_price=item.get("liq_price"),
        net_apy=net_apy(supply_pairs, borrow_pairs, net),
        interest_paid_usd=item.get("interest_paid_usd"),
        interest_earned_usd=item.get("interest_earned_usd"),
        pnl_usd=item.get("pnl_usd"),
        pnl_apr=item.get("pnl_apr"),
    )
    return position_row(
        protocol=PROTOCOL,
        chain=str(item.get("chain") or chain),
        account_ref=address,
        market_ref=market,
        label=str(item.get("label") or f"Pendle {market}"),
        collateral_usd=coll_usd,
        debt_usd=debt_usd,
        details=details,
        opened_at=item.get("opened_at"),
        asof=asof_iso(),
    )


def _from_loop(address: str, chain: str, item: dict[str, Any], valuation: dict[str, Any]) -> dict | None:
    """One Pendle PT-looping box. Health factor and LLTV come from the protocol."""
    coll_usd = dec(valuation.get("collateralValueUsd"))
    debt_usd = dec(valuation.get("debtValueUsd"))
    if coll_usd <= 0 and debt_usd <= 0:
        return None
    threshold = item.get("liquidationThreshold") if isinstance(item.get("liquidationThreshold"), dict) else {}
    liq = dec(threshold.get("liquidationLtv")) or None
    leverage = item.get("leverage") if isinstance(item.get("leverage"), dict) else {}
    apy = item.get("apy") if isinstance(item.get("apy"), dict) else {}
    chain_state = item.get("chainState") if isinstance(item.get("chainState"), dict) else {}
    reported_chain = {1: "ethereum", 42161: "arbitrum", 8453: "base"}.get(item.get("chainId"), chain)
    collateral = [
        leg(
            symbol="PT",
            amount=chain_state.get("collateralRaw"),
            usd=coll_usd,
            contract=str(item.get("ptAddress") or ""),
            apy_supply=apy.get("avgFixedApy") if apy.get("avgFixedApy") is not None else apy.get("collateralSupplyApy"),
        )
    ]
    borrow = []
    if debt_usd > 0 or dec(chain_state.get("debtRaw")) > 0:
        borrow.append(
            leg(
                symbol=str(item.get("debtSymbol") or "debt"),
                amount=chain_state.get("debtRaw"),
                usd=debt_usd,
                contract=str(item.get("debtAsset") or ""),
                apy_borrow=apy.get("borrowApy"),
            )
        )
    # Pendle's healthFactor is max(0, 1 - LTV/LLTV): 1 means healthy, 0 means
    # liquidated. That is not the collateral * threshold / debt ratio the rest
    # of the app alerts on, so recompute ours and keep theirs under another key.
    hf = health_factor(None, collateral_usd=coll_usd, debt_usd=debt_usd, liq_threshold=liq)
    ltv = dec(leverage.get("currentLtv")) if leverage.get("currentLtv") is not None else (
        (debt_usd / coll_usd) if coll_usd > 0 else None
    )
    net = dec(valuation.get("netValueUsd")) if valuation.get("netValueUsd") is not None else coll_usd - debt_usd
    # position_row recomputes net as collateral - debt, which matches netValueUsd.
    _ = net
    market = str(item.get("boxId") or item.get("boxAddress") or "pendle-loop")
    mm = str(item.get("moneyMarket") or "")
    details = empty_details(
        collateral=collateral,
        borrow=borrow,
        health_factor=hf,
        ltv=fnum(ltv) if ltv is not None else None,
        liq_threshold=fnum(liq) if liq is not None else None,
        liq_price=threshold.get("liquidationPtPriceUsd"),
        pendle_health_factor=item.get("healthFactor"),
        net_apy=fnum(apy.get("currentLoopingApy")) if apy.get("currentLoopingApy") is not None else None,
        pnl_usd=item.get("pnlUsd"),
    )
    return position_row(
        protocol=PROTOCOL,
        chain=reported_chain,
        account_ref=address,
        market_ref=market,
        label=f"Pendle loop {mm}".strip(),
        collateral_usd=coll_usd,
        debt_usd=debt_usd,
        details=details,
        opened_at=None,
        asof=asof_iso(),
    )


def _legs(raw: object, *, side: str) -> list[dict]:
    if not isinstance(raw, list):
        return []
    rows: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        rows.append(
            leg(
                symbol=str(item.get("symbol") or ""),
                amount=item.get("amount"),
                usd=item.get("usd"),
                contract=str(item.get("contract") or ""),
                apy_supply=item.get("apy_supply") if side == "collateral" else None,
                apy_borrow=item.get("apy_borrow") if side == "borrow" else None,
            )
        )
    return rows
