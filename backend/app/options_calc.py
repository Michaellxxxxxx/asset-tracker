"""Pure option valuation, PnL, and APR. No network, no keys, no DB.

Caliber (p4a plan §2, field names from p4_spec ``details`` — do not rename):

* mark value = signed size × mark_price × contract multiplier.
  Short size is negative, so the mark value (and therefore NAV) is negative.
* premium (cost) = signed size × avg price × multiplier. A buy is a positive
  cost; a sell is premium income (negative cost).
* unrealized PnL = mark value − premium.
* realized PnL is the cash of close / exercise / expire fills, plus the
  remaining open premium those fills release. A full close realizes the whole
  premium; a partial close realizes it pro-rata by quantity.
* APR = total PnL / average capital / holding days × 365.
  Capital is margin plus the buyer's premium. Zero capital → ``None`` (UI "—").
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

_Q = Decimal("0.00000001")
_DAYS = Decimal(365)


def dec(value: object, default: str = "0") -> Decimal:
    if value is None or value == "":
        return Decimal(default)
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(default)


def fnum(value: object) -> float:
    """JSON-native number, quantized to 8 dp (positions fixture style)."""
    return float(dec(value).quantize(_Q))


def money(value: object) -> str:
    return f"{dec(value).quantize(_Q):f}"


def mark_value(size: object, mark_price: object, multiplier: object = 1) -> Decimal:
    """Signed size × mark × multiplier. Shorts come out negative."""
    return dec(size) * dec(mark_price) * dec(multiplier, default="1")


def premium_cost(size: object, avg_price: object, multiplier: object = 1) -> Decimal:
    """Signed premium. Buy > 0 (cost), sell < 0 (income)."""
    return dec(size) * dec(avg_price) * dec(multiplier, default="1")


def unrealized_pnl(mark_value_usd: object, premium_usd: object) -> Decimal:
    """Mark value minus the signed premium cost."""
    return dec(mark_value_usd) - dec(premium_usd)


def _kind(value: object) -> str:
    text = str(value or "open").strip().lower()
    if text not in {"open", "close", "exercise", "expire"}:
        return "open"
    return text


def _signed_qty(side: object, qty: object) -> Decimal:
    """Buy increases the long position; sell decreases it. Qty is absolute."""
    amount = abs(dec(qty))
    return amount if str(side or "").strip().lower() == "buy" else -amount


def _cash(side: object, qty: object, price: object, fee: object, multiplier: object) -> Decimal:
    """Cash credited to the account. Sell receives premium; buy pays it; fee always out."""
    amount = abs(dec(qty))
    notion = amount * dec(price) * dec(multiplier, default="1")
    paid = notion if str(side or "").strip().lower() == "buy" else -notion
    return -(paid + dec(fee))


def realized_pnl(
    trades: list[dict[str, Any]] | None,
    *,
    multiplier: object = 1,
) -> Decimal:
    """Realized PnL from close / exercise / expire fills.

    Open fills only set the average cost; they are not themselves realized.
    A closing fill realizes ``cash − cost basis of the closed slice``:

    * long: cash in (sell price × qty − fee) minus the premium originally paid;
    * short: cash out is negative, and the cost basis is negative (premium
      received), so covering cheaper than the credit is a gain.

    Partial closes take the cost pro-rata by quantity. A fill larger than the
    open position closes it and opens the leftover at the fill price.
    """
    if not trades:
        return Decimal("0")
    ordered = sorted(trades, key=lambda row: str(row.get("ts") or ""))
    position = Decimal("0")
    avg = Decimal("0")
    realized = Decimal("0")
    mult = dec(multiplier, default="1")

    for row in ordered:
        qty = _signed_qty(row.get("side"), row.get("qty"))
        if qty == 0:
            continue
        price = dec(row.get("price"))
        fee = dec(row.get("fee"))
        kind = _kind(row.get("kind"))
        closing = kind in {"close", "exercise", "expire"} or (
            position != 0 and (position > 0) != (qty > 0)
        )
        if not closing or position == 0:
            new_pos = position + qty
            if new_pos != 0 and (position == 0 or (position > 0) == (qty > 0)):
                avg = (avg * abs(position) + price * abs(qty)) / abs(new_pos)
            elif new_pos == 0:
                avg = Decimal("0")
            position = new_pos
            realized -= fee
            continue

        closing_qty = min(abs(qty), abs(position))
        sign = Decimal("1") if position > 0 else Decimal("-1")
        slice_fee = fee * (closing_qty / abs(qty)) if abs(qty) else Decimal("0")
        cash = _cash(row.get("side"), closing_qty, price, slice_fee, mult)
        # Cost basis carries the position's sign: long premium is positive
        # (a cost to recover), short premium is negative (income already booked).
        cost_basis = sign * avg * closing_qty * mult
        realized += cash - cost_basis
        position = position - sign * closing_qty
        leftover = abs(qty) - closing_qty
        if position == 0:
            avg = price if leftover else Decimal("0")
            position = (Decimal("1") if qty > 0 else Decimal("-1")) * leftover
        elif leftover:
            position += (Decimal("1") if qty > 0 else Decimal("-1")) * leftover
    return realized


def holding_days(opened_at: object, asof: object) -> Decimal | None:
    """Whole-and-fractional days between open and as-of. None when unknown or ≤ 0."""
    start = _parse_dt(opened_at)
    end = _parse_dt(asof)
    if start is None or end is None:
        return None
    seconds = Decimal(str((end - start).total_seconds()))
    if seconds <= 0:
        return None
    return seconds / Decimal(86400)


def _parse_dt(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if value is None or value == "":
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def average_capital(samples: list[object] | None) -> Decimal | None:
    """Mean of capital samples. ``None`` (not zero) when there is nothing to average.

    A stored zero is a real sample (occupied capital really was 0). An empty
    list is "no observation" and stays ``None`` so APR can fall back to spot.
    """
    if not samples:
        return None
    total = sum((dec(item) for item in samples), Decimal("0"))
    return total / Decimal(len(samples))


def option_apr(
    total_pnl: object,
    avg_capital: object,
    days: object,
) -> Decimal | None:
    """Total PnL / average capital / days × 365. ``None`` when capital or days ≤ 0."""
    capital = dec(avg_capital)
    held = dec(days)
    if capital <= 0 or held <= 0:
        return None
    return dec(total_pnl) / capital / held * _DAYS


def capital_of(position: dict[str, Any]) -> Decimal:
    """Margin plus the buyer's premium (the cash a long actually posted).

    Short premium is income, not capital, so only the positive part counts.
    """
    margin = dec(position.get("margin_usd"))
    premium = dec(position.get("premium_usd"))
    buyer = premium if premium > 0 else Decimal("0")
    return margin + buyer


def value_position(
    position: dict[str, Any],
    *,
    mark_price: object | None = None,
    trades: list[dict[str, Any]] | None = None,
    capital_samples: list[object] | None = None,
    asof: object | None = None,
) -> dict[str, Any]:
    """Fill mark value, premium, unrealized PnL, and APR on one position.

    Does not mutate ``position``. ``mark_price`` overrides ``position['mark_price']``.
    Multiplier defaults to 1 (Deribit inverse: size is already coin; linear USDC:
    size is contracts and the multiplier is 1 USDC per quoted mark).

    APR uses ``capital_samples`` when given, otherwise the spot capital
    (margin + buyer premium). Occupied capital of 0 → APR ``None``.
    """
    size = dec(position.get("size"))
    multiplier = dec(position.get("multiplier"), default="1")
    if multiplier == 0:
        multiplier = Decimal("1")
    avg = dec(position.get("avg_price"))
    mark = dec(mark_price if mark_price is not None else position.get("mark_price"))
    mark_usd = mark_value(size, mark, multiplier)
    premium = premium_cost(size, avg, multiplier)
    unreal = unrealized_pnl(mark_usd, premium)
    realized = realized_pnl(trades if trades is not None else position.get("trades"), multiplier=multiplier)
    total = unreal + realized
    samples = capital_samples if capital_samples is not None else position.get("capital_samples")
    averaged = average_capital(samples)
    capital = averaged if averaged is not None else capital_of(
        {"margin_usd": position.get("margin_usd"), "premium_usd": premium}
    )
    days = holding_days(position.get("opened_at"), asof if asof is not None else position.get("asof"))
    apr = option_apr(total, capital, days if days is not None else 0)
    return {
        "size": fnum(size),
        "multiplier": fnum(multiplier),
        "avg_price_usd": fnum(avg),
        "mark_price_usd": fnum(mark),
        "mark_value_usd": fnum(mark_usd),
        "premium_usd": fnum(premium),
        "unrealized_pnl_usd": fnum(unreal),
        "realized_pnl_usd": fnum(realized),
        "pnl_usd": fnum(total),
        "capital_usd": fnum(capital),
        "holding_days": fnum(days) if days is not None else None,
        "pnl_apr": fnum(apr) if apr is not None else None,
    }


def summarize(valued: list[dict[str, Any]]) -> dict[str, Any]:
    """Book-level totals. APR is None when the summed capital is 0.

    Book APR weights by each row's capital and holding days is *not* reused:
    it annualizes the summed PnL against the summed capital and the
    capital-weighted average holding period. A row with no APR (zero capital)
    still contributes its PnL but not its (zero) capital.
    """
    mark = Decimal("0")
    premium = Decimal("0")
    unreal = Decimal("0")
    realized = Decimal("0")
    capital = Decimal("0")
    weighted_days = Decimal("0")
    for row in valued:
        mark += dec(row.get("mark_value_usd"))
        premium += dec(row.get("premium_usd"))
        unreal += dec(row.get("unrealized_pnl_usd"))
        realized += dec(row.get("realized_pnl_usd"))
        row_capital = dec(row.get("capital_usd"))
        capital += row_capital
        days = row.get("holding_days")
        if days is not None and row_capital > 0:
            weighted_days += row_capital * dec(days)
    total = unreal + realized
    days = (weighted_days / capital) if capital > 0 else None
    apr = option_apr(total, capital, days if days is not None else 0)
    return {
        "total_mark_value_usd": fnum(mark),
        "total_premium_usd": fnum(premium),
        "unrealized_pnl_usd": fnum(unreal),
        "realized_pnl_usd": fnum(realized),
        "pnl_usd": fnum(total),
        "capital_usd": fnum(capital),
        "pnl_apr": fnum(apr) if apr is not None else None,
    }
