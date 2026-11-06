"""三期·7: per-user returns baseline date."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import select

from app.migrate import apply_phase2_migrations
from app.models import LedgerEntry, Snapshot, User
from app.twr import calculate_returns
from tests.conftest import auth_header


def _snap(
    db,
    user,
    ts: datetime,
    nav: str,
    *,
    source: str = "sync",
    sources: tuple[str, ...] | None = None,
    folded: bool = False,
) -> None:
    breakdown = None
    if sources is not None:
        breakdown = json.dumps(
            {
                "holdings": [
                    {"source": name, "asset": "USDC", "value_usdt": "1"} for name in sources
                ],
                "positions_included": folded,
            }
        )
    db.add(
        Snapshot(
            user_id=user.id,
            ts=ts,
            nav_usdt=Decimal(nav),
            source=source,
            breakdown=breakdown,
        )
    )


def _utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def _history(db, user) -> None:
    # Before the baseline: a distorting jump. 2026-10-23 00:00 Shanghai = 2026-10-22 16:00 UTC.
    _snap(db, user, _utc(2026, 10, 20, 10), "1000")
    _snap(db, user, _utc(2026, 10, 21, 10), "2000")
    _snap(db, user, _utc(2026, 10, 22, 10), "2000")
    _snap(db, user, _utc(2026, 10, 22, 17), "3000")  # baseline day snapshot
    _snap(db, user, _utc(2026, 10, 23, 10), "3300")
    db.commit()


def test_start_date_ignores_earlier_snapshots(db, demo_user: User):
    _history(db, demo_user)
    full = calculate_returns(db, demo_user.id, "month")
    assert full.returns_start is None and full.window_clamped is False
    assert round(full.return_rate, 3) == 2.3  # 1000 -> 3300, no baseline

    demo_user.returns_start_date = date(2026, 10, 23)
    db.commit()
    clipped = calculate_returns(db, demo_user.id, "month")
    assert clipped.returns_start == date(2026, 10, 23)
    assert clipped.window_clamped is True
    assert clipped.effective_start_ts is not None
    assert clipped.series[0].return_rate == 0.0
    assert len(clipped.series) == 2  # only baseline + one later snapshot
    assert clipped.return_amount_usdt == 300.0
    assert clipped.return_rate == 0.1


def test_flows_after_start_still_removed(db, demo_user: User):
    demo_user.returns_start_date = date(2026, 10, 23)
    _history(db, demo_user)
    db.add(
        LedgerEntry(
            user_id=demo_user.id,
            ts=_utc(2026, 10, 23, 5),
            kind="deposit",
            amount_usdt=Decimal("200"),
        )
    )
    db.commit()
    report = calculate_returns(db, demo_user.id, "day")
    assert report.return_amount_usdt == 100.0  # 3300 - 3000 - 200
    assert report.return_rate == round((3300 - 200) / 3000 - 1, 6)


def test_future_start_returns_empty(db, demo_user: User):
    demo_user.returns_start_date = date(2030, 1, 1)
    _history(db, demo_user)
    report = calculate_returns(db, demo_user.id, "week")
    assert report.series == [] and report.return_rate == 0.0
    assert report.returns_start == date(2030, 1, 1)


def test_api_exposes_returns_start_and_clips_real_curve(client, db, demo_user: User):
    """Sync history is clipped at returns_start. Seed curves stay full.

    _history writes source=sync, so nav_source is not seed and overview keeps
    only ts >= 2026-10-23 00:00 Asia/Shanghai (2026-10-22 16:00 UTC): the
    17:00 UTC baseline-day point and the next day. Current NAV is the last kept point.
    """
    demo_user.returns_start_date = date(2026, 10, 23)
    _history(db, demo_user)
    headers = auth_header(client)
    body = client.get("/api/returns?period=year", headers=headers).json()
    assert body["returns_start"] == "2026-10-23"
    assert body["window_clamped"] is True
    overview = client.get("/api/overview", headers=headers).json()
    assert overview["returns_start"] == "2026-10-23"
    assert overview["nav_source"] == "sync"
    assert len(overview["series"]) == 2
    assert overview["series"][0]["ts"].startswith("2026-10-22T17:00:00")
    assert overview["series"][-1]["nav_usdt"] == 3300.0
    assert overview["current_nav_usdt"] == 3300.0


def test_seed_curve_keeps_full_history_despite_returns_start(client, db, demo_user: User):
    """Demo seed is never clipped, even when a baseline date is set."""
    from tests.conftest import seed_nav

    demo_user.returns_start_date = date(2026, 10, 23)
    db.commit()
    seed_nav(db, demo_user.id)
    overview = client.get("/api/overview", headers=auth_header(client)).json()
    assert overview["nav_source"] == "seed"
    assert overview["returns_start"] == "2026-10-23"
    assert len(overview["series"]) == 2
    assert overview["current_nav_usdt"] == 11200.0


def test_empty_curve_keeps_latest_sync_nav(client, db, demo_user: User):
    """Points entirely before the baseline drop from the series, not from the hero NAV."""
    demo_user.returns_start_date = date(2030, 1, 1)
    _history(db, demo_user)
    overview = client.get("/api/overview", headers=auth_header(client)).json()
    assert overview["nav_source"] == "sync"
    assert overview["series"] == []
    assert overview["current_nav_usdt"] == 3300.0
    assert overview["latest_snapshot_ts"].startswith("2026-10-23T10:00:00")


def test_migration_idempotent_and_admin_only(engine, db, demo_user: User):
    apply_phase2_migrations(engine)
    apply_phase2_migrations(engine)
    assert db.scalar(select(User.returns_start_date).where(User.id == demo_user.id)) is None


def test_source_onboarding_is_flow_not_return(db, demo_user: User):
    """A newly linked exchange adds NAV, not performance."""
    _snap(db, demo_user, _utc(2026, 10, 1, 10), "1000", sources=("gate",))
    _snap(db, demo_user, _utc(2026, 10, 2, 10), "1600", sources=("gate", "binance"))
    _snap(db, demo_user, _utc(2026, 10, 3, 10), "1632", sources=("gate", "binance"))
    db.commit()

    report = calculate_returns(db, demo_user.id, "month")
    assert report.return_rate == 0.02  # 1600 -> 1632, the +600 onboarding is a flow
    assert report.return_amount_usdt == 20.0
    assert [point.return_rate for point in report.series] == [0.0, 0.0, 0.02]
    # The readout must expose the removed step and must not call it a deposit.
    assert report.excluded_flow_usdt == 600.0
    assert report.excluded_coverage_flow_usdt == 600.0
    assert report.excluded_cash_flow_usdt == 0.0
    assert report.excluded_flow_kind == "coverage"


def test_position_fold_switch_is_flow_not_return(db, demo_user: User):
    """Turning the position-fold basis on/off moves NAV without market gain."""
    _snap(db, demo_user, _utc(2026, 10, 1, 10), "10000", sources=("gate",), folded=False)
    _snap(db, demo_user, _utc(2026, 10, 2, 10), "20000", sources=("gate",), folded=True)
    _snap(db, demo_user, _utc(2026, 10, 3, 10), "10000", sources=("gate",), folded=False)
    _snap(db, demo_user, _utc(2026, 10, 4, 10), "20200", sources=("gate",), folded=True)
    _snap(db, demo_user, _utc(2026, 10, 5, 10), "20402", sources=("gate",), folded=True)
    db.commit()

    report = calculate_returns(db, demo_user.id, "month")
    assert report.return_rate == 0.01  # only the final 20200 -> 20402 move
    assert report.return_amount_usdt == 100.0


def test_non_positive_snapshots_are_ignored_but_not_deleted(db, demo_user: User):
    """Old negative backfill stays in the DB, never in the return chain."""
    _snap(db, demo_user, _utc(2026, 1, 1, 10), "1000", source="backfill")
    _snap(db, demo_user, _utc(2026, 1, 2, 10), "-500", source="backfill")
    _snap(db, demo_user, _utc(2026, 1, 3, 10), "1200", source="backfill")
    db.commit()

    report = calculate_returns(db, demo_user.id, "month")
    assert report.return_rate == 0.2
    assert report.return_amount_usdt == 200.0
    assert len(report.series) == 2
    assert all(point.ts != _utc(2026, 1, 2, 10) for point in report.series)
    assert db.scalar(select(Snapshot).where(Snapshot.nav_usdt == Decimal("-500"))) is not None


def test_overview_drops_non_positive_snapshots(client, db, demo_user: User):
    _snap(db, demo_user, _utc(2026, 1, 1, 10), "1000")
    _snap(db, demo_user, _utc(2026, 1, 2, 10), "-500")
    _snap(db, demo_user, _utc(2026, 1, 3, 10), "1200")
    db.commit()

    overview = client.get("/api/overview", headers=auth_header(client)).json()
    assert [point["nav_usdt"] for point in overview["series"]] == [1000.0, 1200.0]
    assert overview["current_nav_usdt"] == 1200.0


def test_excluded_flow_is_cash_only_for_ledger_deposit(db, demo_user: User):
    """A deposit is cash: the side readout may say "充值"."""
    _snap(db, demo_user, _utc(2026, 10, 1, 10), "1000")
    db.add(
        LedgerEntry(
            user_id=demo_user.id,
            ts=_utc(2026, 10, 1, 12),
            kind="deposit",
            amount_usdt=Decimal("200"),
        )
    )
    _snap(db, demo_user, _utc(2026, 10, 2, 10), "1200")
    db.commit()

    report = calculate_returns(db, demo_user.id, "month")
    assert report.return_rate == 0.0
    assert report.return_amount_usdt == 0.0
    assert report.excluded_flow_usdt == 200.0
    assert report.excluded_cash_flow_usdt == 200.0
    assert report.excluded_coverage_flow_usdt == 0.0
    assert report.excluded_flow_kind == "cash"


def test_excluded_flow_is_mixed_when_deposit_and_new_source_coexist(db, demo_user: User):
    """Both components are counted; the UI must use the neutral "净流入"."""
    _snap(db, demo_user, _utc(2026, 10, 1, 10), "1000", sources=("gate",))
    _snap(db, demo_user, _utc(2026, 10, 2, 10), "1600", sources=("gate", "binance"))
    db.add(
        LedgerEntry(
            user_id=demo_user.id,
            ts=_utc(2026, 10, 2, 12),
            kind="deposit",
            amount_usdt=Decimal("200"),
        )
    )
    _snap(db, demo_user, _utc(2026, 10, 3, 10), "1800", sources=("gate", "binance"))
    db.commit()

    report = calculate_returns(db, demo_user.id, "month")
    assert report.return_rate == 0.0  # 600 onboarding + 200 deposit, no market move
    assert report.return_amount_usdt == 0.0
    assert report.excluded_coverage_flow_usdt == 600.0
    assert report.excluded_cash_flow_usdt == 200.0
    assert report.excluded_flow_usdt == 800.0
    assert report.excluded_flow_kind == "mixed"


def test_excluded_flow_is_zero_without_external_flows(db, demo_user: User):
    _snap(db, demo_user, _utc(2026, 10, 1, 10), "1000")
    _snap(db, demo_user, _utc(2026, 10, 2, 10), "1100")
    db.commit()

    report = calculate_returns(db, demo_user.id, "month")
    assert report.excluded_flow_usdt == 0.0
    assert report.excluded_flow_kind == "none"


def test_api_exposes_excluded_flow_fields(client, db, demo_user: User):
    _snap(db, demo_user, _utc(2026, 10, 1, 10), "1000", sources=("gate",))
    _snap(db, demo_user, _utc(2026, 10, 2, 10), "1600", sources=("gate", "binance"))
    _snap(db, demo_user, _utc(2026, 10, 3, 10), "1632", sources=("gate", "binance"))
    db.commit()

    body = client.get("/api/returns?period=month", headers=auth_header(client)).json()
    assert body["excluded_flow_usdt"] == 600.0
    assert body["excluded_cash_flow_usdt"] == 0.0
    assert body["excluded_coverage_flow_usdt"] == 600.0
    assert body["excluded_flow_kind"] == "coverage"


def test_excluded_flow_splits_cash_inside_coverage_interval(db, demo_user: User):
    """Cash that lands in the same interval as a new source counts once as cash."""
    _snap(db, demo_user, _utc(2026, 10, 1, 10), "1000", sources=("gate",))
    db.add(
        LedgerEntry(
            user_id=demo_user.id,
            ts=_utc(2026, 10, 2, 9),
            kind="deposit",
            amount_usdt=Decimal("200"),
        )
    )
    _snap(db, demo_user, _utc(2026, 10, 2, 10), "1800", sources=("gate", "binance"))
    _snap(db, demo_user, _utc(2026, 10, 3, 10), "1836", sources=("gate", "binance"))
    db.commit()

    report = calculate_returns(db, demo_user.id, "month")
    assert report.return_rate == 0.02  # 1800 -> 1836 only
    assert report.excluded_cash_flow_usdt == 200.0
    assert report.excluded_coverage_flow_usdt == 600.0
    assert report.excluded_flow_usdt == 800.0
    assert report.excluded_flow_kind == "mixed"
