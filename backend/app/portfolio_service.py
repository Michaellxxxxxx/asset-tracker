"""Phase-5 portfolio views: classification context, overrides and aggregation.

All functions are read-only except the override CRUD helpers. Sync code must
never import the write helpers, which keeps manual overrides authoritative.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from .config import settings
from .models import AssetOverride
from .portfolio_class import (
    ASSET_CLASSES,
    ASSET_CLASS_LABELS,
    RISK_LABELS,
    RISK_LEVELS,
    classify_holding,
    holding_key,
    position_key,
    resolve_holding,
    resolve_position,
)
from .positions_core import mark_covered_by_position
from .provenance import data_provenance, snapshot_live
from .snapshots import _sync_breakdown_folded, latest_sync_holdings, select_nav_snapshots

POSITION_COLUMNS = (
    "id, kind, protocol, chain, account_ref, market_ref, label, status, "
    "net_value_usdt, collateral_usdt, debt_usdt, details, provenance, live"
)


def _money(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _parse_details(value: Any) -> dict | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def open_position_rows(db: Session, user_id: int) -> list[dict]:
    """Open positions with every field the classifier needs. SQL keeps the
    running image independent of the phase-4 ORM model."""
    try:
        rows = (
            db.execute(
                text(
                    f"SELECT {POSITION_COLUMNS} FROM positions "
                    "WHERE user_id = :user_id AND status = 'open'"
                ),
                {"user_id": user_id},
            )
            .mappings()
            .all()
        )
    except Exception:
        db.rollback()
        return []
    parsed: list[dict] = []
    for row in rows:
        item = dict(row)
        item["details"] = _parse_details(item.get("details"))
        parsed.append(item)
    return parsed


def load_overrides(db: Session, user_id: int) -> dict[str, AssetOverride]:
    try:
        rows = db.query(AssetOverride).filter(AssetOverride.user_id == user_id).all()
    except Exception:
        db.rollback()
        return {}
    return {row.key: row for row in rows}


def annotate_holdings(
    holdings: list[dict], overrides: dict[str, AssetOverride] | None = None
) -> list[dict]:
    annotated: list[dict] = []
    for item in holdings:
        row = dict(item)
        asset_class, risk, reason, overridden = resolve_holding(row, overrides)
        row.update(
            asset_class=asset_class,
            risk_level=risk,
            risk_reason=reason,
            overridden=overridden,
        )
        annotated.append(row)
    return annotated


def annotate_positions(
    positions: list[dict], overrides: dict[str, AssetOverride] | None = None
) -> list[dict]:
    annotated: list[dict] = []
    for item in positions:
        row = dict(item)
        asset_class, risk, reason, overridden = resolve_position(row, overrides)
        row.update(
            asset_class=asset_class,
            risk_level=risk,
            risk_reason=reason,
            overridden=overridden,
        )
        annotated.append(row)
    return annotated


def portfolio_context(db: Session, user_id: int) -> dict:
    """Everything needed by /overview and the phase-5 portfolio endpoints.

    The NAV rule is the single source of truth: latest selected snapshot plus
    open-position net unless that snapshot already folded positions in.
    """
    snapshots, nav_source = select_nav_snapshots(db, user_id)
    if snapshots:
        current_nav = float(snapshots[-1].nav_usdt)
        latest_ts = snapshots[-1].ts
    else:
        current_nav = 0.0
        latest_ts = None

    positions_raw = open_position_rows(db, user_id)
    positions_nav = sum(_money(row.get("net_value_usdt")) for row in positions_raw)
    holdings_raw, holdings_asof = latest_sync_holdings(db, user_id)
    marked = mark_covered_by_position(holdings_raw, positions_raw)
    latest_source = snapshots[-1].source if snapshots else None
    folded = latest_source == "sync" and _sync_breakdown_folded(db, user_id)
    if positions_nav and not folded:
        current_nav += positions_nav

    overrides = load_overrides(db, user_id)
    return {
        "snapshots": snapshots,
        "nav_source": nav_source,
        "provenance": data_provenance(snapshots[-1]) if snapshots else None,
        "live": snapshot_live(snapshots[-1]) if snapshots else None,
        "current_nav": current_nav,
        "latest_ts": latest_ts,
        "holdings": annotate_holdings(marked, overrides),
        "holdings_asof": holdings_asof,
        "positions": annotate_positions(positions_raw, overrides),
        "positions_nav": positions_nav,
        "folded": folded,
        "overrides": overrides,
        "threshold": float(settings.misc_threshold_usdt),
    }


def _holding_payload(item: dict, threshold: float) -> dict:
    value = _money(item.get("value_usdt"))
    return {
        "key": holding_key(item),
        "name": str(item.get("asset") or item.get("symbol") or "未命名资产"),
        "source": str(item.get("source") or "unknown"),
        "kind": "holding",
        "asset_class": item["asset_class"],
        "risk_level": item["risk_level"],
        "risk_reason": item["risk_reason"],
        "overridden": bool(item.get("overridden")),
        "value_usdt": value,
        "misc": value < threshold,
        "chain": item.get("chain"),
        "contract": item.get("contract"),
        "position_id": None,
    }


def _position_payload(item: dict) -> dict:
    return {
        "key": position_key(item),
        "name": str(
            item.get("label")
            or item.get("market_ref")
            or item.get("protocol")
            or "未命名仓位"
        ),
        "source": str(item.get("protocol") or "unknown"),
        "kind": "position",
        "asset_class": item["asset_class"],
        "risk_level": item["risk_level"],
        "risk_reason": item["risk_reason"],
        "overridden": bool(item.get("overridden")),
        "value_usdt": _money(item.get("net_value_usdt")),
        "misc": False,
        "chain": item.get("chain"),
        "contract": None,
        "position_id": item.get("id"),
    }


def portfolio_items(context: dict) -> list[dict]:
    """Asset-level rows. Covered flat holdings are skipped: the open position
    already carries their value, and counting both would double-count."""
    threshold = context["threshold"]
    items: list[dict] = []
    for holding in context["holdings"]:
        if holding.get("covered_by_position"):
            continue
        items.append(_holding_payload(holding, threshold))
    for position in context["positions"]:
        items.append(_position_payload(position))

    # A seed/backfill NAV has no holdings breakdown. Keep the contract
    # "total == current_nav" and percentages summing to 100 by surfacing the
    # unexplained remainder as one 未归属资产 row (never persisted).
    residual = context["current_nav"] - sum(item["value_usdt"] for item in items)
    if residual > 0.01:
        asset_class, risk, reason = classify_holding({"asset": ""})
        items.append(
            {
                "key": "nav:residual",
                "name": "未归属资产",
                "source": "snapshot",
                "kind": "holding",
                "asset_class": asset_class,
                "risk_level": risk,
                "risk_reason": "快照净值与明细差额，临时归入未识别资产",
                "overridden": False,
                "value_usdt": residual,
                "misc": False,
                "chain": None,
                "contract": None,
                "position_id": None,
            }
        )
    return items


def _pct(value: float, total: float) -> float:
    if total <= 0:
        return 0.0
    return round(value / total * 100.0, 4)


def structure(context: dict) -> dict:
    total = context["current_nav"]
    items = portfolio_items(context)
    by_class = {name: 0.0 for name in ASSET_CLASSES}
    counts = {name: 0 for name in ASSET_CLASSES}
    by_risk = {name: 0.0 for name in RISK_LEVELS}
    for item in items:
        by_class[item["asset_class"]] += item["value_usdt"]
        counts[item["asset_class"]] += 1
        by_risk[item["risk_level"]] += item["value_usdt"]
    return {
        "total_usdt": round(total, 8),
        "by_class": [
            {
                "class": name,
                "label": ASSET_CLASS_LABELS[name],
                "value_usdt": round(by_class[name], 8),
                "pct": _pct(by_class[name], total),
                "items": counts[name],
            }
            for name in ASSET_CLASSES
        ],
        "by_risk": [
            {
                "risk": name,
                "value_usdt": round(by_risk[name], 8),
                "pct": _pct(by_risk[name], total),
            }
            for name in RISK_LEVELS
        ],
        "high_risk_pct": _pct(by_risk["high"], total),
    }
