"""p11h: chain/address in pending meta, Solana labels, and the backfill."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.address_labels import (
    EOA_LABEL,
    SOLANA_TOKEN_PROGRAM,
    UNNAMED_CONTRACT_LABEL,
    UNKNOWN_LABEL,
    backfill_counterparty_labels,
    fetch_address_label,
    label_meta_for_transfer,
    refresh_labels,
    solana_known_program,
    transfer_chain,
    upsert_label,
)
from app.integrations.chain.helius import parse_helius_transactions
from app.integrations.types import NormalizedTransfer
from app.models import AddressLabel, PendingConfirmation
from app.sync import service as sync_service
from tests.conftest import auth_header

SINCE = datetime(2026, 1, 6, tzinfo=timezone.utc)
EVM = "0x2a66f992bf227d2e50ef19edd21503c3c4f3f682"
SYSTEM = "11111111111111111111111111111111"
JUPITER = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
WALLET = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
PLACEHOLDER = "OtherSol111"


class _Prices:
    def to_usdt(self, asset, amount):
        return Decimal("1")


def _patch_sync(monkeypatch, transfers, queued):
    monkeypatch.setattr(sync_service, "_collect", lambda **_k: (transfers, {}, ()))
    monkeypatch.setattr(sync_service, "_price_service", lambda _live: _Prices())
    monkeypatch.setattr(sync_service, "write_sync_snapshot", lambda *_a, **_k: None)
    monkeypatch.setattr("app.sync.positions_hook.sync_positions_for_user", lambda *_a, **_k: None)
    monkeypatch.setattr(
        sync_service, "enqueue_label_refresh", lambda pairs: queued.extend(pairs) or 0
    )
    return sync_service


def _zerion(address=EVM, txid="0x" + "a" * 64) -> NormalizedTransfer:
    return NormalizedTransfer(
        source="zerion",
        external_id=f"zerion:{txid}:1",
        ts=SINCE,
        asset="USDC",
        amount=Decimal("2"),
        direction="out",
        address=address,
        txid=txid,
        raw_type="send",
        raw={"chain": "ethereum"},
    )


def _helius(address: str, *, program_ids: list[str] | None = None) -> NormalizedTransfer:
    return NormalizedTransfer(
        source="helius",
        external_id="helius:sig-placeholder:0",
        ts=SINCE,
        asset="SOL",
        amount=Decimal("0.1"),
        direction="out",
        address=address,
        txid="5" * 64,
        raw_type="TRANSFER",
        raw={"chain": "solana", "program_ids": program_ids or []},
    )


def _record(chain, address, label, source="manual", kind="contract") -> object:
    now = datetime.now(timezone.utc)
    from app.address_labels import LabelRecord

    return LabelRecord(
        chain, address, label, kind, source, False, {}, now, now + timedelta(days=30)
    )


def _pending(db, **overrides) -> PendingConfirmation:
    values = dict(
        user_id=1,
        title="p11h row",
        detail="链上记录",
        status="pending",
        source="helius",
        external_id="helius:sig-placeholder:0",
        direction="out",
        asset="SOL",
        amount_raw=Decimal("1"),
        suggested_category="chain_out",
    )
    values.update(overrides)
    row = PendingConfirmation(**values)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _meta_for(transfer, cached=None, hits=None):
    return label_meta_for_transfer(transfer, cached=cached or {}, protocol_hits=hits or {})


# --- sync meta -----------------------------------------------------------------


def test_sync_zerion_writes_lowercase_chain_and_address(db, demo_user, monkeypatch):
    queued: list[tuple[str, str]] = []
    upsert_label(db, _record("ethereum", EVM, "Some Contract"))
    db.commit()
    service = _patch_sync(monkeypatch, [_zerion(EVM.upper().replace("0X", "0x"))], queued)
    service.run_sync(demo_user.id, "live", db=db, since=SINCE)
    row = db.scalar(select(PendingConfirmation))
    assert row.meta["chain"] == "ethereum"
    assert row.meta["counterparty_address"] == EVM
    assert row.meta["counterparty_label"] == "Some Contract"
    assert queued == []


def test_sync_helius_writes_solana_address_and_known_program(db, demo_user, monkeypatch):
    queued: list[tuple[str, str]] = []
    service = _patch_sync(monkeypatch, [_helius(SYSTEM, program_ids=[SYSTEM])], queued)
    service.run_sync(demo_user.id, "live", db=db, since=SINCE)
    row = db.scalar(select(PendingConfirmation))
    assert row.meta["chain"] == "solana"
    assert row.meta["counterparty_address"] == SYSTEM
    assert row.meta["counterparty_label"] == "System Program"
    assert row.meta["counterparty_kind"] == "contract"
    assert row.meta["counterparty_label_source"] == "known_program"
    assert (("solana", SYSTEM)) in queued


def test_sync_unknown_solana_wallet_is_queued_not_fetched(db, demo_user, monkeypatch):
    queued: list[tuple[str, str]] = []
    calls = []
    monkeypatch.setattr(
        "app.address_labels.solana_account_lookup",
        lambda *a, **k: calls.append((a, k)),
    )
    service = _patch_sync(monkeypatch, [_helius(WALLET)], queued)
    service.run_sync(demo_user.id, "live", db=db, since=SINCE)
    row = db.scalar(select(PendingConfirmation))
    assert row.meta["chain"] == "solana"
    assert row.meta["counterparty_address"] == WALLET
    assert row.meta["counterparty_label"] == UNKNOWN_LABEL
    assert queued == [("solana", WALLET)]
    assert calls == []


def test_sync_helius_placeholder_is_unknown_without_network(db, demo_user, monkeypatch):
    queued: list[tuple[str, str]] = []
    calls = []
    monkeypatch.setattr(
        "app.address_labels.solana_account_lookup",
        lambda *a, **k: calls.append((a, k)),
    )
    service = _patch_sync(monkeypatch, [_helius(PLACEHOLDER)], queued)
    service.run_sync(demo_user.id, "live", db=db, since=SINCE)
    row = db.scalar(select(PendingConfirmation))
    assert row.meta["chain"] == "solana"
    assert row.meta["counterparty_address"] == PLACEHOLDER
    assert row.meta["counterparty_label"] == UNKNOWN_LABEL
    assert row.meta["counterparty_kind"] == "unknown"
    assert calls == []
    assert queued == []


def test_refresh_skips_invalid_solana_placeholder(db, monkeypatch):
    def forbidden(*_a, **_k):
        raise AssertionError("placeholder must not reach Helius")

    monkeypatch.setattr("app.address_labels.solana_account_lookup", forbidden)
    records = refresh_labels(db, "solana", [PLACEHOLDER])
    assert records == []


# --- Solana source selection ----------------------------------------------------


def test_known_program_and_tx_program_hint():
    known = fetch_address_label("solana", JUPITER)
    assert known.label == "Jupiter Aggregator v6"
    assert known.kind == "contract"
    assert solana_known_program(JUPITER)

    hinted = fetch_address_label("solana", WALLET, program_ids=[WALLET])
    assert hinted.label == UNNAMED_CONTRACT_LABEL
    assert hinted.kind == "contract"
    assert hinted.meta["sources"][0]["source"] == "solana.tx_program"


def test_solana_account_shape_token_program_and_eoa(monkeypatch):
    payloads = iter(
        [
            {"result": {"value": {"owner": SOLANA_TOKEN_PROGRAM, "executable": False}}},
            {"result": {"value": {"owner": "11111111111111111111111111111111", "executable": False}}},
            {"result": {"value": {"owner": "SomeProgram111111111111111111111111111111", "executable": True}}},
        ]
    )
    seen = []

    def fake_request(method, url, **kwargs):
        seen.append((method, url, kwargs.get("json_body")))
        return next(payloads)

    monkeypatch.setattr("app.address_labels.solana_rpc_url", lambda: "https://rpc.invalid")
    monkeypatch.setattr("app.address_labels.request_json", fake_request)

    token = fetch_address_label("solana", WALLET)
    assert token.label == "代币账户" and token.kind == "token"
    eoa = fetch_address_label("solana", WALLET)
    assert eoa.label == EOA_LABEL and eoa.kind == "eoa"
    program = fetch_address_label("solana", WALLET)
    assert program.label == UNNAMED_CONTRACT_LABEL and program.kind == "contract"
    assert all("api-key" not in str(item[2]) for item in seen)


def test_solana_pda_owned_by_known_program_gets_owner_label(monkeypatch):
    owner = "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK"
    monkeypatch.setattr("app.address_labels.solana_rpc_url", lambda: "https://rpc.invalid")
    monkeypatch.setattr(
        "app.address_labels.request_json",
        lambda *_a, **_k: {"result": {"value": {"owner": owner, "executable": False}}},
    )

    record = fetch_address_label("solana", WALLET)
    assert record.label == "Raydium CLMM 账户"
    assert record.kind == "contract"
    assert record.source == "helius.rpc+known_owner"
    assert record.meta["sources"][0]["source"] == "helius.rpc"
    assert record.meta["sources"][0]["owner"] == owner
    assert record.meta["sources"][0]["executable"] is False
    assert record.meta["names"][0]["source"] == "helius.rpc+known_owner"


def test_solana_pda_owned_by_unknown_program_stays_unnamed(monkeypatch):
    monkeypatch.setattr("app.address_labels.solana_rpc_url", lambda: "https://rpc.invalid")
    monkeypatch.setattr(
        "app.address_labels.request_json",
        lambda *_a, **_k: {
            "result": {
                "value": {
                    "owner": "UnknownProgram1111111111111111111111111",
                    "executable": False,
                }
            }
        },
    )

    record = fetch_address_label("solana", WALLET)
    assert record.label == UNNAMED_CONTRACT_LABEL
    assert record.kind == "contract"
    assert record.meta["sources"][0]["source"] == "helius.rpc"


def test_new_known_program_address_labels_as_itself(monkeypatch):
    def forbidden(*_a, **_k):
        raise AssertionError("known program address must not need an RPC lookup")

    monkeypatch.setattr("app.address_labels.request_json", forbidden)
    titan = "T1TANpTeScyeqVzzgNViGDNrkQ6qHz9KrSBS4aNXvGT"
    record = fetch_address_label("solana", titan)
    assert record.label == "Titan Exchange"
    assert record.kind == "contract"
    assert record.source == "solana.known_program"


def test_solana_no_account_is_unknown_not_truncated(monkeypatch):
    monkeypatch.setattr("app.address_labels.solana_rpc_url", lambda: "https://rpc.invalid")
    monkeypatch.setattr(
        "app.address_labels.request_json",
        lambda *_a, **_k: {"result": {"value": None}},
    )
    record = fetch_address_label("solana", WALLET)
    assert record.label == UNKNOWN_LABEL
    assert record.kind == "unknown"


# --- backfill -------------------------------------------------------------------


def test_backfill_dry_run_is_offline_and_apply_is_idempotent(db):
    row = _pending(
        db,
        detail=f"地址 {PLACEHOLDER}；原始类型 transfer",
        meta={},
    )
    calls = []

    def fetcher(chain, address):
        calls.append((chain, address))
        raise AssertionError("dry-run/placeholder must not fetch")

    dry = backfill_counterparty_labels(db, fetcher=fetcher)
    assert dry.dry_run is True and dry.offline is True
    assert dry.total_pending_rows == 1 and dry.solana_candidate_rows == 1
    assert dry.solana_patchable_rows == 1
    assert dry.written == 0 and calls == []
    assert not db.get(PendingConfirmation, row.id).meta

    applied = backfill_counterparty_labels(db, apply=True, fetcher=fetcher)
    assert applied.written == 1 and calls == []
    db.expire_all()
    written = db.get(PendingConfirmation, row.id)
    assert written.meta["chain"] == "solana"
    assert written.meta["counterparty_address"] == PLACEHOLDER
    assert written.meta["counterparty_label"] == UNKNOWN_LABEL
    assert db.get(AddressLabel, ("solana", PLACEHOLDER)) is None

    again = backfill_counterparty_labels(db, apply=True, fetcher=fetcher)
    assert again.written == 0
    assert calls == []


def test_backfill_evm_dry_run_counts_cache_hit_and_never_network(db, monkeypatch):
    upsert_label(db, _record("ethereum", EVM, "Cached Contract"))
    db.commit()
    row = _pending(
        db,
        source="zerion",
        external_id="zerion:" + "b" * 64 + ":1",
        direction="out",
        asset="USDC",
        detail="链上转出",
        meta={"chain": "ethereum", "counterparty": EVM},
    )

    def forbidden(*_a, **_k):
        raise AssertionError("dry-run must be offline")

    monkeypatch.setattr("app.address_labels.detect_chain_for_tx", forbidden)
    report = backfill_counterparty_labels(db)
    assert report.evm_candidate_rows == 1 and report.evm_patchable_rows == 1
    assert report.cache_hit_rows == 1
    assert report.fetched == 0 and report.written == 0
    assert report.as_dict()["cache_hit_rows"] == 1
    assert db.get(PendingConfirmation, row.id).meta == {"chain": "ethereum", "counterparty": EVM}


def test_backfill_unknown_chain_is_not_guessed(db):
    row = _pending(
        db,
        source="zerion",
        external_id="zerion:" + "c" * 64 + ":1",
        detail="链上转出",
        meta={"counterparty": EVM},
    )
    report = backfill_counterparty_labels(db, apply=True, offline=True)
    assert report.evm_candidate_rows == 1
    assert report.evm_patchable_rows == 0
    assert report.unresolved_chain == 1
    assert report.written == 0
    db.expire_all()
    assert db.get(PendingConfirmation, row.id).meta == {"counterparty": EVM}


def test_backfill_apply_fetches_once_and_is_idempotent(db):
    row = _pending(
        db,
        source="zerion",
        external_id="zerion:" + "d" * 64 + ":1",
        meta={"chain": "ethereum", "counterparty_address": EVM},
    )
    calls = []

    def fetcher(chain, address):
        calls.append((chain, address))
        return _record(chain, address, "Fetched Contract", source="blockscout")

    dry = backfill_counterparty_labels(db, fetcher=fetcher)
    assert dry.unique_addresses == 1
    assert dry.fetched == 0 and dry.written == 0 and calls == []

    first = backfill_counterparty_labels(db, apply=True, fetcher=fetcher)
    assert calls == [("ethereum", EVM)]
    assert first.fetched == 1 and first.written == 1
    db.expire_all()
    meta = db.get(PendingConfirmation, row.id).meta
    assert meta["counterparty_label"] == "Fetched Contract"
    assert meta["counterparty_kind"] == "contract"

    second = backfill_counterparty_labels(db, apply=True, fetcher=fetcher)
    assert calls == [("ethereum", EVM)]
    assert second.written == 0 and second.fetched == 0


def test_backfill_limit_scans_a_batch(db):
    for index in range(2):
        _pending(
            db,
            source="zerion",
            external_id=f"zerion:{index:064x}:1",
            meta={"chain": "ethereum", "counterparty": EVM},
        )
    report = backfill_counterparty_labels(db, limit=1)
    assert report.total_pending_rows == 2
    assert report.scanned_rows == 1
    assert report.evm_candidate_rows == 1


def test_backfill_does_not_guess_ambiguous_detail(db):
    _pending(db, detail=f"地址 {EVM}；地址 {'0x' + '1' * 40}；原始类型 send", meta={})
    report = backfill_counterparty_labels(db, apply=True, offline=True)
    assert report.no_address_rows == 1
    assert report.unavailable_rows == 1
    assert report.written == 0


def test_backfill_ignores_prose_address_word_with_arrow(db):
    sol = "E2yWHexppjJFV1WoZTyKeD6rDf2cRt87szMuEwMwfzW9"
    _pending(
        db,
        detail=(
            "链上流入来自已知自有地址→transfer；方向 out；原始类型 TRANSFER；"
            f"地址 {sol}；txid {'a' * 88}"
        ),
        meta={},
    )
    report = backfill_counterparty_labels(db, offline=True)
    assert report.solana_candidate_rows == 1
    assert report.solana_patchable_rows == 1
    assert report.no_address_rows == 0
    # Offline dry-run counts the unique address but must not resolve it.
    assert report.unique_addresses == 1
    assert report.changes == []


# --- API composition -------------------------------------------------------------


def test_pending_api_exposes_meta_and_cached_label(client, db, demo_user):
    upsert_label(db, _record("solana", JUPITER, "Jupiter Aggregator v6"))
    db.commit()
    row = _pending(
        db,
        user_id=demo_user.id,
        source="helius",
        external_id="helius:api:0",
        detail="Solana transfer",
        meta={
            "chain": "solana",
            "counterparty_address": JUPITER,
            "counterparty_label": UNKNOWN_LABEL,
            "counterparty_kind": "unknown",
            "counterparty_label_source": "fallback",
        },
    )
    headers = auth_header(client)
    response = client.get("/api/pending", headers=headers)
    assert response.status_code == 200, response.text
    item = next(entry for entry in response.json() if entry["id"] == row.id)
    assert item["counterparty_chain"] == "solana"
    assert item["counterparty_address"] == JUPITER
    assert item["counterparty_label"] == "Jupiter Aggregator v6"
    assert item["counterparty_kind"] == "contract"
    assert item["counterparty_label_source"] == "manual"


# --- Helius parser ---------------------------------------------------------------


def test_helius_parser_keeps_program_ids_and_chain():
    payload = [
        {
            "timestamp": 1767657600,
            "signature": "s" * 64,
            "type": "TRANSFER",
            "source": "JUPITER",
            "description": "test",
            "tokenTransfers": [
                {
                    "fromUserAccount": "11111111111111111111111111111111",
                    "toUserAccount": WALLET,
                    "mint": "So11111111111111111111111111111111111111112",
                    "tokenAmount": 0.1,
                }
            ],
            "instructions": [
                {"programId": SYSTEM, "innerInstructions": [{"programId": JUPITER}]}
            ],
        }
    ]
    rows = parse_helius_transactions(WALLET, payload)
    assert len(rows) == 1
    assert rows[0].raw["chain"] == "solana"
    assert rows[0].raw["program_ids"] == sorted([SYSTEM, JUPITER])
    assert rows[0].raw["tx_type"] == "TRANSFER"
    assert rows[0].raw["helius_source"] == "JUPITER"
    assert transfer_chain(rows[0]) == "solana"
