"""Login → sync → pending suggestion → confirm → ledger / returns."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.auth import hash_password
from app.logging_redact import redact_text
from app.models import LedgerEntry, PendingConfirmation, Snapshot, SyncRun, User
from app.sync.cli import main as cli_main
from tests.conftest import auth_header


def test_redact_filter_hides_keys() -> None:
    leaked = "request failed api_key=abc123 secret=s3cr3t Authorization: Bearer deadbeef"
    cleaned = redact_text(leaked)
    assert "abc123" not in cleaned
    assert "s3cr3t" not in cleaned
    assert "deadbeef" not in cleaned
    assert "***" in cleaned


def test_redact_alchemy_url_and_basic_auth() -> None:
    leaked = (
        "POST https://eth-mainnet.g.alchemy.com/v2/AbC_key-123?x=1 failed; "
        "authorization: Basic c2VjcmV0a2V5Og=="
    )
    cleaned = redact_text(leaked)
    assert "AbC_key-123" not in cleaned
    assert "/v2/***" in cleaned
    assert "c2VjcmV0a2V5Og" not in cleaned
    assert "***" in cleaned


def test_redact_masks_wallet_address() -> None:
    leaked = "balance of 0xabc0000000000000000000000000000000000abc failed"
    cleaned = redact_text(leaked)
    assert "0xabc0000000000000000000000000000000000abc" not in cleaned
    assert cleaned.endswith("0xabc0…0abc failed") or "0xabc0…0abc" in cleaned


def test_e2e_login_sync_confirm_returns(client, db) -> None:
    user = User(username="demo", hashed_password=hash_password("demo123"), is_admin=False)
    db.add(user)
    db.commit()
    start = datetime(2026, 10, 19, 23, 59, 59, tzinfo=timezone.utc)
    end = datetime(2026, 10, 20, 23, 59, 59, tzinfo=timezone.utc)
    db.add_all(
        [
            Snapshot(user_id=user.id, ts=start, nav_usdt=Decimal("10000")),
            Snapshot(user_id=user.id, ts=end, nav_usdt=Decimal("10100")),
        ]
    )
    db.commit()

    headers = auth_header(client)
    synced = client.post("/api/sync?async=false&mode=mock", headers=headers)
    assert synced.status_code == 200, synced.text
    stats = synced.json()
    assert stats["fetched"] > 0
    assert stats["enqueued"] == stats["fetched"]
    assert stats["duplicates"] == 0

    pending = client.get("/api/pending", headers=headers)
    assert pending.status_code == 200
    items = pending.json()
    fresh = [item for item in items if item["suggested_category"]]
    assert fresh
    sample = fresh[0]
    assert sample["suggested_reason"]
    assert sample["source"]
    assert sample["status"] == "pending"
    # Phase-4: the chain/execution time must round-trip so the UI can label it.
    assert "ts" in sample and sample["ts"], sample

    # pick a deposit-like row and confirm it, then an airdrop override
    deposit = next(item for item in fresh if item["suggested_category"] == "deposit")
    confirmed = client.post(f"/api/pending/{deposit['id']}/confirm", headers=headers, json={})
    assert confirmed.status_code == 200
    assert confirmed.json()["final_category"] == "deposit"
    assert confirmed.json()["ts"] == deposit["ts"]

    airdrop = next((item for item in fresh if item["suggested_category"] == "airdrop"), None)
    assert airdrop is not None
    confirmed_air = client.post(
        f"/api/pending/{airdrop['id']}/confirm",
        headers=headers,
        json={"category": "airdrop"},
    )
    assert confirmed_air.status_code == 200

    kinds = set(
        db.scalars(select(LedgerEntry.kind).where(LedgerEntry.user_id == user.id)).all()
    )
    assert "deposit" in kinds
    assert "airdrop" in kinds

    returns = client.get("/api/returns?period=day", headers=headers)
    assert returns.status_code == 200
    body = returns.json()
    assert body["period"] == "day"
    # nav 10000 → 10100, plus the confirmed deposit (positive external flow).
    # return amount = 100 - deposit_usdt, which is strictly less than the raw nav change.
    assert "return_amount_usdt" in body
    assert "return_rate" in body
    assert body["series"]

    # second sync is a no-op enqueue
    again = client.post("/api/sync?async=false&mode=mock", headers=headers)
    assert again.status_code == 200
    assert again.json()["enqueued"] == 0
    assert again.json()["duplicates"] == again.json()["fetched"]


def test_confirm_unknown_category_rejected(client, db) -> None:
    user = User(username="demo", hashed_password=hash_password("demo123"), is_admin=False)
    db.add(user)
    db.flush()
    row = PendingConfirmation(
        user_id=user.id,
        title="x",
        detail="y",
        status="pending",
        suggested_category="deposit",
        source="binance",
        external_id="z",
        amount_usdt=Decimal("1"),
        ts=datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    db.add(row)
    db.commit()
    headers = auth_header(client)
    response = client.post(
        f"/api/pending/{row.id}/confirm",
        headers=headers,
        json={"category": "nope"},
    )
    assert response.status_code == 422
    count = db.scalar(select(func.count()).select_from(LedgerEntry))
    assert count == 0


def test_health_still_ok(client) -> None:
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    # P7a additive fields; no token in a fresh test DB means stale, unknown age.
    assert body["wechat_token_stale"] is True
    assert body["wechat_token_age_hours"] is None


def test_manual_sync_and_backfill_write_global_status(client, db, demo_user, monkeypatch) -> None:
    """Every entry point shares sync_runs; status reports the newest row, any user."""
    headers = auth_header(client)

    synced = client.post("/api/sync?async=false&mode=mock", headers=headers)
    assert synced.status_code == 200, synced.text
    body = client.get("/api/sync/status", headers=headers).json()
    assert body["last_status"] == "ok"
    assert body["last_trigger"] == "manual"
    assert body["last_error"] is None
    assert body["last_run_at"] is not None

    other = User(username="other", hashed_password=hash_password("demo123"), is_admin=False)
    db.add(other)
    db.commit()
    other_headers = auth_header(client, username="other")
    other_sync = client.post("/api/sync?async=false&mode=mock", headers=other_headers)
    assert other_sync.status_code == 200, other_sync.text
    # The demo caller still sees the other user's run: status is global.
    seen = client.get("/api/sync/status", headers=headers).json()
    assert seen["last_trigger"] == "manual"
    assert seen["last_status"] == "ok"
    assert db.query(SyncRun).count() == 2

    filled = client.post("/api/sync/backfill?mode=mock", headers=headers)
    assert filled.status_code == 200, filled.text
    after_fill = client.get("/api/sync/status", headers=headers).json()
    assert after_fill["last_trigger"] == "backfill"
    assert after_fill["last_status"] == "ok"
    assert after_fill["last_run_at"] >= seen["last_run_at"]

    # async=true records the same manual row when the worker job runs.
    monkeypatch.setattr(
        "app.sync.queue.enqueue_sync",
        lambda user_id, mode=None: "job-manual-1",
    )
    queued = client.post("/api/sync?async=true&mode=mock", headers=headers)
    assert queued.status_code == 200, queued.text
    from app.sync.jobs import run_sync_job

    class _Keep:
        def __enter__(self):
            return db

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr("app.sync.jobs.SessionLocal", _Keep)
    stats = run_sync_job(demo_user.id, "mock")
    assert stats["fetched"] >= 0
    after_async = client.get("/api/sync/status", headers=headers).json()
    assert after_async["last_trigger"] == "manual"
    assert after_async["last_status"] == "ok"

    def _boom(*_args, **_kwargs):
        raise RuntimeError("adapter down")

    monkeypatch.setattr("app.routers.sync.run_sync", _boom)
    with pytest.raises(RuntimeError, match="adapter down"):
        client.post("/api/sync?async=false&mode=mock", headers=headers)
    after_fail = client.get("/api/sync/status", headers=headers).json()
    assert after_fail["last_status"] == "error"
    assert after_fail["last_trigger"] == "manual"
    assert "adapter down" in (after_fail["last_error"] or "")


def test_cli_sync_and_backfill_write_status(client, db, demo_user, monkeypatch) -> None:
    monkeypatch.setattr("app.sync.cli.wait_for_db", lambda: None)
    monkeypatch.setattr("app.sync.cli.apply_phase2_migrations", lambda engine: None)

    class _Keep:
        def __enter__(self):
            return db

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr("app.sync.cli.SessionLocal", lambda: _Keep())

    assert cli_main(["--user", "demo", "--mode", "mock"]) == 0
    row = db.query(SyncRun).one()
    assert row.trigger == "manual"
    assert row.status == "ok"
    assert row.error is None

    assert cli_main(["--user", "demo", "--backfill", "--mode", "mock"]) == 0
    latest = db.query(SyncRun).order_by(SyncRun.id.desc()).first()
    assert latest is not None
    assert latest.trigger == "backfill"
    assert latest.status == "ok"

    body = client.get("/api/sync/status", headers=auth_header(client)).json()
    assert body["last_trigger"] == "backfill"
    assert body["last_status"] == "ok"
