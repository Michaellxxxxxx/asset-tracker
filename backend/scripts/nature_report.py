#!/usr/bin/env python3
"""Read-only nature distribution report for existing pending rows.

Run inside the app container (or any environment with DATABASE_URL set):

    python /app/scripts/nature_report.py --status pending

The script only issues SELECTs and always rolls the session back.  It never
writes a nature column, never changes ``meta``, and never touches the ledger.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from app.db import SessionLocal
from app.models import PendingConfirmation
from app.nature import NatureContext, build_context, classify_nature


def _counter(values: list[str]) -> dict[str, int]:
    return dict(sorted(Counter(values).items()))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--status",
        default="pending",
        choices=("pending", "confirmed", "rejected", "all"),
        help="which rows to inspect (default: pending)",
    )
    parser.add_argument(
        "--details",
        action="store_true",
        help="include id/status/nature/safety per row (no addresses or amounts)",
    )
    args = parser.parse_args()

    session = SessionLocal()
    try:
        stmt = select(PendingConfirmation).order_by(PendingConfirmation.id)
        if args.status != "all":
            stmt = stmt.where(PendingConfirmation.status == args.status)
        rows = list(session.scalars(stmt).all())

        contexts: dict[int, NatureContext] = {}
        natures: list[str] = []
        safeties: list[str] = []
        statuses: list[str] = []
        pairs: list[str] = []
        details: list[dict[str, Any]] = []
        for row in rows:
            ctx = contexts.get(row.user_id)
            if ctx is None:
                ctx = build_context(session, row.user_id)
                contexts[row.user_id] = ctx
            result = classify_nature(row, ctx)
            natures.append(result.nature)
            safeties.append(result.safety)
            statuses.append(str(row.status))
            pairs.append(f"{result.nature}/{result.safety}")
            if args.details:
                details.append(
                    {
                        "id": row.id,
                        "status": row.status,
                        "nature": result.nature,
                        "safety": result.safety,
                    }
                )

        report: dict[str, Any] = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "filter_status": args.status,
            "rows": len(rows),
            "users": len(contexts),
            "by_nature": _counter(natures),
            "by_safety": _counter(safeties),
            "by_status": _counter(statuses),
            "by_nature_safety": _counter(pairs),
            "read_only": True,
        }
        if args.details:
            report["details"] = details
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    finally:
        # SELECT-only, but make the no-write contract explicit.
        session.rollback()
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
