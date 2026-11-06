"""Morpho Blue positions via the public GraphQL API (no key).

``https://blue-api.morpho.org/graphql`` — ``userByAddress`` returns market
positions with supply, borrow, collateral, health factor and APYs. One open
market becomes one positions row. A market that fails to parse is skipped.
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
from .lending_pendle_apy import pendle_implied_apy
from .yield_sources import usd3_collateral_apy

logger = logging.getLogger(__name__)

PROTOCOL = "morpho"
API_URL = "https://blue-api.morpho.org/graphql"

# ``market.state.price`` is the oracle price as a raw BigInt. Morpho Blue
# scales it by 1e36 and the API keeps the token decimals in the integer, so the
# human price is ``raw * 10 ** (collateralDecimals - loanDecimals - 36)``.
_ORACLE_PRICE_EXPONENT = -36
# Some oracles use a non-standard scale. A ratio this far from the market price
# is treated as unreadable rather than as a real oracle basis.
_ORACLE_RATIO_MIN = Decimal("0.2")
_ORACLE_RATIO_MAX = Decimal("5")
# Keep the old market-price formula when the oracle/HF value is within 0.5%;
# only a material basis gap is worth changing the displayed liquidation price.
_LIQ_PRICE_STABLE_RATIO = Decimal("0.005")
# Mirrors lending_common._HF_SENTINEL: a uint256-max style marker is not a HF.
_PROTOCOL_HF_MAX = Decimal("1e18")

# Morpho chain ids we understand. Unknown ids are skipped, not fatal.
CHAIN_BY_ID = {
    1: "ethereum",
    8453: "base",
    42161: "arbitrum",
    137: "polygon",
    10: "optimism",
}

_QUERY = """
query MorphoUser($address: String!, $chainId: Int) {
  userByAddress(address: $address, chainId: $chainId) {
    address
    marketPositions {
      healthFactor
      state {
        supplyAssets
        supplyAssetsUsd
        borrowAssets
        borrowAssetsUsd
        collateral
        collateralUsd
      }
      market {
        marketId
        lltv
        # ``Asset.price`` is an ``AssetPrice`` object since the 2026-10 schema;
        # the old scalar field now fails validation with HTTP 400.
        loanAsset { symbol decimals address price { usd } }
        collateralAsset { symbol decimals address price { usd } }
        # ``state.price`` is the market oracle price (raw BigInt), used to
        # make liq_price agree with the protocol health factor.
        state { supplyApy borrowApy price }
      }
    }
  }
}
""".strip()


def fetch_positions(
    address: str,
    *,
    client: httpx.Client | None = None,
    use_mock: bool = False,
    chain_id: int | None = 1,
) -> list[dict]:
    """Normalized Morpho Blue rows. ``chain_id=None`` asks the API for every chain."""
    if use_mock:
        import json
        from pathlib import Path

        path = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "lending_evm" / "morpho_user.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        return normalize(address, payload)

    owner = norm_address(address)
    if not owner:
        return []
    own_client = client is None
    http = client or quiet_client(timeout=20.0)
    try:
        variables: dict[str, Any] = {"address": owner, "chainId": chain_id}
        response = http.post(API_URL, json={"query": _QUERY, "variables": variables})
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("morpho response not an object")
        return normalize(owner, payload)
    except Exception as exc:  # noqa: BLE001
        log_failure("morpho", "ethereum" if chain_id in (None, 1) else str(chain_id), exc)
        return []
    finally:
        if own_client:
            http.close()


def normalize(address: str, payload: dict[str, Any]) -> list[dict]:
    """Turn a Morpho ``userByAddress`` payload into positions rows."""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    user = data.get("userByAddress") if isinstance(data, dict) else None
    if user is None:
        return []
    if not isinstance(user, dict):
        raise ValueError("morpho userByAddress not an object")
    positions = user.get("marketPositions")
    if positions is None:
        return []
    if not isinstance(positions, list):
        raise ValueError("morpho marketPositions not a list")
    chain = CHAIN_BY_ID.get(int(user.get("chainId") or 1), "ethereum")
    rows: list[dict] = []
    for item in positions:
        if not isinstance(item, dict):
            continue
        try:
            built = _one(address, chain, item)
        except Exception as exc:  # noqa: BLE001 — one market must not sink the rest
            log_failure("morpho", chain, exc)
            continue
        if built is not None:
            rows.append(built)
    return rows


def _one(address: str, chain: str, item: dict[str, Any]) -> dict | None:
    market = item.get("market") if isinstance(item.get("market"), dict) else {}
    if market.get("chainId"):
        chain = CHAIN_BY_ID.get(int(market["chainId"]), chain)
    loan = market.get("loanAsset") if isinstance(market.get("loanAsset"), dict) else {}
    collateral_asset = market.get("collateralAsset") if isinstance(market.get("collateralAsset"), dict) else {}
    loan_symbol = str(loan.get("symbol") or "LOAN")
    coll_symbol = str(collateral_asset.get("symbol") or "COLL")
    loan_dec = int(loan.get("decimals") or 18)
    coll_dec = int(collateral_asset.get("decimals") or 18)

    # Live API nests balances under ``state``. Recorded fixtures (and older
    # responses) put them on the position itself; accept both.
    state_bal = item.get("state") if isinstance(item.get("state"), dict) else {}
    supply_assets = _units(_first(item, state_bal, "supplyAssets"), loan_dec)
    borrow_assets = _units(_first(item, state_bal, "borrowAssets"), loan_dec)
    coll_assets = _units(_first(item, state_bal, "collateral"), coll_dec)
    loan_price = _asset_price(loan)
    coll_price = _asset_price(collateral_asset)
    supply_usd = _usd(_first(item, state_bal, "supplyAssetsUsd"), supply_assets, loan_price)
    borrow_usd = _usd(_first(item, state_bal, "borrowAssetsUsd"), borrow_assets, loan_price)
    coll_usd = _usd(_first(item, state_bal, "collateralUsd"), coll_assets, coll_price)

    # A supplier-only market has no collateral asset; the supplied loan is the equity.
    collateral_usd = coll_usd if coll_usd > 0 else supply_usd
    debt_usd = borrow_usd
    if collateral_usd <= 0 and debt_usd <= 0 and supply_usd <= 0:
        return None

    market_state = market.get("state") if isinstance(market.get("state"), dict) else {}
    # APY lives on the market. Fixtures keep it on the position state instead.
    supply_apy = _apy(market_state.get("supplyApy") if market_state.get("supplyApy") is not None else state_bal.get("supplyApy"))
    borrow_apy = _apy(market_state.get("borrowApy") if market_state.get("borrowApy") is not None else state_bal.get("borrowApy"))
    collateral_legs: list[dict] = []
    borrow_legs: list[dict] = []
    supply_pairs: list[tuple[Decimal, Decimal]] = []
    borrow_pairs: list[tuple[Decimal, Decimal]] = []
    # Morpho market supplyApy is the loan-asset supply rate, not collateral yield.
    # PT collateral uses Pendle impliedApy. Anything else stays unknown — never 0,
    # never underlyingApy / aggregatedApy.
    collateral_apy_unknown = False
    apy_estimated = False
    if coll_usd > 0 or coll_assets > 0:
        coll_leg = _collateral_leg(
            chain=chain,
            symbol=coll_symbol,
            amount=coll_assets,
            usd=coll_usd,
            contract=str(collateral_asset.get("address") or ""),
        )
        collateral_legs.append(coll_leg)
        coll_apy = coll_leg.get("apy_supply")
        if coll_apy is None:
            collateral_apy_unknown = True
        else:
            supply_pairs.append((coll_usd, Decimal(str(coll_apy))))
            if coll_leg.get("apy_source") == "pendle_implied" and not coll_leg.get("apy_expired"):
                apy_estimated = True
    if supply_usd > 0 or supply_assets > 0:
        collateral_legs.append(
            leg(
                symbol=loan_symbol,
                amount=supply_assets,
                usd=supply_usd,
                contract=str(loan.get("address") or ""),
                apy_supply=supply_apy,
            )
        )
        supply_pairs.append((supply_usd, supply_apy))
        if coll_usd <= 0:
            collateral_usd = supply_usd
    if borrow_usd > 0 or borrow_assets > 0:
        borrow_legs.append(
            leg(
                symbol=loan_symbol,
                amount=borrow_assets,
                usd=borrow_usd,
                contract=str(loan.get("address") or ""),
                apy_borrow=borrow_apy,
            )
        )
        borrow_pairs.append((borrow_usd, borrow_apy))

    lltv = dec(market.get("lltv"))
    # Morpho LLTV is a WAD (1e18) on chain; the API also serves a 0–1 float.
    if lltv > 1:
        lltv = lltv / (Decimal(10) ** 18)
    liq = lltv if lltv > 0 else None
    current_ltv = (debt_usd / collateral_usd) if collateral_usd > 0 else None
    reported = item.get("healthFactor")
    hf = health_factor(reported, collateral_usd=collateral_usd, debt_usd=debt_usd, liq_threshold=liq)
    net = collateral_usd - debt_usd
    market_price = (coll_usd / coll_assets) if coll_assets > 0 and coll_usd > 0 else None
    oracle_price = _oracle_price(
        market_state, coll_dec, loan_dec, market_price, loan_price
    )
    liq_price, liq_basis, liq_estimated, liq_note = _effective_liq_price(
        coll_amount=coll_assets,
        debt_usd=debt_usd,
        lltv=liq,
        market_price=market_price,
        oracle_price=oracle_price,
        health_factor_value=_reported_health_factor(reported),
    )
    unique = str(market.get("uniqueKey") or market.get("marketId") or "")
    market_ref = unique or f"morpho-{chain}-{coll_symbol}-{loan_symbol}".lower()
    label = f"Morpho {coll_symbol}/{loan_symbol}" if coll_usd > 0 else f"Morpho {loan_symbol} supply"
    details = empty_details(
        collateral=collateral_legs,
        borrow=borrow_legs,
        health_factor=hf,
        ltv=fnum(current_ltv) if current_ltv is not None else None,
        liq_threshold=fnum(liq) if liq is not None else None,
        liq_price=liq_price,
        **({"liq_price_basis": liq_basis} if liq_basis else {}),
        **({"liq_price_estimated": True} if liq_estimated else {}),
        **({"liq_price_note": liq_note} if liq_note else {}),
        # A missing collateral yield would make (0 − borrow cost) / equity a
        # large fake negative. Omit net APY until that yield is known.
        net_apy=None if collateral_apy_unknown else net_apy(supply_pairs, borrow_pairs, net),
        yield_data_missing=True if collateral_apy_unknown else False,
        **({"apy_estimated": True} if apy_estimated else {}),
        interest_paid_usd=None,
        interest_earned_usd=None,
        pnl_usd=None,
        pnl_apr=None,
        # Morpho's GraphQL API exposes no per-position interest/PnL history, and
        # position snapshots cannot reconstruct realized interest. Keep the
        # fields null and say so instead of manufacturing an estimate.
        pnl_data_missing=True,
    )
    return position_row(
        protocol=PROTOCOL,
        chain=chain,
        account_ref=address,
        market_ref=market_ref,
        label=label,
        collateral_usd=collateral_usd,
        debt_usd=debt_usd,
        details=details,
        opened_at=None,
        asof=asof_iso(),
    )


def _collateral_leg(
    *,
    chain: str,
    symbol: str,
    amount: Decimal,
    usd: Decimal,
    contract: str,
) -> dict:
    """Collateral leg. PT gets Pendle impliedApy, USD3 gets DefiLlama."""
    row = leg(
        symbol=symbol,
        amount=amount,
        usd=usd,
        contract=contract,
        apy_supply=None,
    )
    if _is_usd3(chain, symbol):
        try:
            quote = usd3_collateral_apy()
        except Exception as exc:  # noqa: BLE001 — a yield feed must not drop the position
            log_failure("defillama", chain, exc, fatal=False)
            return row
        if quote is None:
            return row
        row["apy_supply"] = fnum(quote.apy)
        row["apy_source"] = quote.source
        return row
    if not str(symbol or "").startswith("PT-"):
        return row
    try:
        quote = pendle_implied_apy(chain, contract)
    except Exception as exc:  # noqa: BLE001 — a quote failure must not drop the position
        log_failure("pendle_implied", chain, exc)
        return row
    if quote is None:
        return row
    row["apy_supply"] = fnum(quote.implied_apy)
    row["apy_source"] = quote.source
    if quote.expired:
        row["apy_expired"] = True
    return row


def _is_usd3(chain: str, symbol: str) -> bool:
    """Exact USD3 only: PT-USD3 and sUSD3 are different assets.

    The DefiLlama pool is Ethereum-only, so another chain's token with the same
    symbol must stay unknown rather than inherit this pool's yield.
    """
    return (
        str(chain or "").strip().lower() == "ethereum"
        and str(symbol or "").strip().upper() == "USD3"
    )


def _asset_price(asset: dict[str, Any]) -> object:
    """USD price from the current ``price`` object or the legacy ``priceUsd`` scalar.

    Morpho's GraphQL schema replaced ``Asset.priceUsd`` with an ``AssetPrice``
    object. Reading both keeps recorded fixtures and any transition period
    valid, while the query asks for the current shape.
    """
    price = asset.get("price")
    if isinstance(price, dict):
        value = price.get("usd")
        if value not in (None, ""):
            return value
        return asset.get("priceUsd")
    if price not in (None, "", {}):
        return price
    return asset.get("priceUsd")


def _first(primary: dict, fallback: dict, key: str) -> object:
    if primary.get(key) not in (None, ""):
        return primary.get(key)
    return fallback.get(key)


def _units(raw: object, decimals: int) -> Decimal:
    """Asset amounts. Integers are raw base units; a fractional value is already scaled."""
    number = dec(raw)
    if number == 0:
        return Decimal("0")
    text = str(raw)
    if "." in text or "e" in text.lower():
        return number
    return number / (Decimal(10) ** decimals)


def _usd(raw: object, amount: Decimal, price: object) -> Decimal:
    if raw not in (None, ""):
        return dec(raw)
    if price not in (None, ""):
        return amount * dec(price)
    return Decimal("0")


def _apy(raw: object) -> Decimal:
    """Morpho APYs are fractions (0.05 = 5%). Values above 1 are treated as percent."""
    number = dec(raw)
    if number > 1:
        return number / Decimal(100)
    return number


def _liq_price(coll_amount: Decimal, debt_usd: Decimal, lltv: Decimal | None) -> float | None:
    """USD price of one collateral unit at liquidation. None when not a borrow."""
    if lltv is None or lltv <= 0 or coll_amount <= 0 or debt_usd <= 0:
        return None
    return fnum(debt_usd / (coll_amount * lltv))


def _reported_health_factor(raw: object) -> Decimal | None:
    """Only the protocol's own HF feeds the reverse price; never the fallback."""
    value = dec(raw, default="nan")
    if value.is_finite() and 0 < value < _PROTOCOL_HF_MAX:
        return value
    return None


