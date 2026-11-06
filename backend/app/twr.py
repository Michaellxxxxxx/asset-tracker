from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import LedgerEntry, User
from .provenance import data_provenance, snapshot_live
from .schemas import ReturnPoint, ReturnsOut
from .snapshots import select_nav_snapshots

ReturnPeriod = Literal["day", "week", "month", "year"]

PERIOD_DAYS: dict[str, int] = {
    "day": 1,
    "week": 7,
    "month": 30,
    "year": 365,
}


_SHANGHAI = timezone(timedelta(hours=8))


def returns_start_utc(db: Session, user_id: int) -> tuple[date | None, datetime | None]:
    """The user's baseline date and its 00:00 Asia/Shanghai instant in UTC."""
    start_date = db.scalar(select(User.returns_start_date).where(User.id == user_id))
    if start_date is None:
        return None, None
    midnight = datetime(start_date.year, start_date.month, start_date.day, tzinfo=_SHANGHAI)
    return start_date, midnight.astimezone(timezone.utc)


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


# Kept for callers that imported the private alias.
_as_utc = as_utc


# External cash flows. airdrop / kol_reward are income and stay inside the return.
EXTERNAL_FLOW_KINDS = ("deposit", "withdraw", "transfer")


def positive_nav_snapshot(snapshot: object) -> bool:
    """A NAV at or below zero cannot anchor a time-weighted return.

    Bad historical rows (including old negative backfill) stay in the database;
    this filter only keeps them out of return and curve calculations.
    """
    try:
        return Decimal(str(snapshot.nav_usdt)) > 0
    except (AttributeError, InvalidOperation, TypeError, ValueError):
        return False


def _breakdown_shape(snapshot: object) -> tuple[frozenset[str], bool] | None:
    """Return (holding sources, positions folded) for one snapshot.

    ``None`` means the row predates the breakdown or carries no usable JSON, so
    the caller must not infer a coverage change from it.
    """
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
    sources = frozenset(
        str(item["source"])
        for item in holdings
        if isinstance(item, dict) and item.get("source")
    )
    return sources, bool(payload.get("positions_included"))


def _coverage_changed(previous: object, current: object) -> bool:
    """True when the NAV level changed because the measured portfolio changed.

    Two cases are not market returns: a source (new wallet/exchange) enters or
    leaves the snapshot, or the position-fold valuation basis is switched on or
    off. The whole NAV step is then treated as an external flow instead of a
    return. Ordinary price moves never change the source set or the fold flag.
    """
    before = _breakdown_shape(previous)
    after = _breakdown_shape(current)
    if before is None or after is None:
        return False
    return before[0] != after[0] or before[1] != after[1]


def _signed_flow(entry: LedgerEntry) -> Decimal:
    """Positive = money in. deposit/in-transfer add; withdraw/out-transfer subtract.

    Phase-1 rows store withdraw amounts already signed negative, so abs() keeps
    that seed path stable. Phase-2 transfer rows are signed by direction:
    a positive amount is an inflow, a negative amount is an outflow.
    """
    amount = Decimal(entry.amount_usdt)
    if entry.kind == "deposit":
        return abs(amount)
    if entry.kind == "withdraw":
        return -abs(amount)
    if entry.kind == "transfer":
        return amount
    return Decimal("0")


def _excluded_flow_kind(cash_flow: Decimal, coverage_flow: Decimal) -> str:
    """Which copy the UI may use for the excluded-flow readout.

    ``coverage`` must never be labelled as a deposit: it is existing assets
    entering the measured universe (new source / position-fold switch), not new
    money. Only a cash-only window may say "充值"; a window with both uses the
    neutral "净流入".
    """
    has_cash = cash_flow != 0
    has_coverage = coverage_flow != 0
    if has_cash and has_coverage:
        return "mixed"
    if has_coverage:
        return "coverage"
    if has_cash:
        return "cash"
    return "none"


@dataclass(frozen=True)
class ReturnStep:
    """One snapshot interval, with the flow split used by the UI readouts."""

    previous: object
    current: object
    previous_nav: Decimal
    current_nav: Decimal
    cash_flow: Decimal
    coverage_flow: Decimal
    interval_flow: Decimal
    sub_return: Decimal
    cumulative_before: Decimal
    cumulative_after: Decimal
    coverage_changed: bool


