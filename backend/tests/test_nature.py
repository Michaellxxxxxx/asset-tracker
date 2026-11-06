"""Nature/safety classification is read-only and conservative."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from app.models import PendingConfirmation, UserWallet
from app.nature import (
    NatureContext,
    NatureResult,
    build_context,
    classify_nature,
    format_nature_tag,
    normalize_address,
)
from tests.conftest import auth_header


def _row(**overrides) -> PendingConfirmation:
    values = {
        "id": 1,
        "user_id": 1,
        "title": "测试流水",
        "detail": "",
        "amount_usdt": Decimal("10.00"),
        "status": "pending",
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return PendingConfirmation(**values)


def test_protocol_deposit_is_safe_and_keeps_enrich_explain() -> None:
    meta = {
        "counterparty": "0x2a66f992bf227d2e50ef19edd21503c3c4f3f682",
        "counterparty_label": "合约 · Helper（3Jane USD3 存入助手）",
        "explain": "向 3Jane 存入 2,666.32 USDC，同时收到 2,307.38 USD3 凭证",
        "is_contract": True,
        "sent_tokens": [{"symbol": "USDC", "amount": "2666.320174", "direction": "out"}],
        "received_tokens": [{"symbol": "USD3", "amount": "2307.381804", "direction": "in"}],
    }
    row = _row(
        id=249,
        source="zerion",
        direction="out",
        suggested_category="defi_deposit",
        title="zerion 协议存入 2666.320174 USDC",
        detail="向 3Jane 存入 2,666.32 USDC，同时收到 2,307.38 USD3 凭证；方向 out",
        amount_raw=Decimal("2666.320174"),
        asset="USDC",
        meta=meta,
    )

    result = classify_nature(row, NatureContext())

    assert result.nature == "protocol_deposit"
    assert result.safety == "safe"
    assert result.explain == meta["explain"]


def test_binance_roll_out_is_exchange_internal_for_897_and_898() -> None:
    for row_id in (897, 898):
        row = _row(
            id=row_id,
            source="binance",
            external_id=f"binance-{row_id}-roll_out",
            direction="out",
            title="币安 理财划转 Roll Out",
            detail="原始类型 Roll Out；方向 out；币安理财划转",
            suggested_category="transfer",
        )

        result = classify_nature(row)

        assert result.nature == "exchange_internal", row_id
        assert result.safety == "safe"
        assert "币安" in result.explain
        assert "账户" in result.explain


def test_binance_roll_out_old_withdraw_label_is_still_exchange_internal() -> None:
    """#897/#898 were stored before the attribution fix with withdraw/0.93."""
    row = _row(
        id=897,
        source="binance",
        external_id="binance-897-roll_out",
        direction="out",
        title="binance 提现 0.04630139 USDT",
        detail="交易所提现记录→withdraw；方向 out；原始类型 Roll Out",
        suggested_category="withdraw",
        suggested_confidence=Decimal("0.93"),
    )

    result = classify_nature(row)

    assert result.nature == "exchange_internal"
    assert result.safety == "safe"
    assert "资金仍在你的账户" in result.explain


def test_self_transfer_matches_own_wallet_case_insensitively() -> None:
    own = "0x1d72A5b4C9E2f8a1B3c4D5e6F708192a3b4c5d6e"
    row = _row(
        source="zerion",
        direction="out",
        meta={"counterparty": "0x1D72a5B4c9e2F8A1b3C4d5E6f708192A3b4C5d6E"},
    )

    result = classify_nature(
        row,
        NatureContext(own_addresses=frozenset({normalize_address(own)})),
    )

    assert result.nature == "self_transfer"
    assert result.safety == "safe"
    assert "0x1d72…5d6e" in result.explain
    assert "你自己的钱包" in result.explain


def test_unknown_external_address_is_review() -> None:
    row = _row(
        source="zerion",
        direction="out",
        title="提现",
        meta={"counterparty": "0xabCD00000000000000000000000000000000EF12"},
    )

    result = classify_nature(row, NatureContext())

    assert result.nature == "external_out"
    assert result.safety == "review"
    assert "未知外部地址" in result.explain
    assert "0xabcd…ef12" in result.explain


def test_insufficient_evidence_is_unknown_review() -> None:
    row = _row(source="zerion", direction="out", title="转出", detail="", meta=None)

    result = classify_nature(row, NatureContext())

    assert result.nature == "unknown"
    assert result.safety == "review"
    assert "证据不足" in result.explain


