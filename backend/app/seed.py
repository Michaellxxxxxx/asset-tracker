from __future__ import annotations

import json
import logging
import random
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .auth import hash_password
from .crypto import encrypt_text
from .models import ApiCredential, LedgerEntry, PendingConfirmation, Snapshot, User

logger = logging.getLogger(__name__)

SNAPSHOT_START = date(2026, 1, 1)
SNAPSHOT_END = date(2026, 10, 20)
INITIAL_NAV = Decimal("10000.00")

# date, kind, signed amount, note, local time of the ledger entry
FLOW_PLAN: list[tuple[date, str, Decimal, str, time]] = [
    (date(2025, 12, 31), "deposit", Decimal("10000.00"), "期初入金（演示）", time(9, 0)),
    (date(2026, 3, 5), "deposit", Decimal("2500.00"), "三月定投入金", time(10, 0)),
    (date(2026, 6, 18), "deposit", Decimal("1800.00"), "追加保证金", time(9, 30)),
    (date(2026, 8, 26), "withdraw", Decimal("-1200.00"), "部分利润提取", time(14, 0)),
    (date(2026, 9, 30), "withdraw", Decimal("-500.00"), "季度费用提取", time(11, 0)),
]

PENDING_PLAN: list[tuple[str, str, Decimal | None, str, datetime, datetime | None]] = [
    (
        "确认 9 月利润提取",
        "链上提现 500 USDT 已发起，网络手续费 1.2 USDT。核对到账金额后确认。",
        Decimal("500.00"),
        "pending",
        datetime(2026, 10, 1, 9, 20, tzinfo=timezone.utc),
        None,
    ),
    (
        "核对 8 月提现记录",
        "8 月 26 日提取 1,200 USDT，已核对到账，保留为已确认记录。",
        Decimal("1200.00"),
        "confirmed",
        datetime(2026, 8, 27, 10, 5, tzinfo=timezone.utc),
        datetime(2026, 8, 27, 10, 8, tzinfo=timezone.utc),
    ),
    (
        "复核 Binance 只读凭证",
        "演示凭证以 AES-GCM 加密保存，仅用于验证存储流程，不会连接交易所。",
        None,
        "pending",
        datetime(2026, 10, 10, 16, 40, tzinfo=timezone.utc),
        None,
    ),
    (
        "确认 Q3 净值快照",
        "2026 年第三季度净值快照已生成，等待复核后归档。",
        None,
        "pending",
        datetime(2026, 10, 5, 11, 15, tzinfo=timezone.utc),
        None,
    ),
]


def _build_snapshots() -> list[tuple[datetime, Decimal]]:
    """Build a smooth, volatile daily NAV path with deterministic demo data.

    Deposits/withdrawals are applied at end-of-day, after that day's market
    move. This lines up with the TWR sub-period formula in twr.py.
    """
    rng = random.Random(42)
    simulated_flows = {
        flow_date: amount
        for flow_date, kind, amount, _, _ in FLOW_PLAN
        if flow_date >= SNAPSHOT_START and kind in ("deposit", "withdraw")
    }

    nav = INITIAL_NAV
    first_ts = datetime.combine(SNAPSHOT_START, time(23, 59, 59), tzinfo=timezone.utc)
    points: list[tuple[datetime, Decimal]] = [(first_ts, nav)]

    total_days = (SNAPSHOT_END - SNAPSHOT_START).days
    for offset in range(1, total_days + 1):
        current_day = SNAPSHOT_START + timedelta(days=offset)
        daily_return = Decimal(str(max(-0.04, min(0.04, rng.gauss(0.0005, 0.006)))))
        nav = (nav * (Decimal("1") + daily_return)).quantize(Decimal("0.01"))
        nav += simulated_flows.get(current_day, Decimal("0"))
        nav = max(nav, Decimal("1.00"))
        ts = datetime.combine(current_day, time(23, 59, 59), tzinfo=timezone.utc)
        points.append((ts, nav))

    return points


def seed_database(db: Session) -> None:
    existing_users = db.scalar(select(func.count()).select_from(User)) or 0
    if existing_users > 0:
        logger.info("Seed skipped: users already exist")
        return

    try:
        admin = User(
            username="admin",
            hashed_password=hash_password("admin123"),
            is_admin=True,
            returns_start_date=date(2026, 10, 23),
        )
        demo = User(
            username="demo",
            hashed_password=hash_password("demo123"),
            is_admin=False,
            # P18: demo data stays for history, but the account cannot log in.
            is_active=False,
            token_version=1,
        )
        db.add_all([admin, demo])
        db.flush()

        snapshots = [
            Snapshot(user_id=demo.id, ts=ts, nav_usdt=nav, source="seed")
            for ts, nav in _build_snapshots()
        ]
        db.add_all(snapshots)

        ledger_entries = [
            LedgerEntry(
                user_id=demo.id,
                ts=datetime.combine(flow_date, at, tzinfo=timezone.utc),
                amount_usdt=amount,
                kind=kind,
                note=note,
            )
            for flow_date, kind, amount, note, at in FLOW_PLAN
        ]
        db.add_all(ledger_entries)

        demo_payload = json.dumps(
            {
                "api_key": "demo-not-a-real-api-key",
                "secret": "demo-not-a-real-secret",
                "permissions": "read-only",
            },
            ensure_ascii=False,
        )
        db.add(
            ApiCredential(
                user_id=demo.id,
                exchange_name="binance_demo",
                encrypted_payload=encrypt_text(demo_payload),
                created_at=datetime(2026, 1, 2, 8, 0, tzinfo=timezone.utc),
            )
        )

        for title, detail, amount, status, created_at, confirmed_at in PENDING_PLAN:
            db.add(
                PendingConfirmation(
                    user_id=demo.id,
                    title=title,
                    detail=detail,
                    amount_usdt=amount,
                    status=status,
                    created_at=created_at,
                    confirmed_at=confirmed_at,
                )
            )

        db.commit()
        logger.info(
            "Seeded demo data: 2 users, %s snapshots, %s ledger entries, 1 credential",
            len(snapshots),
            len(ledger_entries),
        )
    except Exception:
        db.rollback()
        raise
