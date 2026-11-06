"""Build a real NAV snapshot from exchange + chain balances priced in USDT.

A snapshot is the sum, at one timestamp, of every source the sync pipeline
already knows about:

* exchange spot balances (``fetch_balances``)
* EVM wallet balances (Zerion positions; Alchemy when Zerion fails)
* Solana wallet balances (Helius)

Fixtures are only for a user with no live wallet at all (demo) or an explicit
``--mode mock``. A user who owns a wallet never mixes mock exchange or Solana
balances into the NAV: a source with no key or address is recorded as skipped.
If the EVM wallet cannot be read from Zerion or Alchemy, the snapshot is not
written.

Each quantity is multiplied by the USDT quote from ``PriceService``. Assets
with no quote are kept in the breakdown with ``priced: false`` and contribute
0 to the NAV — they are not silently dropped, and they are not guessed. Every
holding carries ``live`` so a reader can tell a measured balance from a fixture.

The ``(user_id, ts)`` unique constraint makes the write an upsert: calling
this twice for the same timestamp updates the existing row instead of inserting
a second one.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import has_live_credentials, resolve_sync_mode, settings
from .integrations.chain.helius import SolanaHeliusAdapter
from .integrations.chain.zerion import ZerionAdapter
from .integrations.exchanges.ccxt_adapter import CCXTAdapter
from .integrations.exchanges.errors import describe_exchange_error
from .integrations.exchanges.factory import build_exchange_adapter
from .integrations.exchanges.mock import MockExchangeAdapter
from .integrations.prices.kamino_liquidity import KAMINO_PRICE_SOURCE
from .integrations.prices.service import PriceService, expired_yt_price
from .integrations.types import ChainBalance, NormalizedBalance
from .models import SNAPSHOT_SOURCES, Snapshot, User
from .provenance import classify_provenance, snapshot_provenance
from .wallets import user_addresses, user_exchange_credentials

logger = logging.getLogger(__name__)

EXCHANGES = ("binance", "bitget", "bybit", "kraken", "gate")
# P13 provenance values first; legacy source values are only returned for rows
# written before the provenance column existed.
NavSource = Literal["demo", "fixture", "live", "sync", "backfill", "seed"]

DEMO_ADDRESSES = frozenset({"0xdemo", "So1DemoAddress"})


def _is_fixture_balance(balance: NormalizedBalance | ChainBalance) -> bool:
    """Recorded rows never become live just because the process has keys.

    Demo stand-in addresses carry no wallet of their own, so they are fixtures
    even when an adapter forgot to set the marker.
    """
    return (
        bool(getattr(balance, "fixture", False))
        or getattr(balance, "address", None) in DEMO_ADDRESSES
    )

# Prefer a fresh sync over a reconstructed backfill over the phase-1 demo curve.
_SOURCE_RANK = {"sync": 0, "backfill": 1, "seed": 2}


@dataclass
class Holding:
    source: str
    asset: str
    amount: Decimal
    price_usdt: Decimal | None
    value_usdt: Decimal
    # False only for fixture rows. A live fetch is true even when the venue is an exchange.
    live: bool = True
    # defillama_contract | defillama_symbol | exchange_ticker | zerion | expired_yt | none
    price_source: str = "none"
    # True when this row was copied from the previous snapshot because the
    # venue failed (for example Binance HTTP 451). It is not a fresh read.
    stale: bool = False
    # DefiLlama chain slug and token address. Empty/None are omitted from JSON.
    chain: str | None = None
    contract: str | None = None
    # Provider logo (Zerion). Omitted when the venue did not supply one.
    icon_url: str | None = None
    # True when an open lending position already prices this row. The row stays
    # in the breakdown so the holdings list can label it; the NAV sum skips it.
    covered_by_position: bool = False
    # Internal marker: this quantity came from a recorded fixture. Kept out of
    # JSON; the public flag is ``live``.
    fixture: bool = False

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "source": self.source,
            "asset": self.asset,
            "amount": format(self.amount, "f"),
            "price_usdt": format(self.price_usdt, "f") if self.price_usdt is not None else None,
            "value_usdt": format(self.value_usdt, "f"),
            "priced": self.price_usdt is not None,
            "live": self.live,
            "price_source": self.price_source,
        }
        if self.stale:
            payload["stale"] = True
        if self.chain:
            payload["chain"] = self.chain
        if self.contract:
            payload["contract"] = self.contract
        if self.icon_url:
            payload["icon_url"] = self.icon_url
        if self.covered_by_position:
            payload["covered_by_position"] = True
        return payload


@dataclass
class SkippedSource:
    source: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {"source": self.source, "reason": self.reason}


class SnapshotUnavailable(RuntimeError):
    """Zerion and Alchemy both failed for a wallet that must not use fixtures."""


@dataclass
class NavCapture:
    nav_usdt: Decimal
    holdings: list[Holding] = field(default_factory=list)
    unpriced: list[str] = field(default_factory=list)
    skipped: list[SkippedSource] = field(default_factory=list)
    # Set when the EVM wallet could not be read. The caller must not persist.
    error: str | None = None
    # True once open-position net values were folded into nav_usdt. Overview
    # must not add them a second time. Absent on older snapshots.
    positions_included: bool = False

    def breakdown_json(self) -> str:
        payload = {
            "holdings": [item.as_dict() for item in self.holdings],
            "unpriced": self.unpriced,
            "skipped": [item.as_dict() for item in self.skipped],
            "nav_usdt": format(self.nav_usdt, "f"),
        }
        if self.positions_included:
            payload["positions_included"] = True
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def price_holdings(
    balances: list[NormalizedBalance | ChainBalance],
    prices: PriceService,
    *,
    live: bool = True,
) -> NavCapture:
    """Sum ``amount × USDT price``. Unpriced assets contribute 0 and are listed.

    Chain rows are priced by contract first (one DefiLlama batch), then by the
    existing symbol lookup, then by the Zerion price carried on the balance.
    Exchange rows (binance, gate, …) try that venue's public USDT ticker
    before the symbol lookup, so a listed coin DefiLlama cannot name still
    enters the NAV. A ticker miss falls through; it never drops the row.
    """
    # A fixture row must not inherit a live price call from the process keys.
    # Demo/recorded balances keep their recorded quotes; only real rows may ask
    # DefiLlama or an exchange ticker. This also keeps demo sync offline.
    fixture_seen = any(_is_fixture_balance(balance) for balance in balances)
    fixture_prices = prices
    if fixture_seen and not getattr(prices, "_use_mock", False):
        fixture_prices = PriceService(use_mock=True)
    live_balances = [balance for balance in balances if not _is_fixture_balance(balance)]
    contract_prices = _prefetch_contract_prices(live_balances, prices)
    holdings: list[Holding] = []
    unpriced: list[str] = []
    total = Decimal("0")
    for balance in balances:
        amount = Decimal(balance.amount)
        if amount == 0:
            continue
        fixture_row = _is_fixture_balance(balance)
        price, source = _price_balance(
            balance,
            fixture_prices if fixture_row else prices,
            {} if fixture_row else contract_prices,
        )
        if price is None:
            value = Decimal("0.00")
            unpriced.append(f"{balance.source}:{balance.asset}")
        else:
            value = (amount * price).quantize(Decimal("0.01"))
        chain = getattr(balance, "chain", None) or None
        contract = getattr(balance, "contract", None) or None
        icon_url = getattr(balance, "icon_url", None) or None
        if isinstance(chain, str) and not chain.strip():
            chain = None
        if isinstance(contract, str) and not contract.strip():
            contract = None
        if isinstance(icon_url, str):
            icon_url = icon_url.strip() or None
            if icon_url and not icon_url.startswith(("http://", "https://")):
                icon_url = None
        else:
            icon_url = None
        fixture = _is_fixture_balance(balance)
        holdings.append(
            Holding(
                source=balance.source,
                asset=balance.asset,
                amount=amount,
                price_usdt=price,
                value_usdt=value,
                live=live and not fixture,
                price_source=source,
                chain=chain,
                contract=contract,
                icon_url=icon_url,
                fixture=fixture,
            )
        )
        total += value
    return NavCapture(nav_usdt=total.quantize(Decimal("0.01")), holdings=holdings, unpriced=unpriced)


def _prefetch_contract_prices(
    balances: list[NormalizedBalance | ChainBalance],
    prices: PriceService,
) -> dict[str, Decimal]:
    """One DefiLlama batch for every chain row that has a contract."""
    keys: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for balance in balances:
        chain = getattr(balance, "chain", None)
        contract = getattr(balance, "contract", None)
        if not chain or not contract:
            continue
        pair = (str(chain), str(contract))
        if pair in seen:
            continue
        seen.add(pair)
        keys.append(pair)
    if not keys:
        return {}
    return prices.quote_contracts(keys)


def _price_balance(
    balance: NormalizedBalance | ChainBalance,
    prices: PriceService,
    contract_prices: dict[str, Decimal],
    *,
    on: date | None = None,
) -> tuple[Decimal | None, str]:
    """Contract, then a Kamino share NAV, then the venue ticker, then symbol.

    A matured Pendle YT is worth 0 from its expiry date onward. DefiLlama
    keeps quoting the contract near 1, so that rule wins over every source.
    ``on`` defaults to today (UTC), which is the live snapshot's valuation date.

    A ``KJLP-*`` share mint has no DefiLlama contract quote: the strategy's
    per-share NAV fills the chain-contract path. The share is held in the
    wallet and the underlying sits in the Kamino vault, so the row stays
    uncovered unless a receipt/debt/share leg says otherwise.

    Exchange spot (no chain contract) asks the venue's public ticker first.
    Solana mints are already in ``contract_prices`` under ``solana:<mint>``
    (lower-cased) via ``quote_contracts``.
    """
    expired = expired_yt_price(balance.asset, on or datetime.now(timezone.utc).date())
    if expired is not None:
        return expired, "expired_yt"
    chain = getattr(balance, "chain", None)
    contract = getattr(balance, "contract", None)
    if chain and contract:
        key = f"{str(chain).strip().lower()}:{str(contract).strip().lower()}"
        price = contract_prices.get(key)
        if price is not None:
            return price, "defillama_contract"
        # KJLP-* is a Kamino Liquidity vault share. DefiLlama has no quote for
        # the share mint; the strategy metrics feed reports its per-share NAV.
        share = prices.quote_kamino_share(str(contract))
        if share is not None:
            return share, KAMINO_PRICE_SOURCE
    if not contract and _is_exchange_source(balance.source) and not _is_dust_amount(balance.amount):
        ticker = prices.quote_exchange_ticker(balance.source, balance.asset)
        if ticker is not None:
            return ticker, "exchange_ticker"
    price = prices.quote_usdt(balance.asset)
    if price is not None:
        return price, "defillama_symbol"
    quote = getattr(balance, "quote_usdt", None)
    if quote is not None and quote > 0:
        return quote, "zerion"
    return None, "none"


# Sources whose spot book is an exchange. Alchemy / helius / zerion are not.
_EXCHANGE_SOURCES = frozenset({"binance", "bitget", "bybit", "kraken", "gate", "gateio"})

# A public ticker exists for a lot of dust. An amount below this never enters
# the NAV from the ticker path — it stays unpriced (class B). The cut is on
# the quantity, not the USDT value: a cheap coin with a real balance (FF)
# must still be priced, and a near-zero balance must not.
_DUST_AMOUNT = Decimal("0.000001")


def _is_dust_amount(amount: Decimal) -> bool:
    try:
        return abs(Decimal(amount)) < _DUST_AMOUNT
    except Exception:  # noqa: BLE001
        return False


def _is_exchange_source(source: str | None) -> bool:
    return (source or "").strip().lower() in _EXCHANGE_SOURCES


def _safe_error(exc: BaseException) -> str:
    """Exception type only. The message can carry a URL, and Alchemy puts the key in it."""
    return type(exc).__name__


def collect_balances(
    *,
    use_live: bool,
    evm_addresses: tuple[str, ...] | None = None,
    solana_addresses: tuple[str, ...] | None = None,
    allow_mock: bool = True,
    creds: dict[str, dict[str, str]] | None = None,
) -> tuple[list[NormalizedBalance | ChainBalance], list[SkippedSource], str | None]:
    """Every source the sync pipeline uses, as current balances.

    ``allow_mock`` is true only when the user has no live wallet (demo) or the
    caller forced ``--mode mock``. A live wallet skips an exchange with no key
    and a chain with no address or key; those show up as skipped reasons.
    Zerion and Alchemy both failing returns an error string and no EVM rows —
    the caller must not write a snapshot from fixtures.
    """
    balances: list[NormalizedBalance | ChainBalance] = []
    skipped: list[SkippedSource] = []
    error: str | None = None
    # ``creds is None`` keeps the old "look them up" behaviour out: callers pass
    # this user's keys explicitly. Demo passes ``{}`` so a process-wide key
    # never builds a live adapter here.
    creds = creds or {}
    secrets = [value for cred in creds.values() for value in cred.values()]
    for name in EXCHANGES:
        if name in creds:
            adapter = build_exchange_adapter(name, creds, ccxt_cls=CCXTAdapter)
        elif allow_mock:
            adapter = MockExchangeAdapter(name)
        else:
            skipped.append(SkippedSource(name, "no api key"))
            continue
        try:
            balances.extend(adapter.fetch_balances())
        except Exception as exc:  # noqa: BLE001 - one venue must not abort the snapshot
            detail = describe_exchange_error(exc, secrets, venue=name)
            logger.warning("balance fetch failed: %s", detail)
            skipped.append(SkippedSource(name, detail))

    evm_owned = settings.evm_addresses if evm_addresses is None else evm_addresses
    evm_keys = bool(settings.zerion_api_key or settings.alchemy_api_key)
    if evm_owned and use_live and evm_keys:
        evm: ZerionAdapter | None = ZerionAdapter(
            evm_owned,
            zerion_api_key=settings.zerion_api_key,
            alchemy_api_key=settings.alchemy_api_key,
            # A live wallet never reads the fixture, even if the caller also
            # allows mock for the venues that have no key. ``evm_addresses``
            # being None only means "look the wallet up"; the lookup already
            # happened, so it must not re-enable the fixture.
            allow_fixture=False,
        )
    elif allow_mock:
        evm = ZerionAdapter(("0xdemo",), use_mock=True)
    elif not evm_owned:
        evm = None
        skipped.append(SkippedSource("evm", "no wallet address"))
    else:
        evm = None
        skipped.append(SkippedSource("evm", "no zerion or alchemy key"))
    if evm is not None:
        try:
            balances.extend(evm.fetch_balances())
        except Exception as exc:  # noqa: BLE001
            logger.warning("evm balance fetch failed (%s)", type(exc).__name__)
            if evm._allow_fixture:
                skipped.append(SkippedSource("evm", _safe_error(exc)))
            else:
                # Both providers failed. Do not substitute the fixture.
                error = f"evm balances unavailable ({_safe_error(exc)})"

    sol_owned = settings.solana_addresses if solana_addresses is None else solana_addresses
    if not sol_owned:
        if allow_mock:
            sol_owned = ("So1DemoAddress",)
        else:
            skipped.append(SkippedSource("solana", "no wallet address"))
            sol_owned = ()
    if sol_owned:
        if use_live and settings.helius_api_key and sol_owned != ("So1DemoAddress",):
            sol = SolanaHeliusAdapter(sol_owned, api_key=settings.helius_api_key)
        elif allow_mock:
            sol = SolanaHeliusAdapter(("So1DemoAddress",), use_mock=True)
        else:
            sol = None
            skipped.append(SkippedSource("solana", "no helius key"))
        if sol is not None:
            try:
                balances.extend(sol.fetch_balances())
            except Exception as exc:  # noqa: BLE001
                logger.warning("solana balance fetch failed (%s)", type(exc).__name__)
                skipped.append(SkippedSource("solana", _safe_error(exc)))
    return balances, skipped, error


def capture_nav(
    *,
    use_live: bool,
    prices: PriceService | None = None,
    evm_addresses: tuple[str, ...] | None = None,
    solana_addresses: tuple[str, ...] | None = None,
    allow_mock: bool = True,
    creds: dict[str, dict[str, str]] | None = None,
    positions: list[dict] | None = None,
) -> NavCapture:
    service = prices if prices is not None else _price_service(use_live)
    balances, skipped, error = collect_balances(
        use_live=use_live,
        evm_addresses=evm_addresses,
        solana_addresses=solana_addresses,
        allow_mock=allow_mock,
        creds=creds,
    )
    capture = _price_tagged(balances, service, live=use_live)
    capture.skipped = skipped
    capture.error = error
    return capture


def _price_tagged(
    balances: list[NormalizedBalance | ChainBalance],
    prices: PriceService,
    *,
    live: bool,
) -> NavCapture:
    """Price each balance and mark fixture rows as not live.

    In mock mode every row is a fixture. In live mode a recorded fixture (from
    ``MockExchangeAdapter`` or a chain adapter's fixture fallback) and the demo
    stand-in addresses (``0xdemo``, ``So1DemoAddress``) are not live; a real
    venue read is live. The per-balance ``fixture`` marker is authoritative, so
    an exchange fixture can no longer slip through as live just because it has
    no wallet address.
    """
    return price_holdings(balances, prices, live=live)


def _fold_positions(capture: NavCapture, positions: list[dict]) -> None:
    """NAV = uncovered holdings + open position net value.

    Receipt / debt / share rows a position already prices are flagged and
    removed from the sum (rules match ``mark_covered_by_position``; PLP- /
    PT- / YT- are never covered). The position's own net replaces them, so
    collateral is not counted twice and the debt is deducted exactly once.
    Runs after the stale-venue carry, which recomputes the raw holding sum.
    """
    from .positions_core import mark_covered_by_position

    if positions:
        raw = [item.as_dict() for item in capture.holdings]
        marked = mark_covered_by_position(raw, positions)
        covered_value = Decimal("0")
        for holding, flag in zip(capture.holdings, marked):
            if flag.get("covered_by_position"):
                holding.covered_by_position = True
                covered_value += holding.value_usdt
        net = Decimal("0")
        for row in positions:
            if str(row.get("status") or "open") != "open":
                continue
            try:
                net += Decimal(str(row.get("net_value_usdt") or "0"))
            except Exception:  # noqa: BLE001 — a bad row must not sink the snapshot
                continue
        capture.nav_usdt = (capture.nav_usdt - covered_value + net).quantize(Decimal("0.01"))
        capture.positions_included = True


def _open_position_dicts(db: Session, user_id: int) -> list[dict]:
    """Open positions for the coverage pass. Missing table → no exclusion."""
    from sqlalchemy import text

    try:
        rows = db.execute(
            text(
                "SELECT protocol, status, net_value_usdt, details FROM positions "
                "WHERE user_id = :user_id AND status = 'open'"
            ),
            {"user_id": user_id},
        ).mappings().all()
    except Exception:
        db.rollback()
        return []
    parsed: list[dict] = []
    for row in rows:
        item = dict(row)
        details = item.get("details")
        if isinstance(details, str):
            try:
                details = json.loads(details)
            except json.JSONDecodeError:
                details = None
            item["details"] = details
        parsed.append(item)
    return parsed


def _price_service(use_live: bool) -> PriceService:
    if not use_live:
        return PriceService(use_mock=True)
    return PriceService(coingecko_api_key=settings.coingecko_demo_api_key, use_mock=False)


def upsert_snapshot(
    db: Session,
    user_id: int,
    ts: datetime,
    capture: NavCapture,
    *,
    source: str,
    provenance: str | None = None,
    live: bool | None = None,
) -> tuple[Snapshot, bool]:
    """Insert or update the snapshot at ``ts``. Returns ``(row, created)``.

    ``uq_snapshots_user_ts`` forbids two rows at the same instant, so a repeat
    call for the same timestamp overwrites nav, source and breakdown. P13
    provenance is written only when supplied: an older caller updating an
    existing row must not erase the stored value with NULL.
    """
    if source not in SNAPSHOT_SOURCES:
        raise ValueError(f"unsupported snapshot source: {source}")
    if provenance is not None and provenance not in ("demo", "fixture", "live"):
        raise ValueError(f"unsupported snapshot provenance: {provenance}")
    moment = _aware(ts)
    existing = db.scalar(
        select(Snapshot).where(Snapshot.user_id == user_id, Snapshot.ts == moment)
    )
    created = existing is None
    row = existing or Snapshot(user_id=user_id, ts=moment, nav_usdt=capture.nav_usdt)
    row.nav_usdt = capture.nav_usdt
    row.source = source
    if provenance is not None:
        row.provenance = provenance
    if live is not None:
        row.live = live
    row.breakdown = capture.breakdown_json()
    if created:
        db.add(row)
    db.flush()
    return row, created


def write_sync_snapshot(
    db: Session,
    user_id: int,
    *,
    mode: str | None = None,
    ts: datetime | None = None,
    prices: PriceService | None = None,
    evm_addresses: tuple[str, ...] | None = None,
    solana_addresses: tuple[str, ...] | None = None,
    creds: dict[str, dict[str, str]] | None = None,
    positions: list[dict] | None = None,
) -> Snapshot | None:
    """Capture current balances and store them as a ``sync`` snapshot.

    Returns ``None`` when the user's own EVM wallet could not be read from
    Zerion or Alchemy. Nothing is written in that case: a fixture NAV must not
    stand in for a failed live read. The caller records the error on the sync run.
    """
    resolved = resolve_sync_mode(mode if mode is not None else settings.sync_mode)
    owned_evm = user_addresses(db, user_id, "evm") if evm_addresses is None else evm_addresses
    owned_sol = user_addresses(db, user_id, "solana") if solana_addresses is None else solana_addresses
    # auto is live when the process has credentials, or when this user owns a
    # wallet and a chain key is configured. The key may be present while the
    # env address list is empty: the address lives on the user, not in env.
    user_chain_keys = bool(owned_evm and (settings.zerion_api_key or settings.alchemy_api_key)) or bool(
        owned_sol and settings.helius_api_key
    )
    use_live = resolved == "live" or (resolved == "auto" and (has_live_credentials() or user_chain_keys))
    # Mock fixtures fill gaps only for a user with no wallet of their own, or
    # when the operator forced mock mode. A live wallet skips empty sources.
    allow_mock = resolved == "mock" or not (owned_evm or owned_sol)
    if creds is None:
        creds = user_exchange_credentials(db, user_id) if resolved != "mock" else {}
    if positions is None:
        positions = _open_position_dicts(db, user_id)
    capture = capture_nav(
        use_live=use_live,
        prices=prices,
        evm_addresses=owned_evm,
        solana_addresses=owned_sol,
        allow_mock=allow_mock,
        creds=creds,
    )
    _carry_failed_venues(db, user_id, capture, prices)
    # After the carry: it rebuilds nav_usdt as the raw holding sum, which would
    # put the covered receipt rows back in. Folding here also adds the open
    # position net, so the stored NAV is the single figure overview shows.
    _fold_positions(capture, positions)
    if capture.error:
        raise SnapshotUnavailable(capture.error)
    moment = _aware(ts or datetime.now(timezone.utc))
    # P13: provenance is independent of ``source``. A demo sync stays
    # source="sync" (so the seed curve is not selected) but is not live.
    has_fixture = any(
        getattr(holding, "fixture", False) or not holding.live
        for holding in capture.holdings
    )
    provenance = classify_provenance(
        mode=resolved,
        use_live=use_live,
        user=db.get(User, user_id),
        has_fixture=has_fixture,
    )
    live = provenance == "live"
    row, _created = upsert_snapshot(
        db,
        user_id,
        moment,
        capture,
        source="sync",
        provenance=provenance,
        live=live,
    )
    logger.info(
        "snapshot user=%s source=sync provenance=%s live=%s nav=%s holdings=%s "
        "unpriced=%s skipped=%s",
        user_id,
        provenance,
        live,
        capture.nav_usdt,
        len(capture.holdings),
        len(capture.unpriced),
        len(capture.skipped),
    )
    return row


def _carry_failed_venues(
    db: Session,
    user_id: int,
    capture: NavCapture,
    prices: PriceService | None,
) -> None:
    """Keep the previous quantity when a keyed exchange fails.

    A Binance 451 must not punch a hole in the NAV. If the last non-seed
    snapshot held that venue, those amounts are copied and marked ``stale``.
    If it never held the venue, the source is simply omitted (already the case)
    and the skip reason stays. Either way the snapshot is still written.
    """
    failed = {item.source for item in capture.skipped if item.source in EXCHANGES}
    if not failed:
        return
    previous = _previous_holdings(db, user_id)
    if prices is None:
        prices = _price_service(False)
    for source in sorted(failed):
        carried = [row for row in previous if row.get("source") == source]
        if not carried:
            continue
        reason = next(item.reason for item in capture.skipped if item.source == source)
        for row in carried:
            try:
                amount = Decimal(str(row.get("amount") or "0"))
            except Exception:  # noqa: BLE001
                continue
            asset = str(row.get("asset") or "")
            if not asset or amount == 0:
                continue
            chain = row.get("chain") or None
            contract = row.get("contract") or None
            icon_url = row.get("icon_url") or None
            if isinstance(chain, str) and not chain.strip():
                chain = None
            if isinstance(contract, str) and not contract.strip():
                contract = None
            if isinstance(icon_url, str):
                icon_url = icon_url.strip() or None
                if icon_url and not icon_url.startswith(("http://", "https://")):
                    icon_url = None
            else:
                icon_url = None
            price = _price_carried(source, asset, amount, chain, contract, prices)
            value = (amount * price).quantize(Decimal("0.01")) if price is not None else Decimal("0.00")
            capture.holdings.append(
                Holding(
                    source=source,
                    asset=asset,
                    amount=amount,
                    price_usdt=price,
                    value_usdt=value,
                    live=False,
                    price_source="stale",
                    stale=True,
                    chain=chain,
                    contract=contract,
                    icon_url=icon_url,
                )
            )
            if price is None:
                capture.unpriced.append(f"{source}:{asset}")
        capture.skipped = [
            SkippedSource(item.source, f"{item.reason}; carried previous balances (stale)")
            if item.source == source
            else item
            for item in capture.skipped
        ]
    capture.nav_usdt = sum((item.value_usdt for item in capture.holdings), Decimal("0")).quantize(Decimal("0.01"))


def _price_carried(
    source: str,
    asset: str,
    amount: Decimal,
    chain: str | None,
    contract: str | None,
    prices: PriceService,
) -> Decimal | None:
    """Price a stale carried row the same way as a fresh balance.

    The holding stays ``price_source="stale"`` (the quantity was copied).
    The quote itself still prefers the venue ticker, then the contract,
    then the symbol — a 451 carry must not drop a coin the ticker can name.
    """
    if not contract and _is_exchange_source(source):
        ticker = prices.quote_exchange_ticker(source, asset)
        if ticker is not None and not _is_dust_amount(amount):
            return ticker
    if chain and contract:
        quoted = prices.quote_contract(chain, contract)
        if quoted is not None:
            return quoted
    return prices.quote_usdt(asset)


def _previous_holdings(db: Session, user_id: int) -> list[dict[str, Any]]:
    # Prefer the latest live sync over a later backfill (same rule as select_nav_snapshots).
    row = db.scalar(
        select(Snapshot)
        .where(Snapshot.user_id == user_id, Snapshot.source == "sync")
        .order_by(Snapshot.ts.desc())
        .limit(1)
    )
    if row is None:
        row = db.scalar(
            select(Snapshot)
            .where(Snapshot.user_id == user_id, Snapshot.source != "seed")
            .order_by(Snapshot.ts.desc())
            .limit(1)
        )
    if row is None or not row.breakdown:
        return []
    try:
        payload = json.loads(row.breakdown)
    except json.JSONDecodeError:
        return []
    holdings = payload.get("holdings") if isinstance(payload, dict) else None
    return [item for item in holdings if isinstance(item, dict)] if isinstance(holdings, list) else []




def _sync_breakdown_folded(db: Session, user_id: int) -> bool:
    """True when the newest sync snapshot already folded open position nets."""
    row = db.scalar(
        select(Snapshot)
        .where(Snapshot.user_id == user_id, Snapshot.source == "sync")
        .order_by(Snapshot.ts.desc())
        .limit(1)
    )
    if row is None or not row.breakdown:
        return False
    try:
        payload = json.loads(row.breakdown)
    except json.JSONDecodeError:
        return False
    return bool(isinstance(payload, dict) and payload.get("positions_included"))


def latest_sync_holdings(db: Session, user_id: int) -> tuple[list[dict[str, Any]], datetime | None]:
    """Flat holdings from the user's newest ``source=sync`` snapshot only.

    No seed/backfill fallback — matches overview holdings rules (三期·12).
    """
    row = db.scalar(
        select(Snapshot)
        .where(Snapshot.user_id == user_id, Snapshot.source == "sync")
        .order_by(Snapshot.ts.desc())
        .limit(1)
    )
    if row is None:
        return [], None
    if not row.breakdown:
        return [], row.ts
    try:
        payload = json.loads(row.breakdown)
    except json.JSONDecodeError:
        return [], row.ts
    raw = payload.get("holdings") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return [], row.ts
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        source = item.get("source")
        asset = item.get("asset")
        if not source or not asset:
            continue
        priced = item.get("priced")
        if priced is None:
            priced = item.get("price_usdt") is not None
        row_out: dict[str, Any] = {
            "source": str(source),
            "asset": str(asset),
            "amount": item.get("amount", "0"),
            "price_usdt": item.get("price_usdt"),
            "value_usdt": item.get("value_usdt", "0"),
            "priced": bool(priced),
            "live": bool(item.get("live", True)),
        }
        for key in ("price_source", "chain", "contract", "icon_url"):
            if item.get(key) is not None:
                row_out[key] = item.get(key)
        if item.get("stale"):
            row_out["stale"] = True
        if item.get("covered_by_position"):
            row_out["covered_by_position"] = True
        out.append(row_out)
    return out, row.ts

def select_nav_snapshots(db: Session, user_id: int) -> tuple[list[Snapshot], NavSource]:
    """Non-seed snapshots when any exist, otherwise the phase-1 seed curve.

    Rule: if the user has any ``sync`` snapshot, every sync row stays and a
    ``backfill`` row stays only when its ``ts`` is strictly before the earliest
    sync. A backfill on or after the first live read is an old valuation
    (end-of-day replay) that would sit between real sync points and punch a
    false dip in the curve — those dates are represented by the sync points.
    With no sync, every backfill stays. Seed rows are used only when there has
    never been a sync or a backfill. ``nav_source`` is the latest chosen row's
    provenance (``demo`` / ``fixture`` / ``live`` when written by P13, otherwise
    the legacy ``sync`` / ``backfill`` / ``seed`` source).
    """
    rows = list(
        db.scalars(
            select(Snapshot).where(Snapshot.user_id == user_id).order_by(Snapshot.ts.asc())
        ).all()
    )
    real = [row for row in rows if (row.source or "seed") != "seed"]
    if not real:
        if not rows:
            return [], "seed"
        return rows, "seed"

    sync_rows = [row for row in real if row.source == "sync"]
    if sync_rows:
        earliest_sync_ts = min(_aware(row.ts) for row in sync_rows)
        # Pre-sync history only. Anything on or after the first live read is a
        # stale backfill and must not sit among sync points.
        chosen = [
            row
            for row in real
            if row.source == "sync" or _aware(row.ts) < earliest_sync_ts
        ]
        return chosen, (snapshot_provenance(chosen[-1]) if chosen else "sync") or "sync"

    return real, (snapshot_provenance(real[-1]) if real else "backfill") or "backfill"
