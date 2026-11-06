"""One process, two jobs: the RQ worker and the interval scheduler.

``docker compose`` runs ``python -m app.sync.worker_main``. The scheduler lives
on a daemon thread; the RQ worker runs on the main thread because its signal
handlers require that. A heartbeat key (refreshed by every tick and by this
process on start) is what the compose healthcheck reads.
"""

from __future__ import annotations

import logging
import os
import threading

from redis import Redis
from rq import Worker

from ..config import settings
from ..db import SessionLocal
from .queue import QUEUE_NAME
from .scheduler import HEARTBEAT_KEY, RedisLock, loop

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    connection = Redis.from_url(settings.redis_url)
    lock = RedisLock(connection)
    lock.beat(HEARTBEAT_KEY, max(settings.sync_interval_seconds, 1) * 2)

    scheduler = threading.Thread(
        target=loop,
        kwargs={"lock": lock, "session_factory": SessionLocal},
        name="sync-scheduler",
        daemon=True,
    )
    scheduler.start()
    logger.info("scheduler started, interval=%ss", settings.sync_interval_seconds)

    from ..integrations.wechat.worker import start_wechat_background

    start_wechat_background()

    worker = Worker([QUEUE_NAME], connection=connection)
    worker.work(with_scheduler=False)


if __name__ == "__main__":
    main()