def _oracle_price(
    market_state: dict[str, Any],
    collateral_decimals: int,
    loan_decimals: int,
    market_price: Decimal | None,
    loan_price_usd: object,
) -> Decimal | None:
    """Collateral oracle price in USD, or None when unusable.

    ``market.state.price`` is the raw oracle value, quoted in loan-asset units.
    The API does not expose a pre-scaled ``collateralPrice`` field, so the
    decimals have to be applied here, then the loan leg is converted to USD.
    A value far outside the market price is an oracle with a different scale
    (or bad data) and must not be trusted.
    """
    raw = market_state.get("price") if isinstance(market_state, dict) else None
    if raw in (None, ""):
        return None
    value = dec(raw)
    if value <= 0:
        return None
    exponent = _ORACLE_PRICE_EXPONENT + collateral_decimals - loan_decimals
    value = value * (Decimal(10) ** exponent)
    loan_price = dec(loan_price_usd, default="1")
    if loan_price.is_finite() and loan_price > 0:
        value = value * loan_price
    if value <= 0 or not value.is_finite():
        return None
    if market_price is not None and market_price > 0:
        ratio = value / market_price
        if ratio < _ORACLE_RATIO_MIN or ratio > _ORACLE_RATIO_MAX:
            return None
    return value


def _effective_liq_price(
    *,
    coll_amount: Decimal,
    debt_usd: Decimal,
    lltv: Decimal | None,
    market_price: Decimal | None,
    oracle_price: Decimal | None,
    health_factor_value: Decimal | None,
) -> tuple[float | None, str | None, bool, str | None]:
    """Liquidation price on the same price basis as the protocol health factor.

    The old ``debt / (coll_amount * lltv)`` value is a market-price threshold.
    When the API exposes the oracle price, convert that threshold to a
    market-equivalent price (``* market / oracle``), so it agrees with the
    protocol HF. Without an oracle price, ``market_price / HF`` is the same
    reverse derivation. Small differences keep the old formula to avoid noise.
    """
    if lltv is None or lltv <= 0 or coll_amount <= 0 or debt_usd <= 0:
        return None, None, False, None
    base = debt_usd / (coll_amount * lltv)
    if base <= 0:
        return None, None, False, None
    if market_price is not None and market_price > 0:
        if oracle_price is not None and oracle_price > 0:
            candidate = base * market_price / oracle_price
            if _materially_differs(candidate, base):
                return fnum(candidate), "oracle", False, None
            return fnum(base), "market", True, None
        if health_factor_value is not None and health_factor_value > 0:
            candidate = market_price / health_factor_value
            if _materially_differs(candidate, base):
                return (
                    fnum(candidate),
                    "hf_implied",
                    True,
                    "按协议健康度反推，含 oracle 基差",
                )
            return fnum(base), "market", True, None
    return fnum(base), "market", True, None


def _materially_differs(candidate: Decimal, base: Decimal) -> bool:
    if base <= 0:
        return False
    return abs(candidate - base) / base > _LIQ_PRICE_STABLE_RATIO
