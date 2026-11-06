"""Shared position helpers for phase-4 P0.

Live adapters (lanes A/B/C) and overview wiring stay out of this module.
``mark_covered_by_position`` is a pure function so overview can call it later
without importing the router.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Position, PositionSnapshot

logger = logging.getLogger(__name__)

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "positions"

# Receipt / debt symbols that a lending position already prices. Pendle LP / PT / YT
# (PLP-, PT-, YT-) stay in the flat holdings list — see the spec reconciliation note.
def load_mock_positions() -> list[dict]:
    """Read lending + option fixtures. Returned dicts are JSON-native (not ORM rows)."""
    rows: list[dict] = []
    for name in ("lending.json", "option.json"):
        path = FIXTURES_DIR / name
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError(f"{path.name} must be a JSON list")
        rows.extend(payload)
    return rows


def _dec(value: object, default: str = "0") -> Decimal:
    if value is None or value == "":
        return Decimal(default)
    return Decimal(str(value))


def _parse_dt(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _health_factor(details: dict | None) -> Decimal | None:
    if not isinstance(details, dict):
        return None
    raw = details.get("health_factor")
    if raw is None:
        return None
    return Decimal(str(raw))


def _apy_net(details: dict | None) -> Decimal | None:
    if not isinstance(details, dict):
        return None
    raw = details.get("net_apy")
    if raw is None:
        return None
    return Decimal(str(raw))


def _slim_details(details: dict | None) -> dict | None:
    """Snapshot details stay small: health, apy, and the option mark trio.

    p25 snapshots also keep the loan legs (amount / usd / split APY) and the
    liquidation price so later snapshots can reconstruct deposit/withdrawal
    flows, borrow-rate highs and the interest split.  This only affects rows
    written from now on; historical JSON is left untouched.
    """
    if not isinstance(details, dict):
        return None
    keep = (
        "health_factor",
        "net_apy",
        "apy_estimated",
        "apy_outlier",
        "apy_note",
        "apy_estimate_raw",
        "apy_basis",
        "yield_data_missing",
        "pnl_data_missing",
        "ltv",
        "liq_price",
        "liq_price_basis",
        "liq_price_estimated",
        "liq_price_note",
        "mark_value_usd",
        "premium_usd",
        "unrealized_pnl_usd",
        "pnl_usd",
        "pnl_apr",
    )
    slim = {key: details[key] for key in keep if key in details}
    for side, apy_key in (("collateral", "apy_supply"), ("borrow", "apy_borrow")):
        legs = details.get(side)
        if not isinstance(legs, list):
            continue
        kept_legs = []
        for leg in legs:
            if not isinstance(leg, dict):
                continue
            item = {
                key: leg[key]
                for key in ("symbol", "amount", "usd", apy_key)
                if key in leg and leg[key] is not None
            }
            if item:
                kept_legs.append(item)
        if kept_legs:
            slim[side] = kept_legs
    return slim or None


_OPTIONAL_SOURCE_PREFIX = "optional:"
# Kamino's discovery failure / scan abort cover every market, not one market:
# a source-qualified note for them must still keep the whole protocol open.
_WHOLE_PROTOCOL_SOURCES = frozenset({"markets", "scan_aborted"})
_JUPITER_SOURCE_PREFIXES = (
    ("jupiter-lend-earn", "earn"),
    ("jupiter-lend-borrow", "borrow"),
    ("jupiter-perps", "perps"),
)
_JUPITER_ROW_SOURCES = frozenset(source for _prefix, source in _JUPITER_SOURCE_PREFIXES)


def is_row_source(protocol: str, source: str) -> bool:
    """Whether a failure source can be attributed to individual open rows.

    Unknown naming is not row-level: callers must keep the whole protocol
    rather than close rows that the note was never meant to cover.
    """
    if source in _WHOLE_PROTOCOL_SOURCES:
        return False
    if protocol == "jupiter":
        return source in _JUPITER_ROW_SOURCES
    if protocol == "kamino":
        return source.startswith("market:")
    return False


def _failure_parts(value: object) -> tuple[str, str | None] | None:
    """Split a failure note into ``(protocol, source-or-None)``.

    Failures may be bare (``kamino``: the whole protocol was not read) or
    source-qualified (``jupiter:earn``, ``kamino:market:<pubkey>``: only that
    source/market is unknown). Optional enrichment sources
    (``kamino:optional:pnl``) are non-blocking because the position's main
    collateral/debt data was read successfully.
    """
    text = str(value or "").strip()
    if not text:
        return None
    protocol, sep, source = text.partition(":")
    if not protocol:
        return None
    if sep and source.startswith(_OPTIONAL_SOURCE_PREFIX):
        return None
    if sep and source in _WHOLE_PROTOCOL_SOURCES:
        return protocol, None
    return protocol, (source or None)


def _failure_protocol(value: object) -> str | None:
    """Protocol named by a failure note (see :func:`_failure_parts`)."""
    parts = _failure_parts(value)
    return parts[0] if parts is not None else None


def _split_failures(
    names: Iterable[str],
) -> tuple[set[str], dict[str, set[str]]]:
    """Split notes into whole-protocol failures and per-source failures."""
    whole: set[str] = set()
    sources: dict[str, set[str]] = {}
    for name in names:
        parts = _failure_parts(name)
        if parts is None:
            continue
        protocol, source = parts
        if source is None or not is_row_source(protocol, source):
            # A note we cannot pin to a row source keeps every row: closing a
            # sibling would assume data we did not read.
            whole.add(protocol)
        else:
            sources.setdefault(protocol, set()).add(source)
    return whole, sources


def position_source(position: Position) -> str | None:
    """Return the source label an open row belongs to, or ``None`` if unknown.

    Jupiter market_refs carry a stable prefix. Kamino market_refs are keyed by
    market for current rows and by obligation for legacy rows, so its
    ``details["market"]`` is authoritative; without it the row is unknown.
    """
    protocol = str(position.protocol or "")
    ref = str(position.market_ref or "")
    details = position.details if isinstance(position.details, dict) else {}
    if protocol == "jupiter":
        for prefix, source in _JUPITER_SOURCE_PREFIXES:
            if ref.startswith(prefix):
                return source
        return None
    if protocol == "kamino":
        market = str(details.get("market") or "").strip()
        if market.startswith("market:"):
            market = market[len("market:") :].strip()
        if market:
            return f"market:{market}"
        return None
    return None


def upsert_positions(
    db: Session,
    user_id: int,
    rows: list[dict],
    sync_id: int | None = None,
    failed_protocols: Iterable[str] = (),
    seen_protocols: Iterable[str] | None = None,
    provenance: str | None = None,
    live: bool | None = None,
) -> list[Position]:
    """Upsert by (user_id, protocol, account_ref, market_ref) and append a snapshot.

    Only open rows whose protocol appears in this batch are eligible to close.
    A protocol that is absent from the batch keeps its existing open rows.
    ``failed_protocols`` may be bare (adapter error: the whole protocol is never
    closed) or source-qualified (``jupiter:earn``, ``kamino:market:X``: only
    rows that belong to that source, or whose source cannot be determined, stay
    open). Rows from a successfully read sibling source may still close. A
    source-qualified note is ignored only for ``optional:`` enrichment sources:
    those enrich a row but cannot make its main collateral/debt data unknown.
    An empty batch with no failed protocols closes nothing, because no protocol
    was seen.

    ``seen_protocols`` lets a caller name protocols it read cleanly even when
    that read returned no rows (a wallet that closed its last position). Those
    are eligible to close. Omit it and the batch itself is the only signal,
    which keeps an empty call from closing anything.
    """
    now = datetime.now(timezone.utc)
    touched: list[Position] = []
    seen_keys: set[tuple[str, str, str]] = set()
    seen: set[str] = set()
    failed, failed_sources = _split_failures(failed_protocols)
    if seen_protocols is not None:
        seen.update(str(name) for name in seen_protocols)

    for raw in rows:
        protocol = str(raw["protocol"])
        account_ref = str(raw["account_ref"])
        market_ref = str(raw["market_ref"])
        seen.add(protocol)

        details = raw.get("details") if isinstance(raw.get("details"), dict) else None
        # Adapters may rename a stable market key while keeping the old one as
        # an alias. Match existing rows on either ref, but never rewrite the
        # stored market_ref: that would split snapshots/history for a position
        # that never moved. Exact new-key matches always win.
        legacy_refs: list[str] = []
        alias_values: list[Any] = []
        for source in (raw.get("legacy_market_refs"), (details or {}).get("legacy_market_refs")):
            if isinstance(source, str):
                alias_values.append(source)
            elif isinstance(source, (list, tuple, set)):
                alias_values.extend(source)
        for value in alias_values:
            text = str(value or "").strip()
            if text and text != market_ref and text not in legacy_refs:
                legacy_refs.append(text)

        position = db.scalar(
            select(Position).where(
                Position.user_id == user_id,
                Position.protocol == protocol,
                Position.account_ref == account_ref,
                Position.market_ref == market_ref,
            )
        )
        if position is None and legacy_refs:
            position = db.scalar(
                select(Position)
                .where(
                    Position.user_id == user_id,
                    Position.protocol == protocol,
                    Position.account_ref == account_ref,
                    Position.market_ref.in_(legacy_refs),
                )
                .order_by(Position.id)
            )
        key = (
            (position.protocol, position.account_ref, position.market_ref)
            if position is not None
            else (protocol, account_ref, market_ref)
        )
        seen_keys.add(key)
        asof = _parse_dt(raw.get("asof")) or now
        values = {
            "kind": str(raw["kind"]),
            "chain": raw.get("chain"),
            "label": raw.get("label"),
            "status": str(raw.get("status") or "open"),
            "net_value_usdt": _dec(raw.get("net_value_usdt")),
            "collateral_usdt": _dec(raw.get("collateral_usdt")),
            "debt_usdt": _dec(raw.get("debt_usdt")),
            "details": details,
            "opened_at": _parse_dt(raw.get("opened_at")),
            "asof": asof,
            "sync_id": sync_id,
            # A row that is written again is live: any old orphan marker goes.
            "close_reason": None,
        }
        # P13: only touch provenance when the caller supplies it, so a legacy
        # caller updating an existing row cannot erase the stored value.
        if provenance is not None:
            values["provenance"] = provenance
        if live is not None:
            values["live"] = live
        if position is None:
            position = Position(
                user_id=user_id,
                protocol=protocol,
                account_ref=account_ref,
                market_ref=market_ref,
                **values,
            )
            db.add(position)
            db.flush()
        else:
            for name, value in values.items():
                setattr(position, name, value)
        # A legacy caller may omit provenance; the snapshot still inherits what
        # the position already carries instead of erasing it with NULL.
        snapshot_provenance = provenance if provenance is not None else position.provenance
        snapshot_live = live if live is not None else position.live
        db.add(
            PositionSnapshot(
                position_id=position.id,
                ts=asof,
                net_value_usdt=position.net_value_usdt,
                collateral_usdt=position.collateral_usdt,
                debt_usdt=position.debt_usdt,
                health_factor=_health_factor(details),
                apy_net=_apy_net(details),
                details=_slim_details(details),
                sync_id=sync_id,
                provenance=snapshot_provenance,
                live=snapshot_live,
            )
        )
        touched.append(position)

    # Flush first so rows inserted in this call are visible to the close pass.
    db.flush()
    open_rows = db.scalars(
        select(Position).where(Position.user_id == user_id, Position.status == "open")
    ).all()
    close_protocols = seen - failed
    for position in open_rows:
        key = (position.protocol, position.account_ref, position.market_ref)
        if position.protocol not in close_protocols or key in seen_keys:
            continue
        failed_for_protocol = failed_sources.get(position.protocol)
        if failed_for_protocol:
            source = position_source(position)
            # Unknown provenance is kept: an unread source is the conservative
            # assumption when the row cannot be attributed to a clean sibling.
            if source is None or source in failed_for_protocol:
                continue
        position.status = "closed"
        position.asof = now
        position.sync_id = sync_id
        position.close_reason = None

    db.commit()
    for position in touched:
        db.refresh(position)
    return touched


def positions_net_value(db: Session, user_id: int) -> Decimal:
    """Sum of open positions' net_value_usdt. Closed rows are excluded."""
    total = Decimal("0")
    rows = db.scalars(
        select(Position.net_value_usdt).where(
            Position.user_id == user_id,
            Position.status == "open",
        )
    ).all()
    for value in rows:
        total += Decimal(str(value))
    return total


