"""Pull every configured adapter, attribute transfers, and enqueue unseen rows."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Callable, Iterable

from sqlalchemy import bindparam, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..attribution import Attribution, attribute, linked_transfer_txids, normalize_txid
from ..address_labels import (
    Leg,
    cache_address,
    enqueue_label_refresh,
    is_zero_address,
    label_meta_for_transfer,
    load_cached_labels,
    protocol_deposit_hits,
    transfer_chain,
)
from ..config import (
    has_live_credentials,
    resolve_sync_mode,
    settings,
)
from ..integrations.chain.enrich import Enrichment, enrich_transfer, refine_attribution
from ..integrations.chain.helius import SolanaHeliusAdapter
from ..integrations.chain.routes import route_kind, route_kind_from_text
from ..integrations.chain.zerion import ZerionAdapter
from ..integrations.exchanges.ccxt_adapter import CCXTAdapter
from ..integrations.exchanges.errors import describe_exchange_error
from ..integrations.exchanges.factory import build_exchange_adapter
from ..integrations.exchanges.mock import MockExchangeAdapter
from ..integrations.prices.service import PriceService
from ..integrations.types import NormalizedTransfer
from ..models import PendingConfirmation
from ..snapshots import SnapshotUnavailable, write_sync_snapshot
from ..wallets import user_addresses, user_exchange_credentials

logger = logging.getLogger(__name__)

# The sync path must not call Blockscout directly.  Tests (and an operator
# explicitly opting into the legacy path) monkeypatch this symbol; keeping the
# original lets run_sync distinguish an injected lookup from the imported
# network client without changing the public helper signature.
_ORIGINAL_ENRICH_TRANSFER = enrich_transfer

EXCHANGES = ("binance", "bitget", "bybit", "kraken", "gate")
_CHAIN_SOURCES = frozenset({"zerion", "helius"})
PAIR_WINDOW = timedelta(minutes=30)

# Blockscout budgets: 25 lookups per batch with a polite pause, and a total cap
# so one slow public API cannot stall a sync.  Anything left over is deferred to
# the next worker tick; it never falls back to a high-confidence withdraw.
ENRICH_BATCH_SIZE = 25
ENRICH_TOTAL_BUDGET_SECONDS = 60.0
ENRICH_BATCH_BUDGET_SECONDS = 20.0
ENRICH_BATCH_INTERVAL_SECONDS = 0.2
ENRICH_LOW_CONFIDENCE = 0.8


@dataclass(frozen=True)
class SyncResult:
    fetched: int
    enqueued: int
    duplicates: int
    # Informational only (empty key, carried-forward balance). Never a failure.
    skipped: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, int]:
        return {"fetched": self.fetched, "enqueued": self.enqueued, "duplicates": self.duplicates}


@dataclass(frozen=True)
class PairInfo:
    """One swap/bridge event that arrived as two provider legs."""

    kind: str
    same_tx: bool
    legs: tuple[NormalizedTransfer, ...]
    main: NormalizedTransfer


def _transfer_key(transfer: NormalizedTransfer) -> tuple[str, str]:
    return (transfer.source, transfer.external_id)


def _chain_transfers(transfers: Iterable[NormalizedTransfer]) -> list[NormalizedTransfer]:
    return [
        transfer
        for transfer in transfers
        if transfer.source in _CHAIN_SOURCES and transfer.direction in {"in", "out"}
    ]


def _pair_kind(transfers: Iterable[NormalizedTransfer]) -> str:
    legs = list(transfers)
    direct = next((route_kind(leg.address) for leg in legs if route_kind(leg.address)), None)
    text = route_kind_from_text(
        *[
            value
            for leg in legs
            for value in (leg.address, leg.raw_type, leg.note)
            if value
        ]
    )
    return "bridge" if "bridge" in {direct, text} else "swap"


def pair_chain_transfers(
    transfers: Iterable[NormalizedTransfer],
) -> tuple[dict[tuple[str, str], PairInfo], set[tuple[str, str]]]:
    """Pair out/in legs of one swap or bridge.

    Two shapes are recognised:

    * same tx: at least one out and one in leg share a tx hash.  The largest
      out leg is kept, every other leg is skipped and merged into one suggestion.
    * near window: an out and an in to the *same* counterparty within 30 minutes
      through a known route contract.  This is a bridge hint and is never
      treated as same-tx proof (nature keeps it ``review``).

    Returns ``(pairs, skip_keys)``; ``skip_keys`` legs must not create pending
    rows on their own, otherwise every swap produces a fake withdrawal *and* a
    fake deposit.
    """
    chain = _chain_transfers(transfers)
    # A same-tx deposit into a protocol is not a swap: both legs must stay
    # visible (the zero-address receipt proves the deposit) and the out leg is
    # categorized by the protocol-deposit rule below.
    protocol_txids = set(
        protocol_deposit_hits(
            Leg(
                txid=normalize_txid(transfer.txid),
                direction=transfer.direction,
                rawtype=transfer.raw_type,
                asset=transfer.asset,
                address=(transfer.address or "").casefold(),
            )
            for transfer in chain
        )
    )
    out_by_tx: dict[str, list[NormalizedTransfer]] = {}
    in_by_tx: dict[str, list[NormalizedTransfer]] = {}
    for transfer in chain:
        txid = normalize_txid(transfer.txid)
        if not txid:
            continue
        bucket = out_by_tx if transfer.direction == "out" else in_by_tx
        bucket.setdefault(txid, []).append(transfer)

    pairs: dict[tuple[str, str], PairInfo] = {}
    skip: set[tuple[str, str]] = set()
    for txid in out_by_tx.keys() & in_by_tx.keys():
        if txid in protocol_txids:
            continue
        outs = out_by_tx[txid]
        ins = in_by_tx[txid]
        positive_ins = [leg for leg in ins if leg.amount > 0]
        if not positive_ins:
            continue
        main = max(outs, key=lambda leg: leg.amount)
        legs = tuple([main, *ins])
        pairs[_transfer_key(main)] = PairInfo(
            kind=_pair_kind(legs),
            same_tx=True,
            legs=legs,
            main=main,
        )
        for leg in outs:
            if leg is not main:
                skip.add(_transfer_key(leg))
        for leg in ins:
            skip.add(_transfer_key(leg))

    # Near-window bridge fallback.  Only known bridge routes qualify, otherwise
    # two unrelated opposite flows to the same address would be merged.
    used_in: set[tuple[str, str]] = set()
    unpaired_ins = [
        leg
        for leg in chain
        if leg.direction == "in" and _transfer_key(leg) not in skip
    ]
    for outgoing in chain:
        if outgoing.direction != "out" or _transfer_key(outgoing) in pairs:
            continue
        address = (outgoing.address or "").strip()
        if not address:
            continue
        out_kind = route_kind(address) or route_kind_from_text(
            outgoing.raw_type, outgoing.note
        )
        if out_kind != "bridge":
            continue
        for incoming in unpaired_ins:
            key = _transfer_key(incoming)
            if key in used_in:
                continue
            if (incoming.address or "").strip() != address:
                continue
            if abs((incoming.ts - outgoing.ts).total_seconds()) > PAIR_WINDOW.total_seconds():
                continue
            pairs[_transfer_key(outgoing)] = PairInfo(
                kind="bridge",
                same_tx=False,
                legs=(outgoing, incoming),
                main=outgoing,
            )
            skip.add(key)
            used_in.add(key)
            break

    return pairs, skip


def _pair_meta(info: PairInfo) -> dict[str, object]:
    return {
        "pair_kind": info.kind,
        "paired_same_tx": info.same_tx,
        "paired_legs": [
            {
                "direction": leg.direction,
                "asset": leg.asset,
                "amount": str(leg.amount),
                "txid": leg.txid,
                "address": leg.address,
            }
            for leg in info.legs
        ],
    }


def _pair_reason(info: PairInfo) -> str:
    noun = "跨链" if info.kind == "bridge" else "兑换"
    in_legs = [leg for leg in info.legs if leg.direction == "in" and leg.amount > 0]
    received = "、".join(f"{leg.amount} {leg.asset}" for leg in in_legs)
    if info.same_tx:
        return f"同 tx {noun}配对：转出 {info.main.amount} {info.main.asset}，收到 {received}（非单边外流）"
    return f"近窗 {noun}配对：转出 {info.main.amount} {info.main.asset}，收到 {received}（待同 tx 证据）"


def _enrich_outflows(
    transfers: Iterable[NormalizedTransfer],
    own_addresses: set[str],
    *,
    enrich_fn: Callable[..., Enrichment | None] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep_fn: Callable[[float], None] = time.sleep,
    batch_size: int = ENRICH_BATCH_SIZE,
    total_budget: float = ENRICH_TOTAL_BUDGET_SECONDS,
    batch_budget: float = ENRICH_BATCH_BUDGET_SECONDS,
    interval: float = ENRICH_BATCH_INTERVAL_SECONDS,
) -> tuple[dict[str, Enrichment], set[str]]:
    """Enrich Zerion outflow txs in batches, reusing the process cache.

    The old implementation stopped after 25 lookups and then fell back to the
    low-evidence ``withdraw`` suggestion.  Here every unique tx is attempted in
    batches; a total budget plus a per-batch deadline bounds the run and the
    unprocessed txids are returned as *deferred* so the caller can mark them for
    review and the next worker tick can retry.
    """
    lookup = enrich_fn or enrich_transfer
    candidates: list[NormalizedTransfer] = []
    seen: set[str] = set()
    for transfer in transfers:
        if transfer.source != "zerion" or transfer.direction != "out" or not transfer.txid:
            continue
        txid = normalize_txid(transfer.txid)
        if not txid or txid in seen:
            continue
        seen.add(txid)
        candidates.append(transfer)

    enrichments: dict[str, Enrichment] = {}
    deferred: set[str] = set()
    started = clock()
    for offset in range(0, len(candidates), batch_size):
        if clock() - started >= total_budget:
            deferred.update(normalize_txid(item.txid) for item in candidates[offset:])
            break
        batch = candidates[offset : offset + batch_size]
        batch_started = clock()
        processed = 0
        for transfer in batch:
            if clock() - started >= total_budget or clock() - batch_started >= batch_budget:
                break
            txid = normalize_txid(transfer.txid)
            try:
                result = lookup(transfer, own_addresses=own_addresses)
            except Exception:  # noqa: BLE001 - enrichment must never fail a sync
                logger.exception("blockscout enrichment failed")
                result = None
            if result is not None:
                enrichments[txid] = result
            processed += 1
        if processed < len(batch):
            deferred.update(normalize_txid(item.txid) for item in batch[processed:])
            deferred.update(normalize_txid(item.txid) for item in candidates[offset + len(batch) :])
            break
        if offset + batch_size < len(candidates):
            remaining = total_budget - (clock() - started)
            if remaining <= 0:
                deferred.update(normalize_txid(item.txid) for item in candidates[offset + batch_size :])
                break
            if interval > 0:
                sleep_fn(min(interval, remaining))
    return enrichments, deferred


def run_sync(
    user_id: int,
    mode: str | None = None,
    *,
    db: Session,
    since: datetime | None = None,
    sync_run_id: int | None = None,
) -> SyncResult:
    """Fetch transfers, attribute them, and insert unseen pending rows.

    ``mode`` is ``auto`` | ``mock`` | ``live``. ``auto`` uses live adapters only
    when credentials exist; otherwise it reads fixtures. A user with no wallet
    of their own (demo) fills every source from fixtures. A user who owns a
    wallet skips a source that has no key or address — its fixture is never
    mixed into their pending rows. Explicit ``mock`` mode still uses fixtures.

    ``sync_run_id`` is the ``sync_runs`` row recording this execution. Callers
    that already hold it may pass it; otherwise the scheduler places it on
    ``db.info`` before invoking the action. A direct call with neither keeps
    the historical ``NULL`` behaviour.
    """
    run_id = sync_run_id
    if run_id is None:
        stored_run_id = db.info.get("sync_run_id")
        if stored_run_id is not None:
            run_id = int(stored_run_id)
    resolved = resolve_sync_mode(mode if mode is not None else settings.sync_mode)
    evm_addresses = user_addresses(db, user_id, "evm")
    solana_addresses = user_addresses(db, user_id, "solana")
    # Live chain calls only for this user's wallets. Demo (no wallets) stays on fixtures
    # even when the process has admin keys and admin addresses in the environment.
    user_chain_keys = bool(evm_addresses and (settings.zerion_api_key or settings.alchemy_api_key)) or bool(
        solana_addresses and settings.helius_api_key
    )
    creds = user_exchange_credentials(db, user_id) if resolved != "mock" else {}
    # Exchange keys never flip another user to live. Only this user's chain keys
    # (or process-wide chain/price keys) do.
    use_live = resolved == "live" or (resolved == "auto" and (has_live_credentials() or user_chain_keys))
    window = since or settings.sync_since
    prices = _price_service(use_live)
    own_addresses = {addr.lower() for addr in (*evm_addresses, *solana_addresses)}

    allow_mock = resolved == "mock" or not (evm_addresses or solana_addresses)
    transfers, problems, skipped = _collect(
        use_live=use_live,
        since=window,
        evm_addresses=evm_addresses,
        solana_addresses=solana_addresses,
        allow_mock=allow_mock,
        creds=creds,
    )
    fetched = len(transfers)
    enqueued = 0
    duplicates = 0
    new_pending_ids: list[int] = []

    # Merge swap/bridge legs before enrichment and inserts.  Without this, one
    # on-chain swap becomes a fake withdrawal + a fake deposit.
    pairs, pair_skipped = pair_chain_transfers(transfers)
    if pairs:
        logger.info(
            "paired %d swap/bridge rows, merged %d counter-legs",
            len(pairs),
            len(pair_skipped),
        )

    # Blockscout enrichment runs before the DB lock: a slow public API must not
    # hold the per-user sync lock. Only outflow legs of Zerion transactions can
    # flip a false "withdraw" into a DeFi category. Failures degrade silently
    # and deferred txs stay low-confidence for the next worker tick.
    enrichments: dict[str, Enrichment] = {}
    deferred_txids: set[str] = set()
    # Only an explicitly injected lookup keeps the legacy live enrichment.
    # Production run_sync reads address_labels and never touches Blockscout
    # inline, which keeps the sync latency independent of public APIs.
    if use_live and enrich_transfer is not _ORIGINAL_ENRICH_TRANSFER:
        enrichments, deferred_txids = _enrich_outflows(
            [
                transfer
                for transfer in transfers
                if _transfer_key(transfer) not in pair_skipped
            ],
            own_addresses,
        )

    # Counterparty labels are cache-only on the sync path.  The pure rule
    # detects same-tx protocol deposits for *all* Zerion legs (in and out), so
    # an inbound mint leg can carry the same explanation.  Missing pairs are
    # queued after the commit; the request never waits for a public API.
    label_legs: list[Leg] = []
    label_pairs: set[tuple[str, str]] = set()
    for transfer in transfers:
        chain = transfer_chain(transfer)
        if not chain:
            continue
        if transfer.source == "zerion":
            label_legs.append(
                Leg(
                    txid=normalize_txid(transfer.txid),
                    direction=transfer.direction,
                    rawtype=transfer.raw_type,
                    asset=transfer.asset,
                    address=(transfer.address or "").casefold(),
                )
            )
        if transfer.source in {"zerion", "helius"} and transfer.address:
            if not is_zero_address(transfer.address):
                label_pairs.add((chain, cache_address(chain, transfer.address)))
    cached_labels = load_cached_labels(db, label_pairs)
    protocol_hits = protocol_deposit_hits(label_legs)
    missing_label_pairs: set[tuple[str, str]] = set()

    # Serialize this user's sync against a concurrent manual run and the tick.
    # SQLite (tests) has no advisory locks; the unique index still dedupes.
    _lock_user_sync(db, user_id)
    known = _known_keys(db, user_id)

    linked = linked_transfer_txids(transfers, own_addresses)
    for transfer in transfers:
        key = _transfer_key(transfer)
        # Counter-legs merged into a pair must not become their own pending row.
        if key in pair_skipped:
            continue
        # The asset column is varchar(32). Anything longer is an airdrop name, not a symbol.
        if len(transfer.asset) > 32:
            continue
        suggestion = attribute(transfer, own_addresses=own_addresses, linked_txids=linked)
        enrichment = None
        if transfer.source == "zerion" and transfer.direction == "out":
            enrichment = enrichments.get(normalize_txid(transfer.txid))
        if enrichment is not None:
            suggestion = refine_attribution(suggestion, transfer, enrichment)
        pair = pairs.get(key)
        if pair is not None:
            # A paired event is one swap/bridge suggestion, never a withdraw or
            # deposit.  Same-tx pairing carries more evidence than a near-window
            # bridge hint, so it keeps a higher (but still reviewable) score.
            suggestion = Attribution(
                "swap",
                0.85 if pair.same_tx else 0.7,
                _pair_reason(pair),
            )
        label_meta = None
        chain = transfer_chain(transfer)
        if transfer.source in {"zerion", "helius"} and chain:
            label_meta = label_meta_for_transfer(
                transfer,
                cached=cached_labels,
                protocol_hits=protocol_hits,
            )
            if label_meta.missing and label_meta.chain and transfer.address:
                missing_label_pairs.add(
                    (label_meta.chain, cache_address(label_meta.chain, transfer.address))
                )
            if (
                pair is None
                and transfer.direction == "out"
                and label_meta.protocol_deposit
                and label_meta.protocol_deposit_asset
            ):
                # New rows must not be inserted as a false "withdraw" while the
                # historical backfill fixes the same pattern.
                suggestion = Attribution(
                    "defi_deposit",
                    max(suggestion.confidence, 0.86),
                    f"同 tx 收到 {label_meta.protocol_deposit_asset} 且入账来自零地址，判定为协议存入",
                )
        if key in known:
            duplicates += 1
            continue
        amount_usdt = prices.to_usdt(transfer.asset, transfer.amount)
        detail = _detail(transfer, suggestion.reason)
        if enrichment is not None:
            # Keep the human-readable line visible even to clients that do not
            # read the structured meta field yet.
            detail = f"{enrichment.explain}；{detail}"[:1000]
        txid = normalize_txid(transfer.txid)
        meta: dict[str, object] = enrichment.to_meta() if enrichment is not None else {}
        if pair is not None:
            meta.update(_pair_meta(pair))
        if label_meta is not None:
            meta.update(label_meta.as_meta())
        if txid and txid in deferred_txids:
            # The next worker tick retries this tx; until then it must be
            # explicitly flagged, not presented as a confident withdrawal.
            meta["enrichment_deferred"] = True
            meta["needs_review"] = True
        if suggestion.category == "chain_out" or suggestion.confidence < ENRICH_LOW_CONFIDENCE:
            meta["needs_review"] = True
            meta["suggested_confidence"] = suggestion.confidence
        row = PendingConfirmation(
            user_id=user_id,
            title=_title(
                transfer,
                suggestion.category,
                confidence=suggestion.confidence,
                pair_kind=pair.kind if pair is not None else None,
            ),
            detail=detail,
            amount_usdt=amount_usdt,
            status="pending",
            source=transfer.source,
            external_id=transfer.external_id,
            direction=transfer.direction,
            asset=transfer.asset,
            amount_raw=transfer.amount,
            suggested_category=suggestion.category,
            suggested_confidence=Decimal(str(suggestion.confidence)),
            suggested_reason=suggestion.reason[:255],
            ts=transfer.ts,
            meta=meta or None,
        )
        try:
            # Nested transaction: a unique conflict rolls back only this INSERT.
            # db.rollback() here would abort the advisory lock and every prior row.
            with db.begin_nested():
                db.add(row)
                db.flush()
        except IntegrityError:
            duplicates += 1
            continue
        known.add(key)
        enqueued += 1
        new_pending_ids.append(row.id)

    # Lending positions land before the snapshot so the NAV sum can drop the
    # receipt / debt / share rows those positions already price. A hook failure
    # must not close positions (the hook itself records failed protocols) and
    # must not abort the transfer commit.
    failed_sources = list(problems)
    try:
        from .positions_hook import last_failed_protocols, sync_positions_for_user

        sync_positions_for_user(db, user_id, run_id, use_mock=resolved == "mock")
        failed_sources.extend(
            f"lending:{protocol}" for protocol in last_failed_protocols()
        )
    except Exception:  # noqa: BLE001 — positions are best-effort beside transfers
        logger.exception("positions sync failed")

    # The pending rows and the NAV snapshot commit together. A snapshot failure
    # must not roll back transfers that were already accepted — except when the
    # user's own wallet could not be read at all, which is reported below.
    snapshot_error: str | None = None
    snapshot_row = None
    try:
        snapshot_row = write_sync_snapshot(
            db,
            user_id,
            mode=resolved,
            prices=prices,
            evm_addresses=evm_addresses,
            solana_addresses=solana_addresses,
            creds=creds,
        )
    except SnapshotUnavailable as exc:
        # Both Zerion and Alchemy failed. No snapshot row, and no fixture NAV.
        snapshot_error = str(exc)[:240]
        logger.warning("snapshot skipped: %s", snapshot_error)
    except Exception as exc:  # noqa: BLE001 - transfers still stand if pricing fails
        snapshot_error = type(exc).__name__
        logger.warning("snapshot after sync failed (%s)", snapshot_error)
    db.commit()
    if missing_label_pairs:
        # Cache rows are written by the background RQ job, never inline.  A
        # dead Redis is tolerated and the next sync queues the same pairs.
        enqueue_label_refresh(missing_label_pairs)
    if new_pending_ids:
        # Pending rows are committed. Queue + one flush is best-effort after that.
        try:
            from ..integrations.wechat.queue import enqueue_and_try_flush

            enqueue_and_try_flush(db, new_pending_ids)
        except Exception:  # noqa: BLE001 - never rolls back an already committed pending
            logger.exception("wechat enqueue failed")
    # Reconcile the snapshot that was just committed. This writes only the
    # read-only reconcile log (plus a queued alert on fail) and runs after the
    # pending flush so a reconcile notice is never sent inline.
    if snapshot_row is not None:
        try:
            from .reconcile import record_sync_reconcile

            record_sync_reconcile(
                db,
                user_id=user_id,
                snapshot=snapshot_row,
                failed_sources=failed_sources,
            )
        except Exception:  # noqa: BLE001 - reconcile never fails a committed sync
            logger.exception("reconcile after sync failed")
            db.rollback()
    # Health / expiry alerts. A failure here must not change the sync result.
    try:
        from ..alerts import evaluate_alerts

        evaluate_alerts(db, user_id)
    except Exception:  # noqa: BLE001 - alerts never fail an already committed sync
        logger.exception("alert evaluation failed")
    logger.info(
        "sync user=%s mode=%s fetched=%s enqueued=%s duplicates=%s",
        user_id,
        "live" if use_live else "mock",
        fetched,
        enqueued,
        duplicates,
    )
    result = SyncResult(
        fetched=fetched,
        enqueued=enqueued,
        duplicates=duplicates,
        skipped=tuple(skipped),
    )
    if snapshot_error or problems:
        detail = "; ".join(part for part in (snapshot_error, *problems, _skipped_note(skipped)) if part)[:500]
        raise PartialSync(detail, result) from None
    return result


def _skipped_note(skipped: list[str]) -> str:
    return f"skipped: {'; '.join(skipped)}" if skipped else ""


class PartialSync(RuntimeError):
    """Transfers were committed, but a source or the snapshot failed.

    The message is exception types and skip reasons only — never a key or a raw address.
    ``result`` is the sync counts so a manual call can still answer 200.
    """

    def __init__(self, message: str, result: SyncResult | None = None) -> None:
        super().__init__(message)
        self.result = result


def _collect(
    *,
    use_live: bool,
    since: datetime,
    evm_addresses: tuple[str, ...] = (),
    solana_addresses: tuple[str, ...] = (),
    allow_mock: bool = True,
    creds: dict[str, dict[str, str]] | None = None,
) -> tuple[list[NormalizedTransfer], list[str], list[str]]:
    """Return ``(transfers, problems, skipped)``.

    ``allow_mock`` fills a missing key from fixtures. A live wallet passes
    ``False`` so a venue with no key is skipped (not a failure) instead of mixed in.
    ``creds`` is this user's exchange keys; demo passes ``{}``.
    """
    transfers: list[NormalizedTransfer] = []
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
        except Exception as exc:  # noqa: BLE001 - one venue must not abort the run
            detail = describe_exchange_error(exc, secrets, venue=name)
            logger.warning("exchange sync failed: %s", detail)
            problems.append(detail)

    # No wallets for this user → mock fixtures only. Never fall back to env addresses.
    if evm_addresses and use_live and (settings.zerion_api_key or settings.alchemy_api_key):
        evm: ZerionAdapter | None = ZerionAdapter(
            evm_addresses,
            zerion_api_key=settings.zerion_api_key,
            alchemy_api_key=settings.alchemy_api_key,
            # Live wallet: a Zerion failure must reach Alchemy or raise, never the fixture.
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
            logger.warning("evm sync failed (%s)", type(exc).__name__)
            problems.append(f"evm: {type(exc).__name__}")

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
            logger.warning("solana sync failed (%s)", type(exc).__name__)
            problems.append(f"solana: {type(exc).__name__}")
    return transfers, problems, skipped


def _price_service(use_live: bool) -> PriceService:
    if not use_live:
        return PriceService(use_mock=True)
    # No CoinGecko key: DefiLlama directly, no warning.
    return PriceService(
        coingecko_api_key=settings.coingecko_demo_api_key,
        use_mock=False,
    )


def _lock_user_sync(db: Session, user_id: int) -> None:
    """Transaction-scoped advisory lock so two syncs cannot write the same user.

    Held until commit or rollback. Postgres only; other dialects no-op.
    """
    bind = db.get_bind()
    if bind is None or bind.dialect.name != "postgresql":
        return
    db.execute(
        text("SELECT pg_advisory_xact_lock(872314, :uid)").bindparams(bindparam("uid", user_id)),
    )


def _known_keys(db: Session, user_id: int) -> set[tuple[str, str]]:
    """Existing ``(source, external_id)`` for this user. NULL pairs are ignored."""
    rows = db.execute(
        select(PendingConfirmation.source, PendingConfirmation.external_id).where(
            PendingConfirmation.user_id == user_id,
            PendingConfirmation.source.is_not(None),
            PendingConfirmation.external_id.is_not(None),
        )
    )
    return {(source, external_id) for source, external_id in rows}


def _already_known(db: Session, user_id: int, transfer: NormalizedTransfer) -> bool:
    existing = db.scalar(
        select(PendingConfirmation.id).where(
            PendingConfirmation.user_id == user_id,
            PendingConfirmation.source == transfer.source,
            PendingConfirmation.external_id == transfer.external_id,
        )
    )
    return existing is not None


def _title(
    transfer: NormalizedTransfer,
    category: str,
    *,
    confidence: float = 1.0,
    pair_kind: str | None = None,
) -> str:
    if pair_kind == "bridge":
        verb = "跨链转移"
    elif category == "chain_out" or (
        category == "withdraw" and confidence < ENRICH_LOW_CONFIDENCE
    ):
        # Low-confidence outflow is not a proven withdrawal; say so in the title.
        verb = "待核实的链上转出"
    else:
        verb = {
            "deposit": "充值",
            "withdraw": "提现",
            "airdrop": "空投",
            "kol_reward": "KOL 奖励",
            "transfer": "内部划转",
            "defi_deposit": "协议存入",
            "defi_redeem": "协议赎回",
            "swap": "兑换",
        }.get(category, "转账")
    return f"{transfer.source} {verb} {transfer.amount} {transfer.asset}"[:160]


def _detail(transfer: NormalizedTransfer, reason: str) -> str:
    bits = [
        reason,
        f"方向 {transfer.direction}",
        f"原始类型 {transfer.raw_type}" if transfer.raw_type else "",
        f"地址 {transfer.address}" if transfer.address else "",
        f"txid {transfer.txid}" if transfer.txid else "",
    ]
    return "；".join(bit for bit in bits if bit)[:1000]
