from __future__ import annotations

import json

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..db import get_db
from ..integrations.wechat.health import send_health_status
from ..integrations.wechat.status import token_status
from ..models import SyncReconcileLog, User
from ..portfolio_service import portfolio_context
from ..provenance import data_provenance, snapshot_live
from ..schemas import HoldingOut, MiscSummaryOut, OverviewOut, SnapshotPoint
from ..twr import _as_utc, positive_nav_snapshot, returns_start_utc

router = APIRouter(tags=["overview"])


def _holding_value(item: dict) -> float:
    try:
        return float(item.get("value_usdt") or 0)
    except (TypeError, ValueError):
        return 0.0


def misc_summary(holdings: list[dict], threshold: float) -> MiscSummaryOut:
    """Fold assets into 杂项 after merging the same symbol across sources.

    Case-insensitive symbol. An asset is misc when it is unpriced or its priced
    value is strictly below ``threshold``. ``total_usdt`` sums priced value only.
    """
    buckets: dict[str, dict] = {}
    for item in holdings:
        symbol = str(item.get("asset") or item.get("symbol") or "").strip()
        if not symbol:
            continue
        key = symbol.casefold()
        bucket = buckets.get(key)
        if bucket is None:
            bucket = {"priced_value": 0.0, "any_priced": False, "any_unpriced": False}
            buckets[key] = bucket
        priced = item.get("priced")
        if priced is None:
            priced = item.get("price_usdt") is not None
        if priced:
            bucket["any_priced"] = True
            bucket["priced_value"] += _holding_value(item)
        else:
            bucket["any_unpriced"] = True

    count = 0
    total = 0.0
    for bucket in buckets.values():
        # Mixed priced + unpriced rows of one symbol follow the priced total.
        if bucket["any_priced"]:
            if bucket["priced_value"] < threshold:
                count += 1
                total += bucket["priced_value"]
        elif bucket["any_unpriced"]:
            count += 1
    return MiscSummaryOut(count=count, total_usdt=total)


def _mark_covered(holdings: list[dict], positions: list[dict]) -> list[dict]:
    """Deprecated shim kept for callers/tests that imported it."""
    from ..positions_core import mark_covered_by_position

    return mark_covered_by_position(holdings, positions)


def _positions_folded(snapshot) -> bool:
    raw = getattr(snapshot, "breakdown", None)
    if not raw:
        return False
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return False
    return bool(isinstance(payload, dict) and payload.get("positions_included"))


def partial_snapshot_ids(snapshots) -> set[int]:
    """Read-time only: sync points written without the position fold after
    this user's sync had already started folding positions.

    Such a row (e.g. a snapshot captured mid-sync) carries spot holdings only,
    so its NAV drops by the position net value. Older rows from before the
    fold existed are left alone. Nothing is written back. Returns ``id()`` of
    the row objects.
    """
    ordered = sorted(
        (row for row in snapshots if getattr(row, "source", None) == "sync"),
        key=lambda row: _as_utc(row.ts),
    )
    seen_fold = False
    partial: set[int] = set()
    for row in ordered:
        if _positions_folded(row):
            seen_fold = True
        elif seen_fold:
            partial.add(id(row))
    return partial


@router.get("/overview", response_model=OverviewOut)
def overview(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> OverviewOut:
    context = portfolio_context(db, current_user.id)
    snapshots = context["snapshots"]
    nav_source = context["nav_source"]
    current_nav = context["current_nav"]
    latest_ts = context["latest_ts"]

    # Real curves (sync / backfill) start at the user's returns baseline.
    # Demo seed stays the full curve. Current NAV still uses the latest sync
    # point even when every snapshot falls before the baseline.
    _, start_floor = returns_start_utc(db, current_user.id)
    # Non-positive rows are data errors, not valuation points: keep them in the
    # database but never plot or calculate with them.
    curve = [row for row in snapshots if positive_nav_snapshot(row)]
    if nav_source != "seed" and start_floor is not None:
        curve = [row for row in curve if _as_utc(row.ts) >= start_floor]

    partial_ids = partial_snapshot_ids(snapshots)
    series = [
        SnapshotPoint(
            ts=snapshot.ts,
            nav_usdt=float(snapshot.nav_usdt),
            provenance=data_provenance(snapshot),
            live=snapshot_live(snapshot),
            partial=id(snapshot) in partial_ids,
        )
        for snapshot in curve
    ]
    marked = context["holdings"]
    holdings = [HoldingOut.model_validate(item) for item in marked]
    threshold = context["threshold"]
    # Additive: null for users who have not synced since phase 6.
    reconcile_status = db.scalar(
        select(SyncReconcileLog.status)
        .where(SyncReconcileLog.user_id == current_user.id)
        .order_by(SyncReconcileLog.id.desc())
        .limit(1)
    )

    # P7a: read-only token freshness for the current user only.
    wechat = token_status(db, user_id=current_user.id)
    # p15b: W1 send-path health, read by the site banner.
    send_health = send_health_status(db)

    return OverviewOut(
        current_nav_usdt=current_nav,
        currency="USDT",
        series=series,
        nav_source=nav_source,
        provenance=context["provenance"],
        live=context["live"],
        latest_snapshot_ts=latest_ts,
        returns_start=current_user.returns_start_date,
        holdings=holdings,
        holdings_asof=context["holdings_asof"],
        positions_net_value_usdt=context["positions_nav"],
        misc_threshold_usdt=threshold,
        misc=misc_summary(marked, threshold),
        reconcile_status=reconcile_status,
        wechat_token_stale=wechat.stale,
        wechat_token_age_hours=wechat.age_hours,
        wechat_send_stale=send_health.stale,
        wechat_send_error=send_health.last_send_error,
        wechat_send_since=send_health.failure_since,
    )
