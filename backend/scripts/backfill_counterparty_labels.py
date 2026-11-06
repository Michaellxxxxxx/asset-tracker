"""Backfill pending counterparty chain/address and the label cache.

Dry-run is the default: it only reads pending rows and cached labels, never
writes, and never makes a network request.  ``--apply`` writes the counterparty
chain/address (plus a cached label when available) into ``pending.meta`` and
fills ``address_labels`` when an online lookup is allowed.

Usage (from ``backend/``, with the app's DATABASE_URL set)::

    python -m scripts.backfill_counterparty_labels
    python -m scripts.backfill_counterparty_labels --apply
    python -m scripts.backfill_counterparty_labels --apply --offline --limit 20
    python -m scripts.backfill_counterparty_labels --json

The tx -> chain lookup uses free public endpoints (Routescan, Blockscout
Ethereum, public RPC receipts) and is rate-limited by ``app.address_labels``.
A dry-run is always offline; an ``--apply --offline`` run only uses data that
is already in the row or the local cache.  Rows whose address cannot be
obtained reliably are skipped and counted, never guessed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.address_labels import backfill_counterparty_labels  # noqa: E402
from app.db import SessionLocal  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write pending.meta and cache rows (default: dry-run, no writes/network)",
    )
    parser.add_argument("--user-id", type=int, default=None)
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="max pending rows to process in this batch",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="with --apply: skip all external lookups (local cache only)",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be positive")

    db = SessionLocal()
    try:
        report = backfill_counterparty_labels(
            db,
            apply=args.apply,
            user_id=args.user_id,
            limit=args.limit,
            offline=args.offline,
        )
        payload = report.as_dict()
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"dry_run: {payload['dry_run']} offline: {payload['offline']}")
            print(f"total_pending_rows: {payload['total_pending_rows']}")
            print(f"scanned_rows: {payload['scanned_rows']}")
            print(f"evm_candidate_rows: {payload['evm_candidate_rows']}")
            print(f"solana_candidate_rows: {payload['solana_candidate_rows']}")
            print(f"evm_patchable_rows: {payload['evm_patchable_rows']}")
            print(f"solana_patchable_rows: {payload['solana_patchable_rows']}")
            print(f"cache_hit_rows: {payload['cache_hit_rows']}")
            print(f"unique_addresses_to_fetch: {payload['unique_addresses']}")
            print(f"local_only_pairs: {payload['local_only_pairs']}")
            print(f"no_address_rows: {payload['no_address_rows']}")
            print(f"invalid_address_rows: {payload['invalid_address_rows']}")
            print(f"unresolved_chain: {payload['unresolved_chain']}")
            print(f"unavailable_rows: {payload['unavailable_rows']}")
            print(f"unique_pairs: {payload['unique_pairs']}")
            print(f"cache_hits: {payload['cache_hits']}")
            print(f"fetched: {payload['fetched']}")
            print(f"written: {payload['written']}")
            for change in payload["changes"]:
                print(
                    f"  {change['chain']} {change['address']} -> {change['label']} "
                    f"({change['kind']}/{change['source']})"
                )
            for error in payload["errors"]:
                print(f"  error: {error}")
        return 0
    finally:
        db.rollback()
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
