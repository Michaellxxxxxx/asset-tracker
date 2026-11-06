"""p25 loop-position metrics.

Pure functions over one lending position plus its snapshot history.  Every
number that cannot be derived from stored data is returned as ``None`` with a
human-readable reason in ``missing``; the module never invents a fill value.

Conventions
-----------
* APY / rate fields are fractions (``0.0808`` == 8.08%).
* USD values are plain floats in the API payload.
* ``collateral_interest`` / ``borrow_interest`` in ``pnl_breakdown`` are both
  positive amounts.  The identity tested by the UI is::

      price_pnl + collateral_interest - borrow_interest == pnl_amount

* Historical snapshots that predate p25 carry no per-leg amounts or split APYs.
  They can still produce a cumulative PnL estimate from USD legs and the
  documented deposit/withdrawal heuristics, but the price/interest split stays
  ``None`` until a complete interval has split rates.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

# The spec start date; the effective start is the first snapshot at/after it.
PNL_START = datetime(2026, 10, 23, tzinfo=timezone.utc)
DAY = Decimal("86400")
YEAR_DAYS = Decimal("365")
_EPS = Decimal("0.0000001")

_STABLES = {
    "USDC",
    "USDT",
    "USD3",
    "DAI",
    "PYUSD",
    "FDUSD",
    "USDS",
    "USDE",
    "CRVUSD",
    "GHO",
    "FRAX",
    "LUSD",
    "USDD",
    "TUSD",
    "BUSD",
    "USDG",
    "RLUSD",
    "EURC",
    "EURT",
}


def _dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not result.is_finite():
        return None
    return result


def _as_float(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def _attr(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(key, default)
    return getattr(row, key, default)


def _ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _details(row: Any) -> dict:
    value = _attr(row, "details")
    return value if isinstance(value, dict) else {}


def _legs(details: dict, side: str) -> list[dict]:
    """Normalise one side of ``details`` without copying unknown keys."""
    raw = details.get(side)
    if not isinstance(raw, list):
        return []
    apy_key = "apy_supply" if side == "collateral" else "apy_borrow"
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        amount = _dec(item.get("amount"))
        usd = _dec(item.get("usd"))
        if amount is None and usd is None:
            continue
        price = None
        if amount is not None and amount > 0 and usd is not None and usd > 0:
            price = usd / amount
        out.append(
            {
                "symbol": str(item.get("symbol") or ""),
                "amount": amount,
                "usd": usd,
                "price": price,
                "apy": _dec(item.get(apy_key)),
            }
        )
    return out


def _legs_usd(legs: Sequence[dict]) -> Decimal:
    return sum((leg["usd"] for leg in legs if leg["usd"] is not None), Decimal("0"))


def _legs_amount(legs: Sequence[dict]) -> Decimal:
    return sum((leg["amount"] for leg in legs if leg["amount"] is not None), Decimal("0"))


def _weighted_apy(legs: Sequence[dict]) -> Decimal | None:
    weighted = Decimal("0")
    total = Decimal("0")
    for leg in legs:
        usd = leg["usd"]
        if usd is None or usd <= 0:
            continue
        apy = leg["apy"]
        if apy is None:
            return None
        weighted += apy * usd
        total += usd
    if total <= 0:
        return None
    return weighted / total


def _is_stable_symbol(symbol: str) -> bool:
    text = str(symbol or "").strip().upper()
    if not text:
        return False
    if text in _STABLES:
        return True
    # Pendle principal tokens and common receipt wrappers: PT-USD3-17DEC2026,
    # PT-USDC-... .  Only the base symbol has to be a stablecoin.
    for prefix in ("PT-", "PT_", "A", "C"):
        if text.startswith(prefix):
            base = text[len(prefix) :]
            if base in _STABLES:
                return True
            base = base.split("-")[0].split("_")[0]
            if base in _STABLES:
                return True
    return False


def _side_is_stable(legs: Sequence[dict]) -> bool:
    return bool(legs) and all(_is_stable_symbol(leg["symbol"]) for leg in legs)


def _snapshot_rows(snapshots: Iterable[Any]) -> list[dict]:
    rows: list[dict] = []
    for snap in snapshots:
        ts = _ts(_attr(snap, "ts"))
        if ts is None:
            continue
        rows.append(
            {
                "ts": ts,
                "net": _dec(_attr(snap, "net_value_usdt")),
                "collateral": _dec(_attr(snap, "collateral_usdt")),
                "debt": _dec(_attr(snap, "debt_usdt")),
                "apy_net": _dec(_attr(snap, "apy_net")),
                "details": _details(snap),
            }
        )
    rows.sort(key=lambda row: row["ts"])
    # A duplicate timestamp would create a zero-length interval and double-count
    # flows; keep the last stored row for that instant.
    deduped: list[dict] = []
    for row in rows:
        if deduped and deduped[-1]["ts"] == row["ts"]:
            deduped[-1] = row
        else:
            deduped.append(row)
    return deduped


def _window(samples: Sequence[tuple[datetime, Decimal]], latest: datetime | None, days: int) -> dict:
    key = f"{days}d"
    empty = {
        "days": days,
        "min": None,
        "max": None,
        "avg": None,
        "points": [],
        "samples": 0,
        "coverage_days": 0.0,
        "accumulating": True,
        "missing": "数据积累中：暂无净年化快照",
    }
    if latest is None or not samples:
        return empty
    cutoff = latest - timedelta(days=days)
    window = [(ts, value) for ts, value in samples if cutoff <= ts <= latest]
    if not window:
        return empty
    values = [value for _ts, value in window]
    first = window[0][0]
    coverage = max(
        Decimal("0"),
        Decimal(str((latest - first).total_seconds())) / Decimal("86400"),
    )
    by_day: dict[str, Decimal] = {}
    for ts, value in window:
        # Daily trend points follow the product's Beijing-time calendar.
        day = (ts + timedelta(hours=8)).date().isoformat()
        by_day[day] = value
    points = [{"date": day, "value": _as_float(value)} for day, value in sorted(by_day.items())]
    result = {
        "days": days,
        "min": _as_float(min(values)),
        "max": _as_float(max(values)),
        "avg": _as_float(sum(values) / Decimal(len(values))),
        "points": points,
        "samples": len(values),
        "coverage_days": float(coverage.quantize(Decimal("0.1"))),
        "accumulating": coverage < Decimal(days),
    }
    if result["accumulating"]:
        result["missing"] = f"数据积累中：仅覆盖 {result['coverage_days']:.1f} 天（需 {days} 天）"
    else:
        result["missing"] = None
    return result


def _daily_points(points: Sequence[dict]) -> list[dict]:
    return [{"date": item["date"], "value": item["value"]} for item in points]


def _leg_text(legs: Sequence[dict]) -> list[dict]:
    out = []
    for leg in legs:
        out.append(
            {
                "symbol": leg["symbol"],
                "amount": _as_float(leg["amount"]),
                "usd": _as_float(leg["usd"]),
                "price": _as_float(leg["price"]),
                "apy": _as_float(leg["apy"]),
            }
        )
    return out


def _quantity_flow(
    prev: dict,
    cur: dict,
    *,
    coll_prev: Decimal,
    coll_cur: Decimal,
    debt_prev: Decimal,
    debt_cur: Decimal,
    dt_days: Decimal,
) -> tuple[Decimal | None, bool]:
    """Exact flow when both snapshots carry leg amounts (p25 snapshots).

    Returns ``(flow, estimated)``.  ``None`` means this path cannot be used and
    the caller falls back to the legacy USD heuristic.
    """
    pc = _legs(prev["details"], "collateral")
    cc = _legs(cur["details"], "collateral")
    pb = _legs(prev["details"], "borrow")
    cb = _legs(cur["details"], "borrow")
    if not pc or not cc or not pb or not cb:
        return None, False
    if [leg["symbol"] for leg in pc] != [leg["symbol"] for leg in cc]:
        return None, False
    if [leg["symbol"] for leg in pb] != [leg["symbol"] for leg in cb]:
        return None, False

    def side_flow(before: Sequence[dict], after: Sequence[dict]) -> Decimal | None:
        total = Decimal("0")
        for old, new in zip(before, after):
            if old["amount"] is None or new["amount"] is None or new["price"] is None:
                return None
            total += (new["amount"] - old["amount"]) * new["price"]
        return total

    coll_change = side_flow(pc, cc)
    debt_change = side_flow(pb, cb)
    if coll_change is None or debt_change is None:
        return None, False
    raw = coll_change - debt_change
    expected_interest = Decimal("0")
    coll_apy = _weighted_apy(pc)
    debt_apy = _weighted_apy(pb)
    if coll_prev > 0 and coll_apy is not None:
        expected_interest += coll_prev * coll_apy * dt_days / YEAR_DAYS
    if debt_prev > 0 and debt_apy is not None:
        expected_interest -= debt_prev * debt_apy * dt_days / YEAR_DAYS
    # A quantity change inside the accrued-interest band is interest, not a
    # deposit/withdrawal.  Subtract nothing: the amount leg itself already
    # carries the interest accrual.
    tolerance = max(
        Decimal("0.001") * (coll_prev + debt_prev),
        abs(expected_interest) * Decimal("2"),
        Decimal("0.01"),
    )
    flow = Decimal("0") if abs(raw) <= tolerance else raw
    return flow, False


def _legacy_flow(
    prev: dict,
    cur: dict,
    *,
    coll_prev: Decimal,
    coll_cur: Decimal,
    debt_prev: Decimal,
    debt_cur: Decimal,
    dt_days: Decimal,
    coll_stable: bool,
    debt_stable: bool,
) -> tuple[Decimal, bool]:
    """Deposit/withdrawal heuristic for snapshots without per-leg amounts."""
    prev_details = prev["details"]
    coll_apy = _dec(prev_details.get("apy_supply"))
    debt_apy = _dec(prev_details.get("apy_borrow"))
    if coll_apy is None:
        coll_apy = _weighted_apy(_legs(prev_details, "collateral"))
    if debt_apy is None:
        debt_apy = _weighted_apy(_legs(prev_details, "borrow"))

    estimated = False
    coll_flow = Decimal("0")
    debt_flow = Decimal("0")

    if coll_prev > 0:
        change = coll_cur - coll_prev
        if coll_apy is not None:
            expected = coll_prev * coll_apy * dt_days / YEAR_DAYS
            excess = change - expected
        else:
            excess = change
        threshold = max(Decimal("0.01") * coll_prev, Decimal("0.01"))
        if abs(excess) > threshold:
            coll_flow = excess
            estimated = not coll_stable

    if debt_prev > 0 or debt_cur > 0:
        change = debt_cur - debt_prev
        if debt_apy is not None:
            expected = debt_prev * debt_apy * dt_days / YEAR_DAYS
            excess = change - expected
        else:
            excess = change
        threshold = max(Decimal("0.001") * max(debt_prev, debt_cur), Decimal("0.01"))
        if abs(excess) > threshold:
            # A repayment reduces debt and is a positive capital contribution.
            debt_flow = -excess
            if not debt_stable:
                estimated = True

    return coll_flow + debt_flow, estimated


def _breakdown_interest(
    prev: dict,
    *,
    collateral: Decimal,
    debt: Decimal,
    dt_days: Decimal,
) -> tuple[Decimal | None, Decimal | None]:
    """Interest income/expense for one interval, using split rates only."""
    details = prev["details"]
    coll_apy = _weighted_apy(_legs(details, "collateral"))
    debt_apy = _weighted_apy(_legs(details, "borrow"))
    coll_interest = None
    debt_interest = None
    if collateral > 0:
        if coll_apy is not None:
            coll_interest = collateral * coll_apy * dt_days / YEAR_DAYS
    else:
        coll_interest = Decimal("0")
    if debt > 0:
        if debt_apy is not None:
            debt_interest = debt * debt_apy * dt_days / YEAR_DAYS
    else:
        debt_interest = Decimal("0")
    return coll_interest, debt_interest


def compute_loop_metrics(position: Any, snapshots: Iterable[Any]) -> dict:
    """Build the p25 payload for one lending position.

    ``position`` may be an ORM row or a dict; snapshots may be ORM rows or
    dicts with ``ts``/``net_value_usdt``/``collateral_usdt``/``debt_usdt``/
    ``apy_net``/``details``.
    """
    details = _details(position)
    collateral_legs = _legs(details, "collateral")
    borrow_legs = _legs(details, "borrow")
    collateral_usd = _dec(_attr(position, "collateral_usdt"))
    debt_usd = _dec(_attr(position, "debt_usdt"))

    missing: dict[str, str] = {}
    if collateral_usd is None and collateral_legs:
        collateral_usd = _legs_usd(collateral_legs)
    if debt_usd is None and borrow_legs:
        debt_usd = _legs_usd(borrow_legs)

    principal = None
    if collateral_usd is not None and debt_usd is not None:
        principal = collateral_usd - debt_usd
    if principal is None:
        missing["principal"] = "缺少抵押或借款美元价值"
    elif principal <= 0:
        missing["principal"] = "本金不为正，无法计算"

    coll_apy_avg = _weighted_apy(collateral_legs)
    debt_apy_avg = _weighted_apy(borrow_legs)
    coll_income = None
    borrow_cost = None
    net_apy = None
    if principal is not None and principal > 0:
        if collateral_usd is None or debt_usd is None:
            missing["net_apy"] = "缺少抵押或借款美元价值"
        elif coll_apy_avg is None:
            missing["net_apy"] = "缺少抵押收益率（apy_supply）"
        elif debt_usd > 0 and debt_apy_avg is None:
            missing["net_apy"] = "缺少借款利率（apy_borrow）"
        else:
            income_total = sum(
                (leg["apy"] * leg["usd"] for leg in collateral_legs if leg["usd"]), Decimal("0")
            )
            cost_total = sum(
                (leg["apy"] * leg["usd"] for leg in borrow_legs if leg["usd"]), Decimal("0")
            )
            coll_income = income_total / principal
            borrow_cost = -(cost_total / principal)
            net_apy = coll_income + borrow_cost
    else:
        missing["net_apy"] = "本金不为正或缺少抵押/借款价值"
    if net_apy is None:
        missing.setdefault("collateral_yield_contrib", missing.get("net_apy", "缺少收益率数据"))
        missing.setdefault("borrow_cost_contrib", missing.get("net_apy", "缺少收益率数据"))
    if coll_income is None:
        missing.setdefault("collateral_yield_contrib", "缺少抵押收益率（apy_supply）")
    if borrow_cost is None:
        missing.setdefault("borrow_cost_contrib", "缺少借款利率（apy_borrow）")

    health = _dec(details.get("health_factor"))
    if health is None:
        missing["health_factor"] = "协议未提供健康度"

    liq_price = _dec(details.get("liq_price"))
    liq_basis = details.get("liq_price_basis")
    liq_estimated = details.get("liq_price_estimated") is True
    if liq_price is None:
        missing["liq_price"] = "协议未提供清算价"

    collateral_amount = _legs_amount(collateral_legs)
    current_price = None
    if collateral_usd is not None and collateral_amount > 0:
        current_price = collateral_usd / collateral_amount
    distance = None
    if liq_price is not None and current_price is not None and current_price > 0:
        distance = Decimal("1") - liq_price / current_price
    if distance is None:
        missing["distance_to_liq_pct"] = (
            "缺少清算价" if liq_price is None else "缺少抵押数量，无法计算现价"
        )

    leverage = None
    if principal is not None and principal > 0 and collateral_usd is not None:
        leverage = collateral_usd / principal
    if leverage is None:
        missing["leverage"] = "本金不为正或缺少抵押价值"

    spread = None
    if coll_apy_avg is not None and debt_apy_avg is not None:
        spread = coll_apy_avg - debt_apy_avg
    if spread is None:
        missing["spread"] = "缺少抵押或借款利率"

    depeg_price = None
    depeg_pair = None
    depeg_applicable = _side_is_stable(collateral_legs) and _side_is_stable(borrow_legs)
    if depeg_applicable and liq_price is not None:
        depeg_price = liq_price
        depeg_pair = f"{borrow_legs[0]['symbol']}/{collateral_legs[0]['symbol']}"
    elif depeg_applicable:
        missing["depeg_liq_price"] = "缺少清算价"

    rows = _snapshot_rows(snapshots)
    apy_samples = [(row["ts"], row["apy_net"]) for row in rows if row["apy_net"] is not None]
    latest_ts = rows[-1]["ts"] if rows else None
    windows = {
        "7d": _window(apy_samples, latest_ts, 7),
        "30d": _window(apy_samples, latest_ts, 30),
    }
    for key, window in windows.items():
        if window["missing"]:
            missing[f"window_{key}"] = window["missing"]

    borrow_high = None
    high_at = None
    high_samples = 0
    for row in rows:
        legs = _legs(row["details"], "borrow")
        values = [leg["apy"] for leg in legs if leg["apy"] is not None]
        if not values:
            continue
        value = max(values)
        high_samples += 1
        if borrow_high is None or value > borrow_high:
            borrow_high = value
            high_at = row["ts"]
    if borrow_high is None:
        missing["borrow_apy_high"] = "数据积累中：历史快照未记录借款利率（本期起写入）"

    pnl = {
        "amount": None,
        "rate": None,
        "start": None,
        "end": None,
        "start_principal": None,
        "net_flows": None,
        "positive_flows": None,
        "snapshots": len(rows),
        "flow_estimated": False,
    }
    breakdown = {
        "price": None,
        "collateral_interest": None,
        "borrow_interest": None,
        "interest_net": None,
        "missing": {},
    }
    missing["pnl"] = "暂无仓位快照，无法计算累计收益"
    missing["price_pnl"] = "暂无仓位快照，无法拆分"
    missing["collateral_interest"] = "暂无仓位快照，无法拆分"
    missing["borrow_interest"] = "暂无仓位快照，无法拆分"

    start_index = next((i for i, row in enumerate(rows) if row["ts"] >= PNL_START), None)
    if start_index is not None:
        start = rows[start_index]
        end = rows[-1]
        if len(rows) - start_index < 2:
            missing["pnl"] = "快照不足 2 条，无法计算区间收益"
        elif start["net"] is None or end["net"] is None:
            missing["pnl"] = "缺少快照净值"
        else:
            pnl["start"] = start["ts"].isoformat()
            pnl["end"] = end["ts"].isoformat()
            pnl["start_principal"] = _as_float(start["net"])
            net_flows = Decimal("0")
            positive_flows = Decimal("0")
            flow_ok = True
            estimated = False
            coll_interest_total = Decimal("0")
            debt_interest_total = Decimal("0")
            coll_ok = True
            debt_ok = True
            breakdown_started = False

            for prev, cur in zip(rows[start_index:], rows[start_index + 1 :]):
                dt_days = Decimal(str((cur["ts"] - prev["ts"]).total_seconds())) / DAY
                if dt_days <= 0:
                    continue
                if (
                    prev["collateral"] is None
                    or prev["debt"] is None
                    or cur["collateral"] is None
                    or cur["debt"] is None
                ):
                    flow_ok = False
                    break
                flow, interval_estimated = _quantity_flow(
                    prev,
                    cur,
                    coll_prev=prev["collateral"],
                    coll_cur=cur["collateral"],
                    debt_prev=prev["debt"],
                    debt_cur=cur["debt"],
                    dt_days=dt_days,
                )
                if flow is None:
                    flow, interval_estimated = _legacy_flow(
                        prev,
                        cur,
                        coll_prev=prev["collateral"],
                        coll_cur=cur["collateral"],
                        debt_prev=prev["debt"],
                        debt_cur=cur["debt"],
                        dt_days=dt_days,
                        coll_stable=_side_is_stable(_legs(prev["details"], "collateral")),
                        debt_stable=_side_is_stable(_legs(prev["details"], "borrow")),
                    )
                net_flows += flow
                if flow > 0:
                    positive_flows += flow
                estimated = estimated or interval_estimated
                breakdown_started = True
                ci, di = _breakdown_interest(
                    prev,
                    collateral=prev["collateral"],
                    debt=prev["debt"],
                    dt_days=dt_days,
                )
                if ci is None:
                    coll_ok = False
                else:
                    coll_interest_total += ci
                if di is None:
                    debt_ok = False
                else:
                    debt_interest_total += di

            if flow_ok:
                amount = end["net"] - start["net"] - net_flows
                denominator = start["net"] + positive_flows
                pnl["amount"] = _as_float(amount)
                pnl["net_flows"] = _as_float(net_flows)
                pnl["positive_flows"] = _as_float(positive_flows)
                pnl["flow_estimated"] = estimated
                if denominator > 0:
                    pnl["rate"] = _as_float(amount / denominator)
                else:
                    pnl["rate"] = None
                    missing["pnl_rate"] = "起点本金与正向充提为零，无法计算收益率"
                missing.pop("pnl", None)
            else:
                missing["pnl"] = "充提无法判别，累计收益缺失"

            if pnl["amount"] is None:
                reason = missing.get("pnl", "累计收益缺失")
                for key in ("price_pnl", "collateral_interest", "borrow_interest"):
                    missing[key] = f"{reason}，无法拆分"
                breakdown["missing"] = {
                    "price": missing["price_pnl"],
                    "collateral_interest": missing["collateral_interest"],
                    "borrow_interest": missing["borrow_interest"],
                    "interest_net": reason,
                }
            else:
                if breakdown_started and coll_ok:
                    breakdown["collateral_interest"] = _as_float(coll_interest_total)
                    missing.pop("collateral_interest", None)
                else:
                    reason = "历史快照缺少抵押收益率（apy_supply），无法拆分"
                    missing["collateral_interest"] = reason
                    breakdown["missing"]["collateral_interest"] = reason
                if breakdown_started and debt_ok:
                    breakdown["borrow_interest"] = _as_float(debt_interest_total)
                    missing.pop("borrow_interest", None)
                else:
                    reason = "历史快照缺少借款利率（apy_borrow），无法拆分"
                    missing["borrow_interest"] = reason
                    breakdown["missing"]["borrow_interest"] = reason
                if breakdown_started and coll_ok and debt_ok:
                    interest_net = coll_interest_total - debt_interest_total
                    breakdown["interest_net"] = _as_float(interest_net)
                    breakdown["price"] = _as_float(Decimal(str(pnl["amount"])) - interest_net)
                    missing.pop("price_pnl", None)
                else:
                    reason = "历史快照缺少抵押/借款分项利率，价格收益无法剥离"
                    missing["price_pnl"] = reason
                    breakdown["missing"]["price"] = reason

    return {
        "position_id": _attr(position, "id"),
        "protocol": _attr(position, "protocol"),
        "chain": _attr(position, "chain"),
        "label": _attr(position, "label"),
        "collateral_usd": _as_float(collateral_usd),
        "debt_usd": _as_float(debt_usd),
        "principal_usd": _as_float(principal),
        "net_apy": _as_float(net_apy),
        "collateral_yield_contrib": _as_float(coll_income),
        "borrow_cost_contrib": _as_float(borrow_cost),
        "health_factor": _as_float(health),
        "liq_price": _as_float(liq_price),
        "liq_price_basis": liq_basis if liq_price is not None else None,
        "liq_price_estimated": liq_estimated,
        "current_price": _as_float(current_price),
        "distance_to_liq_pct": _as_float(distance),
        "leverage": _as_float(leverage),
        "spread": _as_float(spread),
        "spread_negative": bool(spread is not None and spread < 0),
        "weighted_apy_supply": _as_float(coll_apy_avg),
        "weighted_apy_borrow": _as_float(debt_apy_avg),
        "collateral": {
            "usd": _as_float(collateral_usd),
            "amount": _as_float(collateral_amount),
            "legs": _leg_text(collateral_legs),
        },
        "borrow": {
            "usd": _as_float(debt_usd),
            "amount": _as_float(_legs_amount(borrow_legs)),
            "legs": _leg_text(borrow_legs),
        },
        "depeg_liq_price": _as_float(depeg_price),
        "depeg_pair": depeg_pair,
        "depeg_applicable": depeg_applicable,
        "borrow_apy_high": (
            {
                "value": _as_float(borrow_high),
                "at": high_at.isoformat() if high_at else None,
                "samples": high_samples,
            }
            if borrow_high is not None
            else None
        ),
        "apy_windows": windows,
        "pnl": pnl,
        "pnl_breakdown": breakdown,
        "missing": missing,
        "snapshot_count": len(rows),
        "pnl_start_floor": PNL_START.date().isoformat(),
    }