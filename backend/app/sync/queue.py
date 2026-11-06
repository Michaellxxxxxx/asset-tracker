"""Redis-backed RQ queue. rq is imported lazily so unit tests without Redis still import."""

from __future__ import annotations

from typing import Any

from ..config import settings

QUEUE_NAME = "asset-tracker"


def redis_connection() -> Any:
    """Short connect and command timeouts so a dead Redis fails instead of hanging.

    redis-py defaults both timeouts to ``None`` (wait forever). A refused port
    or a half-open host then blocks the request, and the test suite must not
    wait on the machine's Redis.
    """
    from redis import Redis

    return Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=2,
        socket_timeout=2,
    )


def get_queue() -> Any:
    from rq import Queue

    return Queue(QUEUE_NAME, connection=redis_connection())


def enqueue_sync(user_id: int, mode: str | None = None) -> str:
    queue = get_queue()
    job = queue.enqueue("app.sync.jobs.run_sync_job", user_id, mode, job_timeout=600)
    return job.id


def fetch_job(job_id: str) -> Any:
    from rq.job import Job

    return Job.fetch(job_id, connection=redis_connection())
