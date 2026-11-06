"""Read-only dry run of the Blockscout enrichment for one pending row.

Usage (from ``backend/``, with the app's DATABASE_URL set)::

    python -m scripts.enrich_dryrun --pending-id 249
    python -m scripts.enrich_dryrun --pending-id 249 --chain ethereum --json

The script only SELECTs. It never updates the pending row, never writes a
ledger entry and never recomputes historical data. ``--fixture-dir`` replays a
saved Blockscout response set (the test fixtures) instead of using the network.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.attribution import Attribution  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.integrations.chain.enrich import (  # noqa: E402
    BlockscoutClient,
    describe_pending,
    enrich_pending,
    refine_attribution,
)
from app.integrations.types import NormalizedTransfer  # noqa: E402
from app.models import PendingConfirmation  # noqa: E402
from app.wallets import user_addresses  # noqa: E402


class _FixtureClient(BlockscoutClient):
    """Replay trimmed Blockscout JSON from ``backend/tests/fixtures/chain``."""

    _FILES = {
        "addresses": "blockscout_249_address.json",
        "transactions": "blockscout_249_tx.json",
        "token-transfers": "blockscout_249_token_transfers.json",
    }

    def __init__(self, directory: Path) -> None:
        super().__init__()
        self._directory = directory

    def _get(
        self,
        chain: str,
        path: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any] | None:
        if "token-transfers" in path:
            name = self._FILES["token-transfers"]
        elif "/transactions/" in path:
            name = self._FILES["transactions"]
        elif "/addresses/" in path:
            name = self._FILES["addresses"]
        else:
            return None
        target = self._directory / name
        if not target.exists():
            return None
        payload = json.loads(target.read_text())
        return payload if isinstance(payload, dict) else None


def _transfer_for(pending: PendingConfirmation, tx_hash: str, user: str) -> NormalizedTransfer:
    return NormalizedTransfer(
        source=str(pending.source or "zerion"),
        external_id=str(pending.external_id or f"zerion:{tx_hash}:0"),
        ts=pending.ts,
        asset=str(pending.asset or "?"),
        amount=pending.amount_raw or 0,
        direction=str(pending.direction or "out"),
        address=None,
        txid=tx_hash,
        raw_type="deposit" if "deposit" in str(pending.detail).lower() else "transfer",
        note=str(pending.suggested_reason or ""),
        raw={"address": user, "operation": "deposit"},
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pending-id", type=int, default=249)
    parser.add_argument("--chain", default=None, help="Blockscout chain key, e.g. ethereum")
    parser.add_argument("--address", default=None, help="override the EVM address")
    parser.add_argument(
        "--fixture-dir",
        type=Path,
        default=None,
        help="replay fixtures instead of calling Blockscout",
    )
    parser.add_argument("--json", action="store_true", help="print one JSON object")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        pending = db.get(PendingConfirmation, args.pending_id)
        if pending is None:
            print(f"pending #{args.pending_id} not found", file=sys.stderr)
            return 2
        user = args.address or (user_addresses(db, pending.user_id, "evm") or ("",))[0]
        source_text = f"{pending.external_id or ''} {pending.detail or ''}"
        match = re.search(r"0x[0-9a-fA-F]{64}", source_text)
        tx_hash = match.group(0).lower() if match else ""
        if not tx_hash:
            print(f"pending #{args.pending_id} has no tx hash", file=sys.stderr)
            return 2

        client = _FixtureClient(args.fixture_dir) if args.fixture_dir else BlockscoutClient()
        enrichment = enrich_pending(
            pending, chain=args.chain, own_address=user, client=client
        )
        base = Attribution(
            str(pending.suggested_category or "withdraw"),
            float(pending.suggested_confidence or 0),
            str(pending.suggested_reason or ""),
        )
        refined = (
            refine_attribution(base, _transfer_for(pending, tx_hash, user), enrichment)
            if enrichment is not None
            else base
        )
        result = {
            "pending_id": pending.id,
            "status": pending.status,
            "tx_hash": tx_hash,
            "chain": args.chain or (enrichment.chain if enrichment else None),
            "current_suggested_category": pending.suggested_category,
            "new_suggested_category": refined.category,
            "new_suggested_confidence": refined.confidence,
            "new_suggested_reason": refined.reason,
            "counterparty_label": enrichment.counterparty_label if enrichment else None,
            "explain": enrichment.explain if enrichment else None,
            "describe_pending": describe_pending(pending),
            "received_tokens": (
                [token.as_dict() for token in enrichment.received] if enrichment else []
            ),
            "enrichment_error": enrichment.error if enrichment else "no enrichment",
            "dry_run": True,
        }
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            for key, value in result.items():
                print(f"{key}: {value}")
        return 0
    finally:
        db.rollback()
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
