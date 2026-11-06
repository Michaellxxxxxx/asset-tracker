"""p26: returns segment table + asset price-effect attribution.

Both readouts reuse :func:`app.twr.build_return_chain`, so their window,
baseline, external-flow cut, and coverage-change rule are identical to the
headline time-weighted return. This module is read-only: it never writes a
snapshot, ledger row, or sync state.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Iterable, Literal

from sqlalchemy.orm import Session

from .portfolio_class import ASSET_CLASS_LABELS, classify_holding
from .schemas import (
    BreakdownAssetOut,
    BreakdownSegmentOut,
    BreakdownSegmentsOut,
    BreakdownSumCheckOut,
    BreakdownSumUnitOut,
    ReturnsBreakdownOut,
)
from .twr import ReturnChain, ReturnStep, ReturnPeriod, as_utc, build_return_chain

_SHANGHAI = timezone(timedelta(hours=8))

SegmentUnit = Literal["month", "week", "day"]

# A day/week with no snapshot interval is shown, never filled with zero.
MISSING_NO_SNAPSHOT = "no_snapshot"

UNATTRIBUTED_REASON = (
    "数量变化（利息/质押收益、Pendle LP/YT 内部数量变化、交易手续费）、"
    "无价资产（如 gate:LN、helius:KJLP-*）、覆盖变化区间整体剔除与链上仓位折叠的差额。"
)

_SEGMENT_UNITS: dict[str, tuple[SegmentUnit, ...]] = {
    "year": ("month",),
    "month": ("week", "day"),
    "week": ("day",),
    "day": (),
}


def _decimal(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _value(value: Decimal | None) -> float | None:
    return None if value is None else round(float(value), 4)


def _rate(value: Decimal | float | None) -> float | None:
    return None if value is None else round(float(value), 6)


def _shanghai(value: datetime) -> datetime:
    return as_utc(value).astimezone(_SHANGHAI)


def _local_midnight(value: datetime) -> datetime:
    local = _shanghai(value)
    return datetime(local.year, local.month, local.day, tzinfo=_SHANGHAI)


def _bucket_key(value: datetime, unit: SegmentUnit) -> tuple[str, str]:
    local = _shanghai(value)
    if unit == "month":
        key = f"{local.year:04d}-{local.month:02d}"
        return key, key
    if unit == "week":
        iso = local.isocalendar()
        return f"{iso.year:04d}-W{iso.week:02d}", f"W{iso.week:02d}"
    key = f"{local.year:04d}-{local.month:02d}-{local.day:02d}"
    return key, f"{local.month:02d}-{local.day:02d}"


def _bucket_start(value: date, unit: SegmentUnit) -> date:
    if unit == "month":
        return value.replace(day=1)
    if unit == "week":
        return value - timedelta(days=value.weekday())
    return value


def _next_bucket(value: date, unit: SegmentUnit) -> date:
    if unit == "month":
        if value.month == 12:
            return date(value.year + 1, 1, 1)
        return date(value.year, value.month + 1, 1)
    if unit == "week":
        return value + timedelta(days=7)
    return value + timedelta(days=1)


def _bucket_bounds(start: date, unit: SegmentUnit) -> tuple[datetime, datetime]:
    begin = datetime(start.year, start.month, start.day, tzinfo=_SHANGHAI)
    end_day = _next_bucket(start, unit)
    end = datetime(end_day.year, end_day.month, end_day.day, tzinfo=_SHANGHAI)
    return begin.astimezone(timezone.utc), end.astimezone(timezone.utc)


def _iter_buckets(start: date, end: date, unit: SegmentUnit) -> Iterable[date]:
    cursor = _bucket_start(start, unit)
    last = _bucket_start(end, unit)
    while cursor <= last:
        yield cursor
        cursor = _next_bucket(cursor, unit)


def _segment_rows(chain: ReturnChain, unit: SegmentUnit) -> tuple[list[BreakdownSegmentOut], Decimal, Decimal]:
    """Build one unit's rows plus the exact telescoping sum and rate chain."""
    rows: list[BreakdownSegmentOut] = []
    grouped: dict[str, list[ReturnStep]] = {}
    for step in chain.steps:
        key, _ = _bucket_key(step.current.ts, unit)
        grouped.setdefault(key, []).append(step)

    start_date = _shanghai(chain.window[0].ts).date()
    end_date = _shanghai(chain.window[-1].ts).date()
    nav0 = chain.start_nav or Decimal("0")
    total = Decimal("0")
    product = Decimal("1")

    for bucket_start in _iter_buckets(start_date, end_date, unit):
        key, label = _bucket_key(
            datetime(bucket_start.year, bucket_start.month, bucket_start.day, tzinfo=_SHANGHAI),
            unit,
        )
        bounds_start, bounds_end = _bucket_bounds(bucket_start, unit)
        steps = grouped.get(key, [])
        if not steps:
            rows.append(
                BreakdownSegmentOut(
                    key=key,
                    label=label,
                    start=bounds_start,
                    end=bounds_end,
                    intervals=0,
                    amount_usdt=None,
                    rate=None,
                    missing=MISSING_NO_SNAPSHOT,
                )
            )
            continue

        cum_start = steps[0].cumulative_before
        cum_end = steps[-1].cumulative_after
        amount = nav0 * (cum_end - cum_start)
        rate = (cum_end / cum_start - Decimal("1")) if cum_start != 0 else Decimal("0")
        total += amount
        product *= Decimal("1") + rate
        rows.append(
            BreakdownSegmentOut(
                key=key,
                label=label,
                start=bounds_start,
                end=bounds_end,
                intervals=len(steps),
                amount_usdt=_value(amount),
                rate=_rate(rate),
            )
        )
    return rows, total, product - Decimal("1")


