"""Read-time API fix for historical Roll/Earn rows mislabelled as withdraw/deposit."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from app.models import PendingConfirmation
from tests.conftest import auth_header

TXID = "0x" + "a" * 64
WITHDRAW_ADDRESS = "0xabCD00000000000000000000000000000000EF12"


def _row(user_id: int, **overrides) -> PendingConfirmation:
    values = {
        "user_id": user_id,
        "title": "测试流水",
        "detail": "",
        "amount_usdt": Decimal("10.00"),
        "status": "pending",
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return PendingConfirmation(**values)


def _item(client, row_id: int) -> dict:
    response = client.get("/api/pending", headers=auth_header(client))
    assert response.status_code == 200, response.text
    return next(value for value in response.json() if value["id"] == row_id)


def test_old_roll_out_withdraw_rows_are_read_as_transfer(client, db, demo_user) -> None:
    """#897/#898: 0.93-confidence old rows still say 提现/withdraw in storage."""
    rows = []
    for row_id in (897, 898):
        row = _row(
            user_id=demo_user.id,
            source="binance",
            external_id=f"binance-{row_id}-roll_out",
            direction="out",
            title="binance 提现 0.04630139 USDT",
            detail="交易所提现记录→withdraw；方向 out；原始类型 Roll Out",
            asset="USDT",
            amount_raw=Decimal("0.04630139"),
            amount_usdt=Decimal("0.05"),
            suggested_category="withdraw",
            suggested_confidence=Decimal("0.93"),
            suggested_reason="交易所提现记录→withdraw",
        )
        db.add(row)
        rows.append(row)
    db.commit()
    for row in rows:
        db.refresh(row)

    for row in rows:
        item = _item(client, row.id)

        assert item["suggested_category"] == "transfer", row.id
        assert item["nature"] == "exchange_internal", row.id
        assert item["safety"] == "safe", row.id
        assert "内部划转" in item["title"], row.id
        assert "提现" not in item["title"], row.id
        assert "提现" not in (item["suggested_reason"] or ""), row.id
        assert "→transfer" in item["suggested_reason"], row.id

        db.expire_all()
        stored = db.get(PendingConfirmation, row.id)
        assert stored is not None
        assert stored.suggested_category == "withdraw"
        assert stored.title == "binance 提现 0.04630139 USDT"
        assert "提现" in (stored.suggested_reason or "")


