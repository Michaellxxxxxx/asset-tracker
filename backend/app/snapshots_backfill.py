"""Reconstruct one NAV snapshot per day from SYNC_SINCE through today.

Mock mode does not invent a random walk. It replays the recorded transfers and
trades onto the current fixture balances:

    historical quantity = current quantity − later inflows + later outflows

so the curve is a pure function of the fixtures and ends exactly on today's
measured balances. Live mode does the same arithmetic with the real current
balances and the transfers/trades the adapters return since SYNC_SINCE.

Limits — these are not covered, on purpose:

* On-chain DeFi positions (LP tokens, staking, lending). Zerion's simple-position
  balance is used as a constant for the whole window; its internal cost basis is
  not reconstructed.
* Tokens with no historical price. The day is priced with the fixture/spot
  fallback when one exists, otherwise the holding contributes 0 and is listed in
  the breakdown as unpriced.
* Exchange-internal transfers (spot ↔ futures) that never appear as a deposit,
  withdrawal or trade. They do not move the reconstructed quantity.
* Fee precision. A trade fee is deducted in ``fee.currency`` when the record
  carries one. Deposit and withdrawal fees are not deducted: venues already
  report the net amount on some of them and the gross on others, and guessing
  which would double-count.

Idempotent: every day is written at 23:59:59 UTC with source='backfill', and
``uq_snapshots_user_ts`` turns a repeat into an update.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .attribution import linked_transfer_txids
from .config import (
    has_live_credentials,
    parse_sync_since,
    resolve_sync_mode,
    settings,
)
from .integrations.chain.helius import SolanaHeliusAdapter
from .integrations.chain.zerion import ZerionAdapter
from .integrations.exchanges.ccxt_adapter import CCXTAdapter
from .integrations.exchanges.errors import describe_exchange_error
from .integrations.exchanges.factory import build_exchange_adapter
from .integrations.exchanges.mock import MockExchangeAdapter
from .integrations.prices.service import PriceService, expired_yt_price
from .integrations.types import NormalizedTrade, NormalizedTransfer
from .models import User
from .provenance import classify_provenance
from .wallets import user_addresses, user_exchange_credentials
from .snapshots import (
    EXCHANGES,
    Holding,
    NavCapture,
    SnapshotUnavailable,
    collect_balances,
    upsert_snapshot,
)

logger = logging.getLogger(__name__)


@dataclass
class BackfillResult:
    created: int = 0
    updated: int = 0
    start: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    end: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # Keyed venues that failed. Empty-key venues are not listed.
    problems: tuple[str, ...] = ()
    # Venues with no key or no address. Not a failure.
    skipped: tuple[str, ...] = ()


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _day_end(day: date) -> datetime:
    return datetime.combine(day, time(23, 59, 59), tzinfo=timezone.utc)


# (source, asset, chain, contract). Chain and contract are "" for exchange rows
# and native coins, so the replay key stays hashable and comparable.
QuantityKey = tuple[str, str, str, str]


def _quantity_key(source: str, asset: str, chain: str | None = None, contract: str | None = None) -> QuantityKey:
    return (source, asset.upper(), (chain or "").strip().lower(), (contract or "").strip().lower())


def replay_quantities(
    current: dict[QuantityKey, Decimal] | dict[tuple[str, str], Decimal],
    movements: list[tuple[datetime, str, str, Decimal]],
    *,
    as_of: datetime,
) -> dict[QuantityKey, Decimal]:
    """Roll ``current`` back to ``as_of`` by undoing every later movement.

    A movement is ``(ts, source, asset, signed_delta)`` where a deposit or a
    trade buy is positive and a withdrawal or a trade sell is negative. Chain
    rows keep their contract on the key; a movement matches the row with the
    same source and asset and an empty contract, which is how exchange legs
    are recorded.
    """
    quantities = {_as_quantity_key(key): amount for key, amount in current.items()}
    cutoff = _aware(as_of)
    for ts, source, asset, delta in movements:
        # Strictly later: a movement at exactly ``as_of`` has already landed.
        if _aware(ts) > cutoff:
            key = _movement_key(quantities, source, asset)
            quantities[key] = quantities.get(key, Decimal("0")) - delta
    return {key: qty for key, qty in quantities.items() if qty != 0}


def _as_quantity_key(key: QuantityKey | tuple[str, str]) -> QuantityKey:
    if len(key) == 2:
        source, asset = key
        return _quantity_key(source, asset)
    source, asset, chain, contract = key  # type: ignore[misc]
    return _quantity_key(source, asset, chain, contract)


def _movement_key(quantities: dict[QuantityKey, Decimal], source: str, asset: str) -> QuantityKey:
    """Prefer an existing row for this source and asset; otherwise an exchange key."""
    symbol = asset.upper()
    for key in quantities:
        if key[0] == source and key[1] == symbol:
            return key
    return _quantity_key(source, symbol)


def price_quantities(
    quantities: dict[QuantityKey, Decimal] | dict[tuple[str, str], Decimal],
    prices: PriceService,
    day: date,
    *,
    live: bool = True,
) -> NavCapture:
    """Price a replayed book. Contract history first, then the symbol history.

    A missing historical contract price is never replaced with today's quote.
    The holding is recorded unpriced instead.
    """
    holdings: list[Holding] = []
    unpriced: list[str] = []
    total = Decimal("0")
    normalized = [_normalize_quantity(key, amount) for key, amount in quantities.items()]
    contract_keys = [
        (chain, contract) for (_source, _asset, chain, contract), _amount in normalized if chain and contract
    ]
    prices.prefetch_contracts_on(contract_keys, day)
    for (source, asset, chain, contract), amount in sorted(normalized, key=lambda item: item[0]):
        price, source_name = _historical_price(prices, asset, chain, contract, day)
        if price is None:
            value = Decimal("0.00")
            unpriced.append(f"{source}:{asset}")
        else:
            value = (amount * price).quantize(Decimal("0.01"))
        holdings.append(
            Holding(
                source=source,
                asset=asset,
                amount=amount,
                price_usdt=price,
                value_usdt=value,
                live=live,
                price_source=source_name,
                chain=chain or None,
                contract=contract or None,
                icon_url=None,
            )
        )
        total += value
    return NavCapture(nav_usdt=total.quantize(Decimal("0.01")), holdings=holdings, unpriced=unpriced)


def _normalize_quantity(
    key: QuantityKey | tuple[str, str],
    amount: Decimal,
) -> tuple[QuantityKey, Decimal]:
    """Accept the 2-tuple keys the replay still uses and the 4-tuple contract keys."""
    if len(key) == 2:
        source, asset = key
        return _quantity_key(source, asset), amount
    source, asset, chain, contract = key  # type: ignore[misc]
    return _quantity_key(source, asset, chain, contract), amount


def _historical_price(
    prices: PriceService,
    asset: str,
    chain: str,
    contract: str,
    day: date,
) -> tuple[Decimal | None, str]:
    # A matured YT is 0 on and after its expiry date, even when DefiLlama
    # still returns ~1 for the contract. PT tokens are not matched.
    expired = expired_yt_price(asset, day)
    if expired is not None:
        return expired, "expired_yt"
    if chain and contract:
        price = prices.quote_contract_on(chain, contract, day)
        if price is not None:
            return price, "defillama_contract"
    price = prices.quote_usdt_on(asset, day)
    if price is not None:
        return price, "defillama_symbol"
    return None, "none"


def _net_linked_legs(
    transfers: list[NormalizedTransfer],
    movements: list[tuple[datetime, str, str, Decimal]],
    own_addresses: set[str] | None = None,
) -> list[tuple[datetime, str, str, Decimal]]:
    """Drop both legs of an exchange↔own-wallet move so the replay NAV is flat.

    The replay books each source separately, so a Gate withdrawal and the
    matching on-chain receipt would otherwise look like the coins left the
    exchange and appeared in the wallet. They are the same coins. Removing
    both deltas leaves the total unchanged, which is what a transfer is.
    """
    linked = linked_transfer_txids(transfers, own_addresses or set())
    # An empty address set still catches a txid that shows up on both an
    # exchange record and a chain record. Passing the user's addresses also
    # links an exchange withdrawal whose destination is their own wallet.
    if not linked:
        return movements
    from .attribution import normalize_txid

    linked_marks = {
        (transfer.source, transfer.asset.upper(), _aware(transfer.ts))
        for transfer in transfers
        if normalize_txid(transfer.txid) in linked
    }
    return [
        item
        for item in movements
        if (item[1], item[2], item[0]) not in linked_marks
    ]


def _movement(ts: datetime, source: str, asset: str, delta: Decimal) -> tuple[datetime, str, str, Decimal]:
    return (_aware(ts), source, asset.upper(), delta)


def movements_from_transfers(
    transfers: list[NormalizedTransfer],
    trades: list[NormalizedTrade] | None = None,
) -> list[tuple[datetime, str, str, Decimal]]:
    """Signed balance deltas implied by transfers and trades.

    ``in`` (deposit, airdrop, reward) adds the asset. ``out`` removes it. A trade
    buy adds the base asset and subtracts the quote cost; a sell does the opposite.
    The trade fee (``raw['fee'].currency``) is deducted. Deposit and withdrawal
    fees are not: the recorded amount is already what the venue says moved.
    """
    movements: list[tuple[datetime, str, str, Decimal]] = []
    for transfer in transfers:
        amount = Decimal(transfer.amount).copy_abs()
        sign = Decimal("1") if transfer.direction == "in" else Decimal("-1")
        movements.append(_movement(transfer.ts, transfer.source, transfer.asset, sign * amount))
    for trade in trades or []:
        if not trade.base_asset:
            continue
        base_sign = Decimal("1") if trade.side.lower() == "buy" else Decimal("-1")
        movements.append(_movement(trade.ts, trade.source, trade.base_asset, base_sign * Decimal(trade.amount).copy_abs()))
        if trade.quote_asset and trade.cost_usdt is not None:
            quote_sign = Decimal("-1") if trade.side.lower() == "buy" else Decimal("1")
            movements.append(
                _movement(trade.ts, trade.source, trade.quote_asset, quote_sign * Decimal(trade.cost_usdt).copy_abs())
            )
        fee = trade.raw.get("fee") if isinstance(trade.raw, dict) else None
        if isinstance(fee, dict) and fee.get("currency") and fee.get("cost") not in (None, "", 0, "0"):
            try:
                fee_amount = Decimal(str(fee["cost"]))
            except Exception:  # noqa: BLE001
                fee_amount = Decimal(0)
            if fee_amount > 0:
                movements.append(_movement(trade.ts, trade.source, str(fee["currency"]), -fee_amount))
    return movements


def _collect_history(
    use_live: bool,
    since: datetime,
    *,
    evm_addresses: tuple[str, ...] = (),
    solana_addresses: tuple[str, ...] = (),
    allow_mock: bool = True,
    creds: dict[str, dict[str, str]] | None = None,
) -> tuple[list[NormalizedTransfer], list[NormalizedTrade], list[str], list[str]]:
    """History for the replay. A live wallet skips a venue with no key.

    Zerion failing falls through to Alchemy inside the adapter. If that also
    fails the error propagates: the curve must not be rebuilt from fixtures.
    Returns ``(transfers, trades, problems, skipped)``. An empty key is a skip, not a problem.
    """
    transfers: list[NormalizedTransfer] = []
    trades: list[NormalizedTrade] = []
    problems: list[str] = []
    skipped: list[str] = []
    creds = creds or {}
    secrets = [value for cred in creds.values() for value in cred.values()]
    for name in EXCHANGES:
        if name in creds:
            adapter = build_exchange_adapter(name, creds, ccxt_cls=CCXTAdapter)
        elif allow_mock:
            adapter = MockExchangeAdapter(name)
        else:
            skipped.append(f"{name} (no api key)")
            continue
        try:
            transfers.extend(adapter.fetch_transfers(since))
            trades.extend(adapter.fetch_trades(since))
        except Exception as exc:  # noqa: BLE001 - one venue must not abort the curve
            detail = describe_exchange_error(exc, secrets, venue=name)
            logger.warning("history fetch failed: %s", detail)
            problems.append(detail)

    if evm_addresses and use_live and (settings.zerion_api_key or settings.alchemy_api_key):
        evm: ZerionAdapter | None = ZerionAdapter(
            evm_addresses,
            zerion_api_key=settings.zerion_api_key,
            alchemy_api_key=settings.alchemy_api_key,
            # Live wallet: history must not be rebuilt from the fixture.
            allow_fixture=False,
        )
    elif allow_mock:
        evm = ZerionAdapter(("0xdemo",), use_mock=True)
    else:
        evm = None
        reason = "no wallet address" if not evm_addresses else "no zerion or alchemy key"
        skipped.append(f"evm ({reason})")
    if evm is not None:
        try:
            transfers.extend(evm.fetch_transfers(since))
        except Exception as exc:  # noqa: BLE001
            detail = describe_exchange_error(exc, secrets, venue="evm")
            logger.warning("history fetch failed: %s", detail)
            problems.append(detail)

    if solana_addresses and use_live and settings.helius_api_key:
        sol: SolanaHeliusAdapter | None = SolanaHeliusAdapter(solana_addresses, api_key=settings.helius_api_key)
    elif allow_mock:
        sol = SolanaHeliusAdapter(("So1DemoAddress",), use_mock=True)
    else:
        sol = None
        reason = "no wallet address" if not solana_addresses else "no helius key"
        skipped.append(f"solana ({reason})")
    if sol is not None:
        try:
            transfers.extend(sol.fetch_transfers(since))
        except Exception as exc:  # noqa: BLE001
            detail = describe_exchange_error(exc, secrets, venue="solana")
            logger.warning("history fetch failed: %s", detail)
            problems.append(detail)
    return transfers, trades, problems, skipped


def _current_quantities(
    use_live: bool,
    *,
    evm_addresses: tuple[str, ...] = (),
    solana_addresses: tuple[str, ...] = (),
    allow_mock: bool = True,
    creds: dict[str, dict[str, str]] | None = None,
) -> tuple[dict[QuantityKey, Decimal], bool]:
    totals: dict[QuantityKey, Decimal] = defaultdict(lambda: Decimal("0"))
    balances, _skipped, error = collect_balances(
        use_live=use_live,
        evm_addresses=evm_addresses,
        solana_addresses=solana_addresses,
        allow_mock=allow_mock,
        creds=creds,
    )
    if error:
        raise SnapshotUnavailable(error)
    has_fixture = False
    for balance in balances:
        if getattr(balance, "fixture", False):
            has_fixture = True
        chain = getattr(balance, "chain", None)
        contract = getattr(balance, "contract", None)
        totals[_quantity_key(balance.source, balance.asset, chain, contract)] += Decimal(balance.amount)
    return dict(totals), has_fixture


def backfill_user(
    db: Session,
    user_id: int,
    *,
    mode: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    prices: PriceService | None = None,
) -> BackfillResult:
    """Write one ``backfill`` snapshot per day. Repeating the call only updates."""
    resolved = resolve_sync_mode(mode if mode is not None else settings.sync_mode)
    evm_owned = user_addresses(db, user_id, "evm")
    sol_owned = user_addresses(db, user_id, "solana")
    user_chain_keys = bool(evm_owned and (settings.zerion_api_key or settings.alchemy_api_key)) or bool(
        sol_owned and settings.helius_api_key
    )
    use_live = resolved == "live" or (resolved == "auto" and (has_live_credentials() or user_chain_keys))
    start = _aware(since or parse_sync_since(settings.sync_since.date().isoformat()))
    # Default "today" is the end of the UTC day, not the instant the request
    # arrived. Otherwise two calls minutes apart miss each other on the last
    # point and the unique constraint cannot make the run idempotent.
    end = _aware(until) if until is not None else _day_end(datetime.now(timezone.utc).date())
    if end < start:
        raise ValueError("backfill end is before start")

    service = prices if prices is not None else (
        PriceService(coingecko_api_key=settings.coingecko_demo_api_key, use_mock=False)
        if use_live
        else PriceService(use_mock=True)
    )
    # Same rule as the sync snapshot: fixtures only when this user has no wallet
    # or the operator forced mock mode.
    allow_mock = resolved == "mock" or not (evm_owned or sol_owned)
    creds = user_exchange_credentials(db, user_id) if resolved != "mock" else {}
    try:
        current, has_fixture = _current_quantities(
            use_live,
            evm_addresses=evm_owned,
            solana_addresses=sol_owned,
            allow_mock=allow_mock,
            creds=creds,
        )
        transfers, trades, problems, skipped = _collect_history(
            use_live,
            start,
            evm_addresses=evm_owned,
            solana_addresses=sol_owned,
            allow_mock=allow_mock,
            creds=creds,
        )
    except SnapshotUnavailable as exc:
        raise SnapshotUnavailable(f"user {user_id}: {exc}") from None
    movements = movements_from_transfers(transfers, trades)
    movements = _net_linked_legs(transfers, movements, {addr.lower() for addr in (*evm_owned, *sol_owned)})

    result = BackfillResult(start=start, end=end)
    provenance = classify_provenance(
        mode=resolved,
        use_live=use_live,
        user=db.get(User, user_id),
        has_fixture=has_fixture,
    )
    live = provenance == "live"
    day = start.date()
    last = end.date()
    while day <= last:
        moment = _day_end(day)
        # The final day is stamped at the requested end so "today" stays inside
        # the window even when the process runs before 23:59:59.
        if day == last and moment > end:
            moment = end
        quantities = replay_quantities(current, movements, as_of=moment)
        # One DefiLlama call per day for every symbol, plus one for contracts.
        # Contract history is fetched inside price_quantities and never falls
        # back to the current price.
        service.prefetch_history([asset for _source, asset, _chain, _contract in quantities], day)
        capture = price_quantities(quantities, service, day, live=use_live and not has_fixture)
        _row, created = upsert_snapshot(
            db,
            user_id,
            moment,
            capture,
            source="backfill",
            provenance=provenance,
            live=live,
        )
        if created:
            result.created += 1
        else:
            result.updated += 1
        day += timedelta(days=1)
    db.commit()
    result.problems = tuple(problems)
    result.skipped = tuple(skipped)
    if problems:
        from .sync.service import PartialSync

        raise PartialSync("; ".join(problems)[:500])
    logger.info(
        "backfill user=%s created=%s updated=%s from=%s to=%s",
        user_id,
        result.created,
        result.updated,
        start.date().isoformat(),
        end.date().isoformat(),
    )
    return result


def backfill_all_users(db: Session, *, mode: str | None = None) -> dict[str, Any]:
    summaries = []
    for user in db.scalars(select(User).order_by(User.id.asc())).all():
        result = backfill_user(db, user.id, mode=mode)
        summaries.append(
            {
                "user_id": user.id,
                "created": result.created,
                "updated": result.updated,
            }
        )
    return {"users": summaries}
