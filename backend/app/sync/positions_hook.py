"""Phase-4 position sync hook.

Live path (default): Aave / Morpho / Pendle for bound EVM wallets, and
Kamino / Jupiter for bound Solana wallets. An adapter that raises is
recorded in ``failed_protocols`` so its existing open rows stay open. A
clean empty read (no position on that protocol) is not a failure and may
close that protocol's stale rows.

``use_mock=True`` still loads the fixture file and does not call the network.
A chain with no bound wallet is treated as unbound: its remaining open lending
rows are marked ``closed`` with ``close_reason='orphaned'`` instead of being
left open forever. Rows are never deleted, and a chain that still has at least
one wallet is never touched by the orphan pass.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..integrations.lending_aave import fetch_positions as fetch_aave
from ..integrations.lending_common import asof_iso, take_failures, take_source_failures
from ..integrations.lending_jupiter import fetch_positions as fetch_jupiter
from ..integrations.lending_kamino import fetch_positions as fetch_kamino
from ..integrations.lending_morpho import fetch_positions as fetch_morpho
from ..integrations.lending_pendle import fetch_positions as fetch_pendle
from ..models import Position, User
from ..positions_core import (
    is_row_source,
    load_mock_positions,
    position_source,
    upsert_positions,
)
from ..provenance import classify_provenance
from ..wallets import user_addresses

logger = logging.getLogger(__name__)

# Protocol names in a stable order so a failure list is deterministic.
# The adapter is resolved at call time (not import time) so a test can
# replace ``fetch_aave`` on this module and the live path sees it.
_LENDING_PROTOCOLS: tuple[str, ...] = ("aave", "morpho", "pendle", "kamino", "jupiter")

# Per-thread outcome of the last hook call, read by run_sync for the reconcile
# detail. The return value stays an int so existing callers do not change.
_failed_state = threading.local()


def last_failed_protocols() -> tuple[str, ...]:
    """Failures of the last hook call.

    Entries are protocol names (``jupiter``) and, when an adapter can name the
    broken feed, ``protocol:source`` (``jupiter:perps``,
    ``kamino:market:<pubkey>``). ``run_sync`` prefixes each with ``lending:``
    for ``detail.failed_sources``; the bare protocol is still present when the
    whole protocol had to be carried forward.
    """
    return tuple(getattr(_failed_state, "value", ()))


def _remember_failed(failed: list[str]) -> None:
    _failed_state.value = tuple(failed)


def _lending_adapters() -> tuple[tuple[str, Callable[..., list[dict]]], ...]:
    return (
        ("aave", fetch_aave),
        ("morpho", fetch_morpho),
        ("pendle", fetch_pendle),
    )


def _solana_adapters() -> tuple[tuple[str, Callable[..., list[dict]]], ...]:
    return (
        ("kamino", fetch_kamino),
        ("jupiter", fetch_jupiter),
    )


def _orphan_positions(
    db: Session,
    user_id: int,
    sync_id: int | None,
    *,
    evm_present: bool,
    solana_present: bool,
) -> int:
    """Close open lending rows whose chain has no wallet left.

    A position's chain is ``solana`` or an EVM network name (``ethereum`` and
    friends). Missing wallets for one family must not affect the other family.
    This is a close, not a delete: the row and its snapshots stay for history.
    """
    now = datetime.now(timezone.utc)
    rows = db.scalars(
        select(Position).where(
            Position.user_id == user_id,
            Position.kind == "lending",
            Position.status == "open",
        )
    ).all()
    closed = 0
    for position in rows:
        chain = str(position.chain or "").strip().lower()
        if not chain:
            continue
        is_solana = chain == "solana"
        if (is_solana and solana_present) or (not is_solana and evm_present):
            continue
        details = dict(position.details) if isinstance(position.details, dict) else {}
        details["close_reason"] = "orphaned"
        details["closed_at"] = now.isoformat()
        position.details = details
        position.status = "closed"
        position.asof = now
        position.sync_id = sync_id
        position.close_reason = "orphaned"
        closed += 1
    if closed:
        db.commit()
        logger.info("positions: orphaned=%s closed (no wallet on their chain)", closed)
    return closed


def sync_positions_for_user(
    db: Session,
    user_id: int,
    sync_id: int | None,
    *,
    use_mock: bool = False,
) -> int:
    """Upsert lending positions for ``user_id``. Returns rows written.

    ``use_mock=False`` (the default) reads live adapters for the user's own
    wallets. It never inserts fixture rows. ``use_mock=True`` loads the
    fixture file and ignores wallets.
    """
    _remember_failed([])
    if use_mock:
        rows = load_mock_positions()
        provenance = classify_provenance(
            mode="mock",
            use_live=False,
            user=db.get(User, user_id),
            has_fixture=True,
        )
        written = upsert_positions(
            db,
            user_id,
            rows,
            sync_id=sync_id,
            provenance=provenance,
            live=False,
        )
        return len(written)

    evm = user_addresses(db, user_id, "evm")
    solana = user_addresses(db, user_id, "solana")
    # Unbinding every wallet of a chain leaves its old rows with no owner.
    # Close them on the next sync; a chain that still has a wallet is skipped.
    _orphan_positions(
        db,
        user_id,
        sync_id,
        evm_present=bool(evm),
        solana_present=bool(solana),
    )
    if not evm and not solana:
        logger.info("positions: no evm wallet, skip live lending")
        return 0

    rows: list[dict] = []
    failed: list[str] = []
    # Protocols answered cleanly, including a genuine empty wallet. A protocol
    # missing from this list was not read, so its open rows must stay open.
    seen: list[str] = []
    # Drop notes left by an earlier call on this thread before reading.
    take_source_failures()
    take_failures()
    if evm:
        for protocol, fetch in _lending_adapters():
            protocol_rows, errored = _fetch_protocol(protocol, fetch, evm)
            if errored:
                failed.append(protocol)
                continue
            seen.append(protocol)
            rows.extend(protocol_rows)
    if solana:
        for protocol, fetch in _solana_adapters():
            protocol_rows, errored = _fetch_protocol(protocol, fetch, solana)
            if errored:
                failed.append(protocol)
                continue
            seen.append(protocol)
            rows.extend(protocol_rows)
    # Adapters swallow transport errors and return []. That is not a clean
    # empty wallet: the protocol logged a failure, so its old rows stay open.
    # Source notes must be read before ``take_failures`` (which clears them).
    source_failures = take_source_failures()
    noted = take_failures()
    # A bare note means the whole protocol is unread. A source note is
    # row-level only when every named source can be mapped back to open rows;
    # otherwise (discovery, scan abort, unknown naming) keep all rows.
    whole = set(failed)
    by_source: dict[str, list[str]] = {}
    for protocol in sorted(set(noted) | set(source_failures)):
        concrete = sorted(
            {
                str(source)
                for source in source_failures.get(protocol, ())
                if source and not str(source).startswith("optional:")
            }
        )
        if protocol in failed:
            continue
        if concrete and all(is_row_source(protocol, source) for source in concrete):
            by_source[protocol] = concrete
        elif protocol in noted or concrete:
            whole.add(protocol)
    # Keep the historical order (bare protocols first, then their sources) so
    # reconcile details and last_failed_protocols stay deterministic.
    failed = [protocol for protocol in _LENDING_PROTOCOLS if protocol in whole]
    source_notes = [
        f"{protocol}:{source}"
        for protocol in _LENDING_PROTOCOLS
        for source in by_source.get(protocol, ())
    ]
    failed_notes = [*failed, *source_notes]

    written = upsert_positions(
        db,
        user_id,
        rows,
        sync_id=sync_id,
        failed_protocols=failed_notes,
        seen_protocols=[protocol for protocol in seen if protocol not in whole],
        provenance="live",
        live=True,
    )
    touched = {row.id for row in written}
    stale = _mark_stale(db, user_id, failed, by_source, touched)
    _remember_failed(failed_notes)
    logger.info(
        "positions: live lending written=%s failed=%s stale=%s",
        len(written),
        ",".join(failed_notes) if failed_notes else "-",
        stale,
    )
    return len(written)


def _mark_stale(
    db: Session,
    user_id: int,
    whole_failures: list[str],
    source_failures: dict[str, list[str]],
    touched: set[int],
) -> int:
    """Flag untouched rows affected by a failed protocol or source.

    Whole-protocol failures mark every open row of the protocol. Source-level
    failures mark only rows attributed to that source, plus rows whose source
    cannot be determined (the conservative choice). The row keeps its last
    successfully read values and ``asof``. ``stale_since`` is the first sync
    that could not refresh it, so repeated failed syncs do not keep moving the
    timestamp. Fresh rows from a partial read are in ``touched`` and are not
    marked.
    """
    protocols = set(whole_failures) | set(source_failures)
    if not protocols:
        return 0
    rows = db.scalars(
        select(Position).where(
            Position.user_id == user_id,
            Position.status == "open",
            Position.protocol.in_(protocols),
        )
    ).all()
    changed = 0
    for row in rows:
        if row.id in touched:
            continue
        if row.protocol not in whole_failures:
            failed_sources = source_failures.get(row.protocol) or ()
            source = position_source(row)
            # Unknown provenance is treated as failed: keep it and mark it
            # stale rather than assuming it belongs to a clean sibling.
            if source is not None and source not in failed_sources:
                continue
        details = dict(row.details) if isinstance(row.details, dict) else {}
        if details.get("stale") is True and details.get("stale_since"):
            continue
        details["stale"] = True
        details["stale_since"] = details.get("stale_since") or asof_iso(row.asof)
        row.details = details
        changed += 1
    if changed:
        db.commit()
    return changed


def _fetch_protocol(
    protocol: str,
    fetch: Callable[..., list[dict]],
    addresses: tuple[str, ...],
) -> tuple[list[dict], bool]:
    """Read one protocol across every bound wallet.

    One address raising fails the whole protocol: a partial list must not
    close the wallets we could not read. An empty list is success.
    """
    rows: list[dict] = []
    for address in addresses:
        try:
            fetched = fetch(address)
        except Exception as exc:  # noqa: BLE001 — adapter bugs must not close old rows
            logger.warning("positions: %s failed (%s)", protocol, type(exc).__name__)
            return [], True
        if not isinstance(fetched, list):
            logger.warning("positions: %s returned %s", protocol, type(fetched).__name__)
            return [], True
        rows.extend(item for item in fetched if isinstance(item, dict))
    return rows, False
