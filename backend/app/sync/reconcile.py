"""Automatic NAV reconciliation after each sync snapshot (phase 6).

The check is read-only with respect to the ledger, snapshots and positions. It
writes only ``sync_reconcile_log`` (and, on a hard failure, one queued WeChat
notice through the existing alert machinery).

Identity (same as ``app.nav_reconcile``)::

    uncovered holdings + open position net = snapshot NAV

where ``uncovered = flat_total - covered_total``. Two extra accounting checks
do not rely on the residual alone:

* every ``covered_by_position`` holding must still match an open position leg
  (otherwise a receipt is covered without a position to replace it → double
  count), and
* a debt-token holding (``variableDebt*`` / ``stableDebt*``) must be covered;
  leaving it in the flat sum counts debt as an asset.

Failed sources (e.g. Jupiter 429) are informational. Their last values are
carried into the snapshot (exchange carry / open positions kept open), so they
are listed in ``detail.failed_sources`` but never by themselves change the
status.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Position, Snapshot, SyncReconcileLog
from ..nav_reconcile import reconcile_nav
from ..positions_core import _covered_symbol

logger = logging.getLogger(__name__)

# Threshold: max(1 USDT, 0.05% of NAV) → warn; 5× that → fail.
RECONCILE_MIN_TOLERANCE_USDT = Decimal("1")
RECONCILE_TOLERANCE_RATIO = Decimal("0.0005")
RECONCILE_FAIL_MULTIPLIER = Decimal("5")
_CENT = Decimal("0.01")


def reconcile_tolerance(nav_usdt: Decimal) -> Decimal:
    """Absolute residual band for one NAV, rounded to cents."""
    floor = RECONCILE_MIN_TOLERANCE_USDT
    scaled = (abs(nav_usdt) * RECONCILE_TOLERANCE_RATIO).quantize(_CENT)
    return max(floor, scaled)


def _position_rows(db: Session, user_id: int) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(Position).where(Position.user_id == user_id, Position.status == "open")
    ).all()
    return [
        {
            "protocol": row.protocol,
            "status": row.status,
            "net_value_usdt": str(row.net_value_usdt),
            "label": row.label,
            "market_ref": row.market_ref,
            "asof": row.asof.isoformat() if row.asof is not None else None,
            "details": row.details if isinstance(row.details, dict) else None,
        }
        for row in rows
    ]


def _covered_legs(positions: list[dict[str, Any]]) -> tuple[set[str], set[str]]:
    symbols: set[str] = set()
    contracts: set[str] = set()
    for position in positions:
        if str(position.get("status") or "open") != "open":
            continue
        details = position.get("details")
        if not isinstance(details, dict):
            continue
        for key in ("collateral", "borrow"):
            legs = details.get(key)
            if not isinstance(legs, list):
                continue
            for leg in legs:
                if not isinstance(leg, dict):
                    continue
                symbol = str(leg.get("symbol") or "").strip()
                if not symbol or not _covered_symbol(symbol):
                    continue
                symbols.add(symbol.lower())
                contract = str(leg.get("contract") or "").strip().lower()
                if contract:
                    contracts.add(contract)
    return symbols, contracts


def _holding_checks(
    breakdown: dict[str, Any], positions: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return ``(covered_orphans, uncovered_debt_tokens)``."""
    holdings = breakdown.get("holdings") if isinstance(breakdown.get("holdings"), list) else []
    symbols, contracts = _covered_legs(positions)
    orphans: list[dict[str, Any]] = []
    debt_tokens: list[dict[str, Any]] = []
    for raw in holdings:
        if not isinstance(raw, dict):
            continue
        symbol = str(raw.get("symbol") or raw.get("asset") or "").strip()
        contract = str(raw.get("contract") or "").strip().lower()
        value = str(raw.get("value_usdt") or "0")
        covered = bool(raw.get("covered_by_position"))
        if covered:
            matched = (bool(contract) and contract in contracts) or (
                bool(symbol) and symbol.lower() in symbols
            )
            if not matched:
                orphans.append(
                    {"asset": symbol, "contract": contract or None, "value_usdt": value}
                )
        elif symbol.lower().startswith(("variabledebt", "stabledebt")):
            try:
                amount = abs(Decimal(str(value)))
            except Exception:  # noqa: BLE001 — a bad value must not break the check
                amount = Decimal("0")
            if amount > Decimal("0.004"):
                debt_tokens.append({"asset": symbol, "value_usdt": value})
    return orphans, debt_tokens


