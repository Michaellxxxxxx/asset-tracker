"""HTTP clients whose request logs cannot leak an API key embedded in a URL."""

from __future__ import annotations

import logging

import httpx

# httpx logs the full URL at INFO, and Alchemy puts the key in the path.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def quiet_client(**kwargs) -> httpx.Client:
    """A client that does not emit INFO request lines."""
    return httpx.Client(**kwargs)