@dataclass
class ReturnChain:
    """The shared window + chained-interval result behind every returns view."""

    period: ReturnPeriod
    nav_source: str
    start_date: date | None
    start_floor: datetime | None
    snapshots: list
    window: list
    latest: object | None
    start: object | None
    start_nav: Decimal | None
    provenance: object | None
    live: object | None
    steps: list[ReturnStep]
    cumulative: Decimal
    total_cash_flow: Decimal
    total_coverage_flow: Decimal
    clamped: bool
    empty: bool
    single: bool


def build_return_chain(db: Session, user_id: int, period: ReturnPeriod) -> ReturnChain:
    """Select the window and chain-link its intervals exactly as the API reports.

    ``calculate_returns`` and the p26 breakdown both consume this one loop, so
    the segments and assets can never use a different baseline, flow cut, or
    coverage rule than the headline return.
    """
    snapshots, nav_source = select_nav_snapshots(db, user_id)
    start_date, start_floor = returns_start_utc(db, user_id)
    if start_floor is not None:
        # Snapshots before the baseline date never take part; the NAV curve elsewhere keeps them.
        snapshots = [s for s in snapshots if _as_utc(s.ts) >= start_floor]

    snapshots = [snapshot for snapshot in snapshots if positive_nav_snapshot(snapshot)]

    if not snapshots:
        return ReturnChain(
            period=period,
            nav_source=nav_source,
            start_date=start_date,
            start_floor=start_floor,
            snapshots=[],
            window=[],
            latest=None,
            start=None,
            start_nav=None,
            provenance=None,
            live=None,
            steps=[],
            cumulative=Decimal("1"),
            total_cash_flow=Decimal("0"),
            total_coverage_flow=Decimal("0"),
            clamped=False,
            empty=True,
            single=False,
        )

    latest = snapshots[-1]
    provenance = data_provenance(latest)
    live = snapshot_live(latest)
    cutoff = _as_utc(latest.ts) - timedelta(days=PERIOD_DAYS[period])
    # The requested window reaches before the first eligible snapshot: it is clamped.
    clamped = start_floor is not None and cutoff < _as_utc(snapshots[0].ts)

    # Use the last snapshot at or before the cutoff as the baseline. With daily
    # demo data this is exact for day/week/month/year windows.
    baseline = snapshots[0]
    for snapshot in snapshots:
        if _as_utc(snapshot.ts) <= cutoff:
            baseline = snapshot
        else:
            break

    window = [s for s in snapshots if _as_utc(s.ts) >= _as_utc(baseline.ts)]
    start = window[0]
    start_nav = Decimal(start.nav_usdt)

    flows = list(
        db.scalars(
            select(LedgerEntry)
            .where(
                LedgerEntry.user_id == user_id,
                LedgerEntry.kind.in_(EXTERNAL_FLOW_KINDS),
                LedgerEntry.ts > start.ts,
                LedgerEntry.ts <= latest.ts,
            )
            .order_by(LedgerEntry.ts.asc())
        ).all()
    )

    cumulative = Decimal("1")
    steps: list[ReturnStep] = []
    flow_index = 0
    total_cash_flow = Decimal("0")
    total_coverage_flow = Decimal("0")

    for previous, current in zip(window, window[1:]):
        previous_nav = Decimal(previous.nav_usdt)
        current_nav = Decimal(current.nav_usdt)

        interval_flow = Decimal("0")
        while flow_index < len(flows) and flows[flow_index].ts <= current.ts:
            flow = flows[flow_index]
            if flow.ts > previous.ts:
                interval_flow += _signed_flow(flow)
            flow_index += 1

        if previous_nav <= 0:
            continue

        coverage_changed = _coverage_changed(previous, current)
        cash_flow = Decimal("0")
        coverage_flow = Decimal("0")
        if coverage_changed:
            # Onboarding a source or switching the position-fold basis moves the
            # NAV level without any market gain; treat the whole step as a flow.
            # Any ledger cash inside the same interval is already inside that
            # step, so split it out instead of double counting: coverage is the
            # pure basis change, cash stays cash, and both still count once.
            cash_flow = interval_flow
            interval_flow = current_nav - previous_nav
            total_cash_flow += cash_flow
            total_coverage_flow += interval_flow - cash_flow
        else:
            total_cash_flow += interval_flow

        sub_return = (current_nav - interval_flow) / previous_nav - Decimal("1")
        cumulative_before = cumulative
        cumulative *= Decimal("1") + sub_return
        steps.append(
            ReturnStep(
                previous=previous,
                current=current,
                previous_nav=previous_nav,
                current_nav=current_nav,
                cash_flow=cash_flow,
                coverage_flow=coverage_flow,
                interval_flow=interval_flow,
                sub_return=sub_return,
                cumulative_before=cumulative_before,
                cumulative_after=cumulative,
                coverage_changed=coverage_changed,
            )
        )

    return ReturnChain(
        period=period,
        nav_source=nav_source,
        start_date=start_date,
        start_floor=start_floor,
        snapshots=snapshots,
        window=window,
        latest=latest,
        start=start,
        start_nav=start_nav,
        provenance=provenance,
        live=live,
        steps=steps,
        cumulative=cumulative,
        total_cash_flow=total_cash_flow,
        total_coverage_flow=total_coverage_flow,
        clamped=clamped,
        empty=False,
        single=len(window) == 1,
    )