def test_mock_marker_wins_and_is_review() -> None:
    row = _row(
        id=251,
        source="binance",
        external_id="wd-binance-1",
        direction="out",
        title="币安 提现 10 USDT",
        detail="地址 TXexternal；txid 0xbinancewithdraw",
        suggested_category="withdraw",
    )

    result = classify_nature(row, NatureContext())

    assert result.nature == "test_data"
    assert result.safety == "review"
    assert "测试数据" in result.explain


def test_inflow_is_not_flagged_for_review() -> None:
    row = _row(source="zerion", direction="in", suggested_category="deposit")

    result = classify_nature(row, NatureContext())

    assert result.nature == "unknown"
    assert result.safety == "none"


def test_build_context_reads_user_wallets_only(db, demo_user) -> None:
    db.add(
        UserWallet(
            user_id=demo_user.id,
            chain="evm",
            address="0xAbCd00000000000000000000000000000000Ef12",
        )
    )
    db.commit()

    ctx = build_context(db, demo_user.id)

    assert "0xabcd00000000000000000000000000000000ef12" in ctx.own_addresses


def test_pending_api_returns_nature_without_writing_columns(client, db, demo_user) -> None:
    row = _row(
        user_id=demo_user.id,
        source="zerion",
        direction="out",
        title="转出到未知地址",
        detail="",
        meta=None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    assert row.meta is None
    assert "nature" not in PendingConfirmation.__table__.columns
    assert "safety" not in PendingConfirmation.__table__.columns

    response = client.get("/api/pending", headers=auth_header(client))

    assert response.status_code == 200, response.text
    payload = next(item for item in response.json() if item["id"] == row.id)
    assert payload["nature"] == "unknown"
    assert payload["safety"] == "review"
    assert "证据不足" in payload["explain"]

    db.expire_all()
    stored = db.get(PendingConfirmation, row.id)
    assert stored is not None
    assert stored.meta is None


def test_pending_api_marks_exchange_internal_safe(client, db, demo_user) -> None:
    row = _row(
        user_id=demo_user.id,
        source="binance",
        external_id="binance-897-roll_out",
        direction="out",
        title="币安 理财划转 Roll Out",
        detail="原始类型 Roll Out；方向 out",
        suggested_category="transfer",
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    response = client.get("/api/pending", headers=auth_header(client))

    assert response.status_code == 200, response.text
    payload = next(item for item in response.json() if item["id"] == row.id)
    assert payload["nature"] == "exchange_internal"
    assert payload["safety"] == "safe"
    assert "资金仍在你的账户" in payload["explain"]


def test_wechat_message_carries_safe_badge_and_no_address() -> None:
    from app.integrations.wechat.queue import format_pending_message

    row = _row(
        id=897,
        source="binance",
        external_id="binance-897-roll_out",
        direction="out",
        title="币安 理财划转 Roll Out",
        detail="原始类型 Roll Out；方向 out",
        suggested_category="transfer",
        ts=datetime(2026, 1, 6, 4, 20, tzinfo=timezone.utc),
    )

    text = format_pending_message(row)

    assert "性质：✅ 已识别 · 非外流（交易所内部划转）" in text
    assert "0x" not in text


def test_wechat_message_reviews_external_address_but_masks_it() -> None:
    from app.integrations.wechat.queue import format_pending_message

    row = _row(
        id=900,
        source="zerion",
        direction="out",
        meta={"counterparty": "0xabCD00000000000000000000000000000000EF12"},
    )

    text = format_pending_message(row)

    assert "性质：⚠️ 请核对（转给外部地址）" in text
    assert "0xabcd…ef12" not in text
    assert "0xabcd" not in text


def test_verified_protocol_contract_without_meta_is_safe() -> None:
    """Historical #249 has meta=NULL; the audited contract is still evidence."""
    row = _row(
        id=249,
        source="zerion",
        direction="out",
        title="zerion 提现 2666.320174 USDC",
        detail=(
            "链上流出，暂记为提现；方向 out；原始类型 deposit；"
            "地址 0x2a66f992bf227d2e50ef19edd21503c3c4f3f682；"
            "txid 0xca93c1b1c4d0b6099826c0e846b9f31cc3182a477e97c1a77cd2b21192a8a7ce"
        ),
        amount_raw=Decimal("2666.320174"),
        asset="USDC",
        suggested_category="withdraw",
        meta=None,
    )

    result = classify_nature(row, NatureContext())

    assert result.nature == "protocol_deposit"
    assert result.safety == "safe"
    assert "向 3Jane 存入 2,666.32 USDC" in result.explain
    assert "DeFi 仓位" in result.explain


def test_missing_direction_is_unknown_review() -> None:
    """No direction means the evidence is incomplete, not that it is benign."""
    row = _row(source="manual", direction=None, suggested_category=None)

    result = classify_nature(row, NatureContext())

    assert result.nature == "unknown"
    assert result.safety == "review"
    assert "无法确认是否外流" in result.explain


def test_format_nature_tag_always_has_a_badge() -> None:
    assert format_nature_tag(
        NatureResult(
            nature="exchange_internal",
            safety="safe",
            explain="",
        )
    ).startswith("性质：✅")
    assert format_nature_tag(
        NatureResult(nature="external_out", safety="review", explain="")
    ).startswith("性质：⚠️")
    assert format_nature_tag(
        NatureResult(nature="unknown", safety="none", explain="")
    ).startswith("性质：ℹ️")


def test_swap_with_same_tx_counterleg_is_safe() -> None:
    row = _row(
        source="zerion",
        direction="out",
        suggested_category="swap",
        meta={
            "is_contract": True,
            "received_tokens": [{"symbol": "USD3", "amount": "2307.38"}],
        },
    )

    result = classify_nature(row, NatureContext())

    assert result.nature == "swap"
    assert result.safety == "safe"
    assert "兑换" in result.explain
    assert "同 tx" in result.explain


def test_swap_without_counterleg_stays_review() -> None:
    row = _row(
        source="zerion",
        direction="out",
        suggested_category="swap",
        meta={"pair_kind": "bridge", "paired_same_tx": False},
    )

    result = classify_nature(row, NatureContext())

    assert result.nature == "bridge"
    assert result.safety == "review"
    assert "缺少同 tx" in result.explain


def test_exchange_income_is_not_internal_safe() -> None:
    row = _row(
        source="binance",
        direction="out",
        title="币安 Earn reward",
        detail="原始类型 transfer；方向 out",
        suggested_category="transfer",
    )

    result = classify_nature(row, NatureContext())

    assert result.nature != "exchange_internal"
    assert result.safety != "safe"


def test_describe_pending_uses_nature_explanation() -> None:
    from app.integrations.chain.enrich import describe_pending

    row = _row(
        source="binance",
        direction="out",
        title="币安 理财划转 Roll Out",
        detail="原始类型 Roll Out；方向 out",
        suggested_category="transfer",
    )

    assert describe_pending(row) == classify_nature(row).explain
    assert "资金仍在你的账户" in (describe_pending(row) or "")


def test_counterparty_address_meta_wins_over_detail_prose() -> None:
    """#305: meta has the real own-wallet address, detail has 地址→transfer."""
    own = "E2yWHexppjJFV1WoZTyKeD6rDf2cRt87szMuEwMwfzW9"
    row = _row(
        id=305,
        source="helius",
        direction="out",
        title="helius 内部划转 3000 EPJFWD",
        detail=(
            "链上流入来自已知自有地址→transfer；方向 out；原始类型 TRANSFER；"
            f"地址 {own}；txid 4Yt9"
        ),
        amount_raw=Decimal("3000"),
        asset="EPJFWD",
        meta={"counterparty_address": own},
    )

    result = classify_nature(
        row, NatureContext(own_addresses=frozenset({normalize_address(own)}))
    )

    assert result.nature == "self_transfer"
    assert result.safety == "safe"
    assert "你自己的钱包" in result.explain


def test_detail_address_parser_skips_arrow_label() -> None:
    """Without meta, the parser must not treat 地址→transfer as an address."""
    own = "E2yWHexppjJFV1WoZTyKeD6rDf2cRt87szMuEwMwfzW9"
    row = _row(
        source="helius",
        direction="out",
        detail=(
            "链上流入来自已知自有地址→transfer；方向 out；原始类型 TRANSFER；"
            f"地址 {own}"
        ),
        amount_raw=Decimal("3000"),
        asset="EPJFWD",
        meta=None,
    )

    result = classify_nature(
        row, NatureContext(own_addresses=frozenset({normalize_address(own)}))
    )

    assert result.nature == "self_transfer"
    assert result.safety == "safe"


def test_tiny_sol_outflow_is_dust_not_external() -> None:
    """#344–#361: historical 1e-9 SOL legs predate the ingestion dust filter."""
    row = _row(
        id=344,
        source="helius",
        direction="out",
        title="helius 提现 1e-9 SOL",
        detail="原始类型 TRANSFER；方向 out",
        amount_raw=Decimal("0.000000001"),
        amount_usdt=Decimal("0"),
        asset="SOL",
        meta={"counterparty_address": "2RPtv93NQ2tesEj5whXrXrwSyAdRUW417LLiAFGBL7eq"},
    )

    result = classify_nature(row, NatureContext())

    assert result.nature == "dust"
    assert result.safety == "none"
    assert "尘埃" in result.explain
    assert "0.000000001 SOL" in result.explain


def test_one_cent_quote_is_dust() -> None:
    """#335: 0.00009 SOL is quoted at exactly 0.01 USDT."""
    row = _row(
        id=335,
        source="helius",
        direction="out",
        detail="原始类型 COMPRESSED_NFT_MINT；方向 out",
        amount_raw=Decimal("0.00009"),
        amount_usdt=Decimal("0.01"),
        asset="SOL",
        meta={"counterparty_address": "7Wpp33Dn5KKUFjaij4zKYy1XZ9kdBtHjUatAT6NcjjGt"},
    )

    result = classify_nature(row, NatureContext())

    assert result.nature == "dust"
    assert result.safety == "none"


def test_small_but_real_sol_outflow_stays_external() -> None:
    """0.004 SOL is worth $0.42; it is not dust and must stay reviewable."""
    row = _row(
        id=440,
        source="helius",
        direction="out",
        title="helius 提现 0.004 SOL",
        detail="原始类型 TRANSFER；方向 out",
        amount_raw=Decimal("0.004"),
        amount_usdt=Decimal("0.42"),
        asset="SOL",
        meta={"counterparty_address": "ANik"},
    )

    result = classify_nature(row, NatureContext())

    assert result.nature == "external_out"
    assert result.safety == "review"


def test_wsol_truncated_asset_without_quote_is_dust() -> None:
    """#878: Helius stores the WSOL mint prefix as SO1111, quote is missing."""
    row = _row(
        id=878,
        source="helius",
        direction="out",
        detail="原始类型 TRANSFER；方向 out",
        amount_raw=Decimal("0.000002345"),
        amount_usdt=None,
        asset="SO1111",
        meta={"counterparty_address": "22HU"},
    )

    result = classify_nature(row, NatureContext())

    assert result.nature == "dust"
    assert result.safety == "none"


def test_unknown_token_without_quote_is_not_hidden_as_dust() -> None:
    """Without a quote we do not guess arbitrary token unit values."""
    row = _row(
        source="helius",
        direction="out",
        detail="原始类型 TRANSFER；方向 out",
        amount_raw=Decimal("0.000000001"),
        amount_usdt=None,
        asset="UNKNOW",
        meta={"counterparty_address": "9xQe"},
    )

    result = classify_nature(row, NatureContext())

    assert result.nature == "external_out"
    assert result.safety == "review"


def test_test_data_still_wins_over_dust() -> None:
    row = _row(
        source="helius",
        external_id="txexternal",
        direction="out",
        detail="原始类型 TRANSFER；方向 out",
        amount_raw=Decimal("0.000000001"),
        amount_usdt=Decimal("0"),
        asset="SOL",
        meta={"counterparty_address": "2RPtv93NQ2tesEj5whXrXrwSyAdRUW417LLiAFGBL7eq"},
    )

    result = classify_nature(row, NatureContext())

    assert result.nature == "test_data"
    assert result.safety == "review"


def test_native_stake_is_protocol_deposit() -> None:
    """#818: STAKE_SOL locks SOL in a user-controlled stake account."""
    row = _row(
        id=818,
        source="helius",
        direction="out",
        title="helius 提现 73.9 SOL",
        detail="原始类型 STAKE_SOL；方向 out",
        amount_raw=Decimal("73.9"),
        amount_usdt=Decimal("7726.06"),
        asset="SOL",
        meta={"counterparty_address": "AC8k"},
    )

    result = classify_nature(row, NatureContext())

    assert result.nature == "protocol_deposit"
    assert result.safety == "safe"
    assert "质押" in result.explain
    assert "你的" in result.explain


def test_native_unstake_is_protocol_redeem() -> None:
    row = _row(
        source="helius",
        direction="out",
        detail="原始类型 UNSTAKE_SOL；方向 out",
        amount_raw=Decimal("73.9"),
        amount_usdt=Decimal("7726.06"),
        asset="SOL",
        meta={"counterparty_address": "AC8k"},
    )

    result = classify_nature(row, NatureContext())

    assert result.nature == "protocol_redeem"
    assert result.safety == "safe"
    assert "解除" in result.explain
    assert "回到你的钱包" in result.explain
