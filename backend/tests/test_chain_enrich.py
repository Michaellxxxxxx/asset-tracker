"""Blockscout enrichment for pending #249 and the classification fallbacks."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select, text

from app.attribution import Attribution, attribute
from app.integrations.chain.enrich import (
    BlockscoutClient,
    build_enrichment,
    clear_cache,
    describe_pending,
    refine_attribution,
)
from app.integrations.types import NormalizedTransfer
from app.models import LedgerEntry, PendingConfirmation
from app.integrations.wechat.service import confirm_pending_item
from tests.conftest import auth_header

FIXTURES = Path(__file__).parent / "fixtures" / "chain"
TX = "0xca93c1b1c4d0b6099826c0e846b9f31cc3182a477e97c1a77cd2b21192a8a7ce"
USER = "0x1d72516e72f34a8de00099a7d8bbccd5d005e916"
HELPER = "0x2a66f992bf227d2e50ef19edd21503c3c4f3f682"
SINCE = datetime(2026, 1, 6, tzinfo=timezone.utc)


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


class _Response:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict:
        return self._payload


class _FakeHttp:
    """Offline Blockscout: path -> recorded fixture. Counts every call."""

    def __init__(
        self,
        *,
        address: dict | None = None,
        tx: dict | None = None,
        token_transfers: dict | None = None,
        error: Exception | None = None,
    ) -> None:
        self.address = address if address is not None else _load("blockscout_249_address.json")
        self.tx = tx if tx is not None else _load("blockscout_249_tx.json")
        self.token_transfers = (
            token_transfers
            if token_transfers is not None
            else _load("blockscout_249_token_transfers.json")
        )
        self.error = error
        self.calls: list[str] = []

    def get(self, url: str, params=None, timeout=None):  # noqa: ANN001 - httpx shape
        self.calls.append(url)
        if self.error is not None:
            raise self.error
        if "token-transfers" in url:
            return _Response(self.token_transfers)
        if "/transactions/" in url:
            return _Response(self.tx)
        if "/addresses/" in url:
            return _Response(self.address)
        return _Response({}, status_code=404)


@pytest.fixture(autouse=True)
def _cache() -> None:
    clear_cache()
    yield
    clear_cache()


def _transfer(**overrides) -> NormalizedTransfer:
    base = dict(
        source="zerion",
        external_id=f"zerion:{TX}:1",
        ts=SINCE,
        asset="USDC",
        amount=Decimal("2666.320174"),
        direction="out",
        address=HELPER,
        txid=TX,
        raw_type="deposit",
        note="deposit",
        raw={"address": USER, "operation": "deposit", "chain": "ethereum"},
    )
    base.update(overrides)
    return NormalizedTransfer(**base)


def _enrich(http: _FakeHttp | None = None):
    return build_enrichment(
        chain="ethereum",
        tx_hash=TX,
        user_address=USER,
        counterparty=HELPER,
        operation="deposit",
        client=BlockscoutClient(client=http or _FakeHttp()),
    )


def test_blockscout_fixture_identifies_contract_and_received_usd3() -> None:
    enrichment = _enrich()
    assert enrichment is not None
    assert enrichment.is_contract is True
    assert enrichment.name == "Helper"
    assert enrichment.method == "deposit"
    assert [token.symbol for token in enrichment.received] == ["USD3"]
    assert enrichment.received[0].amount == Decimal("2307.381804")
    assert "Helper" in enrichment.counterparty_label
    assert "3Jane USD3 存入助手" in enrichment.counterparty_label
    assert enrichment.explain.startswith("向 3Jane 存入 2,666.32 USDC")
    assert "同时收到 2,307.38 USD3 凭证" in enrichment.explain


def test_249_outflow_is_not_withdraw() -> None:
    enrichment = _enrich()
    assert enrichment is not None
    base = Attribution("withdraw", 0.55, "链上流出，暂记为提现")
    refined = refine_attribution(base, _transfer(), enrichment)
    assert refined.category != "withdraw"
    assert refined.category == "defi_deposit"
    assert "收到 2,307.38 USD3" in refined.reason
    assert "非提现" in refined.reason


def test_pending_describe_uses_meta() -> None:
    enrichment = _enrich()
    assert enrichment is not None
    pending = PendingConfirmation(
        user_id=1,
        title="zerion 提现 2666.320174 USDC",
        detail="链上流出，暂记为提现",
        status="pending",
        suggested_category="withdraw",
        meta=enrichment.to_meta(),
    )
    text = describe_pending(pending)
    assert text is not None
    assert text.startswith("向 3Jane 存入")
    assert "同时收到 2,307.38 USD3 凭证" in text
    assert pending.counterparty_label and "Helper" in pending.counterparty_label
    assert pending.received_tokens[0]["symbol"] == "USD3"


def test_plain_outflow_to_eoa_stays_withdraw() -> None:
    address = {
        "hash": "0x000000000000000000000000000000000000dead",
        "is_contract": False,
        "name": None,
        "ens_domain_name": None,
        "public_tags": [],
        "metadata": None,
    }
    tx = {
        "hash": TX,
        "method": "transfer",
        "status": "ok",
        "to": {"hash": "0x000000000000000000000000000000000000dead", "is_contract": False},
    }
    http = _FakeHttp(address=address, tx=tx, token_transfers={"items": []})
    enrichment = _enrich(http)
    assert enrichment is not None
    assert enrichment.is_contract is False
    transfer = _transfer(address=tx["to"]["hash"], raw_type="transfer", note="")
    refined = refine_attribution(
        Attribution("withdraw", 0.55, "链上流出，暂记为提现"), transfer, enrichment
    )
    assert refined.category == "withdraw"
    assert "普通钱包" in enrichment.counterparty_label


def test_blockscout_timeout_degrades_without_raising() -> None:
    http = _FakeHttp(error=httpx.TimeoutException("boom"))
    enrichment = _enrich(http)
    assert enrichment is not None
    assert enrichment.error
    assert enrichment.received == ()
    assert "未标记地址" in enrichment.counterparty_label
    # A failed lookup must not turn an unknown outflow into a DeFi deposit.
    refined = refine_attribution(
        Attribution("withdraw", 0.55, "链上流出，暂记为提现"), _transfer(), enrichment
    )
    assert refined.category == "withdraw"


def test_enrichment_cache_hit_skips_second_lookup() -> None:
    http = _FakeHttp()
    first = _enrich(http)
    second = _enrich(http)
    assert first is not None and second is first
    assert len(http.calls) == 3  # tx + address + token-transfers


def test_zerion_deposit_operation_is_not_withdraw_without_blockscout() -> None:
    base = attribute(_transfer())
    assert base.category == "defi_deposit"
    assert "非提现" in base.reason


def test_pending_api_returns_enrichment_fields(client, db, demo_user) -> None:
    enrichment = _enrich()
    assert enrichment is not None
    db.add(
        PendingConfirmation(
            user_id=demo_user.id,
            title="zerion 提现 2666.320174 USDC",
            detail="链上流出，暂记为提现",
            status="pending",
            source="zerion",
            external_id=f"zerion:{TX}:1",
            direction="out",
            asset="USDC",
            amount_raw=Decimal("2666.320174"),
            suggested_category="defi_deposit",
            suggested_confidence=Decimal("0.900"),
            suggested_reason="同 tx 收到 2307.38 USD3",
            meta=enrichment.to_meta(),
        )
    )
    db.commit()

    response = client.get("/api/pending", headers=auth_header(client))
    assert response.status_code == 200, response.text
    row = response.json()[0]
    assert row["counterparty_label"].startswith("合约 · Helper")
    assert "同时收到 2,307.38 USD3 凭证" in row["explain"]
    assert row["received_tokens"][0]["symbol"] == "USD3"
    assert row["received_tokens"][0]["amount"] == pytest.approx(2307.381804)


def test_confirm_defi_deposit_writes_a_valid_ledger_kind(db, demo_user) -> None:
    pending = PendingConfirmation(
        user_id=demo_user.id,
        title="zerion 协议存入 2666.32 USDC",
        detail="同 tx 收到 2307.38 USD3",
        status="pending",
        direction="out",
        asset="USDC",
        amount_usdt=Decimal("2666.32"),
        suggested_category="defi_deposit",
        ts=SINCE,
    )
    db.add(pending)
    db.commit()
    db.refresh(pending)

    confirmed = confirm_pending_item(db, demo_user.id, pending.id, "defi_deposit")
    assert confirmed.final_category == "defi_deposit"
    entry = db.scalar(select(LedgerEntry).where(LedgerEntry.id == confirmed.ledger_entry_id))
    assert entry is not None
    assert entry.kind == "defi_deposit"


def test_run_sync_persists_enrichment_on_new_pending(db, demo_user, monkeypatch) -> None:
    """The sync wiring stores meta and the refined category, not just the helper."""
    from app.sync import service as sync_service

    enrichment = _enrich()
    assert enrichment is not None

    class _Prices:
        def to_usdt(self, asset: str, amount: Decimal) -> Decimal:
            return Decimal("2666.32")

    monkeypatch.setattr(
        sync_service,
        "_collect",
        lambda **_kwargs: ([_transfer()], {}, ()),
    )
    monkeypatch.setattr(sync_service, "_price_service", lambda _live: _Prices())
    monkeypatch.setattr(
        sync_service,
        "enrich_transfer",
        lambda transfer, own_addresses=None: enrichment,
    )
    monkeypatch.setattr(
        "app.sync.positions_hook.sync_positions_for_user",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(sync_service, "write_sync_snapshot", lambda *_args, **_kwargs: None)

    result = sync_service.run_sync(demo_user.id, "live", db=db, since=SINCE)
    assert result.enqueued == 1

    row = db.scalar(
        select(PendingConfirmation).where(
            PendingConfirmation.user_id == demo_user.id,
            PendingConfirmation.external_id == f"zerion:{TX}:1",
        )
    )
    assert row is not None
    assert row.suggested_category == "defi_deposit"
    assert row.title.startswith("zerion 协议存入")
    assert row.detail.startswith("向 3Jane 存入")
    assert row.meta and row.meta["received_tokens"][0]["symbol"] == "USD3"
    assert describe_pending(row) == row.meta["explain"]


def test_unenriched_pending_meta_is_sql_null(db, demo_user) -> None:
    """Historical-style rows must not turn into JSON ``null`` values."""
    row = PendingConfirmation(
        user_id=demo_user.id,
        title="普通提现",
        detail="链上流出",
        status="pending",
        direction="out",
    )
    db.add(row)
    db.commit()
    is_null = db.execute(
        text("select meta is null from pending_confirmations where id = :id"),
        {"id": row.id},
    ).scalar_one()
    assert bool(is_null) is True