def _norm_contract(value: object) -> str:
    return str(value or "").strip().lower()


def _norm_symbol(value: object) -> str:
    return str(value or "").strip().lower()


def _is_pendle_spot(symbol: str) -> bool:
    """PLP / PT / YT holdings stay in the flat list (spec: do not cover them)."""
    upper = symbol.upper()
    return upper.startswith(("PLP-", "PLP_", "PT-", "PT_", "YT-", "YT_"))


def _covered_symbol(symbol: str) -> bool:
    """aToken (a / aEth*), Aave debt tokens, and Morpho share tokens.

    Aave receipt symbols are ``a`` + underlying with the second character upper
    (``aUSDC``, ``aEthWETH``). Assets that merely start with "a" (ARB, APE, ATOM)
    are not receipts and are not covered by this naming rule. The underlying
    itself (``USDC``, ``WETH``) is not a receipt: a wallet balance of it is the
    user's own cash, not the protocol position.
    """
    if not symbol or _is_pendle_spot(symbol):
        return False
    folded = symbol.lower()
    if folded.startswith(("variabledebt", "stabledebt", "aeth")):
        return True
    # Jupiter Earn receipts are ``jl`` + underlying (``jlUSDC``, ``jlJupUSD``).
    # The underlying itself (USDC, SOL) is the wallet's own cash.
    if len(symbol) >= 3 and symbol[:2] == "jl" and symbol[2].isupper():
        return True
    # Morpho Blue share symbols are ``m`` + underlying. The fixture and the
    # live adapter spell the second character either way (``mwETH``, ``mUSDC``).
    if len(folded) >= 2 and folded[0] == "m" and folded[1].isalpha():
        return True
    return len(symbol) >= 2 and symbol[0] in ("a", "A") and symbol[1].isupper()


