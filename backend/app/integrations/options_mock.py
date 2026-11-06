"""Mock option book for the no-key phase (p4a plan §7).

Several contracts each of BTC and ETH, long and short: one expiring inside
three days, one short, one already exercised. Numbers are hand-set so the
pure functions in ``options_calc`` reproduce them exactly — this file does not
import those functions, so a regression cannot be hidden by computing the
fixture from the code under test.

These rows are **not** the P0 positions fixture (``tests/fixtures/positions/option.json``).
That file feeds the shared positions summary tests and is left untouched.
Nothing here is a live account.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FIXTURE_PATH = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "options" / "mock_book.json"


def load_mock_book(path: Path | None = None) -> dict[str, Any]:
    """``{"positions": [...], "trades": [...], "quotes": {...}}``."""
    payload = json.loads((path or FIXTURE_PATH).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("options mock book must be an object")
    return payload


def mock_positions(path: Path | None = None) -> list[dict[str, Any]]:
    rows = load_mock_book(path).get("positions") or []
    return [row for row in rows if isinstance(row, dict)]


def mock_trades(path: Path | None = None) -> list[dict[str, Any]]:
    rows = load_mock_book(path).get("trades") or []
    return [row for row in rows if isinstance(row, dict)]


def mock_quotes(path: Path | None = None) -> dict[str, dict[str, Any]]:
    raw = load_mock_book(path).get("quotes") or {}
    return {str(key): value for key, value in raw.items() if isinstance(value, dict)}
