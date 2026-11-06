"""Compose healthcheck: exit 0 when the worker has refreshed its heartbeat.

The scheduler writes ``asset-tracker:worker:heartbeat`` with a TTL. A dead
process stops refreshing it, the key expires, and this probe fails.
"""

from __future__ import annotations

import sys

from redis import Redis

from ..config import settings
from .scheduler import HEARTBEAT_KEY


def main() -> int:
    try:
        client = Redis.from_url(settings.redis_url)
        if not client.ping():
            return 1
        if client.get(HEARTBEAT_KEY) is None:
            return 1
    except Exception:  # noqa: BLE001 - a probe reports failure, it does not explain it
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
