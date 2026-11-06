"""Parse CCXT-shaped raw payloads into unified dataclasses.

Live and mock adapters both call these functions so fixture JSON matches the
shape ccxt returns (deposits, withdrawals, balances, trades).
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from ..types import NormalizedBalance, NormalizedTrade, NormalizedTransfer

_IN_TYPES = {"deposit", "in", "receive", "incoming"}
_OUT_TYPES = {"withdrawal", "withdraw", "out", "send", "outgoing"}


def _decimal(value: Any, default: str = "0") -> Decimal:
    if value is None or value == "":
        return Decimal(default)
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal(default)


def _ts_from_ms(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        millis = int(value)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(millis / 1000, tz=timezone.utc)


def _direction(raw_type: str, amount: Decimal) -> str:
    normalized = (raw_type or "").strip().lower()
    if normalized in _IN_TYPES:
        return "in"
    if normalized in _OUT_TYPES:
        return "out"
    return "out" if amount < 0 else "in"


def normalize_transfer(source: str, raw: dict[str, Any]) -> NormalizedTransfer | None:
    """Normalize one ccxt deposit/withdrawal dict. Returns None when unusable."""
    ts = _ts_from_ms(raw.get("timestamp"))
    if ts is None and isinstance(raw.get("datetime"), str):
        try:
            parsed = datetime.fromisoformat(raw["datetime"].replace("Z", "+00:00"))
            ts = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            ts = None
    if ts is None:
        return None

    asset = str(raw.get("currency") or raw.get("asset") or "").upper()
    if not asset:
        return None

    amount = abs(_decimal(raw.get("amount")))
    raw_type = str(raw.get("type") or "")
    external_id = str(
        raw.get("id")
        or raw.get("txid")
        or raw.get("txId")
        or f"{source}:{asset}:{int(ts.timestamp() * 1000)}:{amount}"
    )
    address = raw.get("address") or raw.get("addressTo") or raw.get("addressFrom")
    fee = raw.get("fee") if isinstance(raw.get("fee"), dict) else None
    fee_note = ""
    if fee and fee.get("cost") not in (None, "", 0):
        fee_note = f"fee={fee.get('cost')} {fee.get('currency') or asset}"
    note_parts = [str(raw.get("status") or ""), str(raw.get("tag") or ""), fee_note]
    note = " ".join(part for part in note_parts if part and part != "{}").strip()
    return NormalizedTransfer(
        source=source,
        external_id=external_id,
        ts=ts,
        asset=asset,
        amount=amount,
        direction=_direction(raw_type, _decimal(raw.get("amount"))),  # type: ignore[arg-type]
        address=str(address) if address else None,
        txid=str(raw["txid"]) if raw.get("txid") else None,
        raw_type=raw_type or ("deposit" if _direction(raw_type, amount) == "in" else "withdrawal"),
        note=note[:500],
        raw=raw,
    )


def normalize_balance(source: str, raw: dict[str, Any]) -> list[NormalizedBalance]:
    """Parse a ccxt ``fetch_balance`` payload (``total`` map, optional ``info``).

    Binance Simple Earn receipts arrive as ``LD<ASSET>`` (LDUSDT). Those fold
    into ``<ASSET>`` and merge with the spot balance of the same name. Funding
    wallets are never requested (that endpoint is a POST).
    """
    totals = raw.get("total") or {}
    merged: dict[str, Decimal] = {}
    if isinstance(totals, dict):
        for asset, amount in totals.items():
            qty = _decimal(amount)
            if qty == 0:
                continue
            name = str(asset).upper()
            if source == "binance" and name.startswith("LD") and len(name) > 2:
                name = name[2:]
            merged[name] = merged.get(name, Decimal(0)) + qty
    return [
        NormalizedBalance(source=source, asset=asset, amount=qty, raw={"total": str(qty)})
        for asset, qty in merged.items()
    ]


def normalize_trade(source: str, raw: dict[str, Any]) -> NormalizedTrade | None:
    ts = _ts_from_ms(raw.get("timestamp"))
    if ts is None:
        return None
    symbol = str(raw.get("symbol") or "")
    if not symbol:
        return None
    external_id = str(raw.get("id") or raw.get("order") or f"{source}:{symbol}:{int(ts.timestamp() * 1000)}")
    cost = raw.get("cost")
    return NormalizedTrade(
        source=source,
        external_id=external_id,
        ts=ts,
        symbol=symbol,
        side=str(raw.get("side") or ""),
        amount=_decimal(raw.get("amount")),
        cost_usdt=_decimal(cost) if cost not in (None, "") else None,
        raw=raw,
    )


def filter_since(items: list, since: datetime):
    since_utc = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
    return [item for item in items if item.ts >= since_utc]