def _position_legs(position: dict) -> list[dict]:
    details = position.get("details") if isinstance(position, dict) else None
    if not isinstance(details, dict):
        return []
    legs: list[dict] = []
    for key in ("collateral", "borrow"):
        raw = details.get(key)
        if isinstance(raw, list):
            legs.extend(item for item in raw if isinstance(item, dict))
    return legs


def _open_positions(positions: list[dict]) -> list[dict]:
    return [row for row in positions if str(row.get("status") or "open") == "open"]


def mark_covered_by_position(
    holdings: list[dict],
    positions: list[dict],
    *,
    drop_covered: bool = False,
) -> list[dict]:
    """Flag holdings already priced inside an open protocol position.

    Match order (first hit wins), case-insensitive. A leg only covers a
    holding when the leg itself is a receipt, a debt token, or a Morpho share
    (``a*`` / ``variableDebt*`` / ``stableDebt*`` / ``m*``). Matching the
    underlying contract would drop the wallet's own USDC just because a Morpho
    borrow lists that USDC contract.

    1. ``holding.contract`` equals such a leg's ``contract``.
    2. Else ``holding.symbol`` / ``holding.asset`` exactly equals such a leg's
       symbol.

    Pendle PLP / PT / YT names are never flagged. Holdings are shallow-copied;
    the caller's list is not mutated.
    """
    contracts: set[str] = set()
    symbols: set[str] = set()
    for position in _open_positions(positions):
        for leg in _position_legs(position):
            symbol = str(leg.get("symbol") or "").strip()
            # The underlying (USDC, WETH, USD3, a Pendle PT posted as
            # collateral) is not the receipt. Only the receipt / debt / share
            # symbol covers a flat holding.
            if not symbol or not _covered_symbol(symbol):
                continue
            symbols.add(_norm_symbol(symbol))
            contract = _norm_contract(leg.get("contract"))
            if contract:
                contracts.add(contract)

    marked: list[dict] = []
    for holding in holdings:
        row = dict(holding)
        symbol = str(row.get("symbol") or row.get("asset") or "").strip()
        contract = _norm_contract(row.get("contract"))
        covered = False
        if not _is_pendle_spot(symbol):
            if contract and contract in contracts:
                covered = True
            elif _norm_symbol(symbol) in symbols:
                covered = True
        row["covered_by_position"] = covered
        if drop_covered and covered:
            continue
        marked.append(row)
    return marked
