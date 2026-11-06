"""RQ entry points. Imported by the worker process; the API enqueues ``run_sync_job``."""

from __future__ import annotations

import logging

from ..address_labels import refresh_labels
from ..db import SessionLocal, release_failed_session
from ..logging_redact import install_redact_filter
from .scheduler import record_run
from .service import PartialSync, run_sync

logger = logging.getLogger(__name__)
install_redact_filter()


def run_sync_job(user_id: int, mode: str | None = None) -> dict[str, int]:
    """Worker entry. Returns the sync stats dict so RQ can store it as the result."""
    with SessionLocal() as db:
        try:
            result = record_run(
                db,
                trigger="manual",
                action=lambda: run_sync(user_id, mode, db=db),
            )
        except PartialSync as exc:
            # Already recorded as partial. Return the counts so the job is not
            # marked failed and retried over the same transfers.
            result = exc.result
            logger.info("sync job partial user=%s", user_id)
            return result.as_dict() if result is not None else {}
        except Exception:
            # A deadlock/connection error can leave the session in
            # PendingRollbackError. Roll back (and retire a poisoned
            # connection) before the context manager closes, so the shared
            # pool stays usable for the WeChat threads.
            release_failed_session(db, label=f"sync job user={user_id}")
            raise
    logger.info("sync job finished user=%s", user_id)
    return result.as_dict()


def refresh_address_labels_job(chain: str, addresses: list[str]) -> dict[str, object]:
    """Fill ``address_labels`` for the pairs a sync could not answer inline.

    RQ calls this with a small, deduplicated list from one chain.  The job is
    idempotent: an already valid cache row is simply fetched again and upserted.
    A source failure degrades to a fallback row instead of failing the queue.
    """
    with SessionLocal() as db:
        try:
            records = refresh_labels(db, chain, addresses)
        except Exception:
            release_failed_session(db, label=f"address labels chain={chain}")
            raise
    return {
        "chain": chain,
        "requested": len(addresses),
        "written": len(records),
        "labels": [record.label for record in records],
    }