def _holdings(snapshot: object) -> list[dict] | None:
    raw = getattr(snapshot, "breakdown", None)
    if not raw:
        return None
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    holdings = payload.get("holdings")
    if not isinstance(holdings, list):
        return None
    return [item for item in holdings if isinstance(item, dict)]


def _merge_priced(holdings: list[dict] | None) -> dict[tuple[str, str], tuple[Decimal, Decimal]]:
    """(source, asset) -> (amount, value-weighted price) for priced rows only."""
    merged: dict[tuple[str, str], list[Decimal]] = {}
    for item in holdings or []:
        if not item.get("priced"):
            continue
        source = str(item.get("source") or "").strip()
        asset = str(item.get("asset") or "").strip()
        if not asset:
            continue
        amount = _decimal(item.get("amount"))
        price = _decimal(item.get("price_usdt"))
        value = _decimal(item.get("value_usdt"))
        if amount is None or price is None:
            continue
        if value is None:
            value = amount * price
        key = (source, asset)
        bucket = merged.setdefault(key, [Decimal("0"), Decimal("0"), Decimal("0")])
        bucket[0] += amount
        bucket[1] += value
        bucket[2] += amount * price
    result: dict[tuple[str, str], tuple[Decimal, Decimal]] = {}
    for key, (amount, value, weighted) in merged.items():
        if amount != 0:
            result[key] = (amount, value / amount if value != 0 else weighted / amount)
        else:
            result[key] = (amount, weighted if weighted != 0 else value)
    return result


def _first_values(chain: ReturnChain) -> dict[str, Decimal | None]:
    """The first valuation of each asset symbol anywhere inside the window."""
    first: dict[str, Decimal | None] = {}
    for snapshot in chain.window:
        holdings = _holdings(snapshot)
        if holdings is None:
            continue
        seen: dict[str, Decimal | None] = {}
        for item in holdings:
            asset = str(item.get("asset") or "").strip()
            if not asset:
                continue
            value = _decimal(item.get("value_usdt"))
            if asset not in seen:
                seen[asset] = value
            elif seen[asset] is not None and value is not None:
                seen[asset] += value
        for asset, value in seen.items():
            if asset not in first:
                first[asset] = value
    return first


def _asset_rows(chain: ReturnChain) -> tuple[list[BreakdownAssetOut], Decimal, bool]:
    """Aggregate price effect by symbol, excluding every coverage-change step."""
    effects: dict[str, Decimal] = {}
    sources: dict[str, set[str]] = {}
    for step in chain.steps:
        if step.coverage_changed:
            # The whole NAV step is a basis/source change, not a market move.
            continue
        before = _merge_priced(_holdings(step.previous))
        after = _merge_priced(_holdings(step.current))
        if not before or not after:
            continue
        for (source, asset), (amount_before, price_before) in before.items():
            current = after.get((source, asset))
            if current is None:
                continue
            _, price_after = current
            delta = amount_before * (price_after - price_before)
            effects[asset] = effects.get(asset, Decimal("0")) + delta
            sources.setdefault(asset, set()).add(source)

    first = _first_values(chain)
    rows: list[BreakdownAssetOut] = []
    for asset, amount in effects.items():
        asset_class, _risk, _reason = classify_holding({"asset": asset})
        base = first.get(asset)
        rate = (amount / base) if base is not None and base != 0 else None
        rows.append(
            BreakdownAssetOut(
                asset=asset,
                asset_class=asset_class,
                asset_class_label=ASSET_CLASS_LABELS.get(asset_class, "未分类"),
                amount_usdt=round(float(amount), 4),
                rate=_rate(rate),
                base_value_usdt=_value(base),
                sources=sorted(sources.get(asset, ())),
            )
        )
    rows.sort(key=lambda row: (-abs(row.amount_usdt), row.asset))

    has_holdings = any((_holdings(snapshot) or []) for snapshot in chain.window)
    return rows, sum(effects.values(), Decimal("0")), has_holdings


