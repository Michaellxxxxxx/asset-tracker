"""Fix pending Zerion outflows falsely suggested as ``withdraw``.

Rule (report 5.2): same tx has an out leg with raw type ``deposit`` and an in
leg with raw type ``deposit`` from the zero address, for a different asset.
Those outflows are protocol deposits, not withdrawals.

Safety:
* only ``status='pending'`` + ``suggested_category='withdraw'`` are candidates;
* confirmed rows (for example row 249 / ledger_entry 6) are never updated;
* default is dry-run; ``--apply`` requires ``--backup`` and writes the affected
  ids and old values before committing;
* rerunning is a no-op once the category is corrected.

Usage (from ``backend/``)::

    python -m scripts.fix_protocol_deposit_categories
    python -m scripts.fix_protocol_deposit_categories --list-confirmed
    python -m scripts.fix_protocol_deposit_categories --apply \
        --backup /secure/p11g_protocol_deposit_backup.json

Do not run ``--apply`` without a reviewed backup.  This script never edits a
ledger entry or a confirmed pending row.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.address_labels import (  # noqa: E402
    find_protocol_deposit_withdraw_rows,
    fix_pending_protocol_deposit_categories,
)
from app.db import SessionLocal  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write pending category changes")
    parser.add_argument(
        "--backup",
        type=Path,
        default=None,
        help="JSON path for affected ids and old values (required with --apply)",
    )
    parser.add_argument("--user-id", type=int, default=None)
    parser.add_argument(
        "--list-confirmed",
        action="store_true",
        help="also print the read-only detection list, including confirmed rows",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.apply and args.backup is None:
        parser.error("--apply requires --backup")

    db = SessionLocal()
    try:
        if args.list_confirmed:
            detected = find_protocol_deposit_withdraw_rows(db, user_id=args.user_id)
            print(json.dumps(detected, ensure_ascii=False, indent=2) if args.json else detected)

        result = fix_pending_protocol_deposit_categories(
            db,
            apply=args.apply,
            user_id=args.user_id,
            backup_path=args.backup,
        )
        summary = {
            "dry_run": result["dry_run"],
            "candidate_rows": result["candidate_rows"],
            "matched_rows": result["matched_rows"],
            "written": result["written"],
            "backup_path": result["backup_path"],
            "changes": [
                {
                    "id": change["id"],
                    "txid": change["txid"],
                    "asset": change["asset"],
                    "in_asset": change["in_asset"],
                    "old_suggested_category": change["old_suggested_category"],
                    "new_suggested_category": change["new_suggested_category"],
                }
                for change in result["changes"]
            ],
        }
        if args.json:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        else:
            print(f"dry_run: {summary['dry_run']}")
            print(f"candidate_rows: {summary['candidate_rows']}")
            print(f"matched_rows: {summary['matched_rows']}")
            print(f"written: {summary['written']}")
            if summary["backup_path"]:
                print(f"backup_path: {summary['backup_path']}")
            for change in summary["changes"]:
                print(
                    f"  id={change['id']} {change['asset']} -> {change['in_asset']} "
                    f"({change['old_suggested_category']} -> {change['new_suggested_category']})"
                )
        return 0
    finally:
        db.rollback()
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
