"""Read-only NAV identity check for one user. Prints JSON, writes nothing.

    python -m app.sync.reconcile_cli --user admin

Identity: flat holdings − covered + open position net = current snapshot nav.
Exit 0 when the latest sync snapshot folds and the residual is within one cent.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[3] / ".env")

from sqlalchemy import select

from ..db import SessionLocal, wait_for_db
from ..models import Position, Snapshot, User
from ..nav_reconcile import reconcile_nav


def reconcile_user(username: str) -> dict:
    wait_for_db()
    db = SessionLocal()
    try:
        user = db.scalar(select(User).where(User.username == username))
        if user is None:
            return {"ok": False, "error": "user_not_found", "user": username}
        snapshot = db.scalar(
            select(Snapshot)
            .where(Snapshot.user_id == user.id, Snapshot.source == "sync")
            .order_by(Snapshot.ts.desc(), Snapshot.id.desc())
            .limit(1)
        )
        if snapshot is None:
            return {"ok": False, "error": "no_sync_snapshot", "user": username}
        positions = [
            {
                "status": row.status,
                "protocol": row.protocol,
                "net_value_usdt": str(row.net_value_usdt or 0),
                "label": row.label,
            }
            for row in db.scalars(
                select(Position).where(Position.user_id == user.id, Position.status == "open")
            ).all()
        ]
        report = reconcile_nav(
            nav_usdt=snapshot.nav_usdt,
            breakdown=snapshot.breakdown,
            positions=positions,
        )
        report["user"] = username
        report["snapshot_id"] = snapshot.id
        report["snapshot_ts"] = snapshot.ts.isoformat() if snapshot.ts else None
        return report
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only NAV identity check")
    parser.add_argument("--user", required=True)
    args = parser.parse_args(argv)
    report = reconcile_user(args.user)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
