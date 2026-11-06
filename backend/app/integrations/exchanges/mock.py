"""Fixture-backed exchange adapter. Reads recorded ccxt-shaped JSON and normalizes it."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from ..types import NormalizedBalance, NormalizedTrade, NormalizedTransfer
from .normalize import filter_since, normalize_balance, normalize_trade, normalize_transfer

FIXTURE_DIR = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "exchanges"


class MockExchangeAdapter:
    def __init__(self, exchange_id: str, fixture_dir: Path | None = None) -> None:
        self.source = exchange_id
        self._dir = fixture_dir or FIXTURE_DIR

    def _load(self, name: str) -> Any:
        path = self._dir / f"{self.source}_{name}.json"
        if not path.is_file():
            return [] if name != "balances" else {}
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)

    def fetch_balances(self) -> list[NormalizedBalance]:
        raw = self._load("balances")
        rows = normalize_balance(self.source, raw if isinstance(raw, dict) else {})
        return [replace(row, fixture=True) for row in rows]

    def fetch_transfers(self, since: datetime) -> list[NormalizedTransfer]:
        items: list[NormalizedTransfer] = []
        for raw in self._load("transfers"):
            parsed = normalize_transfer(self.source, raw)
            if parsed is not None:
                items.append(parsed)
        return filter_since(items, since)

    def fetch_trades(self, since: datetime) -> list[NormalizedTrade]:
        items: list[NormalizedTrade] = []
        for raw in self._load("trades"):
            parsed = normalize_trade(self.source, raw)
            if parsed is not None:
                items.append(parsed)
        return filter_since(items, since)