def test_old_roll_in_deposit_row_is_read_as_transfer(client, db, demo_user) -> None:
    """Symmetric Roll In: direction=in makes classify_nature stop at unknown."""
    row = _row(
        user_id=demo_user.id,
        source="binance",
        external_id="binance-899-roll_in",
        direction="in",
        title="binance 充值 100 USDT",
        detail="交易所充值记录→deposit；方向 in；原始类型 Roll In",
        asset="USDT",
        amount_raw=Decimal("100"),
        amount_usdt=Decimal("100"),
        suggested_category="deposit",
        suggested_confidence=Decimal("0.93"),
        suggested_reason="交易所充值记录→deposit",
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    item = _item(client, row.id)

    assert item["suggested_category"] == "transfer"
    assert item["nature"] == "exchange_internal"
    assert item["safety"] == "safe"
    assert "内部划转" in item["title"]
    assert "充值" not in item["title"]
    assert "充值" not in (item["suggested_reason"] or "")
    assert "→transfer" in item["suggested_reason"]

    db.expire_all()
    stored = db.get(PendingConfirmation, row.id)
    assert stored is not None
    assert stored.suggested_category == "deposit"
    assert stored.title == "binance 充值 100 USDT"
    assert "充值" in (stored.suggested_reason or "")


def test_real_withdrawal_with_address_and_txid_is_untouched(client, db, demo_user) -> None:
    row = _row(
        user_id=demo_user.id,
        source="binance",
        external_id="binance-real-withdraw",
        direction="out",
        title="binance 提现 20 USDT",
        detail=(
            "交易所提现记录→withdraw；方向 out；原始类型 Withdrawal；"
            f"地址 {WITHDRAW_ADDRESS}；txid {TXID}"
        ),
        asset="USDT",
        amount_raw=Decimal("20"),
        amount_usdt=Decimal("20"),
        suggested_category="withdraw",
        suggested_confidence=Decimal("0.93"),
        suggested_reason="交易所提现记录→withdraw",
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    item = _item(client, row.id)

    assert item["suggested_category"] == "withdraw"
    assert item["nature"] != "exchange_internal"
    assert "提现" in item["title"]
    assert "内部划转" not in item["title"]

    db.expire_all()
    stored = db.get(PendingConfirmation, row.id)
    assert stored is not None
    assert stored.suggested_category == "withdraw"
    assert stored.title == "binance 提现 20 USDT"


def test_old_earn_savings_principal_is_read_as_transfer(client, db, demo_user) -> None:
    row = _row(
        user_id=demo_user.id,
        source="binance",
        external_id="binance-earn-principal",
        direction="out",
        title="binance 提现 100 USDT",
        detail="交易所提现记录→withdraw；方向 out；原始类型 Flexible Savings",
        asset="USDT",
        amount_raw=Decimal("100"),
        amount_usdt=Decimal("100"),
        suggested_category="withdraw",
        suggested_confidence=Decimal("0.93"),
        suggested_reason="交易所提现记录→withdraw",
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    item = _item(client, row.id)

    assert item["suggested_category"] == "transfer"
    assert item["nature"] == "exchange_internal"
    assert "内部划转" in item["title"]
    assert "提现" not in item["title"]


def test_earn_reward_deposit_is_not_treated_as_internal(client, db, demo_user) -> None:
    """Income is not an internal principal move, even when stored as deposit."""
    row = _row(
        user_id=demo_user.id,
        source="binance",
        external_id="binance-earn-reward",
        direction="in",
        title="binance 充值 0.01 USDT",
        detail="交易所充值记录→deposit；方向 in；原始类型 Earn reward",
        asset="USDT",
        amount_raw=Decimal("0.01"),
        amount_usdt=Decimal("0.01"),
        suggested_category="deposit",
        suggested_confidence=Decimal("0.93"),
        suggested_reason="交易所充值记录→deposit",
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    item = _item(client, row.id)

    assert item["suggested_category"] == "deposit"
    assert item["nature"] != "exchange_internal"
    assert "充值" in item["title"]
    assert "内部划转" not in item["title"]

    db.expire_all()
    stored = db.get(PendingConfirmation, row.id)
    assert stored is not None
    assert stored.suggested_category == "deposit"
    assert stored.title == "binance 充值 0.01 USDT"


def test_classify_exchange_internal_is_direction_independent() -> None:
    from app.nature import classify_exchange_internal

    roll_out = _row(
        1,
        source="binance",
        direction="out",
        title="binance 提现 1 USDT",
        detail="原始类型 Roll Out；方向 out",
        suggested_category="withdraw",
    )
    roll_in = _row(
        1,
        source="binance",
        direction="in",
        title="binance 充值 1 USDT",
        detail="原始类型 Roll In；方向 in",
        suggested_category="deposit",
    )
    reward = _row(
        1,
        source="binance",
        direction="in",
        title="binance 充值 0.01 USDT",
        detail="原始类型 Earn reward；方向 in",
        suggested_category="deposit",
    )
    real_withdraw = _row(
        1,
        source="binance",
        direction="out",
        title="binance 提现 1 USDT",
        detail=f"原始类型 Withdrawal；地址 {WITHDRAW_ADDRESS}；txid {TXID}",
        suggested_category="withdraw",
    )

    for row in (roll_out, roll_in):
        result = classify_exchange_internal(row)
        assert result is not None
        assert result.nature == "exchange_internal"
        assert result.safety == "safe"

    assert classify_exchange_internal(reward) is None
    assert classify_exchange_internal(real_withdraw) is None
