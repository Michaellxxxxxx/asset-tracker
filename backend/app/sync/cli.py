"""python -m app.sync.cli --user demo [--mode mock] [--backfill] [--all-users]"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

# Settings is read once at import, so the repo .env has to be loaded first.
# Existing process env (docker) wins; this only fills in what a shell omitted.
load_dotenv(Path(__file__).resolve().parents[3] / ".env")

from sqlalchemy import select

from ..db import SessionLocal, engine, wait_for_db
from ..logging_redact import install_redact_filter
from ..migrate import apply_phase2_migrations
from ..models import User
from ..snapshots_backfill import backfill_all_users, backfill_user
from .scheduler import record_run
from .service import PartialSync, run_sync


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run an asset sync or a historical backfill")
    parser.add_argument("--user", default=None, help="username, e.g. demo")
    parser.add_argument("--mode", default=None, choices=["auto", "mock", "live"], help="override SYNC_MODE")
    parser.add_argument("--backfill", action="store_true", help="reconstruct daily NAV since SYNC_SINCE")
    parser.add_argument("--all-users", action="store_true", help="run for every user instead of --user")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    install_redact_filter()

    if not args.all_users and not args.user:
        print("--user is required unless --all-users is set", file=sys.stderr)
        return 2

    wait_for_db()
    apply_phase2_migrations(engine)
    with SessionLocal() as db:
        if args.backfill and args.all_users:
            user_count = len(list(db.scalars(select(User.id)).all()))
            summary = record_run(
                db,
                trigger="backfill",
                user_count=user_count,
                action=lambda: backfill_all_users(db, mode=args.mode),
            )
            created = sum(item["created"] for item in summary["users"])
            updated = sum(item["updated"] for item in summary["users"])
            print(f"users={len(summary['users'])} created={created} updated={updated}")
            return 0
        if args.all_users:
            print("--all-users is only supported with --backfill", file=sys.stderr)
            return 2
        user = db.scalar(select(User).where(User.username == args.user))
        if user is None:
            print(f"user not found: {args.user}", file=sys.stderr)
            return 1
        if args.backfill:
            result = record_run(
                db,
                trigger="backfill",
                action=lambda: backfill_user(db, user.id, mode=args.mode),
            )
            print(
                f"created={result.created} updated={result.updated} "
                f"from={result.start.date().isoformat()} to={result.end.date().isoformat()}"
            )
            return 0
        try:
            result = record_run(
                db,
                trigger="manual",
                action=lambda: run_sync(user.id, args.mode, db=db),
            )
        except PartialSync as exc:
            # The run is already recorded as partial. Print the counts and exit
            # non-zero so a cron notices the skipped snapshot.
            result = exc.result
            print(
                f"fetched={result.fetched} enqueued={result.enqueued} duplicates={result.duplicates} partial"
            )
            return 1
    print(
        f"fetched={result.fetched} enqueued={result.enqueued} duplicates={result.duplicates}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
