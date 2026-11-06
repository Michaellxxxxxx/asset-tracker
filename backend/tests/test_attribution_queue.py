"""Attribution rules, queue dedup, confirm (including category override) and TWR."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select

from app.attribution import attribute
from app.integrations.types import NormalizedTransfer
from app.models import LedgerEntry, PendingConfirmation
from app.sync.service import run_sync
from app.twr import calculate_returns
from tests.conftest import auth_header, seed_nav

SINCE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _transfer(**overrides) -> NormalizedTransfer:
    base = dict(
        source="binance",
        external_id="x",
        ts=datetime(2026, 2, 1, tzinfo=timezone.utc),
        asset="USDT",
        amount=Decimal("10"),
        direction="in",
        address=None,
        txid=None,
        raw_type="deposit",
        note="",
    )
    base.update(overrides)
    return NormalizedTransfer(**base)


def test_attribution_rules() -> None:
    deposit = attribute(_transfer())
    assert deposit.category == "deposit"
    assert "充值" in deposit.reason
    assert 0 < deposit.confidence <= 1

    withdraw = attribute(_transfer(direction="out", raw_type="withdrawal", external_id="w"))
    assert withdraw.category == "withdraw"
    assert "提现" in withdraw.reason

    own = attribute(
        _transfer(
            source="zerion",
            direction="in",
            raw_type="receive",
            address="0xabc",
            asset="ETH",
            amount=Decimal("1"),
        ),
        own_addresses={"0xABC"},
    )
    assert own.category == "transfer"
    assert "自有地址" in own.reason

    airdrop = attribute(
        _transfer(
            source="zerion",
            raw_type="claim",
            asset="DUST",
            amount=Decimal("3"),
            note="airdrop claim",
            direction="in",
        )
    )
    assert airdrop.category == "airdrop"
    assert "airdrop" in airdrop.reason

    kol = attribute(_transfer(note="KOL referral reward", raw_type="deposit"))
    assert kol.category == "kol_reward"
    assert "kol_reward" in kol.reason


def test_sync_enqueues_and_dedupes(db, demo_user) -> None:
    first = run_sync(demo_user.id, "mock", db=db, since=SINCE)
    assert first.fetched > 0
    assert first.enqueued == first.fetched
    assert first.duplicates == 0

    rows = list(db.scalars(select(PendingConfirmation).where(PendingConfirmation.user_id == demo_user.id)))
    assert rows
    assert all(row.suggested_category for row in rows)
    assert all(row.suggested_reason for row in rows)
    assert all(row.source and row.external_id for row in rows)

    # Same session, second pass: prefetch hits every key, no IntegrityError.
    second = run_sync(demo_user.id, "mock", db=db, since=SINCE)
    assert second.enqueued == 0
    assert second.duplicates == second.fetched
    count = db.scalar(
        select(func.count()).select_from(PendingConfirmation).where(PendingConfirmation.user_id == demo_user.id)
    )
    assert count == first.enqueued


def test_integrity_conflict_does_not_abort_the_sync(db, demo_user, monkeypatch) -> None:
    """A unique conflict rolls back only the savepoint. Later rows still enqueue.

    Prefetch is hidden so the first insert hits the unique index the way a
    concurrent sync would. A full ``db.rollback()`` would drop the rows
    inserted before the conflict and leave the session unusable.
    """
    first = run_sync(demo_user.id, "mock", db=db, since=SINCE)
    assert first.enqueued == first.fetched
    assert first.duplicates == 0

    monkeypatch.setattr("app.sync.service._known_keys", lambda _session, _user_id: set())
    second = run_sync(demo_user.id, "mock", db=db, since=SINCE)
    assert second.enqueued == 0
    assert second.duplicates == second.fetched
    count = db.scalar(
        select(func.count()).select_from(PendingConfirmation).where(PendingConfirmation.user_id == demo_user.id)
    )
    assert count == first.enqueued


def test_confirm_writes_ledger_once_and_override(client, db, demo_user) -> None:
    run_sync(demo_user.id, "mock", db=db, since=SINCE)
    headers = auth_header(client)
    listing = client.get("/api/pending", headers=headers)
    assert listing.status_code == 200
    items = listing.json()
    suggested = next(item for item in items if item["suggested_category"])
    target = suggested["id"]

    # phase-1 style: no body uses the suggestion
    confirmed = client.post(f"/api/pending/{target}/confirm", headers=headers)
    assert confirmed.status_code == 200
    body = confirmed.json()
    assert body["status"] == "confirmed"
    assert body["final_category"] == body["suggested_category"]
    assert "suggested_reason" in body  # additive field, phase-1 keys still present
    for key in ("id", "title", "detail", "amount_usdt", "status", "created_at", "confirmed_at"):
        assert key in body

    ledger_count = db.scalar(select(func.count()).select_from(LedgerEntry).where(LedgerEntry.user_id == demo_user.id))
    assert ledger_count == 1

    again = client.post(f"/api/pending/{target}/confirm", headers=headers)
    assert again.status_code == 200
    ledger_count = db.scalar(select(func.count()).select_from(LedgerEntry).where(LedgerEntry.user_id == demo_user.id))
    assert ledger_count == 1

    other = next(item for item in items if item["id"] != target and item["suggested_category"] != "airdrop")
    overridden = client.post(
        f"/api/pending/{other['id']}/confirm",
        headers=headers,
        json={"category": "airdrop"},
    )
    assert overridden.status_code == 200
    assert overridden.json()["final_category"] == "airdrop"
    entry = db.scalar(
        select(LedgerEntry).where(LedgerEntry.user_id == demo_user.id, LedgerEntry.kind == "airdrop")
    )
    assert entry is not None


def test_phase1_confirm_without_suggestion_writes_no_ledger(client, db, demo_user) -> None:
    row = PendingConfirmation(
        user_id=demo_user.id,
        title="旧事项",
        detail="一期记录，没有建议分类",
        amount_usdt=Decimal("10"),
        status="pending",
    )
    db.add(row)
    db.commit()
    headers = auth_header(client)
    response = client.post(f"/api/pending/{row.id}/confirm", headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "confirmed"
    assert response.json()["suggested_category"] is None
    count = db.scalar(select(func.count()).select_from(LedgerEntry).where(LedgerEntry.user_id == demo_user.id))
    assert count == 0


def test_twr_excludes_flows_and_counts_airdrop(db, demo_user) -> None:
    seed_nav(db, demo_user.id)
    # Window is 1 day anchored at the last snapshot (2026-10-02): start 10000, end 11200,
    # deposit +1000, withdraw -200 → net flow +800. PnL = 11200 - 10000 - 800 = 400.
    baseline = calculate_returns(db, demo_user.id, "day")
    assert baseline.return_amount_usdt == 400.0

    db.add(
        LedgerEntry(
            user_id=demo_user.id,
            ts=datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc),
            amount_usdt=Decimal("50.00"),
            kind="airdrop",
            note="airdrop income",
        )
    )
    db.add(
        LedgerEntry(
            user_id=demo_user.id,
            ts=datetime(2026, 10, 2, 13, 0, tzinfo=timezone.utc),
            amount_usdt=Decimal("-30.00"),
            kind="transfer",
            note="internal out",
        )
    )
    db.commit()
    # transfer is an external flow (−30), airdrop is income and does not change the flow.
    # net flow = 1000 - 200 - 30 = 770. PnL = 1200 - 770 = 430.
    updated = calculate_returns(db, demo_user.id, "day")
    assert updated.return_amount_usdt == 430.0
    # rate uses sub-period formula: (11200 - 770) / 10000 - 1 = 0.043
    assert updated.return_rate == pytest_rate(0.043)


def pytest_rate(value: float) -> float:
    return value
