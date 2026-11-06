"""ExchangeAdapter protocol implemented by both CCXT and fixture-backed adapters."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from ..types import NormalizedBalance, NormalizedTrade, NormalizedTransfer


class ExchangeAdapter(Protocol):
    source: str

    def fetch_balances(self) -> list[NormalizedBalance]:
        """Return spot balances in a unified shape."""

    def fetch_transfers(self, since: datetime) -> list[NormalizedTransfer]:
        """Return deposits and withdrawals at or after ``since``."""

    def fetch_trades(self, since: datetime) -> list[NormalizedTrade]:
        """Return spot trades at or after ``since``."""