def _stale_positions(positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Open rows carried from an earlier sync because their source failed."""
    stale: list[dict[str, Any]] = []
    for position in positions:
        details = position.get("details")
        if not isinstance(details, dict) or details.get("stale") is not True:
            continue
        stale.append(
            {
                "protocol": str(position.get("protocol") or ""),
                "label": position.get("label"),
                "market_ref": position.get("market_ref"),
                "asof": position.get("asof"),
                "stale_since": details.get("stale_since"),
            }
        )
    stale.sort(key=lambda item: (item["protocol"], str(item["market_ref"] or "")))
    return stale


def _breakdown_dict(breakdown: object) -> dict[str, Any]:
    if isinstance(breakdown, dict):
        return breakdown
    if not breakdown:
        return {}
    try:
        parsed = json.loads(str(breakdown))
    except Exception:  # noqa: BLE001
        return {}
    return parsed if isinstance(parsed, dict) else {}


def build_reconcile(
    *,
    nav_usdt: object,
    breakdown: object,
    positions: list[dict[str, Any]],
    failed_sources: list[str] | tuple[str, ...] = (),
    now: datetime | None = None,
) -> dict[str, Any]:
    """Pure calculation for one snapshot. Returns status, money legs and detail."""
    core = reconcile_nav(nav_usdt=nav_usdt, breakdown=breakdown, positions=positions)
    nav = Decimal(core["nav_usdt"])
    tolerance = reconcile_tolerance(nav)
    fail_threshold = (tolerance * RECONCILE_FAIL_MULTIPLIER).quantize(_CENT)
    residual = Decimal(core["residual_usdt"])
    payload = _breakdown_dict(breakdown)
    orphans, debt_tokens = _holding_checks(payload, positions)

    # A user with no open positions has nothing to fold; older snapshots leave
    # ``positions_included`` unset in that case and are still checkable because
    # NAV must equal the flat sum. With positions present, a missing flag is a
    # real structural failure.
    folded = bool(core["folded"]) or (
        not positions and int(core["covered_rows"]) == 0
    )
    if not folded:
        status = "fail"
        reason = "snapshot_not_folded"
    elif orphans:
        status = "fail"
        reason = "covered_holding_without_position"
    elif debt_tokens:
        status = "fail"
        reason = "debt_token_in_flat_holdings"
    elif abs(residual) > fail_threshold:
        status = "fail"
        reason = "residual_above_fail_threshold"
    elif abs(residual) > tolerance:
        status = "warn"
        reason = "residual_above_threshold"
    else:
        status = "ok"
        reason = "within_tolerance"

    sources = list(dict.fromkeys(str(item) for item in failed_sources if item))
    stale_positions = _stale_positions(positions)
    residual_pct = None
    if nav:
        residual_pct = str((residual / nav * 100).quantize(Decimal("0.0001")))
    detail: dict[str, Any] = {
        "reason": reason,
        "folded": folded,
        "open_positions": int(core["open_positions"]),
        "covered_rows": int(core["covered_rows"]),
        "threshold_usdt": f"{tolerance:f}",
        "fail_threshold_usdt": f"{fail_threshold:f}",
        "residual_pct_nav": residual_pct,
        "failed_sources": sources,
        "partial": bool(sources),
        "stale_positions": stale_positions,
        "stale_count": len(stale_positions),
        "stale_sources": sorted({item["protocol"] for item in stale_positions}),
        "carry_forward": bool(sources),
        "carry_forward_affects_nav": False,
        "carry_forward_nav_note": (
            "本次未更新的来源沿用上次值：交易所按上次快照回填，借贷仓位保留上次 open 行；"
            "这些金额仍计入 NAV，所以不构成本次 residual 差额，但该来源的市值可能滞后。"
            if sources
            else "无失败来源，全部来源本次均已更新。"
        ),
        "checks": {
            "covered_accounted": not orphans,
            "debt_accounted": not debt_tokens,
            "covered_orphans": orphans,
            "uncovered_debt_tokens": debt_tokens,
        },
    }
    return {
        "status": status,
        "reason": reason,
        "nav_usdt": nav,
        "flat_total_usdt": Decimal(core["flat_holdings_usdt"]),
        "covered_total_usdt": Decimal(core["covered_usdt"]),
        "positions_net_usdt": Decimal(core["open_position_net_usdt"]),
        "residual_usdt": residual,
        "tolerance_usdt": tolerance,
        "detail": detail,
    }


def _previous_freshness_state(db: Session, user_id: int) -> dict:
    """Alert identity (first_seen/event_id) is carried in the last log detail."""
    row = db.scalar(
        select(SyncReconcileLog)
        .where(SyncReconcileLog.user_id == user_id)
        .order_by(SyncReconcileLog.ts.desc(), SyncReconcileLog.id.desc())
        .limit(1)
    )
    if row is None or not isinstance(row.detail, dict):
        return {}
    state = row.detail.get("data_freshness")
    return state if isinstance(state, dict) else {}


def record_sync_reconcile(
    db: Session,
    *,
    user_id: int,
    snapshot: Snapshot,
    failed_sources: list[str] | tuple[str, ...] = (),
    now: datetime | None = None,
) -> SyncReconcileLog:
    """Check ``snapshot`` and persist one read-only log row.

    Commits the log itself: the sync has already committed transfers/snapshot,
    and a reconcile problem must never roll them back. On ``fail`` one alert is
    queued through the existing WeChat queue (subject to the rule cooldown).
    """
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    positions = _position_rows(db, user_id)
    result = build_reconcile(
        nav_usdt=snapshot.nav_usdt,
        breakdown=snapshot.breakdown,
        positions=positions,
        failed_sources=failed_sources,
        now=moment,
    )
    detail = dict(result["detail"])
    previous_state = _previous_freshness_state(db, user_id)
    freshness_state = previous_state
    try:
        from ..alerts import evaluate_data_freshness

        with db.begin_nested():
            freshness_state = evaluate_data_freshness(
                db,
                user_id,
                failed_sources=detail.get("failed_sources") or failed_sources,
                stale_positions=detail.get("stale_positions") or (),
                previous_state=previous_state,
                now=moment,
            )
    except Exception:  # noqa: BLE001 — stale notice must not lose the log
        logger.exception("data freshness alert failed user=%s", user_id)
        freshness_state = dict(previous_state or {})
        freshness_state["error"] = True
    detail["data_freshness"] = freshness_state
    alert_state = "not_needed"
    alert_event_id: int | None = None
    if result["status"] == "fail":
        try:
            from ..alerts import enqueue_reconcile_failure

            with db.begin_nested():
                event = enqueue_reconcile_failure(
                    db,
                    user_id=user_id,
                    nav_usdt=result["nav_usdt"],
                    residual_usdt=result["residual_usdt"],
                    tolerance_usdt=result["tolerance_usdt"],
                    now=moment,
                )
            if event is None:
                alert_state = "suppressed_cooldown"
            else:
                alert_state = "queued"
                alert_event_id = event.id
        except Exception:  # noqa: BLE001 — alert bookkeeping must not lose the log
            logger.exception("reconcile alert enqueue failed user=%s", user_id)
            alert_state = "error"
    detail["alert"] = {"status": alert_state, "event_id": alert_event_id}

    row = SyncReconcileLog(
        user_id=user_id,
        snapshot_id=snapshot.id,
        ts=snapshot.ts,
        nav_usdt=result["nav_usdt"],
        flat_total_usdt=result["flat_total_usdt"],
        covered_total_usdt=result["covered_total_usdt"],
        positions_net_usdt=result["positions_net_usdt"],
        residual_usdt=result["residual_usdt"],
        tolerance_usdt=result["tolerance_usdt"],
        status=result["status"],
        detail=detail,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    logger.info(
        "reconcile user=%s status=%s nav=%s residual=%s threshold=%s failed_sources=%s",
        user_id,
        row.status,
        row.nav_usdt,
        row.residual_usdt,
        row.tolerance_usdt,
        ",".join(detail["failed_sources"]) if detail["failed_sources"] else "-",
    )
    return row


def latest_reconcile_status(db: Session, user_id: int) -> str | None:
    """Newest status for one user, or ``None`` before the first sync snapshot."""
    return db.scalar(
        select(SyncReconcileLog.status)
        .where(SyncReconcileLog.user_id == user_id)
        .order_by(SyncReconcileLog.id.desc())
        .limit(1)
    )