def _sum_check(
    chain: ReturnChain,
    unit: SegmentUnit,
    exact_sum: Decimal,
    chain_rate: Decimal,
) -> BreakdownSumUnitOut:
    total = (chain.start_nav or Decimal("0")) * (chain.cumulative - Decimal("1"))
    total_rate = chain.cumulative - Decimal("1")
    return BreakdownSumUnitOut(
        unit=unit,
        sum_usdt=round(float(exact_sum), 4),
        total_usdt=round(float(total), 4),
        diff_usdt=round(float(total - exact_sum), 6),
        chain_rate=round(float(chain_rate), 6),
        total_rate=round(float(total_rate), 6),
        rate_diff=round(float(chain_rate - total_rate), 6),
    )


def calculate_returns_breakdown(
    db: Session, user_id: int, period: ReturnPeriod
) -> ReturnsBreakdownOut:
    """Read-only p26 view built from the same chain as ``calculate_returns``."""
    chain = build_return_chain(db, user_id, period)
    total_exact = (chain.start_nav or Decimal("0")) * (chain.cumulative - Decimal("1"))
    sum_check = BreakdownSumCheckOut(total_usdt=round(float(total_exact), 4))

    if chain.empty or chain.single:
        return ReturnsBreakdownOut(
            period=period,
            return_amount_usdt=round(float(total_exact), 2),
            return_rate=round(float(chain.cumulative - Decimal("1")), 6),
            nav0_usdt=round(float(chain.start_nav or 0), 2),
            window_start=chain.window[0].ts if chain.window else None,
            window_end=chain.window[-1].ts if chain.window else None,
            segments=BreakdownSegmentsOut(),
            assets=[],
            assets_missing=bool(chain.window) and not any(
                (_holdings(snapshot) or []) for snapshot in chain.window
            ),
            assets_missing_reason="窗口内没有可用的持仓 breakdown（旧回填）"
            if chain.window
            else "窗口内没有快照",
            unattributed_usdt=None,
            unattributed_reason=UNATTRIBUTED_REASON,
            sum_check=sum_check,
            nav_source=chain.nav_source,
            provenance=chain.provenance,
            live=chain.live,
            latest_snapshot_ts=chain.latest.ts if chain.latest else None,
            returns_start=chain.start_date,
            window_clamped=chain.clamped,
            effective_start_ts=chain.start.ts if chain.clamped and chain.start else None,
        )

    segments = BreakdownSegmentsOut()
    for unit in _SEGMENT_UNITS[period]:
        rows, exact_sum, chain_rate = _segment_rows(chain, unit)
        setattr(segments, unit, rows)
        setattr(sum_check, unit, _sum_check(chain, unit, exact_sum, chain_rate))

    assets, attributed, has_holdings = _asset_rows(chain)
    assets_missing = not has_holdings
    unattributed = None if assets_missing else total_exact - attributed

    return ReturnsBreakdownOut(
        period=period,
        return_amount_usdt=round(float(total_exact), 2),
        return_rate=round(float(chain.cumulative - Decimal("1")), 6),
        nav0_usdt=round(float(chain.start_nav or 0), 2),
        window_start=chain.start.ts if chain.start else None,
        window_end=chain.latest.ts if chain.latest else None,
        segments=segments,
        assets=assets,
        assets_missing=assets_missing,
        assets_missing_reason="窗口内没有可用的持仓 breakdown（旧回填）" if assets_missing else None,
        unattributed_usdt=None if unattributed is None else round(float(unattributed), 4),
        unattributed_reason=UNATTRIBUTED_REASON,
        sum_check=sum_check,
        nav_source=chain.nav_source,
        provenance=chain.provenance,
        live=chain.live,
        latest_snapshot_ts=chain.latest.ts if chain.latest else None,
        returns_start=chain.start_date,
        window_clamped=chain.clamped,
        effective_start_ts=chain.start.ts if chain.clamped and chain.start else None,
    )
