"""Aave V3 lending positions for bound EVM wallets.

Read-only. Prefers the keyless Aave public GraphQL
(``https://api.v3.aave.com/graphql``: ``userSupplies`` / ``userBorrows`` /
``userMarketState``). The hosted subgraph name is deprecated (301), so it is
only a fallback for recorded fixtures. Alchemy ``eth_call`` of
``Pool.getUserAccountData`` is the last resort when a key is configured.

Default chain is Ethereum. Arbitrum / Base / Polygon / Optimism stay in
``AAVE_CHAINS`` but are off unless ``chains`` names them. One market or one
chain failing returns that slice as ``[]`` and does not sink the rest.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

import httpx

from ..config import settings
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

PROTOCOL = "aave"
RAY = Decimal(10) ** 27
WAD = Decimal(10) ** 18
USD_BASE = Decimal(10) ** 8  # Aave base currency is USD with 8 decimals.

# Pool.getUserAccountData(address) → (uint256 * 6)
GET_USER_ACCOUNT_DATA = "0xbf92857c"
# UiPoolDataProviderV3.getUserReservesData(provider, user) → (UserReserveData[], uint8)
GET_USER_RESERVES_DATA = "0x2032d7cc"

# Keyless public API. ``chainId`` is the numeric ChainId scalar (1 = Ethereum).
AAVE_GQL = "https://api.v3.aave.com/graphql"

# Pool addresses confirmed against ``markets(request: { chainIds })``.
# Other chains are configurable, default off. Optimism's pool was not in the
# public market list at probe time, so it has no pool and is skipped.
AAVE_CHAINS: dict[str, dict[str, Any]] = {
    "ethereum": {
        "chain_id": 1,
        "pools": (
            "0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2",  # AaveV3Ethereum
            "0x0AA97c284e98396202b6A04024F5E2c65026F3c0",  # AaveV3EthereumEtherFi
            "0x4e033931ad43597d96D6bcc25c280717730B58B1",  # AaveV3EthereumLido
            "0xAe05Cd22df81871bc7cC2a04BeCfb516bFe332C8",  # AaveV3EthereumHorizon
        ),
        "pool": "0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2",
        "ui_pool": "0x3F78BBD206e4D3c504Eb854232EdA7e47E9Fd8FC",
        "pool_addresses_provider": "0x2f39d218133AFaB8F2B819B1066c7E434Ad94E9e",
        "subgraph": "https://api.thegraph.com/subgraphs/name/aave/protocol-v3",
    },
    "arbitrum": {
        "chain_id": 42161,
        "pools": ("0x794a61358D6845594F94dc1DB02A252b5b4814aD",),
        "pool": "0x794a61358D6845594F94dc1DB02A252b5b4814aD",
        "ui_pool": "0x5c5228aC8BC1528482514aF3e27E692495148717",
        "pool_addresses_provider": "0xa97684ead0e402dC232d5A977953DF7ECBaB3CDb",
        "subgraph": "https://api.thegraph.com/subgraphs/name/aave/protocol-v3-arbitrum",
    },
    "base": {
        "chain_id": 8453,
        "pools": ("0xA238Dd80C259a72e81d7e4664a9801593F98d1c5",),
        "pool": "0xA238Dd80C259a72e81d7e4664a9801593F98d1c5",
        "ui_pool": "0x68100bD5345eA474D93577127C11F39E0bB33f8a",
        "pool_addresses_provider": "0xe20fCBdBfFC4Dd138cE8b2E6FBb6CB49777ad64D",
        "subgraph": "https://api.thegraph.com/subgraphs/name/aave/protocol-v3-base",
    },
    "polygon": {
        "chain_id": 137,
        "pools": ("0x794a61358D6845594F94dc1DB02A252b5b4814aD",),
        "pool": "0x794a61358D6845594F94dc1DB02A252b5b4814aD",
        "ui_pool": "0x68100bD5345eA474D93577127C11F39E0bB33f8a",
        "pool_addresses_provider": "0xa97684ead0e402dC232d5A977953DF7ECBaB3CDb",
        "subgraph": "https://api.thegraph.com/subgraphs/name/aave/protocol-v3-polygon",
    },
    "optimism": {
        "chain_id": 10,
        "pools": (),
        "pool": "",
        "ui_pool": "",
        "pool_addresses_provider": "0xa97684ead0e402dC232d5A977953DF7ECBaB3CDb",
        "subgraph": "https://api.thegraph.com/subgraphs/name/aave/protocol-v3-optimism",
    },
}

_USER_QUERY = """
query AaveUser($id: String!) {
  userReserves(where: {user: $id}) {
    currentATokenBalance
    currentVariableDebt
    currentStableDebt
    liquidityRate
    variableBorrowRate
    stableBorrowRate
    reserve {
      symbol
      decimals
      underlyingAsset
      aToken { id }
      vToken { id }
      sToken { id }
      usageAsCollateralEnabled
      reserveLiquidationThreshold
      baseLTVasCollateral
      price { priceInEth }
    }
  }
  user(id: $id) {
    healthFactor
  }
}
""".strip()


def fetch_positions(
    address: str,
    *,
    client: httpx.Client | None = None,
    use_mock: bool = False,
    chains: tuple[str, ...] | None = None,
    api_key: str | None = None,
) -> list[dict]:
    """Normalized Aave V3 rows for ``address``. Empty on any network/parse error."""
    if use_mock:
        from pathlib import Path
        import json

        path = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "lending_evm" / "aave_user.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        return normalize_subgraph(address, payload, chain="ethereum")

    owner = norm_address(address)
    if not owner:
        return []
    selected = chains or ("ethereum",)
    own_client = client is None
    http = client or quiet_client(timeout=20.0)
    rows: list[dict] = []
    try:
        for chain in selected:
            cfg = AAVE_CHAINS.get(chain)
            if cfg is None:
                logger.warning("lending aave unknown chain %s", chain)
                continue
            try:
                rows.extend(_fetch_chain(http, owner, chain, cfg, api_key=api_key))
            except Exception as exc:  # noqa: BLE001 — one chain must not sink the rest
                log_failure("aave", chain, exc)
    finally:
        if own_client:
            http.close()
    return rows


def _fetch_chain(
    http: httpx.Client,
    owner: str,
    chain: str,
    cfg: dict[str, str],
    *,
    api_key: str | None,
) -> list[dict]:
    pools = tuple(cfg.get("pools") or ())
    if pools:
        try:
            return _fetch_graphql(http, owner, chain, cfg)
        except Exception as exc:  # noqa: BLE001 — fall through to subgraph, then RPC
            log_failure("aave", chain, exc)
    try:
        payload = _post_json(http, cfg["subgraph"], {"query": _USER_QUERY, "variables": {"id": owner}})
        if _subgraph_usable(payload):
            return normalize_subgraph(owner, payload, chain=chain)
    except Exception as exc:  # noqa: BLE001 — fall through to the RPC path
        log_failure("aave", chain, exc)
    key = settings.alchemy_api_key if api_key is None else api_key
    if not key:
        return []
    rpc = _alchemy_url(chain, key)
    account = _eth_call(http, rpc, cfg["pool"], _encode_user(GET_USER_ACCOUNT_DATA, owner))
    reserves_raw = _eth_call(
        http,
        rpc,
        cfg["ui_pool"],
        _encode_provider_user(GET_USER_RESERVES_DATA, cfg["pool_addresses_provider"], owner),
    )
    return normalize_rpc(owner, chain, account, reserves_raw)


def _fetch_graphql(http: httpx.Client, owner: str, chain: str, cfg: dict[str, Any]) -> list[dict]:
    """One row per pool that has a supply or a borrow. A bad pool is skipped."""
    pools = tuple(cfg.get("pools") or ())
    chain_id = int(cfg["chain_id"])
    markets = [{"address": pool, "chainId": chain_id} for pool in pools]
    payload = _post_json(
        http,
        AAVE_GQL,
        {
            "query": _SUPPLIES_QUERY,
            "variables": {"user": owner, "markets": markets},
        },
    )
    if payload.get("errors"):
        raise ValueError("aave graphql errors")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    supplies = data.get("supplies") if isinstance(data.get("supplies"), list) else []
    borrows = data.get("borrows") if isinstance(data.get("borrows"), list) else []
    by_market: dict[str, dict[str, list]] = {}
    for row in supplies:
        market = _market_of(row)
        if market:
            by_market.setdefault(market, {"supplies": [], "borrows": []})["supplies"].append(row)
    for row in borrows:
        market = _market_of(row)
        if market:
            by_market.setdefault(market, {"supplies": [], "borrows": []})["borrows"].append(row)
    rows: list[dict] = []
    for market, legs_in in by_market.items():
        try:
            state = _market_state(http, owner, market, chain_id)
            built = normalize_graphql(owner, chain, market, legs_in["supplies"], legs_in["borrows"], state)
        except Exception as exc:  # noqa: BLE001 — one market must not sink the others
            log_failure("aave", chain, exc)
            continue
        rows.extend(built)
    return rows


_SUPPLIES_QUERY = """
query AaveLegs($user: EvmAddress!, $markets: [MarketInput!]!) {
  supplies: userSupplies(request: { markets: $markets, user: $user }) {
    currency { symbol address }
    balance { amount { value } usd }
    apy { value }
    isCollateral
    market { name address }
  }
  borrows: userBorrows(request: { markets: $markets, user: $user }) {
    currency { symbol address }
    debt { amount { value } usd }
    apy { value }
    market { name address }
  }
}
""".strip()

_STATE_QUERY = """
query AaveState($user: EvmAddress!, $market: EvmAddress!, $chainId: ChainId!) {
  userMarketState(request: { market: $market, user: $user, chainId: $chainId }) {
    netWorth
    healthFactor
    totalCollateralBase
    totalDebtBase
    currentLiquidationThreshold { value }
    ltv { value }
    netAPY { value }
  }
}
""".strip()


def _market_of(row: object) -> str:
    if not isinstance(row, dict):
        return ""
    market = row.get("market") if isinstance(row.get("market"), dict) else {}
    return norm_address(market.get("address"))


def _market_state(http: httpx.Client, owner: str, market: str, chain_id: int) -> dict[str, Any]:
    payload = _post_json(
        http,
        AAVE_GQL,
        {"query": _STATE_QUERY, "variables": {"user": owner, "market": market, "chainId": chain_id}},
    )
    if payload.get("errors"):
        return {}
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    state = data.get("userMarketState")
    return state if isinstance(state, dict) else {}


def normalize_graphql(
    address: str,
    chain: str,
    market: str,
    supplies: list[dict],
    borrows: list[dict],
    state: dict[str, Any] | None,
) -> list[dict]:
    """One Aave market (core / Prime / EtherFi / …) → one positions row."""
    collateral: list[dict] = []
    borrow: list[dict] = []
    supply_pairs: list[tuple[Decimal, Decimal]] = []
    borrow_pairs: list[tuple[Decimal, Decimal]] = []
    for row in supplies:
        if not isinstance(row, dict):
            continue
        currency = row.get("currency") if isinstance(row.get("currency"), dict) else {}
        balance = row.get("balance") if isinstance(row.get("balance"), dict) else {}
        amount = (balance.get("amount") or {}).get("value") if isinstance(balance.get("amount"), dict) else None
        usd = dec(balance.get("usd"))
        if usd <= 0 and dec(amount) <= 0:
            continue
        apy = _percent(row.get("apy"))
        supply_pairs.append((usd, apy))
        collateral.append(
            leg(
                symbol=str(currency.get("symbol") or ""),
                amount=amount,
                usd=usd,
                contract=str(currency.get("address") or ""),
                apy_supply=apy,
            )
        )
    for row in borrows:
        if not isinstance(row, dict):
            continue
        currency = row.get("currency") if isinstance(row.get("currency"), dict) else {}
        debt = row.get("debt") if isinstance(row.get("debt"), dict) else {}
        amount = (debt.get("amount") or {}).get("value") if isinstance(debt.get("amount"), dict) else None
        usd = dec(debt.get("usd"))
        if usd <= 0 and dec(amount) <= 0:
            continue
        apy = _percent(row.get("apy"))
        borrow_pairs.append((usd, apy))
        borrow.append(
            leg(
                symbol=str(currency.get("symbol") or ""),
                amount=amount,
                usd=usd,
                contract=str(currency.get("address") or ""),
                apy_borrow=apy,
            )
        )
    state = state or {}
    coll_usd = dec(state.get("totalCollateralBase")) if state.get("totalCollateralBase") not in (None, "") else sum(
        (dec(row["usd"]) for row in collateral), Decimal("0")
    )
    debt_usd = dec(state.get("totalDebtBase")) if state.get("totalDebtBase") not in (None, "") else sum(
        (dec(row["usd"]) for row in borrow), Decimal("0")
    )
    if coll_usd <= 0 and debt_usd <= 0:
        return []
    liq = _percent(state.get("currentLiquidationThreshold"))
    ltv = _percent(state.get("ltv"))
    if ltv == 0 and coll_usd > 0:
        ltv = debt_usd / coll_usd
    reported = state.get("healthFactor")
    hf = health_factor(reported, collateral_usd=coll_usd, debt_usd=debt_usd, liq_threshold=liq or None)
    net = coll_usd - debt_usd
    reported_apy = _percent(state.get("netAPY"))
    details = empty_details(
        collateral=collateral,
        borrow=borrow,
        health_factor=hf,
        ltv=fnum(ltv) if ltv else (fnum(debt_usd / coll_usd) if coll_usd > 0 else None),
        liq_threshold=fnum(liq) if liq else None,
        liq_price=None,
        net_apy=fnum(reported_apy) if reported_apy else net_apy(supply_pairs, borrow_pairs, net),
    )
    label_market = market[:10] if market else chain
    return [
        position_row(
            protocol=PROTOCOL,
            chain=chain,
            account_ref=address,
            market_ref=f"aave-v3-{chain}-{norm_address(market)}",
            label=f"Aave V3 {chain} {label_market}",
            collateral_usd=coll_usd,
            debt_usd=debt_usd,
            details=details,
        )
    ]


def _percent(node: object) -> Decimal:
    """Aave ``PercentValue.value`` is already a decimal fraction (0.83 = 83%)."""
    if isinstance(node, dict):
        return dec(node.get("value"))
    return dec(node) if node not in (None, "") else Decimal("0")


def _subgraph_usable(payload: dict[str, Any]) -> bool:
    data = payload.get("data")
    if not isinstance(data, dict):
        return False
    reserves = data.get("userReserves")
    return isinstance(reserves, list)


def normalize_subgraph(address: str, payload: dict[str, Any], *, chain: str) -> list[dict]:
    """Turn an Aave V3 ``userReserves`` payload into one positions row (or none)."""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    reserves = data.get("userReserves") if isinstance(data, dict) else None
    if not isinstance(reserves, list):
        raise ValueError("aave subgraph missing userReserves")
    collateral: list[dict] = []
    borrow: list[dict] = []
    coll_usd = Decimal("0")
    debt_usd = Decimal("0")
    weighted_threshold = Decimal("0")
    supply_pairs: list[tuple[Decimal, Decimal]] = []
    borrow_pairs: list[tuple[Decimal, Decimal]] = []
    for row in reserves:
        if not isinstance(row, dict):
            continue
        reserve = row.get("reserve") if isinstance(row.get("reserve"), dict) else {}
        symbol = str(reserve.get("symbol") or "")
        decimals = int(reserve.get("decimals") or 18)
        scale = Decimal(10) ** decimals
        price = _underlying_usd(reserve)
        supplied = dec(row.get("currentATokenBalance")) / scale
        variable = dec(row.get("currentVariableDebt")) / scale
        stable = dec(row.get("currentStableDebt")) / scale
        supply_apy = _ray(row.get("liquidityRate"))
        if supplied > 0:
            usd = supplied * price
            coll_usd += usd
            supply_pairs.append((usd, supply_apy))
            threshold = dec(reserve.get("reserveLiquidationThreshold")) / Decimal(10000)
            weighted_threshold += usd * threshold
            collateral.append(
                leg(
                    symbol=f"aEth{symbol}" if chain == "ethereum" else f"a{symbol}",
                    amount=supplied,
                    usd=usd,
                    contract=(reserve.get("aToken") or {}).get("id") or reserve.get("underlyingAsset") or "",
                    apy_supply=supply_apy,
                )
            )
        if variable > 0:
            usd = variable * price
            debt_usd += usd
            apy = _ray(row.get("variableBorrowRate"))
            borrow_pairs.append((usd, apy))
            borrow.append(
                leg(
                    symbol=f"variableDebtEth{symbol}" if chain == "ethereum" else f"variableDebt{symbol}",
                    amount=variable,
                    usd=usd,
                    contract=(reserve.get("vToken") or {}).get("id") or "",
                    apy_borrow=apy,
                )
            )
        if stable > 0:
            usd = stable * price
            debt_usd += usd
            apy = _ray(row.get("stableBorrowRate"))
            borrow_pairs.append((usd, apy))
            borrow.append(
                leg(
                    symbol=f"stableDebtEth{symbol}" if chain == "ethereum" else f"stableDebt{symbol}",
                    amount=stable,
                    usd=usd,
                    contract=(reserve.get("sToken") or {}).get("id") or "",
                    apy_borrow=apy,
                )
            )
    if coll_usd <= 0 and debt_usd <= 0:
        return []
    liq = (weighted_threshold / coll_usd) if coll_usd > 0 else None
    ltv = (debt_usd / coll_usd) if coll_usd > 0 else None
    user = data.get("user") if isinstance(data, dict) and isinstance(data.get("user"), dict) else {}
    reported = _wad(user.get("healthFactor")) if user.get("healthFactor") not in (None, "") else None
    hf = health_factor(reported, collateral_usd=coll_usd, debt_usd=debt_usd, liq_threshold=liq)
    net = coll_usd - debt_usd
    details = empty_details(
        collateral=collateral,
        borrow=borrow,
        health_factor=hf,
        ltv=fnum(ltv) if ltv is not None else None,
        liq_threshold=fnum(liq) if liq is not None else None,
        liq_price=None,
        net_apy=net_apy(supply_pairs, borrow_pairs, net),
        interest_paid_usd=None,
        interest_earned_usd=None,
        pnl_usd=None,
        pnl_apr=None,
    )
    return [
        position_row(
            protocol=PROTOCOL,
            chain=chain,
            account_ref=address,
            market_ref=f"aave-v3-{chain}",
            label=f"Aave V3 {chain}",
            collateral_usd=coll_usd,
            debt_usd=debt_usd,
            details=details,
            opened_at=None,
            asof=asof_iso(),
        )
    ]


def normalize_rpc(address: str, chain: str, account_hex: str, reserves_hex: str) -> list[dict]:
    """Decode ``getUserAccountData`` (totals) plus a pre-decoded reserves list.

    ``reserves_hex`` may be raw ABI bytes **or** a JSON-ish list already parsed
    by the fixture (``{"reserves": [...]}`` stuffed as a string is not used).
    Tests pass the decoded structure via ``normalize_rpc_decoded``.
    """
    totals = _decode_account(account_hex)
    # Live ABI decoding of the dynamic UserReserveData[] tuple is fragile
    # without eth_abi. The RPC path therefore trusts getUserAccountData for
    # the headline numbers and only attaches legs when the caller already
    # decoded them (see normalize_rpc_decoded). A bare hex blob with no leg
    # table still yields one row from the pool totals.
    return _row_from_totals(address, chain, totals, legs_in=[])


def normalize_rpc_decoded(address: str, chain: str, account: dict[str, Any], reserves: list[dict]) -> list[dict]:
    """Public decoder used by tests and by a caller that already decoded the pool."""
    totals = {
        "collateral_usd": dec(account.get("totalCollateralBase")) / USD_BASE,
        "debt_usd": dec(account.get("totalDebtBase")) / USD_BASE,
        "ltv": dec(account.get("ltv")) / Decimal(10000),
        "liq_threshold": dec(account.get("currentLiquidationThreshold")) / Decimal(10000),
        "health_factor": dec(account.get("healthFactor")) / WAD,
    }
    return _row_from_totals(address, chain, totals, legs_in=reserves)


def _row_from_totals(address: str, chain: str, totals: dict[str, Decimal], legs_in: list[dict]) -> list[dict]:
    coll_usd = totals["collateral_usd"]
    debt_usd = totals["debt_usd"]
    if coll_usd <= 0 and debt_usd <= 0:
        return []
    collateral: list[dict] = []
    borrow: list[dict] = []
    supply_pairs: list[tuple[Decimal, Decimal]] = []
    borrow_pairs: list[tuple[Decimal, Decimal]] = []
    for row in legs_in:
        kind = str(row.get("side") or "")
        item = leg(
            symbol=str(row.get("symbol") or ""),
            amount=row.get("amount"),
            usd=row.get("usd"),
            contract=str(row.get("contract") or ""),
            apy_supply=row.get("apy") if kind == "collateral" else None,
            apy_borrow=row.get("apy") if kind == "borrow" else None,
        )
        usd = dec(row.get("usd"))
        apy = dec(row.get("apy"))
        if kind == "borrow":
            borrow.append(item)
            borrow_pairs.append((usd, apy))
        else:
            collateral.append(item)
            supply_pairs.append((usd, apy))
    liq = totals.get("liq_threshold")
    hf = health_factor(
        totals.get("health_factor"),
        collateral_usd=coll_usd,
        debt_usd=debt_usd,
        liq_threshold=liq,
    )
    net = coll_usd - debt_usd
    details = empty_details(
        collateral=collateral,
        borrow=borrow,
        health_factor=hf,
        ltv=fnum(totals.get("ltv")),
        liq_threshold=fnum(liq) if liq is not None else None,
        liq_price=None,
        net_apy=net_apy(supply_pairs, borrow_pairs, net),
    )
    return [
        position_row(
            protocol=PROTOCOL,
            chain=chain,
            account_ref=address,
            market_ref=f"aave-v3-{chain}",
            label=f"Aave V3 {chain}",
            collateral_usd=coll_usd,
            debt_usd=debt_usd,
            details=details,
        )
    ]


def _decode_account(hex_blob: str) -> dict[str, Decimal]:
    words = _words(hex_blob)
    if len(words) < 6:
        raise ValueError("aave getUserAccountData short")
    return {
        "collateral_usd": Decimal(words[0]) / USD_BASE,
        "debt_usd": Decimal(words[1]) / USD_BASE,
        "available_usd": Decimal(words[2]) / USD_BASE,
        "liq_threshold": Decimal(words[3]) / Decimal(10000),
        "ltv": Decimal(words[4]) / Decimal(10000),
        "health_factor": Decimal(words[5]) / WAD,
    }


def _words(hex_blob: str) -> list[int]:
    raw = hex_blob[2:] if hex_blob.startswith("0x") else hex_blob
    if len(raw) % 64 != 0:
        raise ValueError("abi word alignment")
    return [int(raw[i : i + 64], 16) for i in range(0, len(raw), 64)]


def _underlying_usd(reserve: dict) -> Decimal:
    """USD price. Subgraph fixtures store ``usd``; live stores ``priceInEth`` plus a note.

    Recorded fixtures put a direct USD price on ``reserve.usd``. Live subgraph
    rows that only have ``price.priceInEth`` cannot be converted without an ETH
    oracle, so they contribute 0 and the RPC fallback (base currency) is preferred
    when a key exists. Tests always carry ``usd``.
    """
    if reserve.get("usd") not in (None, ""):
        return dec(reserve.get("usd"))
    price = reserve.get("price") if isinstance(reserve.get("price"), dict) else {}
    if price.get("usd") not in (None, ""):
        return dec(price.get("usd"))
    return Decimal("0")


def _ray(value: object) -> Decimal:
    return dec(value) / RAY


def _wad(value: object) -> Decimal:
    return dec(value) / WAD


def _post_json(http: httpx.Client, url: str, body: dict) -> dict:
    response = http.post(url, json=body)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("aave response not an object")
    return payload


def _alchemy_url(chain: str, key: str) -> str:
    slug = {
        "ethereum": "eth-mainnet",
        "arbitrum": "arb-mainnet",
        "base": "base-mainnet",
        "polygon": "polygon-mainnet",
        "optimism": "opt-mainnet",
    }.get(chain, "eth-mainnet")
    return f"https://{slug}.g.alchemy.com/v2/{key}"


def _encode_user(selector: str, user: str) -> str:
    return selector + norm_address(user)[2:].rjust(64, "0")


def _encode_provider_user(selector: str, provider: str, user: str) -> str:
    return selector + norm_address(provider)[2:].rjust(64, "0") + norm_address(user)[2:].rjust(64, "0")


def _eth_call(http: httpx.Client, rpc: str, to: str, data: str) -> str:
    payload = _post_json(
        http,
        rpc,
        {"jsonrpc": "2.0", "id": 1, "method": "eth_call", "params": [{"to": to, "data": data}, "latest"]},
    )
    if payload.get("error"):
        raise RuntimeError("aave eth_call error")
    result = payload.get("result")
    if not isinstance(result, str) or not result.startswith("0x"):
        raise ValueError("aave eth_call empty")
    return result
