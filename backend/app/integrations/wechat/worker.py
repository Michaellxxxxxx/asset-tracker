"""Daemon threads: flush the queue, and optionally long-poll ClawBot."""

from __future__ import annotations

import logging
import threading
import time

from ...config import settings
from ...db import SessionLocal, release_failed_session
from .client import get_wechat_client
from .queue import flush_all_queued

logger = logging.getLogger(__name__)

_started = False
_guard = threading.Lock()


def start_wechat_background() -> None:
    """Always start the flush loop. Start the poller only for live + token + flag."""
    global _started
    with _guard:
        if _started:
            return
        _started = True

    interval = max(settings.wechat_flush_interval_seconds, 1)
    threading.Thread(
        target=_flush_loop,
        kwargs={"interval": interval},
        name="wechat-flush",
        daemon=True,
    ).start()
    logger.info("wechat flush started, interval=%ss", interval)

    if settings.wechat_poll_enabled and settings.wechat_push_mode == "live" and settings.wechat_bot_token:
        threading.Thread(target=_poll_loop, name="wechat-poll", daemon=True).start()
        logger.info("wechat poll started")
    else:
        logger.info(
            "wechat poll skipped (enabled=%s mode=%s token=%s)",
            settings.wechat_poll_enabled,
            settings.wechat_push_mode,
            bool(settings.wechat_bot_token),
        )


def _flush_loop(interval: int) -> None:
    while True:
        db = SessionLocal()
        try:
            flush_all_queued(db)
        except Exception:  # noqa: BLE001 - a flush error must not kill the daemon
            logger.exception("wechat flush failed")
            # Do not hand a dirty (INTRANS) connection back to the pool that
            # RQ sync jobs also borrow from.
            release_failed_session(db, label="wechat flush")
        finally:
            db.close()
        time.sleep(interval)


def _poll_loop() -> None:
    client = get_wechat_client()
    while True:
        db = SessionLocal()
        try:
            from .poll import process_updates

            started = time.monotonic()
            process_updates(db, client)
            if time.monotonic() - started < 2:  # error/empty fast return: do not spin
                time.sleep(3)
        except Exception:  # noqa: BLE001 - never log the token
            logger.exception("wechat poll failed")
            release_failed_session(db, label="wechat poll")
            time.sleep(5)
        finally:
            db.close()
