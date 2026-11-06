"""p26: segment table + asset price-effect attribution, all from the TWR chain."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.models import LedgerEntry, Snapshot, User
from app.returns_breakdown import calculate_returns_breakdown
from app.twr import calculate_returns
from tests.conftest import auth_header


def _utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def _holding(source: str, asset: str, amount: str, price: str, priced: bool = True) -> dict:
    value = Decimal(amount) * Decimal(price)
    return {
        "source": source,
        "asset": asset,
        "amount": amount,
        "price_usdt": price,
        "value_usdt": str(value),
        "priced": priced,
    }


def _snap(
    db,
    user,
    ts: datetime,
    nav: str,
    holdings: list[dict] | None,
    *,
    folded: bool = True,
) -> None:
    breakdown = None
    if holdings is not None:
        breakdown = json.dumps(
            {"holdings": holdings, "positions_included": folded},
            ensure_ascii=False,
        )
    db.add(
        Snapshot(
            user_id=user.id,
            ts=ts,
            nav_usdt=Decimal(nav),
            source="sync",
            breakdown=breakdown,
        )
    )


def _history(db, user) -> None:
    """Baseline 10-23; a deposit, a gap, a coverage step, then a price move."""
    user.returns_start_date = date(2026, 10, 23)
    # 10-23 08:30 / 12:00 / 20:00 / 10-24 12:00 / 10-28 12:00 / 10-29 12:00 SH.
    _snap(
        db,
        user,
        _utc(2026, 10, 22, 16, 30),
        "1000",
        [
            _holding("gate", "USDC", "900", "1"),
            _holding("gate", "BTC", "1", "100"),
            _holding("gate", "ZERO", "0", "5"),
        ],
    )
    _snap(
        db,
        user,
        _utc(2026, 10, 23, 4, 0),
        "1010",
        [
            _holding("gate", "USDC", "900", "1"),
            _holding("gate", "BTC", "1", "110"),
            _holding("gate", "ZERO", "1", "5"),
        ],
    )
    _snap(
        db,
        user,
        _utc(2026, 10, 24, 4, 0),
        "1120",
        [
            _holding("gate", "USDC", "1010", "1"),
            _holding("gate", "BTC", "1", "110"),
            _holding("gate", "ZERO", "1", "6"),
        ],
    )
    # A 100 USDT deposit inside this interval must not be counted as return.
    db.add(
        LedgerEntry(
            user_id=user.id,
            ts=_utc(2026, 10, 24, 3, 0),
            kind="deposit",
            amount_usdt=Decimal("100"),
        )
    )
    # 10-25..10-27 deliberately have no snapshot.
    _snap(
        db,
        user,
        _utc(2026, 10, 28, 4, 0),
        "1100",
        [
            _holding("gate", "USDC", "1000", "1"),
            _holding("gate", "BTC", "1", "100"),
            _holding("binance", "USDC", "100", "1"),
        ],
    )
    _snap(
        db,
        user,
        _utc(2026, 10, 29, 4, 0),
        "1200",
        [
            _holding("gate", "USDC", "1000", "1"),
            _holding("gate", "BTC", "1", "200"),
            _holding("binance", "USDC", "100", "1"),
        ],
    )
    db.commit()


def test_segments_telescope_to_total_amount_and_rate(db, demo_user: User):
    _history(db, demo_user)
    report = calculate_returns(db, demo_user.id, "month")
    breakdown = calculate_returns_breakdown(db, demo_user.id, "month")

    assert breakdown.return_amount_usdt == report.return_amount_usdt
    assert breakdown.return_rate == report.return_rate

    for unit in ("week", "day"):
        rows = getattr(breakdown.segments, unit)
        assert rows
        total = sum(row.amount_usdt or 0 for row in rows)
        assert abs(total - breakdown.return_amount_usdt) < 0.01
        product = 1.0
        for row in rows:
            if row.rate is not None:
                product *= 1 + row.rate
        assert abs((product - 1) - breakdown.return_rate) < 1e-5
        check = getattr(breakdown.sum_check, unit)
        assert abs(check.diff_usdt) < 0.01
        assert abs(check.rate_diff) < 1e-5

    # The deposit is removed: (1120 - 100) / 1010 - 1 = +0.9901%. In nav0 units
    # the interval contributes exactly 1000 * (1.02 - 1.01) = 10, never 120.
    day = {row.label: row for row in breakdown.segments.day}
    assert day["10-24"].amount_usdt == pytest.approx(10.0, abs=0.001)
    assert day["10-24"].rate == pytest.approx(0.009901, abs=1e-6)
    assert day["10-23"].amount_usdt == pytest.approx(10.0, abs=0.001)


def test_missing_days_are_null_never_zero(db, demo_user: User):
    _history(db, demo_user)
    breakdown = calculate_returns_breakdown(db, demo_user.id, "month")
    day = {row.label: row for row in breakdown.segments.day}

    for label in ("10-25", "10-26", "10-27"):
        assert day[label].missing == "no_snapshot"
        assert day[label].amount_usdt is None
        assert day[label].rate is None
        assert day[label].intervals == 0

    # A coverage step is still a real interval (amount 0), not a missing day.
    assert day["10-28"].missing is None
    assert day["10-28"].amount_usdt == pytest.approx(0.0, abs=0.001)
    assert day["10-28"].intervals == 1


def test_coverage_change_step_is_excluded_from_assets(db, demo_user: User):
    _history(db, demo_user)
    breakdown = calculate_returns_breakdown(db, demo_user.id, "month")
    assets = {row.asset: row for row in breakdown.assets}

    # gate BTC 100 -> 110 (+10), 110 -> 110 (0), coverage 110 -> 100 skipped,
    # 100 -> 200 (+100). Total +110, never the -10 inside the coverage step.
    assert assets["BTC"].amount_usdt == pytest.approx(110.0, abs=0.01)
    assert assets["BTC"].sources == ["gate"]
    # Price effect is positive and appears before the zero-effect stablecoin.
    amounts = [row.amount_usdt for row in breakdown.assets]
    assert amounts == sorted(amounts, key=lambda value: (-abs(value),))


def test_asset_rate_denominator_zero_is_null(db, demo_user: User):
    _history(db, demo_user)
    breakdown = calculate_returns_breakdown(db, demo_user.id, "month")
    zero = next(row for row in breakdown.assets if row.asset == "ZERO")

    assert zero.base_value_usdt == 0.0
    assert zero.rate is None
    assert zero.amount_usdt == pytest.approx(1.0, abs=0.01)


def test_unattributed_is_total_minus_attributed(db, demo_user: User):
    _history(db, demo_user)
    breakdown = calculate_returns_breakdown(db, demo_user.id, "month")

    attributed = sum(row.amount_usdt for row in breakdown.assets)
    assert breakdown.unattributed_usdt == pytest.approx(
        breakdown.return_amount_usdt - attributed, abs=0.01
    )
    assert breakdown.assets_missing is False
    assert "数量变化" in breakdown.unattributed_reason


def test_day_period_has_no_segment_table(db, demo_user: User):
    _history(db, demo_user)
    breakdown = calculate_returns_breakdown(db, demo_user.id, "day")

    assert breakdown.segments.month == []
    assert breakdown.segments.week == []
    assert breakdown.segments.day == []
    assert breakdown.sum_check.month is None
    assert breakdown.sum_check.week is None
    assert breakdown.sum_check.day is None
    # The asset detail is shown for every period, including one day.
    assert breakdown.assets


def test_year_uses_month_buckets_only(db, demo_user: User):
    _history(db, demo_user)
    breakdown = calculate_returns_breakdown(db, demo_user.id, "year")

    assert [row.label for row in breakdown.segments.month] == ["2026-10"]
    assert breakdown.segments.week == []
    assert breakdown.segments.day == []
    assert breakdown.segments.month[0].amount_usdt == pytest.approx(
        breakdown.return_amount_usdt, abs=0.01
    )
    assert breakdown.sum_check.month is not None
    assert abs(breakdown.sum_check.month.diff_usdt) < 0.01
    assert abs(breakdown.sum_check.month.rate_diff) < 1e-5
    assert breakdown.sum_check.week is None
    assert breakdown.sum_check.day is None


def test_same_symbol_on_two_sources_is_merged(db, demo_user: User):
    """Price effects use (source, asset) pairs but report one row per symbol."""
    demo_user.returns_start_date = date(2026, 10, 23)
    _snap(
        db,
        demo_user,
        _utc(2026, 10, 22, 16, 30),
        "200",
        [
            _holding("gate", "BTC", "1", "100"),
            _holding("binance", "BTC", "1", "100"),
        ],
    )
    _snap(
        db,
        demo_user,
        _utc(2026, 10, 23, 4, 0),
        "250",
        [
            _holding("gate", "BTC", "1", "120"),
            _holding("binance", "BTC", "1", "130"),
        ],
    )
    db.commit()

    breakdown = calculate_returns_breakdown(db, demo_user.id, "month")
    assert len(breakdown.assets) == 1
    btc = breakdown.assets[0]
    assert btc.asset == "BTC"
    assert btc.amount_usdt == pytest.approx(50.0, abs=0.01)
    # Combined first valuation is 1*100 + 1*100, not one source's 100.
    assert btc.base_value_usdt == pytest.approx(200.0, abs=0.01)
    assert btc.rate == pytest.approx(0.25, abs=1e-6)
    assert btc.sources == ["binance", "gate"]
    assert breakdown.unattributed_usdt == pytest.approx(0.0, abs=0.01)
    assert breakdown.sum_check.day is not None
    assert abs(breakdown.sum_check.day.diff_usdt) < 0.01


def test_unpriced_asset_stays_out_of_assets_and_into_unattributed(db, demo_user: User):
    demo_user.returns_start_date = date(2026, 10, 23)
    _snap(
        db,
        demo_user,
        _utc(2026, 10, 22, 16, 30),
        "1000",
        [
            _holding("gate", "USDC", "900", "1"),
            _holding("gate", "MYST", "10", "10", priced=False),
        ],
    )
    _snap(
        db,
        demo_user,
        _utc(2026, 10, 23, 4, 0),
        "1100",
        [
            _holding("gate", "USDC", "900", "1"),
            _holding("gate", "MYST", "10", "20", priced=False),
        ],
    )
    db.commit()

    breakdown = calculate_returns_breakdown(db, demo_user.id, "month")
    assert [row.asset for row in breakdown.assets] == ["USDC"]
    assert breakdown.assets[0].amount_usdt == pytest.approx(0.0, abs=0.01)
    assert breakdown.assets_missing is False
    assert breakdown.unattributed_usdt == pytest.approx(100.0, abs=0.01)


def test_no_breakdown_marks_assets_missing(db, demo_user: User):
    demo_user.returns_start_date = date(2026, 10, 23)
    _snap(db, demo_user, _utc(2026, 10, 22, 16, 30), "1000", None)
    _snap(db, demo_user, _utc(2026, 10, 23, 4, 0), "1010", None)
    db.commit()

    breakdown = calculate_returns_breakdown(db, demo_user.id, "month")
    assert breakdown.assets == []
    assert breakdown.assets_missing is True
    assert breakdown.unattributed_usdt is None


def test_api_exposes_additive_breakdown(client, db, demo_user: User):
    _history(db, demo_user)
    headers = auth_header(client)
    response = client.get("/api/returns/breakdown?period=month", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["period"] == "month"
    assert body["return_amount_usdt"] == pytest.approx(112.73, abs=0.01)
    assert {"segments", "assets", "unattributed_usdt", "sum_check"} <= set(body)
    assert body["segments"]["day"]

    # The old response keeps its shape.
    old = client.get("/api/returns?period=month", headers=headers)
    assert old.status_code == 200
    assert "series" in old.json()
    assert "segments" not in old.json()
