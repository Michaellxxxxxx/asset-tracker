"""Normalized records shared by live and mock adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

Direction = Literal["in", "out"]


@dataclass(frozen=True)
class NormalizedBalance:
    source: str
    asset: str
    amount: Decimal
    raw: dict[str, Any] = field(default_factory=dict, compare=False)
    # True when this row came from a recorded fixture, not a venue read.
    fixture: bool = field(default=False, compare=False)


@dataclass(frozen=True)
class ChainBalance:
    """A wallet token balance. ``source`` is the chain adapter name (zerion / helius).

    ``address`` is the wallet that holds the token. ``chain`` is the DefiLlama
    chain name (``ethereum``, ``bsc``, …) and ``contract`` is the token address
    on that chain. Native coins leave ``contract`` empty so pricing uses the
    symbol (WETH→ETH, stables) instead of a contract that does not exist.
    ``quote_usdt`` is the provider's own USD price (Zerion ``attributes.price``)
    and is only a last resort after DefiLlama and the symbol lookup miss.
    ``icon_url`` is the provider logo (Zerion ``fungible_info.icon``). The
    overview prefers it over Trust Wallet / Octav when the contract is obscure.
    ``fixture`` marks a recorded payload so provenance never depends on whether
    the address happens to be a demo stand-in.
    """

    source: str
    asset: str
    amount: Decimal
    address: str | None = None
    chain: str | None = None
    contract: str | None = None
    quote_usdt: Decimal | None = None
    icon_url: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)
    fixture: bool = field(default=False, compare=False)


@dataclass(frozen=True)
class NormalizedTransfer:
    """Unified deposit / withdrawal / on-chain movement."""

    source: str
    external_id: str
    ts: datetime
    asset: str
    amount: Decimal
    direction: Direction
    address: str | None = None
    txid: str | None = None
    raw_type: str = ""
    note: str = ""
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)


@dataclass(frozen=True)
class NormalizedTrade:
    source: str
    external_id: str
    ts: datetime
    symbol: str
    side: str
    amount: Decimal
    cost_usdt: Decimal | None = None
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def base_asset(self) -> str:
        return self.symbol.split("/")[0].upper()

    @property
    def quote_asset(self) -> str:
        parts = self.symbol.split("/")
        return parts[1].upper() if len(parts) > 1 else ""
