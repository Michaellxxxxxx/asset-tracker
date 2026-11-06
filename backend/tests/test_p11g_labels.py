"""p11g: counterparty labels, protocol-deposit rule, cache, sources, scripts."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import inspect, select, text

from app.address_labels import (
    EOA_LABEL,
    UNNAMED_CONTRACT_LABEL,
    UNKNOWN_LABEL,
    ZERO_LABEL,
    AddressInfo,
    GateRegistry,
    HostCircuitOpen,
    HostGate,
    LabelRecord,
    Leg,
    NameCandidate,
    SourceLookup,
    backfill_counterparty_labels,
    blockscout_lookup,
    fetch_address_label,
    find_protocol_deposit_withdraw_rows,
    fix_pending_protocol_deposit_categories,
    get_cached_label,
    label_meta_for_transfer,
    protocol_deposit_hits,
    protocol_deposit_out_rows,
    request_json,
    routescan_lookup,
    select_label,
    sourcify_lookup,
    set_manual_label,
    ttl_seconds,
    upsert_label,
)
from app.integrations.types import NormalizedTransfer
from app.migrate import apply_phase2_migrations
from app.models import AddressLabel, PendingConfirmation
from tests.conftest import auth_header

FIXTURE = Path(__file__).parent / "fixtures" / "chain" / "p11g_legs.json"
ZERO = "0x" + "0" * 40
HELPER = "0x2a66f992bf227d2e50ef19edd21503c3c4f3f682"
PENDLE = "0x888888888889758f76e7103c6cbf23abbf58f946"
TOKENIZED = "0xad958c4c0c90bf0216e0f5472f074a9ab30f595f"
TX_A = "0xca93c1b1c4d0b6099826c0e846b9f31cc3182a477e97c1a77cd2b21192a8a7ce"
TX_B = "0x49e1d2ce46c324439f050c441b3266efa31d6f02950b3ec3d3abb40c5d07ea23"
TX_C = "0x82574520ac3062b55f820c464b55f4fb7ffe446b872eeac13f65a30321b7e8d1"
TX_D = "0xa76045c73906d113819b7333a37ad868b06479152ac86f4911746b799cba146e"
TX_E = "0x68877fcc434eb26ca0a578d21dda7b51ca367b95ee320ac6fca7b48067da7274"
TX_F = "0x6d91e22d2c9258e3cb8aaa60cb51361a7d138c6588d41687466ccc1761746b68"
SINCE = datetime(2026, 1, 6, tzinfo=timezone.utc)


def _rows() -> list[dict]:
    return json.loads(FIXTURE.read_text())


def _leg(row: dict) -> Leg:
    return Leg(row["tx"], row["direction"], row["rawtype"], row["asset"], row["address"], row["id"])


def _transfer(row: dict) -> NormalizedTransfer:
    return NormalizedTransfer(
        source="zerion",
        external_id=f"zerion:{row['tx']}:{row['id']}",
        ts=SINCE,
        asset=row["asset"],
        amount=Decimal(row["amount"]),
        direction=row["direction"],
        address=row["address"],
        txid=row["tx"],
        raw_type=row["rawtype"],
        raw={"chain": row["chain"], "operation": row["rawtype"]},
    )


def _pending(db, **overrides) -> PendingConfirmation:
    values = dict(
        user_id=1,
        title="zerion test",
        detail="链上记录",
        status="pending",
        source="zerion",
        external_id="zerion:" + TX_A + ":" + str(overrides.get("id", 999)),
        direction="out",
        asset="USDC",
        amount_raw=Decimal("1"),
        suggested_category="withdraw",
    )
    values.update(overrides)
    row = PendingConfirmation(**values)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _record(chain, address, label, source="blockscout", *, expired=False) -> LabelRecord:
    now = datetime.now(timezone.utc) - (timedelta(days=2) if expired else timedelta())
    return LabelRecord(
        chain, address, label, "contract", source, False, {}, now,
        now - timedelta(days=1) if expired else now + timedelta(days=1),
    )


class _Clock:
    def __init__(self):
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


class _Response:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload or {}

    def json(self):
        return self._payload


class _FakeHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def request(self, method, url, params=None, json=None):
        self.calls += 1
        return self.responses.pop(0)


# --- rule: A/B/C positive, D/E/F negative ---------------------------------


def test_rule_positives_and_negatives():
    rows = _rows()
    assert protocol_deposit_hits(_leg(row) for row in rows) == {
        TX_A: "USD3",
        TX_B: "YT-USD3-29JAN2026",
        TX_C: "OGUSDX",
    }
    negatives = [row for row in rows if row["tx"] in {TX_D, TX_E, TX_F}]
    assert protocol_deposit_hits(_leg(row) for row in negatives) == {}


def test_rule_requires_different_asset(db):
    same = [
        Leg("0x" + "1" * 64, "out", "deposit", "USDC", HELPER),
        Leg("0x" + "1" * 64, "in", "deposit", "USDC", ZERO),
    ]
    assert protocol_deposit_hits(same) == {}


def test_protocol_deposit_out_rows_only_returns_out_legs():
    rows = [
        PendingConfirmation(
            user_id=1, title="t", status="pending", source="zerion",
            external_id=f"zerion:{item['tx']}:{item['id']}", direction=item["direction"],
            asset=item["asset"],
            detail=f"原始类型 {item['rawtype']}；txid {item['tx']}",
            meta={"chain": item["chain"], "counterparty": item["address"]},
        )
        for item in _rows()
        if item["tx"] in {TX_A, TX_B, TX_C}
    ]
    matched = protocol_deposit_out_rows(rows)
    assert {row.asset for row, _ in matched} == {"USDC", "USD3"}
    assert all(row.direction == "out" for row, _ in matched)


# --- priority / weak names / fallbacks ------------------------------------


def _info(*lookups, is_contract=True):
    return AddressInfo("ethereum", HELPER, tuple(lookups), is_contract, False)


def test_weak_proxy_with_implementation_shows_arrow():
    info = _info(
        SourceLookup(
            "blockscout",
            (NameCandidate("TransparentUpgradeableProxy", "blockscout", is_weak=True),),
            True, False, "TokenizedVault",
        )
    )
    assert select_label(info)[:3] == (
        "TransparentUpgradeableProxy → TokenizedVault", "contract",
        "blockscout+implementation",
    )


def test_priority_specific_then_impl_then_token_then_weak():
    specific = _info(SourceLookup(
        "blockscout",
        (NameCandidate("PendleRouterV4", "blockscout"),
         NameCandidate("PENDLE", "blockscout.token", is_token=True)),
        True, False, "Impl",
    ))
    assert select_label(specific)[:2] == ("PendleRouterV4", "contract")
    impl = _info(SourceLookup(
        "sourcify", (NameCandidate("SYMBOL", "rpc.symbol", is_token=True),),
        True, False, "RealVault",
    ))
    assert select_label(impl)[:2] == ("RealVault", "contract")
    token = _info(SourceLookup(
        "blockscout",
        (NameCandidate("Helper", "blockscout", is_weak=True),
         NameCandidate("USDC", "blockscout.token", is_token=True)),
        True,
    ))
    assert select_label(token)[:2] == ("USDC", "token")
    weak = _info(SourceLookup(
        "blockscout", (NameCandidate("GnosisSafeProxy", "blockscout", is_weak=True),), True,
    ))
    assert select_label(weak)[0] == "GnosisSafeProxy"


def test_fallbacks_contract_eoa_unknown_zero():
    assert select_label(_info(is_contract=True))[0] == UNNAMED_CONTRACT_LABEL
    assert select_label(_info(is_contract=False))[0] == EOA_LABEL
    assert select_label(_info(is_contract=None))[0].startswith("0x2a66")
    assert select_label(AddressInfo("ethereum", ZERO, (), False, False))[0] == ZERO_LABEL
    assert fetch_address_label(None, HELPER, client=object()).label == UNKNOWN_LABEL


# --- rule vs cache priority ------------------------------------------------


def test_manual_beats_protocol_beats_external_name():
    out = _transfer(_rows()[1])
    manual = {("ethereum", HELPER): _record("ethereum", HELPER, "手工名称", "manual")}
    meta = label_meta_for_transfer(out, cached=manual, protocol_hits={TX_A: "USD3"})
    assert (meta.label, meta.source) == ("手工名称", "manual")
    assert meta.protocol_deposit is True

    external = {("ethereum", HELPER): _record("ethereum", HELPER, "Helper")}
    meta = label_meta_for_transfer(out, cached=external, protocol_hits={TX_A: "USD3"})
    assert (meta.label, meta.source) == ("协议存入 · 收到 USD3", "rule")


def test_nonzero_in_leg_uses_cached_label():
    inbound = NormalizedTransfer(
        source="zerion",
        external_id="zerion:" + TX_D + ":1",
        ts=SINCE,
        asset="USDC",
        amount=Decimal("10"),
        direction="in",
        address=HELPER,
        txid=TX_D,
        raw_type="deposit",
        raw={"chain": "ethereum", "operation": "deposit"},
    )
    meta = label_meta_for_transfer(
        inbound,
        cached={("ethereum", HELPER): _record("ethereum", HELPER, "3Jane Helper")},
        protocol_hits={},
    )
    assert (meta.label, meta.source, meta.protocol_deposit) == (
        "3Jane Helper", "blockscout", False,
    )


def test_in_leg_zero_label_and_miss_unknown():
    inbound = _transfer(_rows()[0])
    meta = label_meta_for_transfer(inbound, cached={}, protocol_hits={TX_A: "USD3"})
    assert (meta.label, meta.kind, meta.protocol_deposit) == (ZERO_LABEL, "zero", True)
    outbound = _transfer(_rows()[1])
    miss = label_meta_for_transfer(outbound, cached={}, protocol_hits={})
    assert (miss.label, miss.missing) == (UNKNOWN_LABEL, True)


# --- cache expiry / manual -------------------------------------------------


def test_cache_expiry_and_manual_never_expires(db):
    upsert_label(db, _record("ethereum", HELPER, "Old", expired=True))
    db.commit()
    assert get_cached_label(db, "ethereum", HELPER) is None
    row = set_manual_label(db, "ethereum", PENDLE, "手工 Pendle")
    db.commit()
    assert row.source == "manual" and row.expires_at is None
    assert get_cached_label(
        db, "ethereum", PENDLE, now=datetime.now(timezone.utc) + timedelta(days=3650)
    ).label == "手工 Pendle"
    assert ttl_seconds("contract", "x", source="manual", is_scam=False) is None


# --- rate limit / breaker --------------------------------------------------


def test_token_bucket_and_403_breaker():
    clock = _Clock()
    gate = HostGate(rate=2.0, burst=1.0, clock=clock, sleep=clock.sleep)
    gate.acquire()
    gate.acquire()
    assert clock.slept == [0.5]

    registry = GateRegistry(clock=clock, sleep=clock.sleep)
    http = _FakeHttp([_Response(403)])
    with pytest.raises(HostCircuitOpen):
        request_json("GET", "https://base.blockscout.com/a", client=http, registry=registry)
    with pytest.raises(HostCircuitOpen):
        request_json("GET", "https://base.blockscout.com/b", client=http, registry=registry)
    assert http.calls == 1


def test_429_backoff_then_success():
    clock = _Clock()
    registry = GateRegistry(clock=clock, sleep=clock.sleep)
    http = _FakeHttp([_Response(429), _Response(200, {"ok": True})])
    assert request_json("GET", "https://sourcify.dev/x", client=http, registry=registry) == {"ok": True}
    assert (http.calls, clock.slept) == (2, [0.5])


# --- source parsing (all network mocked) -----------------------------------


def test_source_parsers_are_mocked(monkeypatch):
    def fake_request(method, url, **kwargs):
        if "blockscout" in url:
            return {
                "name": "Helper", "is_contract": True,
                "implementations": [{"name": "HelperImpl"}],
                "public_tags": [{"name": "bridge"}],
            }
        if "sourcify" in url:
            return {
                "compilation": {"name": "SourcifyName"},
                "proxyResolution": {"isProxy": True, "implementations": [{"name": "ImplName"}]},
            }
        if "routescan" in url:
            return {"result": [{"ContractName": "PlasmaSettler", "ABI": "[]"}]}
        raise AssertionError(url)

    monkeypatch.setattr("app.address_labels.request_json", fake_request)
    blockscout = blockscout_lookup("ethereum", HELPER)
    assert [name.name for name in blockscout.names] == ["Helper"]
    assert blockscout.implementation == "HelperImpl"
    sourcify = sourcify_lookup("base", HELPER)
    assert sourcify.names[0].name == "SourcifyName"
    assert sourcify.implementation == "ImplName"
    routescan = routescan_lookup("plasma", HELPER)
    assert routescan.names[0].name == "PlasmaSettler"


# --- API / sync wiring -----------------------------------------------------


def test_pending_api_exposes_label_fields(client, db, demo_user):
    db.add(PendingConfirmation(
        user_id=demo_user.id, title="zerion 协议存入", detail="d", status="pending",
        source="zerion", external_id="zerion:" + TX_A + ":1", direction="out",
        asset="USDC", suggested_category="defi_deposit",
        meta={
            "chain": "ethereum", "counterparty_label": "协议存入 · 收到 USD3",
            "counterparty_kind": "contract", "counterparty_label_source": "rule",
            "protocol_deposit": True, "protocol_deposit_asset": "USD3",
        },
    ))
    db.commit()
    row = client.get("/api/pending", headers=auth_header(client)).json()[0]
    assert row["counterparty_label"] == "协议存入 · 收到 USD3"
    assert row["counterparty_kind"] == "contract"
    assert row["counterparty_label_source"] == "rule"
    assert row["counterparty_chain"] == "ethereum"
    assert row["protocol_deposit"] is True
    assert row["protocol_deposit_asset"] == "USD3"


class _Prices:
    def to_usdt(self, asset, amount):
        return Decimal("1")


def _patch_sync(monkeypatch, transfers, queued):
    from app.sync import service as sync_service
    monkeypatch.setattr(sync_service, "_collect", lambda **_k: (transfers, {}, ()))
    monkeypatch.setattr(sync_service, "_price_service", lambda _live: _Prices())
    monkeypatch.setattr(sync_service, "write_sync_snapshot", lambda *_a, **_k: None)
    monkeypatch.setattr("app.sync.positions_hook.sync_positions_for_user", lambda *_a, **_k: None)
    monkeypatch.setattr(
        sync_service, "enqueue_label_refresh", lambda pairs: queued.extend(pairs) or 0
    )
    return sync_service


def test_run_sync_out_in_rule_and_zero(db, demo_user, monkeypatch):
    rows = _rows()
    queued = []
    service = _patch_sync(monkeypatch, [_transfer(rows[0]), _transfer(rows[1])], queued)
    assert service.run_sync(demo_user.id, "live", db=db, since=SINCE).enqueued == 2
    out = db.scalar(select(PendingConfirmation).where(
        PendingConfirmation.external_id == f"zerion:{TX_A}:249"))
    inbound = db.scalar(select(PendingConfirmation).where(
        PendingConfirmation.external_id == f"zerion:{TX_A}:248"))
    assert out.suggested_category == "defi_deposit"
    assert out.meta["counterparty_label"] == "协议存入 · 收到 USD3"
    assert out.meta["chain"] == "ethereum" and out.meta["protocol_deposit"] is True
    assert inbound.meta["counterparty_label"] == ZERO_LABEL
    assert inbound.meta["counterparty_kind"] == "zero"
    assert queued == []


def test_run_sync_cache_hit_and_miss_queue(db, demo_user, monkeypatch):
    queued = []
    transfer = _transfer(_rows()[1])
    service = _patch_sync(monkeypatch, [transfer], queued)
    service.run_sync(demo_user.id, "live", db=db, since=SINCE)
    row = db.scalar(select(PendingConfirmation).where(
        PendingConfirmation.external_id == f"zerion:{TX_A}:249"))
    assert row.meta["counterparty_label"] == UNKNOWN_LABEL
    assert ("ethereum", HELPER) in queued

    upsert_label(db, _record("ethereum", HELPER, "3Jane Helper"))
    db.commit()
    db.query(PendingConfirmation).delete()
    db.commit()
    queued.clear()
    service.run_sync(demo_user.id, "live", db=db, since=SINCE)
    row = db.scalar(select(PendingConfirmation))
    assert row.meta["counterparty_label"] == "3Jane Helper"
    assert queued == []


# --- backfill / detection / fix -------------------------------------------


def test_backfill_dry_run_apply_and_idempotent(db):
    address = "0x1111111111111111111111111111111111111111"
    _pending(db, external_id="zerion:" + TX_D + ":1",
             meta={"chain": "ethereum", "counterparty": address})
    calls = []

    def fetcher(chain, addr):
        calls.append((chain, addr))
        return _record(chain, addr, "Test Label")

    dry = backfill_counterparty_labels(db, fetcher=fetcher)
    # p11h: a dry-run is offline by contract; it counts the pair but fetches
    # nothing and writes nothing.
    assert (dry.fetched, dry.written) == (0, 0)
    assert dry.unique_addresses == 1
    assert calls == []
    assert db.get(AddressLabel, ("ethereum", address)) is None
    applied = backfill_counterparty_labels(db, apply=True, fetcher=fetcher)
    assert applied.written == 1
    assert db.get(AddressLabel, ("ethereum", address)).label == "Test Label"
    again = backfill_counterparty_labels(db, apply=True, fetcher=fetcher)
    assert (again.cache_hits, again.fetched) == (1, 0)


def test_fix_only_pending_hit_rows_and_is_idempotent(db, tmp_path):
    confirmed = _pending(
        db, external_id="zerion:" + TX_A + ":249", status="confirmed",
        ledger_entry_id=6,
        meta={"chain": "ethereum", "counterparty": HELPER, "operation": "deposit"},
    )
    pending = _pending(
        db, external_id="zerion:" + TX_C + ":77", status="pending",
        meta={"chain": "ethereum", "counterparty": TOKENIZED, "operation": "deposit"},
    )
    for tx, asset, uid in ((TX_A, "USD3", 248), (TX_C, "OGUSDX", 76)):
        _pending(db, external_id=f"zerion:{tx}:{uid}", direction="in", asset=asset,
                 meta={"chain": "ethereum", "counterparty": ZERO, "operation": "deposit"})
    unrelated = _pending(
        db, external_id="zerion:" + TX_D + ":121", status="pending",
        meta={"chain": "ethereum", "counterparty": "0x4a6c312ec70e8747a587ee860a0353cd42be0ae0",
              "operation": "trade"},
    )
    detected = find_protocol_deposit_withdraw_rows(db)
    assert {item["id"] for item in detected} >= {confirmed.id, pending.id}
    assert fix_pending_protocol_deposit_categories(db)["matched_rows"] == 1
    assert db.get(PendingConfirmation, pending.id).suggested_category == "withdraw"

    backup = tmp_path / "backup.json"
    applied = fix_pending_protocol_deposit_categories(db, apply=True, backup_path=backup)
    assert applied["written"] == 1
    db.expire_all()
    assert db.get(PendingConfirmation, pending.id).suggested_category == "defi_deposit"
    assert db.get(PendingConfirmation, confirmed.id).suggested_category == "withdraw"
    assert db.get(PendingConfirmation, confirmed.id).ledger_entry_id == 6
    assert db.get(PendingConfirmation, unrelated.id).suggested_category == "withdraw"
    payload = json.loads(backup.read_text())
    assert [c["id"] for c in payload["changes"]] == [pending.id]
    assert payload["changes"][0]["old_suggested_category"] == "withdraw"
    assert fix_pending_protocol_deposit_categories(db, apply=True, backup_path=backup)["matched_rows"] == 0


def test_fix_apply_requires_backup(db):
    pending = _pending(db, external_id="zerion:" + TX_C + ":77",
                       meta={"chain": "ethereum", "counterparty": TOKENIZED,
                             "operation": "deposit"})
    _pending(db, external_id="zerion:" + TX_C + ":76", direction="in", asset="OGUSDX",
             meta={"chain": "ethereum", "counterparty": ZERO})
    with pytest.raises(ValueError, match="backup"):
        fix_pending_protocol_deposit_categories(db, apply=True, backup_path=None)
    db.rollback()
    db.expire_all()
    assert db.get(PendingConfirmation, pending.id).suggested_category == "withdraw"


# --- migration / offline stats --------------------------------------------


def test_address_labels_migration_idempotent(engine):
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS address_labels"))
    apply_phase2_migrations(engine)
    columns = {c["name"] for c in inspect(engine).get_columns("address_labels")}
    assert columns == {"chain", "address", "label", "kind", "source", "is_scam",
                       "meta", "fetched_at", "expires_at"}
    apply_phase2_migrations(engine)


def test_p11g_raw_offline_stats_when_available():
    raw = Path("/workspace/p11g_raw/zerion_txlegs.tsv")
    if not raw.exists():
        pytest.skip("p11g_raw fixtures not mounted")
    import csv
    with raw.open(encoding="utf-8") as handle:
        legs = [
            Leg(row["tx"], row["direction"], row["rawtype"], row["asset"], row["addr"])
            for row in csv.DictReader(handle, delimiter="\t")
        ]
    hits = protocol_deposit_hits(legs)
    out_legs = [leg for leg in legs if leg.direction == "out" and leg.txid in hits]
    assert len(hits) == 21
    assert len(out_legs) == 21
    assert {leg.address for leg in out_legs} == {HELPER, PENDLE, TOKENIZED}