def calculate_returns(db: Session, user_id: int, period: ReturnPeriod) -> ReturnsOut:
    """Chain-link sub-period returns, removing external deposits/withdrawals.

    Each snapshot interval uses:
        r = (ending_nav - net_external_flow) / starting_nav - 1

    Ledger flows are treated as end-of-interval cash movements, which matches
    the seeded daily ledger. A source-set or position-fold change is a coverage
    change rather than performance, so its NAV step is also booked as a flow.
    Non-positive NAV rows are ignored here (never deleted) because they cannot
    anchor a ratio. Period windows are anchored to the latest snapshot so the
    demo data stays meaningful regardless of the wall-clock date.
    """
    chain = build_return_chain(db, user_id, period)
    extra = {
        "returns_start": chain.start_date,
        "window_clamped": chain.clamped,
        "effective_start_ts": chain.start.ts if chain.clamped and chain.start else None,
    }

    if chain.empty:
        return ReturnsOut(
            period=period,
            return_amount_usdt=0.0,
            return_rate=0.0,
            series=[],
            nav_source=chain.nav_source,
            provenance=None,
            live=None,
            latest_snapshot_ts=None,
            returns_start=chain.start_date,
        )

    start = chain.start
    latest = chain.latest
    assert start is not None and latest is not None
    series = [ReturnPoint(ts=start.ts, return_rate=0.0)]
    series.extend(
        ReturnPoint(
            ts=step.current.ts,
            return_rate=round(float(step.cumulative_after - Decimal("1")), 6),
        )
        for step in chain.steps
    )

    if chain.single:
        return ReturnsOut(
            period=period,
            return_amount_usdt=0.0,
            return_rate=0.0,
            series=series,
            nav_source=chain.nav_source,
            provenance=chain.provenance,
            live=chain.live,
            latest_snapshot_ts=latest.ts,
            **extra,
        )

    # Time-weighted return expressed in starting NAV units. This stays
    # consistent with the chained rate even when several external flows land at
    # different NAV levels (the old end-start-flow amount did not).
    return_amount = (chain.start_nav or Decimal("0")) * (chain.cumulative - Decimal("1"))
    excluded_flow = chain.total_cash_flow + chain.total_coverage_flow

    return ReturnsOut(
        period=period,
        return_amount_usdt=round(float(return_amount), 2),
        return_rate=round(float(chain.cumulative - Decimal("1")), 6),
        excluded_flow_usdt=round(float(excluded_flow), 2),
        excluded_cash_flow_usdt=round(float(chain.total_cash_flow), 2),
        excluded_coverage_flow_usdt=round(float(chain.total_coverage_flow), 2),
        excluded_flow_kind=_excluded_flow_kind(chain.total_cash_flow, chain.total_coverage_flow),
        series=series,
        nav_source=chain.nav_source,
        provenance=chain.provenance,
        live=chain.live,
        latest_snapshot_ts=latest.ts,
        **extra,
    )
