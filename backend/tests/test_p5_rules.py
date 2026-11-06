"""Phase-5 rule hardening: no mechanical withdraw/deposit, pair swaps, dust."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.attribution import attribute
from app.integrations.chain.enrich import Enrichment
from app.integrations.chain.helius import parse_helius_transactions
from app.integrations.chain.routes import ROUTE_CONTRACTS
from app.integrations.types import NormalizedTransfer
from app.integrations.wechat.queue import format_pending_message
from app.models import PendingConfirmation
from app.nature import NatureContext, classify_nature
from app.sync.service import _enrich_outflows, _title, pair_chain_transfers
from tests.conftest import auth_header

SINCE = datetime(2026, 1, 1, tzinfo=timezone.utc)
OWNER = "So1DemoAddress"
TX_A = "0x" + "a" * 64
TX_B = "0x" + "b" * 64


def _transfer(**overrides) -> NormalizedTransfer:
    values = {
        "source": "zerion",
        "external_id": "ext-1",
        "ts": datetime(2026, 2, 1, tzinfo=timezone.utc),
        "asset": "USDC",
        "amount": Decimal("100"),
        "direction": "out",
        "address": None,
        "txid": TX_A,
        "raw_type": "",
        "note": "",
    }
    values.update(overrides)
    return NormalizedTransfer(**values)


def _row(**overrides) -> PendingConfirmation:
    values = {
        "id": 9001,
        "user_id": 1,
        "title": "zerion 待核实的链上转出 100 USDC",
        "detail": "",
        "amount_usdt": Decimal("100"),
        "amount_raw": Decimal("100"),
        "asset": "USDC",
        "status": "pending",
        "source": "zerion",
        "direction": "out",
        "suggested_category": "chain_out",
        "suggested_confidence": Decimal("0.4"),
        "created_at": SINCE,
    }
    values.update(overrides)
    return PendingConfirmation(**values)


def test_unknown_operation_is_chain_out_not_withdraw() -> None:
    suggestion = attribute(_transfer(raw_type="", note=""))

    assert suggestion.category == "chain_out"
    assert suggestion.category != "withdraw"
    assert suggestion.confidence < 0.8
    assert "待核实" in suggestion.reason


def test_trade_and_swap_raw_types_never_become_withdraw_or_deposit() -> None:
    for direction in ("out", "in"):
        suggestion = attribute(_transfer(direction=direction, raw_type="trade"))
        assert suggestion.category == "swap"
        assert suggestion.category not in {"withdraw", "deposit"}

    assert attribute(_transfer(raw_type="swap")).category == "swap"


def test_receive_mint_burn_execute_map_by_semantics() -> None:
    assert attribute(_transfer(direction="in", raw_type="receive")).category == "deposit"
    deposited = attribute(_transfer(raw_type="mint"))
    assert deposited.category == "defi_deposit"
    assert "非提现" in deposited.reason
    redeemed = attribute(_transfer(raw_type="burn"))
    assert redeemed.category == "defi_redeem"
    assert "非提现" in redeemed.reason
    assert attribute(_transfer(raw_type="execute")).category == "chain_out"


def test_low_confidence_title_says_pending_review() -> None:
    transfer = _transfer(raw_type="")
    assert "待核实" in _title(transfer, "chain_out", confidence=0.4)
    assert "提现" not in _title(transfer, "withdraw", confidence=0.55)
    assert "提现" in _title(transfer, "withdraw", confidence=0.93)


def test_same_tx_swap_legs_merge_into_one_suggestion() -> None:
    outgoing = _transfer(external_id="out-1", txid=TX_A)
    incoming = _transfer(
        external_id="in-1",
        txid=TX_A,
        direction="in",
        asset="USD3",
        amount=Decimal("99"),
    )

    pairs, skip = pair_chain_transfers([outgoing, incoming])

    assert (outgoing.source, outgoing.external_id) in pairs
    info = pairs[(outgoing.source, outgoing.external_id)]
    assert info.same_tx is True
    assert info.kind == "swap"
    assert (incoming.source, incoming.external_id) in skip


def test_same_tx_bridge_pair_is_tagged_bridge() -> None:
    outgoing = _transfer(external_id="out-2", txid=TX_A, raw_type="bridge")
    incoming = _transfer(
        external_id="in-2", txid=TX_A, direction="in", asset="USDC", amount=Decimal("99")
    )

    pairs, _ = pair_chain_transfers([outgoing, incoming])

    assert pairs[(outgoing.source, outgoing.external_id)].kind == "bridge"


def test_near_window_bridge_pair_is_not_same_tx_proof() -> None:
    bridge = next(addr for addr, kind in ROUTE_CONTRACTS.items() if kind == "bridge")
    outgoing = _transfer(
        external_id="out-3",
        txid=TX_A,
        address=bridge,
        ts=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )
    incoming = _transfer(
        external_id="in-3",
        txid=TX_B,
        direction="in",
        address=bridge,
        asset="USDC",
        amount=Decimal("99"),
        ts=outgoing.ts + timedelta(minutes=10),
    )

    pairs, skip = pair_chain_transfers([outgoing, incoming])
    info = pairs[(outgoing.source, outgoing.external_id)]

    assert info.kind == "bridge"
    assert info.same_tx is False
    assert (incoming.source, incoming.external_id) in skip


def test_unrelated_opposite_flow_is_not_paired() -> None:
    outgoing = _transfer(external_id="out-4", txid=TX_A, address="0x" + "1" * 40)
    incoming = _transfer(
        external_id="in-4",
        txid=TX_B,
        direction="in",
        address="0x" + "1" * 40,
        ts=outgoing.ts + timedelta(minutes=5),
    )

    pairs, skip = pair_chain_transfers([outgoing, incoming])

    assert pairs == {}
    assert skip == set()


def test_nature_swap_safe_only_with_same_tx_counterleg() -> None:
    paired = _row(
        suggested_category="swap",
        suggested_confidence=Decimal("0.85"),
        meta={
            "pair_kind": "swap",
            "paired_same_tx": True,
            "paired_legs": [
                {"direction": "out", "asset": "USDC", "amount": "100"},
                {"direction": "in", "asset": "USD3", "amount": "99"},
            ],
        },
    )
    result = classify_nature(paired, NatureContext())
    assert result.nature == "swap"
    assert result.safety == "safe"

    unproven = _row(
        suggested_category="swap",
        meta={"pair_kind": "bridge", "paired_same_tx": False},
    )
    bridge = classify_nature(unproven, NatureContext())
    assert bridge.nature == "bridge"
    assert bridge.safety == "review"
    assert "缺少同 tx" in bridge.explain


def test_helius_zero_and_dust_rows_are_not_imported(caplog) -> None:
    timestamp = int(datetime(2026, 2, 1, tzinfo=timezone.utc).timestamp())
    payload = [
        {
            "timestamp": timestamp,
            "signature": "sig-bookkeeping",
            "type": "REFRESH_OBLIGATION",
            "tokenTransfers": [
                {
                    "mint": "mint-usdc",
                    "tokenSymbol": "USDC",
                    "tokenAmount": "0",
                    "toUserAccount": OWNER,
                    "fromUserAccount": "other",
                }
            ],
        },
        {
            "timestamp": timestamp,
            "signature": "sig-dust",
            "type": "TRANSFER",
            "tokenTransfers": [
                {
                    "mint": "mint-usdc",
                    "tokenSymbol": "USDC",
                    "tokenAmount": "0.000001",
                    "toUserAccount": "other",
                    "fromUserAccount": OWNER,
                }
            ],
        },
        {
            "timestamp": timestamp,
            "signature": "sig-real",
            "type": "TRANSFER",
            "tokenTransfers": [
                {
                    "mint": "mint-usdc",
                    "tokenSymbol": "USDC",
                    "tokenAmount": "0.5",
                    "toUserAccount": "other",
                    "fromUserAccount": OWNER,
                }
            ],
        },
        {
            "timestamp": timestamp,
            "signature": "sig-init",
            "type": "INITIALIZE_ACCOUNT",
            "nativeTransfers": [
                {
                    "amount": "2000000",
                    "toUserAccount": "other",
                    "fromUserAccount": OWNER,
                }
            ],
        },
    ]

    with caplog.at_level("INFO"):
        items = parse_helius_transactions(OWNER, payload)

    assert len(items) == 1
    assert items[0].asset == "USDC"
    assert items[0].amount == Decimal("0.5")
    assert "helius ignored 3" in caplog.text


def test_enrich_batches_more_than_25_and_dedupes_txids() -> None:
    transfers = [
        _transfer(
            external_id=f"out-{index}",
            txid="0x" + f"{index:064x}",
        )
        for index in range(30)
    ]
    transfers.append(_transfer(external_id="duplicate", txid=transfers[0].txid))
    calls: list[str] = []
    sleeps: list[float] = []

    def fake_enrich(transfer, *, own_addresses):  # noqa: ANN001, ARG001
        calls.append(transfer.txid or "")
        return Enrichment(tx_hash=transfer.txid)

    enrichments, deferred = _enrich_outflows(
        transfers,
        set(),
        enrich_fn=fake_enrich,
        sleep_fn=sleeps.append,
    )

    assert len(enrichments) == 30
    assert len(calls) == 30
    assert len(set(calls)) == 30
    assert sleeps == [0.2]
    assert deferred == set()


def test_enrich_budget_defers_remaining_txids_without_withdraw_fallback() -> None:
    transfers = [
        _transfer(external_id=f"budget-{index}", txid="0x" + f"{index + 100:064x}")
        for index in range(3)
    ]
    ticks = iter([0.0, 999.0, 999.0, 999.0, 999.0])

    enrichments, deferred = _enrich_outflows(
        transfers,
        set(),
        enrich_fn=lambda transfer, **_: Enrichment(tx_hash=transfer.txid),
        clock=lambda: next(ticks, 999.0),
        total_budget=10.0,
    )

    assert enrichments == {}
    assert deferred == {transfer.txid for transfer in transfers}


def test_low_confidence_historical_title_is_read_only(client, db, demo_user) -> None:
    row = _row(
        user_id=demo_user.id,
        title="zerion 提现 5 USDC",
        suggested_category="withdraw",
        suggested_confidence=Decimal("0.55"),
        external_id="hist-1",
    )
    db.add(row)
    db.commit()

    response = client.get("/api/pending", headers=auth_header(client))
    assert response.status_code == 200
    item = next(value for value in response.json() if value["id"] == row.id)
    assert "待核实的链上转出" in item["title"]
    assert "提现" not in item["title"]

    db.refresh(row)
    assert row.title == "zerion 提现 5 USDC"


def test_historical_low_confidence_protocol_title_uses_nature(client, db, demo_user) -> None:
    row = _row(
        user_id=demo_user.id,
        title="zerion 提现 2666.32 USDC",
        suggested_category="withdraw",
        suggested_confidence=Decimal("0.55"),
        external_id="hist-249",
        meta={
            "is_contract": True,
            "counterparty": "0x2a66f992bf227d2e50ef19edd21503c3c4f3f682",
            "received_tokens": [{"symbol": "USD3", "amount": "2307.38"}],
        },
    )
    db.add(row)
    db.commit()

    response = client.get("/api/pending", headers=auth_header(client))
    item = next(value for value in response.json() if value["id"] == row.id)
    assert item["nature"] == "protocol_deposit"
    assert "协议存入" in item["title"]
    assert "提现" not in item["title"]


def test_review_push_copy_asks_for_second_confirmation() -> None:
    text = format_pending_message(
        _row(suggested_category="withdraw", suggested_confidence=Decimal("0.55"))
    )

    assert "确认入账" in text
    assert "二次确认" in text
