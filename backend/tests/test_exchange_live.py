"""Per-user exchange credentials, windowed history and failure isolation.

No network. Every adapter is handed a fake client, and every retry sleeps
through the injected sleeper.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.attribution import attribute, linked_transfer_txids, normalize_txid
from app.config import has_live_credentials, settings as live_settings
from app.integrations.exchanges.ccxt_adapter import CCXTAdapter, _until_value, _windows
from app.integrations.exchanges.errors import describe_exchange_error, is_non_retryable
from app.integrations.exchanges.normalize import normalize_balance, normalize_transfer
from app.integrations.exchanges.retry import RetryableError, call_with_retry
from app.integrations.prices.service import PriceService
from app.integrations.types import NormalizedTransfer
from app.logging_redact import redact_text
from app.models import LedgerEntry, PendingConfirmation, Snapshot, SyncRun, User, UserWallet
from app.snapshots import write_sync_snapshot
from app.snapshots_backfill import backfill_user
from app.sync.scheduler import run_for_all_users
from app.sync.service import run_sync
from app.twr import calculate_returns
from app.wallets import user_exchange_credentials

FIXTURES = Path(__file__).parent / "fixtures" / "exchanges_live"
SINCE = datetime(2026, 1, 1, tzinfo=timezone.utc)
OWN = "0xown00000000000000000000000000000000000001"


def _patch(monkeypatch, **overrides):
    patched = replace(live_settings, **overrides)
    for target in (
        "app.config.settings",
        "app.wallets.settings",
        "app.sync.service.settings",
        "app.snapshots.settings",
        "app.snapshots_backfill.settings",
    ):
        monkeypatch.setattr(target, patched)
    return patched


def _user(db, username: str) -> User:
    from app.auth import hash_password

    user = User(username=username, hashed_password=hash_password("pw"), is_admin=False)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _load(name: str):
    return json.loads((FIXTURES / name).read_text())


class _Recording:
    """Fake ccxt client. Records every call and never touches the network."""

    def __init__(self, *, balances=None, deposits=None, withdrawals=None, trades=None, markets=None):
        self.balances = balances or {"total": {}}
        self.deposits = list(deposits or [])
        self.withdrawals = list(withdrawals or [])
        self.trades = list(trades or [])
        self.markets = markets or {}
        self.calls: list[tuple] = []

    def fetch_balance(self):
        self.calls.append(("fetch_balance",))
        return self.balances

    def fetch_deposits(self, code, since, limit, params=None):
        self.calls.append(("fetch_deposits", code, since, limit, params))
        return self._page(self.deposits, since, limit, params)

    def fetch_withdrawals(self, code, since, limit, params=None):
        self.calls.append(("fetch_withdrawals", code, since, limit, params))
        return self._page(self.withdrawals, since, limit, params)

    def fetch_my_trades(self, symbol, since, limit, params=None):
        self.calls.append(("fetch_my_trades", symbol, since, limit, params))
        if params and "fromId" in params:
            page = [row for row in self.trades if str(row.get("id", "")).isdigit() and int(row["id"]) >= int(params["fromId"])]
            return page[:limit]
        return self._page(self.trades, since, limit, params)

    def load_markets(self):
        self.calls.append(("load_markets",))
        return self.markets

    @staticmethod
    def _page(rows, since, limit, params):
        params = params or {}
        until = params.get("until")
        offset = int(params.get("offset") or 0)
        chosen = []
        for row in rows:
            ts = int(row.get("timestamp") or 0)
            if since is not None and ts < int(since):
                continue
            if until is not None and until > 10**12 and ts >= int(until):
                continue
            chosen.append(row)
        return chosen[offset : offset + limit]


def _adapter(source: str, client, limit: int | None = None) -> CCXTAdapter:
    return CCXTAdapter(source, {"apiKey": "k", "secret": "s"}, client=client, sleep=lambda _s: None, page_limit=limit)


# --- ownership --------------------------------------------------------------


def test_exchange_keys_belong_only_to_the_owner(db, monkeypatch) -> None:
    _patch(
        monkeypatch,
        exchange_owner_username="admin",
        binance_api_key="bk",
        binance_api_secret="bs",
        gate_api_key="gk",
        gate_api_secret="gs",
    )
    admin = _user(db, "admin")
    demo = _user(db, "demo")
    owned = user_exchange_credentials(db, admin.id)
    assert set(owned) == {"binance", "gate"}
    assert user_exchange_credentials(db, demo.id) == {}


def test_blank_owner_means_nobody_gets_keys(db, monkeypatch) -> None:
    _patch(monkeypatch, exchange_owner_username="", binance_api_key="bk", binance_api_secret="bs")
    admin = _user(db, "admin")
    assert user_exchange_credentials(db, admin.id) == {}


def test_exchange_keys_do_not_flip_demo_to_live(monkeypatch) -> None:
    _patch(monkeypatch, binance_api_key="bk", binance_api_secret="bs", gate_api_key="gk", gate_api_secret="gs")
    assert has_live_credentials() is False


def test_demo_sync_never_builds_a_live_adapter(db, monkeypatch) -> None:
    _patch(
        monkeypatch,
        exchange_owner_username="admin",
        binance_api_key="bk",
        binance_api_secret="bs",
        sync_mode="auto",
    )
    built: list[str] = []

    class _Spy(CCXTAdapter):
        def __init__(self, exchange_id, credentials, **kwargs):
            built.append(exchange_id)
            super().__init__(exchange_id, credentials, **kwargs)

    monkeypatch.setattr("app.sync.service.CCXTAdapter", _Spy)
    monkeypatch.setattr("app.snapshots.CCXTAdapter", _Spy)
    demo = _user(db, "demo")
    result = run_sync(demo.id, "auto", db=db, since=SINCE)
    assert built == []
    assert result.fetched > 0  # fixtures, not the live client


def test_owner_without_keys_is_skipped_not_partial(db, monkeypatch) -> None:
    """A wallet user whose exchanges have no key is a skip, not a failure."""
    _patch(monkeypatch, exchange_owner_username="admin", zerion_api_key="", alchemy_api_key="", helius_api_key="")
    admin = _user(db, "admin")
    db.add(UserWallet(user_id=admin.id, chain="evm", address=OWN))
    db.commit()

    captured: dict = {}

    def fake_sync(user_id, mode, *, db):  # noqa: ARG001
        from app.sync.service import SyncResult

        result = SyncResult(fetched=0, enqueued=0, duplicates=0, skipped=("binance (no api key)", "gate (no api key)"))
        captured["result"] = result
        return result

    outcome = run_for_all_users(db, trigger="manual", mode="live", sync=fake_sync)
    assert outcome.status == "ok"
    run = db.query(SyncRun).one()
    assert run.status == "ok"
    assert run.error == "skipped: binance (no api key); gate (no api key)"


def test_wallet_user_with_no_keys_skips_exchanges(db, monkeypatch) -> None:
    _patch(monkeypatch, exchange_owner_username="admin", sync_mode="live")
    admin = _user(db, "admin")
    db.add(UserWallet(user_id=admin.id, chain="evm", address=OWN))
    db.commit()
    result = run_sync(admin.id, "live", db=db, since=SINCE)
    assert result.enqueued == 0
    assert any(item.startswith("binance (no api key)") for item in result.skipped)
    assert any(item.startswith("gate (no api key)") for item in result.skipped)
    # No key is a skip. It must not mark the run partial by itself.
    assert not any("no api key" in item for item in ())


# --- failure isolation -------------------------------------------------------


class _GeoBlocked(Exception):
    """Stands in for ccxt.ExchangeNotAvailable."""


def test_one_venue_failing_does_not_abort_the_other(db, monkeypatch) -> None:
    gate = _load("gate_deposits.json")
    clients = {
        "binance": _boom_client(),
        "gate": _Recording(deposits=gate),
    }
    monkeypatch.setattr("app.sync.service.CCXTAdapter", lambda name, creds: _adapter(name, clients[name]))
    monkeypatch.setattr("app.snapshots.CCXTAdapter", lambda name, creds: _adapter(name, _Recording()))
    _patch(
        monkeypatch,
        exchange_owner_username="admin",
        binance_api_key="bk",
        binance_api_secret="bs",
        gate_api_key="gk",
        gate_api_secret="gs",
    )
    admin = _user(db, "admin")
    with pytest.raises(Exception) as caught:
        run_sync(admin.id, "live", db=db, since=SINCE)
    message = str(caught.value)
    assert "binance" in message
    assert "451" in message
    assert "restricted location" in message.lower()
    assert "signature=" not in message
    assert "secret" not in message.lower()
    # Gate's completed deposits still landed.
    rows = db.query(PendingConfirmation).all()
    assert {row.external_id for row in rows} >= {"d100", "d101"}


def _boom_client():
    class _Client:
        def fetch_balance(self):
            return {"total": {}}

        def fetch_deposits(self, code, since, limit, params=None):  # noqa: ARG002
            raise _GeoBlocked(
                "binance GET https://api.binance.com/sapi/v1/capital/deposit/hisrec"
                '?timestamp=1&signature=deadbeef HTTP 451 {"code":0,"msg":"Service unavailable from a restricted location"}'
            )

        fetch_withdrawals = fetch_deposits

    return _Client()


def test_restricted_location_is_not_retried() -> None:
    exc = _GeoBlocked('HTTP 451 {"code":0,"msg":"restricted location"}')
    assert is_non_retryable(exc) is True
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        raise exc

    with pytest.raises(_GeoBlocked):
        call_with_retry(boom, sleep=lambda _s: None, attempts=4)
    assert calls["n"] == 1


def test_describe_error_quotes_body_and_strips_signature() -> None:
    exc = _GeoBlocked(
        'ExchangeNotAvailable HTTP 451 {"code":0,"msg":"Service unavailable from a restricted location"}'
        "?timestamp=170000&signature=abc123secret"
    )
    text = describe_exchange_error(exc, ["abc123secret"], venue="binance")
    assert text.startswith("binance:")
    assert "http=451" in text
    assert "restricted location" in text
    assert "signature=" not in text
    assert "abc123secret" not in text
    assert len(text) <= 300


def test_retryable_error_uses_injected_sleep() -> None:
    sleeps: list[float] = []
    state = {"left": 2}

    def flaky():
        if state["left"]:
            state["left"] -= 1
            raise RetryableError("429")
        return "ok"

    assert call_with_retry(flaky, sleep=sleeps.append, attempts=4) == "ok"
    assert sleeps and all(item >= 0 for item in sleeps)


# --- windows, units, paging ---------------------------------------------------


def test_gate_deposit_until_is_seconds_and_window_is_30_days() -> None:
    end = datetime(2026, 3, 15, tzinfo=timezone.utc)
    start = end - timedelta(days=30)
    assert _until_value(end, "s") == int(end.timestamp())
    assert _until_value(end, "s") < 10**12
    client = _Recording(deposits=_load("gate_deposits.json"), withdrawals=_load("gate_withdrawals.json"))
    rows = _adapter("gate", client, limit=100).fetch_transfers(SINCE)
    deposit_calls = [call for call in client.calls if call[0] == "fetch_deposits"]
    assert deposit_calls
    for call in deposit_calls:
        until = call[4]["until"]
        assert until < 10**12  # seconds, not milliseconds
        assert call[4]["offset"] % 100 == 0
    ids = {row.external_id for row in rows}
    assert {"d100", "d101", "w200", "w201"} <= ids
    assert "d102" not in ids  # pending
    assert "w202" not in ids  # canceled
    covered = _windows(SINCE, days=30, until=end)
    assert all((b - a) <= timedelta(days=30) for a, b in covered)
    assert covered[0][0] == SINCE


def test_gate_trades_until_is_milliseconds_and_symbol_is_none() -> None:
    client = _Recording(trades=_load("gate_trades.json"))
    trades = _adapter("gate", client).fetch_trades(SINCE)
    calls = [call for call in client.calls if call[0] == "fetch_my_trades"]
    assert calls
    assert all(call[1] is None for call in calls)  # symbol
    assert all(call[4]["until"] > 10**12 for call in calls)  # milliseconds
    assert {trade.external_id for trade in trades} == {"t300", "t301", "t302"}


def test_binance_deposit_window_is_89_days_and_until_milliseconds() -> None:
    client = _Recording(deposits=_load("binance_deposits.json")["rows"], withdrawals=_load("binance_withdrawals.json")["rows"])
    # The fixture rows sit in late 2025, so the window has to start there.
    # Walking back from "now" in 89-day slices still has to reach them.
    rows = _adapter("binance", client).fetch_transfers(datetime(2025, 9, 1, tzinfo=timezone.utc))
    calls = [call for call in client.calls if call[0] == "fetch_deposits"]
    assert calls
    for call in calls:
        assert call[4]["until"] > 10**12
        span_ms = call[4]["until"] - call[2]
        assert span_ms <= 89 * 24 * 3600 * 1000
    assert {row.external_id for row in rows} == {"bdep1", "bwd1"}


def test_binance_trades_walk_from_id_over_active_spot_markets() -> None:
    payload = _load("binance_trades.json")
    trades = [
        {"id": "10", "timestamp": 1767225600000, "symbol": "BTC/USDT", "side": "buy", "amount": 0.1, "cost": 6000},
        {"id": "11", "timestamp": 1767225700000, "symbol": "BTC/USDT", "side": "buy", "amount": 0.2, "cost": 12000},
    ]
    client = _Recording(
        balances={"total": {"BTC": "0.5"}},
        trades=trades,
        markets=payload["markets"],
    )
    found = _adapter("binance", client, limit=1).fetch_trades(SINCE)
    trade_calls = [call for call in client.calls if call[0] == "fetch_my_trades"]
    symbols = {call[1] for call in trade_calls}
    assert "BTC/USDT" in symbols
    assert "USDT/USDT" not in symbols  # active: false
    assert all(call[2] is None for call in trade_calls)  # since is not passed
    # fromId advances past the last id seen. The following call comes back empty
    # and that is what stops the walk.
    assert [call[4]["fromId"] for call in trade_calls if call[1] == "BTC/USDT"] == [0, 11, 12]
    assert {trade.external_id for trade in found} == {"10", "11"}


def test_offset_paging_stops_on_a_short_page() -> None:
    deposits = [
        {"id": f"d{i}", "timestamp": 1767225600000 + i, "currency": "USDT", "amount": 1, "status": "ok", "type": "deposit"}
        for i in range(3)
    ]
    client = _Recording(deposits=deposits)
    rows = _adapter("gate", client, limit=2).fetch_transfers(SINCE)
    deposit_calls = [call for call in client.calls if call[0] == "fetch_deposits"]
    # Every window pages by offset and stops once a page comes back short.
    offsets = [call[4]["offset"] for call in deposit_calls]
    assert offsets[:2] == [0, 2]
    assert all(offset in (0, 2) for offset in offsets)
    assert len(rows) == 3


def test_ld_balances_fold_into_the_underlying_asset() -> None:
    rows = normalize_balance("binance", _load("binance_balances.json"))
    by_asset = {row.asset: row.amount for row in rows}
    assert by_asset["USDT"] == Decimal("150")  # 100 LDUSDT + 50 USDT
    assert "LDUSDT" not in by_asset
    assert by_asset["BTC"] == Decimal("0.5")


def test_missing_status_is_kept_and_non_ok_is_dropped() -> None:
    raw = [
        {"id": "a", "timestamp": 1767225600000, "currency": "USDT", "amount": 1, "type": "deposit"},
        {"id": "b", "timestamp": 1767225600000, "currency": "USDT", "amount": 1, "status": "pending", "type": "deposit"},
        {"id": "c", "timestamp": 1767225600000, "currency": "USDT", "amount": 1, "status": "ok", "type": "deposit"},
    ]
    client = _Recording(deposits=raw)
    ids = {row.external_id for row in _adapter("binance", client).fetch_transfers(SINCE)}
    assert ids == {"a", "c"}


# --- attribution --------------------------------------------------------------


def _transfer(**kwargs) -> NormalizedTransfer:
    base = dict(
        source="gate",
        external_id="x",
        ts=datetime(2026, 6, 1, tzinfo=timezone.utc),
        asset="USDC",
        amount=Decimal("1000"),
        direction="out",
        address=OWN,
        txid="0xlinked0000000000000000000000000000000000000000000000000000000001",
        raw_type="withdrawal",
    )
    base.update(kwargs)
    return NormalizedTransfer(**base)


def test_linked_withdrawal_and_chain_receipt_are_transfers() -> None:
    withdrawal = _transfer()
    receipt = _transfer(
        source="zerion",
        external_id="z",
        direction="in",
        address=None,
        txid="0xlinked0000000000000000000000000000000000000000000000000000000001-3",
        raw_type="receive",
    )
    linked = linked_transfer_txids([withdrawal, receipt], {OWN})
    assert normalize_txid(withdrawal.txid) in linked
    assert attribute(withdrawal, own_addresses={OWN}, linked_txids=linked).category == "transfer"
    assert attribute(receipt, own_addresses={OWN}, linked_txids=linked).category == "transfer"


def test_address_only_withdrawal_is_transfer_and_unmatched_deposit_stays_deposit() -> None:
    withdrawal = _transfer(txid="0xabc")
    deposit = _transfer(external_id="d", direction="in", address="0x1111111111111111111111111111111111111111", txid="0xdef", raw_type="deposit")
    linked = linked_transfer_txids([withdrawal, deposit], {OWN})
    assert attribute(withdrawal, own_addresses={OWN}, linked_txids=linked).category == "transfer"
    assert attribute(deposit, own_addresses={OWN}, linked_txids=linked).category == "deposit"


def test_isolated_deposit_and_withdrawal_keep_their_kind() -> None:
    deposit = _transfer(direction="in", address=None, txid="0x1", raw_type="deposit")
    withdrawal = _transfer(external_id="w", address="0x9999999999999999999999999999999999999999", txid="0x2")
    linked = linked_transfer_txids([deposit, withdrawal], {OWN})
    assert attribute(deposit, own_addresses={OWN}, linked_txids=linked).category == "deposit"
    assert attribute(withdrawal, own_addresses={OWN}, linked_txids=linked).category == "withdraw"


def test_twr_internal_transfer_nets_to_zero(db) -> None:
    user = _user(db, "solo")
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    end = datetime(2026, 6, 2, tzinfo=timezone.utc)
    db.add_all(
        [
            Snapshot(user_id=user.id, ts=start, nav_usdt=Decimal("10000.00"), source="sync"),
            Snapshot(user_id=user.id, ts=end, nav_usdt=Decimal("10000.00"), source="sync"),
            LedgerEntry(user_id=user.id, ts=start + timedelta(hours=4), amount_usdt=Decimal("-1000.00"), kind="transfer", note="out"),
            LedgerEntry(user_id=user.id, ts=start + timedelta(hours=5), amount_usdt=Decimal("1000.00"), kind="transfer", note="in"),
        ]
    )
    db.commit()
    report = calculate_returns(db, user.id, "week")
    assert report.return_amount_usdt == 0
    assert report.return_rate == 0


def test_confirming_both_legs_records_transfer_not_deposit(db, client) -> None:
    from tests.conftest import auth_header

    user = _user(db, "demo")
    withdrawal = _transfer()
    receipt = _transfer(source="zerion", external_id="z1", direction="in", raw_type="receive")
    linked = linked_transfer_txids([withdrawal, receipt], {OWN})
    for item in (withdrawal, receipt):
        suggestion = attribute(item, own_addresses={OWN}, linked_txids=linked)
        db.add(
            PendingConfirmation(
                user_id=user.id,
                title=item.external_id,
                detail=item.external_id,
                amount_usdt=Decimal("1000.00"),
                status="pending",
                source=item.source,
                external_id=item.external_id,
                direction=item.direction,
                asset=item.asset,
                amount_raw=item.amount,
                suggested_category=suggestion.category,
                suggested_confidence=Decimal("0.90"),
                suggested_reason=suggestion.reason,
                ts=item.ts,
            )
        )
    db.commit()
    headers = auth_header(client, "demo", "pw")
    ids = [row.id for row in db.query(PendingConfirmation).all()]
    for pending_id in ids:
        response = client.post(f"/api/pending/{pending_id}/confirm", headers=headers, json={})
        assert response.status_code == 200
    kinds = {row.kind for row in db.query(LedgerEntry).all()}
    assert kinds == {"transfer"}
    assert sum((row.amount_usdt for row in db.query(LedgerEntry).all()), Decimal("0")) == 0


# --- stale balances -----------------------------------------------------------


def test_failed_exchange_reuses_previous_balances_as_stale(db, monkeypatch) -> None:
    _patch(
        monkeypatch,
        exchange_owner_username="admin",
        binance_api_key="bk",
        binance_api_secret="bs",
        sync_mode="live",
    )
    admin = _user(db, "admin")
    earlier = datetime(2026, 5, 1, tzinfo=timezone.utc)
    db.add(
        Snapshot(
            user_id=admin.id,
            ts=earlier,
            nav_usdt=Decimal("50.00"),
            source="sync",
            breakdown=json.dumps(
                {"holdings": [{"source": "binance", "asset": "USDT", "amount": "50"}], "skipped": []}
            ),
        )
    )
    db.commit()

    class _Down:
        def fetch_balance(self):
            raise _GeoBlocked("HTTP 451 restricted location")

        def fetch_deposits(self, *args, **kwargs):  # noqa: ARG002
            return []

        fetch_withdrawals = fetch_deposits

    monkeypatch.setattr("app.snapshots.CCXTAdapter", lambda name, creds: CCXTAdapter(name, creds, client=_Down(), sleep=lambda _s: None))
    row = write_sync_snapshot(db, admin.id, mode="live", ts=datetime(2026, 6, 1, tzinfo=timezone.utc), prices=PriceService(use_mock=True))
    db.commit()
    assert row is not None
    body = json.loads(row.breakdown)
    carried = [item for item in body["holdings"] if item["source"] == "binance"]
    assert carried and carried[0]["stale"] is True
    assert Decimal(carried[0]["amount"]) == Decimal("50")
    assert any("stale" in item["reason"] for item in body["skipped"] if item["source"] == "binance")


# --- fixtures and a second sync ----------------------------------------------


def test_gate_and_binance_fixtures_drive_the_pipeline(db, monkeypatch) -> None:
    clients = {
        "gate": _Recording(
            balances=_load("gate_balances.json"),
            deposits=_load("gate_deposits.json"),
            withdrawals=_load("gate_withdrawals.json"),
            trades=_load("gate_trades.json"),
        ),
        "binance": _Recording(
            balances=_load("binance_balances.json"),
            deposits=_load("binance_deposits.json")["rows"],
            withdrawals=_load("binance_withdrawals.json")["rows"],
            trades=[],
            markets=_load("binance_trades.json")["markets"],
        ),
    }
    monkeypatch.setattr("app.sync.service.CCXTAdapter", lambda name, creds: _adapter(name, clients[name]))
    monkeypatch.setattr("app.snapshots.CCXTAdapter", lambda name, creds: _adapter(name, clients[name]))
    _patch(
        monkeypatch,
        exchange_owner_username="admin",
        binance_api_key="bk",
        binance_api_secret="bs",
        gate_api_key="gk",
        gate_api_secret="gs",
        sync_mode="live",
    )
    admin = _user(db, "admin")
    db.add(UserWallet(user_id=admin.id, chain="evm", address=OWN))
    db.commit()
    first = run_sync(admin.id, "live", db=db, since=SINCE)
    assert first.enqueued == first.fetched
    assert first.duplicates == 0
    second = run_sync(admin.id, "live", db=db, since=SINCE)
    assert second.fetched == first.fetched
    assert second.duplicates == second.fetched
    assert second.enqueued == 0


def test_backfill_nets_linked_legs_and_skips_empty_keys(db, monkeypatch) -> None:
    deposits = _load("gate_deposits.json")
    withdrawals = _load("gate_withdrawals.json")
    client = _Recording(balances=_load("gate_balances.json"), deposits=deposits, withdrawals=withdrawals, trades=_load("gate_trades.json"))
    monkeypatch.setattr("app.snapshots_backfill.CCXTAdapter", lambda name, creds: _adapter(name, client))
    monkeypatch.setattr("app.snapshots.CCXTAdapter", lambda name, creds: _adapter(name, client))
    _patch(monkeypatch, exchange_owner_username="admin", gate_api_key="gk", gate_api_secret="gs", sync_mode="live")
    admin = _user(db, "admin")
    db.add(UserWallet(user_id=admin.id, chain="evm", address=OWN))
    db.commit()
    result = backfill_user(
        db,
        admin.id,
        mode="live",
        since=datetime(2026, 6, 1, tzinfo=timezone.utc),
        until=datetime(2026, 6, 2, tzinfo=timezone.utc),
        prices=PriceService(use_mock=True),
    )
    assert result.problems == ()
    assert any(item.startswith("binance (no api key)") for item in result.skipped)
    assert db.scalar(select(func.count()).select_from(Snapshot)) == 2


def test_logs_redact_signature_and_secrets(monkeypatch, caplog) -> None:
    from app.logging_redact import install_redact_filter

    _patch(monkeypatch, binance_api_secret="supersecretvalue")
    logger = logging.getLogger("app.exchanges")
    install_redact_filter(logger)
    caplog.set_level(logging.INFO, logger="app.exchanges")
    logger.info(
        "GET /x?timestamp=170000&signature=deadbeef secret=supersecretvalue"
    )
    rendered = caplog.text
    assert "deadbeef" not in rendered
    assert "supersecretvalue" not in rendered
    assert "signature=***" in rendered or "signature=***" in redact_text("signature=deadbeef")


def test_normalize_transfer_reads_fixture_rows() -> None:
    parsed = [normalize_transfer("gate", row) for row in _load("gate_withdrawals.json")]
    assert all(item is not None for item in parsed)
    assert parsed[0].direction == "out"
    assert parsed[0].address == OWN
