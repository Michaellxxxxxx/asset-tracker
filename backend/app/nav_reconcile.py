"""Read-only NAV identity check. No writes, no network, no keys.

Identity (p4_spec anti-double-count):

    uncovered holdings + open position net = snapshot nav

where uncovered holdings = flat holding sum − rows flagged
``covered_by_position``. Open position net is ``sum(positions.net_value_usdt)``
for ``status='open'`` only. A snapshot whose breakdown lacks
``positions_included`` was written before the fold and is reported as such,
not treated as a mismatch.
"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any

_Q = Decimal("0.01")


def _dec(value: object) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def reconcile_nav(
    *,
    nav_usdt: object,
    breakdown: dict[str, Any] | str | None,
    positions: list[dict[str, Any]],
    tolerance: Decimal = _Q,
) -> dict[str, Any]:
    """Compare one snapshot against the open positions that should be inside it.

    ``positions`` are the open rows (caller filters ``status='open'``). Closed
    rows must not be passed in: they are not in the snapshot NAV.

    Returns a dict with the three legs, the residual, and ``ok``. ``ok`` is
    False when the breakdown was not folded (``positions_included`` missing) —
    that snapshot cannot be checked, which is itself a finding.
    """
    payload = _payload(breakdown)
    folded = bool(payload.get("positions_included"))
    holdings = payload.get("holdings") if isinstance(payload.get("holdings"), list) else []

    flat = Decimal("0")
    covered = Decimal("0")
    for row in holdings:
        if not isinstance(row, dict):
            continue
        value = _dec(row.get("value_usdt"))
        flat += value
        if row.get("covered_by_position"):
            covered += value
    uncovered = flat - covered

    position_net = Decimal("0")
    for row in positions:
        if str(row.get("status") or "open") != "open":
            continue
        position_net += _dec(row.get("net_value_usdt"))

    nav = _dec(nav_usdt)
    expected = uncovered + position_net
    residual = (nav - expected).quantize(_Q)
    ok = folded and abs(residual) <= tolerance
    return {
        "ok": ok,
        "folded": folded,
        "nav_usdt": f"{nav.quantize(_Q):f}",
        "flat_holdings_usdt": f"{flat.quantize(_Q):f}",
        "covered_usdt": f"{covered.quantize(_Q):f}",
        "uncovered_usdt": f"{uncovered.quantize(_Q):f}",
        "open_position_net_usdt": f"{position_net.quantize(_Q):f}",
        "expected_usdt": f"{expected.quantize(_Q):f}",
        "residual_usdt": f"{residual:f}",
        "open_positions": sum(1 for row in positions if str(row.get("status") or "open") == "open"),
        "covered_rows": sum(1 for row in holdings if isinstance(row, dict) and row.get("covered_by_position")),
    }


def _payload(breakdown: dict[str, Any] | str | None) -> dict[str, Any]:
    if isinstance(breakdown, dict):
        return breakdown
    if not breakdown:
        return {}
    try:
        parsed = json.loads(breakdown)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
