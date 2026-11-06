"""Counterparty address labels for EVM pending rows.

The module has three jobs, kept deliberately separate:

* a small, pure protocol-deposit rule (same-tx deposit out + zero-address
  deposit in, different assets);
* a read-through label cache backed by ``address_labels``;
* free public lookups (Blockscout, Sourcify v2, Routescan, public RPC, ENS).

Sync code only reads the cache and the pure rule.  Network access happens in
the background job or the backfill script; a cache miss returns a fallback
label immediately and queues the pair for the worker.  No key is used or
logged, and a 403 (Cloudflare) opens a per-host circuit breaker instead of
being retried or bypassed.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urlsplit

import httpx

from sqlalchemy import select

from .config import settings
from .models import ADDRESS_LABEL_KINDS, AddressLabel, PendingConfirmation

logger = logging.getLogger(__name__)

ZERO_ADDRESS = "0x" + "0" * 40
UNKNOWN_LABEL = "未识别地址"
UNNAMED_CONTRACT_LABEL = "未识别合约"
EOA_LABEL = "个人地址"
ZERO_LABEL = "零地址(销毁/铸造)"
PROTOCOL_DEPOSIT_CATEGORY = "defi_deposit"
SOLANA_CHAIN = "solana"
SOLANA_SYSTEM_PROGRAM = "11111111111111111111111111111111"
SOLANA_TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
SOLANA_TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
SOLANA_ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
SOLANA_TOKEN_ACCOUNT_LABEL = "代币账户"

# Public, well-known Solana program ids only.  A missing entry must fall back
# to "未识别" (unknown); never guess a protocol name from a transaction type.
# A non-executable account owned by one of these programs is a program-owned
# PDA/vault, so it may be labelled "<program> 账户"; the owner still has to be
# an explicit table hit.
SOLANA_KNOWN_PROGRAMS: dict[str, str] = {
    SOLANA_SYSTEM_PROGRAM: "System Program",
    SOLANA_TOKEN_PROGRAM: "SPL Token Program",
    SOLANA_TOKEN_2022_PROGRAM: "Token-2022 Program",
    SOLANA_ATA_PROGRAM: "Associated Token Account Program",
    "ComputeBudget111111111111111111111111111111": "Compute Budget Program",
    "Memo1UhkJRfHyvLMcVucJwxXeuD728EqVDDwQDxFMNo": "Memo Program v1",
    "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr": "Memo Program v2",
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4": "Jupiter Aggregator v6",
    "JUP4Fb2cqiRUcaTHdrPC8h2gNsA2ETXiPDD33WcGuJB": "Jupiter Aggregator v4",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "Raydium AMM v4",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "Raydium CLMM",
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C": "Raydium CPMM",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "Orca Whirlpool",
    "9W959DqEETiGZocYWCQPaJ6sBmUzgfxXfqGeTEdp3aQP": "Orca Token Swap v2",
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": "Meteora DLMM",
    "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB": "Meteora Dynamic AMM",
    "MarBmsSgKXdrN1egZf5sqe1TMai9K1rChYNDJgjq7aD": "Marinade Finance",
    "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin": "Serum DEX v3",
    "M2mx93ekt1fmXSVkTrUL9xVFHkmME8HTUi5Cyc5aF7K": "Magic Eden v2",
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "Pump.fun",
    "So1endDq2YkqhipRh3WViPa8hdiSpxWy6z3Z6tMCpAo": "Solend",
    "KLend2g3cP87fffoy8q1mQqGKjrxjC8boSyAYavgmjD": "Kamino Lend",
    "dRiftyHA39MWEi3m9aunc5MzRF1JYuBsbn6VPcn33UH": "Drift Protocol",
    "PhoeNiXZ8ByJGLkxNfZRnkUfjvmuYqLR89jjFHGqdXY": "Phoenix DEX",
    "worm2ZoG2kUd4vFXhvjh93UUH596ayRfgQ2MgjNMTth": "Wormhole Token Bridge",
    "AddressLookupTab1e1111111111111111111111111": "Address Lookup Table Program",
    "FarmsPZpWu9i7Kky8tPN37rs2TpmMrAZrC7S7vJa91Hr": "Kamino Farms",
    "T1TANpTeScyeqVzzgNViGDNrkQ6qHz9KrSBS4aNXvGT": "Titan Exchange",
    "REALQqNEomY6cQGZJUGwywTBD2UmDT32rZcNnfxQ5N2": "Byreal CLMM",
    "TessVdML9pBGgG9yGks7o4HewRaXVAMuoVj4x83GLQH": "Tessera V",
    "jupeiUmn818Jg1ekPURTpr4mFo29p46vygyykFJ3wZC": "Jupiter Lend Liquidity",
    "jup3YeL8QhtSx1e253b2FDvsMNC87fDrgQZivbrndc9": "Jupiter Lend Earn",
    "jupr81YtYssSyPt8jbnGuiWon5f6x9TcDEFxYe3Bdzi": "Jupiter Lend Vaults",
    "BGUMAp9Gq7iTEuizy4pqaxsTyUCBK68MDfK752saRPUY": "Metaplex Bubblegum",
    # p44: verified in p43 (official docs / SDK constants); see p43 result.
    "BULK2CNYn3mbgfYXEXiBBFxmmDChznpjQ4oRfce8w6R4": "Bulk",
    "onreuGhHHgVzMWSkj2oQDLDtvvGvoepBPkqyaubFcwe": "OnRe",
    "sVau1tXvayVWfotzm9Ahcv2qfnnfRWttt78BCnNC6dD": "Exponent Strategy Vaults",
    "HpNfyc2Saw7RKkQd8nEL4khUcuPhQ7WwY1B2qjx8jxFq": "PancakeSwap CLMM",
    "AQU1FRd7papthgdrwPTTq5JacJh8YtwEXaBfKU3bTz45": "Aquifer",
    "QuaNtZsgYRe5Z9Bk4LZ4cTD9tbkVoyCNf1R2BN9bBDv": "Quantum",
    "goonuddtQRrWqqn5nFyczVKaie28f3kDkHWkHtURSLE": "GoonFi v2",
    "riptK81hDxhe5pW5jSzSM9iRA8azgEgLJ4dXkPtBS7j": "Riptide",
    "SV2EYYJyRz2YhfXwXnhNAevDEui5Q6yrfyo13WtupPF": "SolFi v2",
}
_SOLANA_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_SOLANA_B58_INDEX = {char: index for index, char in enumerate(_SOLANA_B58_ALPHABET)}

# Canonical chain names match the Zerion/DefiLlama names already used by the
# rest of the backend.  The numeric ids are only used by the public APIs.
CHAIN_IDS: dict[str, int] = {
    "ethereum": 1,
    "optimism": 10,
    "bsc": 56,
    "gnosis": 100,
    "polygon": 137,
    "sonic": 146,
    "zksync": 324,
    "mantle": 5000,
    "base": 8453,
    "arbitrum": 42161,
    "celo": 42220,
    "linea": 59144,
    "scroll": 534352,
    "blast": 81457,
    "ink": 57073,
    "plasma": 9745,
    "unichain": 130,
    "soneium": 1868,
    "avalanche": 43114,
}
CHAIN_BY_ID = {value: key for key, value in CHAIN_IDS.items()}

CHAIN_ALIASES: dict[str, str] = {
    "solana": "solana",
    "sol": "solana",
    "eth": "ethereum",
    "mainnet": "ethereum",
    "ethereum-mainnet": "ethereum",
    "arbitrum-one": "arbitrum",
    "arbitrum_one": "arbitrum",
    "optimism-mainnet": "optimism",
    "polygon-pos": "polygon",
    "polygon_pos": "polygon",
    "zksync-era": "zksync",
    "bnb": "bsc",
    "binance": "bsc",
}

BLOCKSCOUT_HOSTS: dict[str, str] = {
    "ethereum": "https://eth.blockscout.com",
    "base": "https://base.blockscout.com",
    "arbitrum": "https://arbitrum.blockscout.com",
    "optimism": "https://optimism.blockscout.com",
    "polygon": "https://polygon.blockscout.com",
    "gnosis": "https://gnosis.blockscout.com",
    "scroll": "https://scroll.blockscout.com",
    "zksync": "https://zksync.blockscout.com",
    "celo": "https://celo.blockscout.com",
    "unichain": "https://unichain.blockscout.com",
    "soneium": "https://soneium.blockscout.com",
}

# PublicNode free endpoints used by the public research script.  They need no
# key and are independently rate-limited by HostGate.
RPC_HOSTS: dict[str, str] = {
    "base": "https://base-rpc.publicnode.com",
    "arbitrum": "https://arbitrum-one-rpc.publicnode.com",
    "polygon": "https://polygon-bor-rpc.publicnode.com",
    "bsc": "https://bsc-rpc.publicnode.com",
    "avalanche": "https://avalanche-c-chain-rpc.publicnode.com",
    "linea": "https://linea-rpc.publicnode.com",
    "sonic": "https://sonic-rpc.publicnode.com",
    "mantle": "https://mantle-rpc.publicnode.com",
    "blast": "https://blast-rpc.publicnode.com",
    "ink": "https://ink-rpc.publicnode.com",
    "optimism": "https://optimism-rpc.publicnode.com",
    "ethereum": "https://ethereum-rpc.publicnode.com",
}

SOURCIFY_URL = "https://sourcify.dev/server/v2/contract/{chain_id}/{address}"
ROUTESCAN_URL = (
    "https://api.routescan.io/v2/network/mainnet/evm/{chain_id}/etherscan/api"
)
ROUTESCAN_TX_URL = (
    "https://api.routescan.io/v2/network/mainnet/evm/all/transactions/{txid}"
)
ENS_REGISTRY = "0x00000000000C2E074eC69A0dFb2997BA6C7d2e1e"

HTTP_TIMEOUT_SECONDS = 8.0
REQUESTS_PER_SECOND = 2.0
BURST = 2.0
BREAKER_SECONDS = 300.0
MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 0.5
BACKOFF_MAX_SECONDS = 4.0

# Names that describe a proxy/helper shape, not an identity.  They are only
# shown alone when no implementation name exists; with an implementation the
# label is "weak name → implementation name".
WEAK_NAMES = frozenset(
    {
        "transparentupgradeableproxy",
        "erc1967proxy",
        "helper",
        "gnosissafeproxy",
        "proxy",
        "proxyadmin",
        "upgradeableproxy",
        "bitfiproxy",
        "afiproxy",
        "proxytomeson",
    }
)

_SOURCE_RANK = {
    "ens": 0,
    "blockscout": 1,
    "blockscout.ens": 1,
    "routescan": 2,
    "sourcify": 3,
    "rpc.name": 4,
}

_TXID_RE = re.compile(r"0x[a-fA-F0-9]{64}")
_DETAIL_ADDRESS_RE = re.compile(
    r"(?:^|[；;，,。\s])(?:对方地址|地址)\s*[:：=]?\s*([^\s；;，,。]+)",
    re.IGNORECASE,
)
_DETAIL_TYPE_RE = re.compile(r"原始类型\s*[:：=]?\s*([^；;，,。]+)", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Small value objects


@dataclass(frozen=True)
class NameCandidate:
    name: str
    source: str
    is_token: bool = False
    is_weak: bool = False


@dataclass(frozen=True)
class SourceLookup:
    source: str
    names: tuple[NameCandidate, ...] = ()
    is_contract: bool | None = None
    is_scam: bool = False
    implementation: str | None = None
    token: dict[str, Any] | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AddressInfo:
    chain: str | None
    address: str
    lookups: tuple[SourceLookup, ...]
    is_contract: bool | None
    is_scam: bool
    errors: tuple[str, ...] = ()

    @property
    def implementation(self) -> str | None:
        for lookup in self.lookups:
            if lookup.implementation:
                return lookup.implementation
        return None

    @property
    def token(self) -> dict[str, Any] | None:
        for lookup in self.lookups:
            if lookup.token:
                return lookup.token
        return None

    @property
    def names(self) -> tuple[NameCandidate, ...]:
        return tuple(name for lookup in self.lookups for name in lookup.names)


@dataclass(frozen=True)
class LabelRecord:
    chain: str
    address: str
    label: str
    kind: str
    source: str
    is_scam: bool
    meta: dict[str, Any]
    fetched_at: datetime
    expires_at: datetime | None


@dataclass(frozen=True)
class LabelMeta:
    """The subset written into pending ``meta`` on the sync path."""

    chain: str | None
    label: str
    kind: str
    source: str
    is_scam: bool
    protocol_deposit: bool
    protocol_deposit_asset: str | None
    missing: bool
    address: str | None = None

    def as_meta(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "counterparty_label": self.label,
            "counterparty_kind": self.kind,
            "counterparty_label_source": self.source,
            "counterparty_is_scam": self.is_scam,
        }
        if self.chain:
            result["chain"] = self.chain
        if self.address:
            result["counterparty_address"] = self.address
        if self.protocol_deposit:
            result["protocol_deposit"] = True
            if self.protocol_deposit_asset:
                result["protocol_deposit_asset"] = self.protocol_deposit_asset
        return result


@dataclass(frozen=True)
class BackfillChange:
    chain: str
    address: str
    label: str
    kind: str
    source: str
    expires_at: str | None
    pending_id: int | None = None


@dataclass
class BackfillReport:
    total_pending_rows: int = 0
    scanned_rows: int = 0
    evm_candidate_rows: int = 0
    solana_candidate_rows: int = 0
    evm_patchable_rows: int = 0
    solana_patchable_rows: int = 0
    cache_hit_rows: int = 0
    no_address_rows: int = 0
    invalid_address_rows: int = 0
    unique_pairs: int = 0
    unique_addresses: int = 0
    local_only_pairs: int = 0
    unresolved_chain: int = 0
    cache_hits: int = 0
    fetched: int = 0
    written: int = 0
    skipped_rows: int = 0
    dry_run: bool = True
    offline: bool = True
    changes: list[BackfillChange] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def unavailable_rows(self) -> int:
        """Rows with no reliable chain/address, never guessed."""
        return self.no_address_rows + self.unresolved_chain + self.skipped_rows

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_pending_rows": self.total_pending_rows,
            "scanned_rows": self.scanned_rows,
            "evm_candidate_rows": self.evm_candidate_rows,
            "solana_candidate_rows": self.solana_candidate_rows,
            "evm_patchable_rows": self.evm_patchable_rows,
            "solana_patchable_rows": self.solana_patchable_rows,
            "cache_hit_rows": self.cache_hit_rows,
            "no_address_rows": self.no_address_rows,
            "invalid_address_rows": self.invalid_address_rows,
            "unique_pairs": self.unique_pairs,
            "unique_addresses": self.unique_addresses,
            "local_only_pairs": self.local_only_pairs,
            "unresolved_chain": self.unresolved_chain,
            "cache_hits": self.cache_hits,
            "fetched": self.fetched,
            "written": self.written,
            "skipped_rows": self.skipped_rows,
            "unavailable_rows": self.unavailable_rows,
            "dry_run": self.dry_run,
            "offline": self.offline,
            "changes": [change.__dict__ for change in self.changes],
            "errors": self.errors,
        }


# ---------------------------------------------------------------------------
# Normalisation and presentation helpers


def normalize_chain(value: object) -> str | None:
    text = str(value or "").strip().casefold()
    if not text:
        return None
    if text in CHAIN_IDS:
        return text
    if text in CHAIN_ALIASES:
        return CHAIN_ALIASES[text]
    if text.isdigit():
        return CHAIN_BY_ID.get(int(text))
    return None


def normalize_address(value: object) -> str:
    text = str(value or "").strip()
    if text.lower().startswith("0x"):
        return text.lower()
    return text


def is_zero_address(value: object) -> bool:
    return normalize_address(value) == ZERO_ADDRESS


def is_valid_evm_address(value: object) -> bool:
    return bool(re.fullmatch(r"0x[a-f0-9]{40}", normalize_address(value)))


def _base58_decoded_length(text: str) -> int | None:
    """Return the byte length of a base58 string, or None if it is invalid."""
    if not text:
        return None
    number = 0
    for char in text:
        index = _SOLANA_B58_INDEX.get(char)
        if index is None:
            return None
        number = number * 58 + index
    decoded = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    leading_zeroes = len(text) - len(text.lstrip("1"))
    return len(decoded) + leading_zeroes


def is_valid_solana_address(value: object) -> bool:
    """A Solana public key is exactly 32 base58-encoded bytes."""
    text = str(value or "").strip()
    if not 32 <= len(text) <= 44:
        return False
    return _base58_decoded_length(text) == 32


def solana_known_program(address: object) -> str | None:
    return SOLANA_KNOWN_PROGRAMS.get(str(address or "").strip())


def cache_address(chain: str | None, address: object) -> str:
    """Cache identity: EVM addresses are case-insensitive, Solana is not."""
    if normalize_chain(chain) == SOLANA_CHAIN:
        return str(address or "").strip()
    return normalize_address(address)


def abbreviate_address(value: object) -> str:
    text = normalize_address(value)
    if not text:
        return ""
    if len(text) <= 12:
        return text
    if text.startswith("0x"):
        return f"{text[:6]}…{text[-4:]}"
    return f"{text[:4]}…{text[-4:]}"


def _is_weak(name: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", name.casefold())
    return normalized in WEAK_NAMES


def fallback_label(kind: str, address: object = "", chain: str | None = None) -> str:
    if is_zero_address(address):
        return ZERO_LABEL
    if not chain:
        return UNKNOWN_LABEL
    if kind == "contract":
        return UNNAMED_CONTRACT_LABEL
    if kind == "eoa":
        return EOA_LABEL
    if kind == "zero":
        return ZERO_LABEL
    if chain == SOLANA_CHAIN and kind == "unknown":
        # Solana has no chain explorer fallback in this feature: an unresolved
        # account is reported as "未识别", never as a truncated address.
        return UNKNOWN_LABEL
    abbreviated = abbreviate_address(address)
    return abbreviated or UNKNOWN_LABEL


def protocol_deposit_label(asset: object) -> str:
    symbol = str(asset or "").strip()
    return f"协议存入 · 收到 {symbol}" if symbol else "协议存入"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _truncate(value: str, limit: int = 255) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# Protocol-deposit rule (pure)


@dataclass(frozen=True)
class Leg:
    """One pending/transfer leg, reduced to the fields the rule needs."""

    txid: str
    direction: str
    rawtype: str
    asset: str
    address: str
    id: int | None = None


def _read_leg(value: Leg | Mapping[str, Any] | Any) -> Leg | None:
    if isinstance(value, Leg):
        return value
    if isinstance(value, Mapping):
        data = value
        getter = data.get
    else:
        def getter(key: str, default: Any = None) -> Any:
            return getattr(value, key, default)

    txid = normalize_txid_value(getter("txid") or getter("tx") or getter("tx_hash"))
    direction = str(getter("direction") or "").strip().casefold()
    rawtype = str(getter("rawtype") or getter("raw_type") or getter("operation") or "").strip()
    asset = str(getter("asset") or "").strip()
    address = normalize_address(getter("address") or getter("addr") or getter("counterparty"))
    leg_id = getter("id")
    if not txid or direction not in {"in", "out"}:
        return None
    return Leg(
        txid=txid,
        direction=direction,
        rawtype=rawtype,
        asset=asset,
        address=address,
        id=int(leg_id) if leg_id is not None else None,
    )


def normalize_txid_value(value: object) -> str:
    text = str(value or "").strip().casefold()
    match = _TXID_RE.search(text)
    return match.group(0).casefold() if match else ""


def _valid_in_asset(asset: str) -> bool:
    return bool(asset.strip()) and asset.strip().upper() not in {"UNKNOWN", "UNKN", "N/A", "NA", "-"}


def protocol_deposit_hits(legs: Iterable[Leg | Mapping[str, Any] | Any]) -> dict[str, str]:
    """Return ``{txid: in_asset}`` for the report's 5.2 rule.

    A hit requires all of: an out leg with raw type ``deposit``, an in leg with
    raw type ``deposit`` to the zero address, a non-empty/non-UNKNOWN in asset,
    and a different in/out asset.  The rule is intentionally exact so a normal
    self-transfer or a swap is not reclassified.
    """
    grouped: dict[str, list[Leg]] = {}
    for raw in legs:
        leg = _read_leg(raw)
        if leg is None:
            continue
        grouped.setdefault(leg.txid, []).append(leg)

    hits: dict[str, str] = {}
    for txid, group in grouped.items():
        outs = [
            leg
            for leg in group
            if leg.direction == "out" and leg.rawtype.strip().casefold() == "deposit"
        ]
        ins = [
            leg
            for leg in group
            if leg.direction == "in"
            and leg.rawtype.strip().casefold() == "deposit"
            and is_zero_address(leg.address)
            and _valid_in_asset(leg.asset)
        ]
        if not outs or not ins:
            continue
        for out in outs:
            matching = [
                leg
                for leg in ins
                if leg.asset.strip().upper() != out.asset.strip().upper()
            ]
            if matching:
                hits[txid] = matching[0].asset.strip()
                break
    return hits


def leg_from_pending(row: PendingConfirmation) -> Leg | None:
    """Best-effort reduction of a historical pending row to a rule leg."""
    meta = row.meta_data
    txid = normalize_txid_value(row.external_id) or normalize_txid_value(meta.get("tx_hash"))
    detail = str(row.detail or "")
    if not txid:
        txid = normalize_txid_value(detail)
    direction = str(row.direction or "").strip().casefold()
    rawtype = str(meta.get("operation") or meta.get("raw_type") or "").strip()
    if not rawtype:
        match = _DETAIL_TYPE_RE.search(detail)
        if match:
            rawtype = match.group(1).strip()
    address = normalize_address(meta.get("counterparty") or meta.get("address"))
    if not address:
        match = _DETAIL_ADDRESS_RE.search(detail)
        if match:
            address = normalize_address(match.group(1))
    if not txid or direction not in {"in", "out"}:
        return None
    return Leg(
        txid=txid,
        direction=direction,
        rawtype=rawtype,
        asset=str(row.asset or "").strip(),
        address=address,
        id=row.id,
    )


def protocol_deposit_out_rows(
    rows: Iterable[PendingConfirmation],
) -> list[tuple[PendingConfirmation, str]]:
    """Return pending/confirmed out rows hit by the rule, with the in asset."""
    row_list = list(rows)
    legs = [leg for leg in (leg_from_pending(row) for row in row_list) if leg is not None]
    hits = protocol_deposit_hits(legs)
    by_tx: dict[str, list[PendingConfirmation]] = {}
    for row in row_list:
        leg = leg_from_pending(row)
        if leg is not None:
            by_tx.setdefault(leg.txid, []).append(row)
    matched: list[tuple[PendingConfirmation, str]] = []
    for txid, in_asset in hits.items():
        for row in by_tx.get(txid, []):
            if str(row.direction or "").casefold() == "out":
                matched.append((row, in_asset))
    return matched


# ---------------------------------------------------------------------------
# Rate limiting, circuit breaker, and HTTP


class HostCircuitOpen(RuntimeError):
    """The host returned 403; do not touch it until the cooldown ends."""


class SourceUnavailable(RuntimeError):
    """A public source stayed unavailable after bounded retries."""


class HostGate:
    """Token bucket + Cloudflare breaker for one host.

    Both ``clock`` and ``sleep`` are injectable so tests can advance a fake
    clock without waiting.  A 403 trips the breaker; 429/5xx are retried with
    exponential backoff inside :meth:`request_json`.
    """

    def __init__(
        self,
        *,
        rate: float = REQUESTS_PER_SECOND,
        burst: float = BURST,
        breaker_seconds: float = BREAKER_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.rate = max(float(rate), 0.01)
        self.burst = max(float(burst), 1.0)
        self.breaker_seconds = max(float(breaker_seconds), 0.0)
        self.clock = clock
        self.sleep = sleep
        self.tokens = self.burst
        self.last = clock()
        self.blocked_until = 0.0
        self.requests = 0

    def _refill(self) -> None:
        now = self.clock()
        elapsed = max(now - self.last, 0.0)
        self.tokens = min(self.burst, self.tokens + elapsed * self.rate)
        self.last = now

    def acquire(self) -> None:
        now = self.clock()
        if now < self.blocked_until:
            raise HostCircuitOpen("host circuit open")
        self._refill()
        if self.tokens < 1.0:
            wait = (1.0 - self.tokens) / self.rate
            self.sleep(wait)
            self.last = self.clock()
            self.tokens = 0.0
        else:
            self.tokens -= 1.0
        self.requests += 1

    def trip(self) -> None:
        self.blocked_until = self.clock() + self.breaker_seconds
        self.tokens = 0.0

    @property
    def blocked(self) -> bool:
        return self.clock() < self.blocked_until


class GateRegistry:
    """One gate per host, shared by all sources in a process."""

    def __init__(self, *, clock: Callable[[], float] | None = None, sleep: Callable[[float], None] | None = None):
        self.clock = clock or time.monotonic
        self.sleep = sleep or time.sleep
        self._gates: dict[str, HostGate] = {}
        self._lock = threading.Lock()

    def gate(self, url: str) -> HostGate:
        host = urlsplit(url).netloc or url
        with self._lock:
            gate = self._gates.get(host)
            if gate is None:
                gate = HostGate(clock=self.clock, sleep=self.sleep)
                self._gates[host] = gate
            return gate

    def reset(self) -> None:
        with self._lock:
            self._gates.clear()


_GATES = GateRegistry()


def reset_gates() -> None:
    _GATES.reset()


def gate_for(url: str, registry: GateRegistry | None = None) -> HostGate:
    return (registry or _GATES).gate(url)


_HTTP_LOCK = threading.Lock()
_HTTP: httpx.Client | None = None


def default_client() -> httpx.Client:
    global _HTTP
    with _HTTP_LOCK:
        if _HTTP is None or _HTTP.is_closed:
            _HTTP = httpx.Client(
                timeout=HTTP_TIMEOUT_SECONDS,
                headers={"User-Agent": "asset-tracker/1.0 (counterparty-labels)"},
                follow_redirects=True,
            )
        return _HTTP


def request_json(
    method: str,
    url: str,
    *,
    client: httpx.Client | None = None,
    registry: GateRegistry | None = None,
    params: Mapping[str, Any] | None = None,
    json_body: Mapping[str, Any] | None = None,
) -> Any:
    """One public request with a token bucket and bounded retries.

    HTTP 403 means Cloudflare blocked the host: trip the breaker and raise.
    429/5xx and transport errors retry with exponential backoff.
    """
    gate = gate_for(url, registry)
    http = client or default_client()
    last_error: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        gate.acquire()
        try:
            response = http.request(method, url, params=params, json=json_body)
        except Exception as exc:  # noqa: BLE001 - transport errors degrade
            last_error = exc
            if attempt + 1 < MAX_ATTEMPTS:
                gate.sleep(min(BACKOFF_BASE_SECONDS * (2**attempt), BACKOFF_MAX_SECONDS))
            continue
        status = int(getattr(response, "status_code", 0))
        if status == 403:
            gate.trip()
            raise HostCircuitOpen(f"403 from {urlsplit(url).netloc}")
        if status == 429 or status >= 500:
            last_error = SourceUnavailable(f"HTTP {status}")
            if attempt + 1 < MAX_ATTEMPTS:
                gate.sleep(min(BACKOFF_BASE_SECONDS * (2**attempt), BACKOFF_MAX_SECONDS))
            continue
        if status >= 400:
            return None
        try:
            return response.json()
        except ValueError:
            return None
    raise SourceUnavailable(str(last_error or "source unavailable"))


def _first(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return None


# ---------------------------------------------------------------------------
# Source clients


def blockscout_lookup(
    chain: str,
    address: str,
    *,
    client: httpx.Client | None = None,
    registry: GateRegistry | None = None,
) -> SourceLookup | None:
    host = BLOCKSCOUT_HOSTS.get(chain)
    if not host:
        return None
    payload = request_json(
        "GET",
        f"{host}/api/v2/addresses/{address}",
        client=client,
        registry=registry,
    )
    if not isinstance(payload, dict):
        return None
    names: list[NameCandidate] = []
    name = str(payload.get("name") or "").strip()
    if name:
        names.append(NameCandidate(name=name, source="blockscout", is_weak=_is_weak(name)))
    ens_name = str(payload.get("ens_domain_name") or "").strip()
    if ens_name:
        names.append(NameCandidate(name=ens_name, source="blockscout.ens"))
    token = payload.get("token") if isinstance(payload.get("token"), dict) else None
    token_name = str((token or {}).get("name") or "").strip()
    if token_name:
        names.append(NameCandidate(name=token_name, source="blockscout.token", is_token=True))
    implementations = payload.get("implementations") or []
    implementation = None
    if isinstance(implementations, list):
        for item in implementations:
            if isinstance(item, dict):
                implementation = str(
                    item.get("name") or item.get("display_name") or item.get("address_hash") or ""
                ).strip() or None
            elif isinstance(item, str):
                implementation = item.strip() or None
            if implementation:
                break
    tags = _tag_names(payload)
    blob = " ".join(tags).casefold()
    is_scam = bool(payload.get("is_scam")) or any(
        marker in blob for marker in ("scam", "phish", "fake")
    )
    return SourceLookup(
        source="blockscout",
        names=tuple(names),
        is_contract=payload.get("is_contract") if isinstance(payload.get("is_contract"), bool) else None,
        is_scam=is_scam,
        implementation=implementation,
        token=token,
        meta={
            "proxy_type": payload.get("proxy_type"),
            "is_verified": payload.get("is_verified"),
            "tags": tags,
        },
    )


def _tag_names(payload: Mapping[str, Any]) -> list[str]:
    tags: list[str] = []
    public = payload.get("public_tags")
    if isinstance(public, list):
        for item in public:
            if isinstance(item, str):
                tags.append(item)
            elif isinstance(item, dict):
                tags.append(str(item.get("name") or item.get("label") or ""))
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        raw_tags = metadata.get("tags")
        if isinstance(raw_tags, list):
            for item in raw_tags:
                if isinstance(item, str):
                    tags.append(item)
                elif isinstance(item, dict):
                    tags.append(str(item.get("name") or ""))
    return [tag for tag in tags if tag]


def sourcify_lookup(
    chain: str,
    address: str,
    *,
    client: httpx.Client | None = None,
    registry: GateRegistry | None = None,
) -> SourceLookup | None:
    chain_id = CHAIN_IDS.get(chain)
    if chain_id is None:
        return None
    payload = request_json(
        "GET",
        SOURCIFY_URL.format(chain_id=chain_id, address=address),
        client=client,
        registry=registry,
        params={"fields": "compilation,proxyResolution"},
    )
    if not isinstance(payload, dict):
        return None
    compilation = payload.get("compilation") if isinstance(payload.get("compilation"), dict) else {}
    name = str(compilation.get("name") or payload.get("name") or "").strip()
    proxy = payload.get("proxyResolution") if isinstance(payload.get("proxyResolution"), dict) else {}
    implementations = proxy.get("implementations") or []
    implementation = None
    if isinstance(implementations, list):
        for item in implementations:
            if isinstance(item, dict):
                implementation = str(item.get("name") or item.get("displayName") or "").strip() or None
            elif isinstance(item, str):
                implementation = item.strip() or None
            if implementation:
                break
    names = (NameCandidate(name=name, source="sourcify", is_weak=_is_weak(name)),) if name else ()
    return SourceLookup(
        source="sourcify",
        names=names,
        is_contract=True,
        implementation=implementation,
        meta={"match": payload.get("match"), "is_proxy": proxy.get("isProxy")},
    )


def routescan_lookup(
    chain: str,
    address: str,
    *,
    client: httpx.Client | None = None,
    registry: GateRegistry | None = None,
) -> SourceLookup | None:
    chain_id = CHAIN_IDS.get(chain)
    if chain_id is None:
        return None
    payload = request_json(
        "GET",
        ROUTESCAN_URL.format(chain_id=chain_id),
        client=client,
        registry=registry,
        params={
            "module": "contract",
            "action": "getsourcecode",
            "address": address,
        },
    )
    if not isinstance(payload, dict):
        return None
    result = payload.get("result")
    if not isinstance(result, list) or not result or not isinstance(result[0], dict):
        return None
    item = result[0]
    name = str(item.get("ContractName") or "").strip()
    abi = str(item.get("ABI") or "")
    verified = bool(abi and abi != "Contract source code not verified")
    implementation = str(item.get("Implementation") or "").strip() or None
    if implementation and is_zero_address(implementation):
        implementation = None
    names = (
        (NameCandidate(name=name, source="routescan", is_weak=_is_weak(name)),)
        if name and verified
        else ()
    )
    return SourceLookup(
        source="routescan",
        names=names,
        is_contract=True,
        implementation=implementation,
        meta={"verified": verified, "proxy": item.get("Proxy")},
    )


def _rpc_call(
    chain: str,
    method: str,
    params: list[Any],
    *,
    client: httpx.Client | None = None,
    registry: GateRegistry | None = None,
) -> Any:
    host = RPC_HOSTS.get(chain)
    if not host:
        return None
    payload = request_json(
        "POST",
        host,
        client=client,
        registry=registry,
        json_body={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
    )
    if not isinstance(payload, dict):
        return None
    if payload.get("error"):
        return None
    return payload.get("result")


def _decode_abi_string(value: object) -> str | None:
    text = str(value or "").strip()
    if not text.startswith("0x") or len(text) <= 2:
        return None
    try:
        data = bytes.fromhex(text[2:])
    except ValueError:
        return None
    try:
        if len(data) >= 64:
            offset = int.from_bytes(data[:32], "big")
            if offset + 32 <= len(data):
                length = int.from_bytes(data[offset : offset + 32], "big")
                body = data[offset + 32 : offset + 32 + length]
                if body:
                    return body.decode("utf-8", "replace").strip("\x00").strip() or None
        if len(data) == 32:
            return data.rstrip(b"\x00").decode("utf-8", "replace").strip() or None
    except (ValueError, OverflowError):
        return None
    return None


def rpc_lookup(
    chain: str,
    address: str,
    *,
    client: httpx.Client | None = None,
    registry: GateRegistry | None = None,
) -> SourceLookup | None:
    if chain not in RPC_HOSTS:
        return None
    code = _rpc_call(chain, "eth_getCode", [address, "latest"], client=client, registry=registry)
    if code is None:
        return SourceLookup(source="rpc", meta={"code": None})
    code_text = str(code)
    code_len = max((len(code_text) - 2) // 2, 0) if code_text.startswith("0x") else 0
    if code_len == 0:
        return SourceLookup(
            source="rpc",
            is_contract=False,
            meta={"code_len": 0, "kind": "eoa"},
        )
    names: list[NameCandidate] = []
    name = _decode_abi_string(
        _rpc_call(chain, "eth_call", [{"to": address, "data": "0x06fdde03"}, "latest"], client=client, registry=registry)
    )
    if name:
        names.append(NameCandidate(name=name, source="rpc.name", is_weak=_is_weak(name)))
    symbol = _decode_abi_string(
        _rpc_call(chain, "eth_call", [{"to": address, "data": "0x95d89b41"}, "latest"], client=client, registry=registry)
    )
    token = {"name": name, "symbol": symbol} if (name or symbol) else None
    if symbol:
        names.append(NameCandidate(name=symbol, source="rpc.symbol", is_token=True))
    return SourceLookup(
        source="rpc",
        names=tuple(names),
        is_contract=True,
        token=token,
        meta={"code_len": code_len, "kind": "contract"},
    )


def _namehash(name: str) -> str | None:
    try:
        from Crypto.Hash import keccak  # type: ignore[import-not-found]
    except Exception:  # pragma: no cover - optional dependency
        return None

    def digest(value: bytes) -> bytes:
        hasher = keccak.new(digest_bits=256)
        hasher.update(value)
        return hasher.digest()

    node = b"\x00" * 32
    for label in reversed(name.split(".")):
        node = digest(node + digest(label.encode("utf-8")))
    return "0x" + node.hex()


def ens_lookup(
    chain: str,
    address: str,
    *,
    client: httpx.Client | None = None,
    registry: GateRegistry | None = None,
) -> SourceLookup | None:
    if chain != "ethereum" or not re.fullmatch(r"0x[a-f0-9]{40}", address):
        return None
    node = _namehash(f"{address[2:]}.addr.reverse")
    if not node:
        return None
    resolver_raw = _rpc_call(
        "ethereum",
        "eth_call",
        [{"to": ENS_REGISTRY, "data": "0x0178b8bf" + node[2:]}, "latest"],
        client=client,
        registry=registry,
    )
    if not resolver_raw:
        return None
    resolver_text = str(resolver_raw)
    try:
        resolver_int = int(resolver_text, 16)
    except ValueError:
        return None
    if resolver_int == 0:
        return None
    resolver = "0x" + resolver_text[-40:].rjust(40, "0")
    name_raw = _rpc_call(
        "ethereum",
        "eth_call",
        [{"to": resolver, "data": "0x691f3431" + node[2:]}, "latest"],
        client=client,
        registry=registry,
    )
    name = _decode_abi_string(name_raw)
    if not name:
        return None
    return SourceLookup(
        source="ens",
        names=(NameCandidate(name=name, source="ens"),),
        is_contract=None,
        meta={"resolver": resolver},
    )


# ---------------------------------------------------------------------------
# Label selection and TTL


def select_label(info: AddressInfo) -> tuple[str, str, str, bool]:
    """Return ``(label, kind, source, is_scam)`` for one address.

    Priority: specific external name > implementation name > token name >
    weak proxy/helper name > contract/EOA/unknown fallback.  A weak name with
    an implementation becomes ``weak → implementation``.
    """
    if is_zero_address(info.address):
        return ZERO_LABEL, "zero", "fallback", False

    names = list(info.names)
    specific = [
        name
        for name in names
        if name.name and not name.is_token and not name.is_weak
    ]
    specific.sort(key=lambda item: _SOURCE_RANK.get(item.source, 9))
    token_names = [name for name in names if name.is_token and name.name]
    weak_names = [name for name in names if name.is_weak and name.name]
    weak_names.sort(key=lambda item: _SOURCE_RANK.get(item.source, 9))
    implementation = info.implementation
    is_scam = info.is_scam

    if specific:
        chosen = specific[0]
        token_name = (info.token or {}).get("name")
        kind = (
            "token"
            if token_name and chosen.name.casefold() == str(token_name).casefold()
            else "contract"
        )
        return _truncate(chosen.name), kind, chosen.source, is_scam
    if weak_names and implementation:
        # A proxy/helper shape is weak on its own, but the implementation name
        # is the real identity: show both instead of hiding the proxy.
        chosen = weak_names[0]
        return (
            _truncate(f"{chosen.name} → {implementation}"),
            "contract",
            f"{chosen.source}+implementation",
            is_scam,
        )
    if implementation:
        return _truncate(implementation), "contract", "implementation", is_scam
    if token_names:
        chosen = token_names[0]
        return _truncate(chosen.name), "token", chosen.source, is_scam
    if weak_names:
        chosen = weak_names[0]
        kind = "contract"
        source = chosen.source
        # A weak name without implementation is still more informative than
        # "unnamed", but mark it so callers can explain the weaker evidence.
        return _truncate(chosen.name), kind, source, is_scam
    if info.is_contract is True:
        return UNNAMED_CONTRACT_LABEL, "contract", "fallback", is_scam
    if info.is_contract is False:
        return EOA_LABEL, "eoa", "rpc", is_scam
    return fallback_label("unknown", info.address, info.chain), "unknown", "fallback", is_scam


def ttl_seconds(kind: str, label: str, *, source: str, is_scam: bool) -> int | None:
    if source == "manual":
        return None
    if is_scam:
        return 24 * 3600
    if kind == "zero":
        return 30 * 24 * 3600
    if kind == "contract":
        if label in {UNNAMED_CONTRACT_LABEL, UNKNOWN_LABEL}:
            return 3 * 24 * 3600
        if any(weak in label.casefold() for weak in ("proxy", "helper")):
            return 7 * 24 * 3600
        return 30 * 24 * 3600
    if kind == "token":
        return 30 * 24 * 3600
    if kind == "eoa":
        return 7 * 24 * 3600
    return 24 * 3600


def build_label_record(
    info: AddressInfo,
    *,
    now: datetime | None = None,
) -> LabelRecord:
    label, kind, source, is_scam = select_label(info)
    current = _as_utc(now) or _utcnow()
    ttl = ttl_seconds(kind, label, source=source, is_scam=is_scam)
    expires = current + timedelta(seconds=ttl) if ttl is not None else None
    meta = {
        "address": info.address,
        "chain": info.chain,
        "names": [
            {
                "name": candidate.name,
                "source": candidate.source,
                "is_token": candidate.is_token,
                "is_weak": candidate.is_weak,
            }
            for candidate in info.names
        ],
        "implementation": info.implementation,
        "token": info.token,
        "is_contract": info.is_contract,
        "sources": [
            {
                "source": lookup.source,
                "is_contract": lookup.is_contract,
                "is_scam": lookup.is_scam,
                **lookup.meta,
            }
            for lookup in info.lookups
        ],
        "errors": list(info.errors),
    }
    return LabelRecord(
        chain=str(info.chain or ""),
        address=info.address,
        label=_truncate(label),
        kind=kind if kind in ADDRESS_LABEL_KINDS else "unknown",
        source=source,
        is_scam=is_scam,
        meta=meta,
        fetched_at=current,
        expires_at=expires,
    )


def solana_rpc_url() -> str | None:
    """Build the Helius RPC URL from existing config without exposing the key."""
    key = str(getattr(settings, "helius_api_key", "") or "").strip()
    if not key:
        return None
    return f"https://mainnet.helius-rpc.com/?api-key={key}"


def solana_account_lookup(
    chain: str,
    address: str,
    *,
    client: httpx.Client | None = None,
    registry: GateRegistry | None = None,
) -> SourceLookup | None:
    """Resolve a Solana account via Helius ``getAccountInfo``.

    Only the account shape is used: executable/system-owned/program-owned and
    token-program-owned.  No transaction payload or API key is ever returned
    or logged.
    """
    url = solana_rpc_url()
    if not url:
        return None
    payload = request_json(
        "POST",
        url,
        client=client,
        registry=registry,
        json_body={
            "jsonrpc": "2.0",
            "id": "p11h-account",
            "method": "getAccountInfo",
            "params": [address, {"encoding": "base64", "commitment": "confirmed"}],
        },
    )
    if not isinstance(payload, Mapping):
        return None
    result = payload.get("result")
    if not isinstance(result, Mapping):
        return None
    value = result.get("value")
    if not isinstance(value, Mapping):
        return None
    owner = str(value.get("owner") or "").strip()
    executable = value.get("executable") is True
    base_meta = {"owner": owner, "executable": executable}
    if owner in {SOLANA_TOKEN_PROGRAM, SOLANA_TOKEN_2022_PROGRAM} and not executable:
        return SourceLookup(
            "helius.rpc",
            (NameCandidate(SOLANA_TOKEN_ACCOUNT_LABEL, "helius.rpc", is_token=True),),
            is_contract=False,
            meta=base_meta,
        )
    known_owner = SOLANA_KNOWN_PROGRAMS.get(owner)
    if known_owner and not executable and owner != SOLANA_SYSTEM_PROGRAM:
        return SourceLookup(
            "helius.rpc",
            (
                NameCandidate(
                    f"{known_owner} 账户",
                    "helius.rpc+known_owner",
                ),
            ),
            is_contract=True,
            meta=base_meta,
        )
    if executable:
        return SourceLookup("helius.rpc", (), is_contract=True, meta=base_meta)
    if owner == SOLANA_SYSTEM_PROGRAM:
        return SourceLookup("helius.rpc", (), is_contract=False, meta=base_meta)
    # A non-executable account owned by another program is a PDA/program
    # account, not a user wallet.
    return SourceLookup("helius.rpc", (), is_contract=True, meta=base_meta)


def fetch_address_info(
    chain: str | None,
    address: str,
    *,
    client: httpx.Client | None = None,
    registry: GateRegistry | None = None,
    program_ids: Iterable[str] | None = None,
) -> AddressInfo:
    canonical_chain = normalize_chain(chain)
    canonical_address = normalize_address(address)
    if is_zero_address(canonical_address):
        return AddressInfo(
            chain=canonical_chain,
            address=canonical_address,
            lookups=(),
            is_contract=False,
            is_scam=False,
        )
    if not canonical_chain:
        return AddressInfo(
            chain=None,
            address=canonical_address,
            lookups=(),
            is_contract=None,
            is_scam=False,
            errors=("unknown_chain",),
        )

    if canonical_chain == SOLANA_CHAIN:
        if not is_valid_solana_address(canonical_address):
            return AddressInfo(
                chain=canonical_chain,
                address=canonical_address,
                lookups=(),
                is_contract=None,
                is_scam=False,
                errors=("invalid_address",),
            )
        hints = {str(value).strip() for value in (program_ids or ()) if value}
        lookups: list[SourceLookup] = []
        errors: list[str] = []
        known = solana_known_program(canonical_address)
        if known:
            lookups.append(
                SourceLookup(
                    "solana.known_program",
                    (NameCandidate(known, "solana.known_program"),),
                    is_contract=True,
                    meta={"program_id": canonical_address},
                )
            )
        elif canonical_address in hints:
            lookups.append(
                SourceLookup(
                    "solana.tx_program",
                    (),
                    is_contract=True,
                    meta={"program_id": canonical_address},
                )
            )
        else:
            try:
                result = solana_account_lookup(
                    canonical_chain, canonical_address, client=client, registry=registry
                )
            except HostCircuitOpen:
                errors.append("solana:circuit_open")
                result = None
            except SourceUnavailable:
                errors.append("solana:unavailable")
                result = None
            except Exception:  # noqa: BLE001 - one source never aborts the rest
                logger.debug("solana account lookup failed", exc_info=True)
                errors.append("solana:error")
                result = None
            if result is not None:
                lookups.append(result)
        is_contract = lookups[0].is_contract if lookups else None
        return AddressInfo(
            chain=canonical_chain,
            address=canonical_address,
            lookups=tuple(lookups),
            is_contract=is_contract,
            is_scam=False,
            errors=tuple(errors),
        )

    lookups = []
    errors = []

    def run(fn: Callable[..., SourceLookup | None], source: str) -> SourceLookup | None:
        try:
            result = fn(canonical_chain, canonical_address, client=client, registry=registry)
        except HostCircuitOpen:
            errors.append(f"{source}:circuit_open")
            return None
        except SourceUnavailable:
            errors.append(f"{source}:unavailable")
            return None
        except Exception:  # noqa: BLE001 - one source never aborts the rest
            logger.debug("address label source failed: %s", source, exc_info=True)
            errors.append(f"{source}:error")
            return None
        if result is not None:
            lookups.append(result)
        return result

    run(blockscout_lookup, "blockscout")
    rpc_result = run(rpc_lookup, "rpc")
    is_contract: bool | None = None
    if rpc_result is not None and rpc_result.is_contract is not None:
        is_contract = rpc_result.is_contract
    if is_contract is None:
        blockscout = next((lookup for lookup in lookups if lookup.source == "blockscout"), None)
        if blockscout is not None and blockscout.is_contract is not None:
            is_contract = blockscout.is_contract

    if is_contract is not False:
        run(sourcify_lookup, "sourcify")
        run(routescan_lookup, "routescan")
    run(ens_lookup, "ens")

    is_scam = any(lookup.is_scam for lookup in lookups)
    return AddressInfo(
        chain=canonical_chain,
        address=canonical_address,
        lookups=tuple(lookups),
        is_contract=is_contract,
        is_scam=is_scam,
        errors=tuple(errors),
    )


def fetch_address_label(
    chain: str | None,
    address: str,
    *,
    client: httpx.Client | None = None,
    registry: GateRegistry | None = None,
    now: datetime | None = None,
    program_ids: Iterable[str] | None = None,
) -> LabelRecord:
    """Fetch and select a label.  Never writes the database."""
    info = fetch_address_info(
        chain, address, client=client, registry=registry, program_ids=program_ids
    )
    record = build_label_record(info, now=now)
    if not record.chain and not is_zero_address(address):
        # An unknown chain must not share a row with a real chain.
        record = LabelRecord(
            chain="",
            address=record.address,
            label=UNKNOWN_LABEL,
            kind="unknown",
            source="fallback",
            is_scam=False,
            meta={**record.meta, "chain": None},
            fetched_at=record.fetched_at,
            expires_at=record.fetched_at + timedelta(hours=24),
        )
    return record


# ---------------------------------------------------------------------------
# Cache access


def _row_to_record(row: AddressLabel) -> LabelRecord:
    return LabelRecord(
        chain=row.chain,
        address=row.address,
        label=row.label,
        kind=row.kind,
        source=row.source,
        is_scam=bool(row.is_scam),
        meta=dict(row.meta or {}),
        fetched_at=_as_utc(row.fetched_at) or _utcnow(),
        expires_at=_as_utc(row.expires_at),
    )


def get_cached_label(
    db,
    chain: str | None,
    address: str,
    *,
    now: datetime | None = None,
) -> LabelRecord | None:
    canonical_chain = normalize_chain(chain)
    canonical_address = normalize_address(address)
    if not canonical_chain or not canonical_address:
        return None
    row = db.get(AddressLabel, (canonical_chain, canonical_address))
    if row is None:
        return None
    expires = _as_utc(row.expires_at)
    current = _as_utc(now) or _utcnow()
    if expires is not None and expires <= current:
        return None
    return _row_to_record(row)


def load_cached_labels(
    db,
    pairs: Iterable[tuple[str | None, str]],
    *,
    now: datetime | None = None,
) -> dict[tuple[str, str], LabelRecord]:
    result: dict[tuple[str, str], LabelRecord] = {}
    for chain, address in pairs:
        canonical_chain = normalize_chain(chain)
        canonical_address = normalize_address(address)
        if not canonical_chain or not canonical_address:
            continue
        # db.get is cheap and avoids cross-dialect tuple-IN quirks.  A sync has
        # at most a few hundred distinct pairs.
        row = db.get(AddressLabel, (canonical_chain, canonical_address))
        if row is None:
            continue
        expires = _as_utc(row.expires_at)
        current = _as_utc(now) or _utcnow()
        if expires is not None and expires <= current:
            continue
        result[(canonical_chain, canonical_address)] = _row_to_record(row)
    return result


def upsert_label(db, record: LabelRecord) -> AddressLabel:
    """Insert/update one cache row.  Manual overrides are never overwritten."""
    if not record.chain:
        raise ValueError("address label needs a chain")
    existing = db.get(AddressLabel, (record.chain, record.address))
    if existing is not None and existing.source == "manual":
        return existing
    if existing is None:
        existing = AddressLabel(
            chain=record.chain,
            address=record.address,
            label=record.label,
            kind=record.kind,
            source=record.source,
            is_scam=record.is_scam,
            meta=record.meta or None,
            fetched_at=record.fetched_at,
            expires_at=record.expires_at,
        )
        db.add(existing)
    else:
        existing.label = record.label
        existing.kind = record.kind
        existing.source = record.source
        existing.is_scam = record.is_scam
        existing.meta = record.meta or None
        existing.fetched_at = record.fetched_at
        existing.expires_at = record.expires_at
    return existing


def set_manual_label(
    db,
    chain: str,
    address: str,
    label: str,
    *,
    kind: str = "contract",
    is_scam: bool = False,
    meta: dict[str, Any] | None = None,
) -> AddressLabel:
    canonical_chain = normalize_chain(chain)
    canonical_address = normalize_address(address)
    if not canonical_chain or not canonical_address:
        raise ValueError("manual label needs chain and address")
    if kind not in ADDRESS_LABEL_KINDS:
        raise ValueError(f"invalid kind: {kind}")
    row = db.get(AddressLabel, (canonical_chain, canonical_address))
    if row is None:
        row = AddressLabel(
            chain=canonical_chain,
            address=canonical_address,
            label=label,
            kind=kind,
            source="manual",
            is_scam=is_scam,
            meta=meta or None,
            fetched_at=_utcnow(),
            expires_at=None,
        )
        db.add(row)
    else:
        row.label = label
        row.kind = kind
        row.source = "manual"
        row.is_scam = is_scam
        row.meta = meta or None
        row.fetched_at = _utcnow()
        row.expires_at = None
    return row


# ---------------------------------------------------------------------------
# Sync-path application and queueing


def transfer_chain(transfer: Any) -> str | None:
    raw = getattr(transfer, "raw", None) or {}
    if not isinstance(raw, Mapping):
        return None
    chain = normalize_chain(raw.get("chain"))
    if chain:
        return chain
    # Helius rows are Solana even if an older fixture omitted raw.chain.
    if str(getattr(transfer, "source", "") or "") == "helius":
        return SOLANA_CHAIN
    return None


def transfer_program_ids(transfer: Any) -> set[str]:
    raw = getattr(transfer, "raw", None) or {}
    if not isinstance(raw, Mapping):
        return set()
    value = raw.get("program_ids") or raw.get("program_id") or ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return set()
    return {str(item).strip() for item in value if str(item).strip()}


def label_meta_for_transfer(
    transfer: Any,
    *,
    cached: Mapping[tuple[str, str], LabelRecord],
    protocol_hits: Mapping[str, str],
    now: datetime | None = None,
) -> LabelMeta:
    """Apply priority to one transfer using cache/rule only (no network)."""
    chain = transfer_chain(transfer)
    address = normalize_address(getattr(transfer, "address", ""))
    cache_key = cache_address(chain, address)
    txid = normalize_txid_value(getattr(transfer, "txid", ""))
    hit_asset = protocol_hits.get(txid) if txid else None
    direction = str(getattr(transfer, "direction", "") or "").casefold()
    record = cached.get((chain or "", cache_key)) if chain else None
    written_address = cache_key or None

    if is_zero_address(address):
        return LabelMeta(
            chain=chain,
            label=ZERO_LABEL,
            kind="zero",
            source="fallback",
            is_scam=False,
            protocol_deposit=bool(hit_asset),
            protocol_deposit_asset=hit_asset,
            missing=False,
            address=written_address,
        )
    if record is not None and record.source == "manual":
        return LabelMeta(
            chain=chain,
            label=record.label,
            kind=record.kind,
            source="manual",
            is_scam=record.is_scam,
            protocol_deposit=bool(hit_asset),
            protocol_deposit_asset=hit_asset,
            missing=False,
            address=written_address,
        )
    if direction == "out" and hit_asset:
        return LabelMeta(
            chain=chain,
            label=protocol_deposit_label(hit_asset),
            kind="contract",
            source="rule",
            is_scam=False,
            protocol_deposit=True,
            protocol_deposit_asset=hit_asset,
            missing=False,
            address=written_address,
        )
    if record is not None:
        return LabelMeta(
            chain=chain,
            label=record.label,
            kind=record.kind,
            source=record.source,
            is_scam=record.is_scam,
            protocol_deposit=bool(hit_asset),
            protocol_deposit_asset=hit_asset,
            missing=False,
            address=written_address,
        )
    if chain == SOLANA_CHAIN and address:
        if not is_valid_solana_address(address):
            # Test/placeholder addresses must never reach Helius.
            return LabelMeta(
                chain=chain,
                label=UNKNOWN_LABEL,
                kind="unknown",
                source="fallback",
                is_scam=False,
                protocol_deposit=False,
                protocol_deposit_asset=None,
                missing=False,
                address=written_address,
            )
        known = solana_known_program(address)
        if known:
            return LabelMeta(
                chain=chain,
                label=known,
                kind="contract",
                source="known_program",
                is_scam=False,
                protocol_deposit=False,
                protocol_deposit_asset=None,
                missing=True,
                address=written_address,
            )
        if address in transfer_program_ids(transfer):
            # The tx itself names this account as a program, so it is a
            # contract even when the id is not in the bundled list.
            return LabelMeta(
                chain=chain,
                label=UNNAMED_CONTRACT_LABEL,
                kind="contract",
                source="tx_program",
                is_scam=False,
                protocol_deposit=False,
                protocol_deposit_asset=None,
                missing=True,
                address=written_address,
            )
    # A zero-address in-leg is handled above.  Everything else is a cache
    # miss: give the neutral "not found" fallback now, never a guessed name,
    # and let the job fill the cache.
    return LabelMeta(
        chain=chain,
        label=UNKNOWN_LABEL,
        kind="unknown",
        source="fallback",
        is_scam=False,
        protocol_deposit=bool(hit_asset),
        protocol_deposit_asset=hit_asset,
        missing=bool(chain and address),
        address=written_address,
    )


def enqueue_label_refresh(
    pairs: Iterable[tuple[str | None, str]],
    *,
    enqueue: Callable[[str, list[str]], Any] | None = None,
) -> int:
    """Queue one background job per chain for missing (chain,address) pairs.

    The caller has already committed pending rows.  A dead Redis is logged and
    ignored: a label refresh must never fail a sync.
    """
    grouped: dict[str, set[str]] = {}
    for chain, address in pairs:
        canonical_chain = normalize_chain(chain)
        canonical_address = cache_address(canonical_chain, address)
        if not canonical_chain or not canonical_address or is_zero_address(canonical_address):
            continue
        if canonical_chain == SOLANA_CHAIN and not is_valid_solana_address(canonical_address):
            continue
        grouped.setdefault(canonical_chain, set()).add(canonical_address)
    if not grouped:
        return 0
    count = 0
    for chain, addresses in grouped.items():
        try:
            if enqueue is not None:
                enqueue(chain, sorted(addresses))
            else:
                from .sync.queue import get_queue

                get_queue().enqueue(
                    "app.sync.jobs.refresh_address_labels_job",
                    chain,
                    sorted(addresses),
                    job_timeout=300,
                )
            count += len(addresses)
        except Exception:  # noqa: BLE001 - Redis is optional for this feature
            logger.warning("address label enqueue failed chain=%s count=%s", chain, len(addresses))
    return count


def refresh_labels(
    db,
    chain: str,
    addresses: Iterable[str],
    *,
    client: httpx.Client | None = None,
    registry: GateRegistry | None = None,
    now: datetime | None = None,
) -> list[LabelRecord]:
    """Background-job body: fetch and cache each address, one host at a time."""
    records: list[LabelRecord] = []
    canonical_chain = normalize_chain(chain)
    for value in dict.fromkeys(addresses):
        address = cache_address(canonical_chain, value)
        if not address:
            continue
        if canonical_chain == SOLANA_CHAIN and not is_valid_solana_address(address):
            continue
        record = fetch_address_label(
            canonical_chain, address, client=client, registry=registry, now=now
        )
        if not record.chain:
            continue
        upsert_label(db, record)
        records.append(record)
    db.commit()
    return records


# ---------------------------------------------------------------------------
# Transaction -> chain inference (used by the backfill only)


def chain_from_routescan(
    txid: str,
    *,
    client: httpx.Client | None = None,
    registry: GateRegistry | None = None,
) -> str | None:
    payload = request_json(
        "GET",
        ROUTESCAN_TX_URL.format(txid=txid),
        client=client,
        registry=registry,
    )
    if not isinstance(payload, dict):
        return None
    try:
        chain_id = int(payload.get("chainId"))
    except (TypeError, ValueError):
        return None
    return CHAIN_BY_ID.get(chain_id)


def detect_chain_for_tx(
    txid: str,
    *,
    client: httpx.Client | None = None,
    registry: GateRegistry | None = None,
) -> str | None:
    """Resolve one tx hash to a chain with public, read-only sources.

    Routescan's cross-chain endpoint is one request.  If it has no answer,
    fall back to Blockscout Ethereum and then public RPC receipts in the same
    order the research script used.  Never guesses from the address alone.
    """
    normalized = normalize_txid_value(txid)
    if not normalized:
        return None
    chain = chain_from_routescan(normalized, client=client, registry=registry)
    if chain:
        return chain
    try:
        payload = request_json(
            "GET",
            f"{BLOCKSCOUT_HOSTS['ethereum']}/api/v2/transactions/{normalized}",
            client=client,
            registry=registry,
        )
    except (HostCircuitOpen, SourceUnavailable):
        payload = None
    if isinstance(payload, dict) and payload.get("hash"):
        return "ethereum"
    order = [
        "base",
        "arbitrum",
        "polygon",
        "bsc",
        "avalanche",
        "optimism",
        "linea",
        "sonic",
        "mantle",
        "blast",
        "ink",
    ]
    for candidate in order:
        try:
            receipt = _rpc_call(
                candidate,
                "eth_getTransactionReceipt",
                [normalized],
                client=client,
                registry=registry,
            )
        except (HostCircuitOpen, SourceUnavailable):
            continue
        if receipt:
            return candidate
    return None


# ---------------------------------------------------------------------------
# Backfill and detection/fix scripts


_EVM_ADDRESS_RE = re.compile(r"0x[a-fA-F0-9]{40}\b")
_BASE58_ADDRESS_RE = re.compile(r"(?<![0-9A-Za-z])[1-9A-HJ-NP-Za-km-z]{32,44}(?![0-9A-Za-z])")


def _pending_source(row: PendingConfirmation) -> str:
    meta = row.meta_data
    return str(meta.get("source") or row.source or "").strip().casefold()


def _addresses_in_text(text: str) -> tuple[list[str], list[str]]:
    evm = list(dict.fromkeys(match.group(0).lower() for match in _EVM_ADDRESS_RE.finditer(text)))
    solana: list[str] = []
    for match in _BASE58_ADDRESS_RE.finditer(text):
        candidate = match.group(0)
        if is_valid_solana_address(candidate) and candidate not in solana:
            solana.append(candidate)
    return evm, solana


def _pending_address(row: PendingConfirmation) -> str:
    meta = row.meta_data
    for key in ("counterparty_address", "counterparty", "address"):
        value = meta.get(key)
        if value:
            return cache_address(normalize_chain(meta.get("chain")), value)
    detail = str(row.detail or "")
    evm, solana = _addresses_in_text(detail)
    if len(evm) > 1 or len(solana) > 1 or (evm and solana):
        return ""  # ambiguous: never pick a counterparty by guessing
    match = _DETAIL_ADDRESS_RE.search(detail)
    if match:
        candidate = match.group(1).strip()
        if candidate.lower().startswith("0x"):
            return candidate.lower()
        if is_valid_solana_address(candidate):
            return candidate
        if _pending_source(row) == "helius" and re.fullmatch(
            r"[0-9A-Za-z]+", candidate
        ):
            # Keep the explicit Helius detail as an unknown-later address even
            # when it is a known test placeholder rather than valid base58.
            # Plain alphanumerics only: prose such as "自有地址→transfer" must
            # never become a counterparty.
            return candidate
    source = _pending_source(row)
    if source == "helius":
        return solana[0] if len(solana) == 1 else ""
    if len(evm) == 1:
        return evm[0]
    if len(evm) > 1:
        return ""  # ambiguous: never pick a counterparty by guessing
    return solana[0] if len(solana) == 1 else ""


def _pending_txid(row: PendingConfirmation) -> str:
    meta = row.meta_data
    return (
        normalize_txid_value(row.external_id)
        or normalize_txid_value(meta.get("tx_hash"))
        or normalize_txid_value(meta.get("txid"))
        or normalize_txid_value(row.detail)
    )


def _cached_chains_for_address(db, address: str) -> set[str]:
    rows = db.scalars(
        select(AddressLabel.chain).where(AddressLabel.address == normalize_address(address))
    ).all()
    return {str(chain) for chain in rows if chain}


def _pending_chain(
    db,
    row: PendingConfirmation,
    address: str,
    *,
    offline: bool,
    chain_resolver: Callable[[str], str | None] | None,
    cache: dict[str, str | None],
) -> str | None:
    meta = row.meta_data
    chain = normalize_chain(meta.get("chain")) or normalize_chain(meta.get("chain_id"))
    if chain:
        return chain
    source = _pending_source(row)
    if source == "helius" or is_valid_solana_address(address):
        return SOLANA_CHAIN
    if not is_valid_evm_address(address):
        return None
    chains = _cached_chains_for_address(db, address)
    if len(chains) == 1:
        return normalize_chain(next(iter(chains)))
    if offline:
        return None
    txid = _pending_txid(row)
    if not txid:
        return None
    if txid not in cache:
        try:
            cache[txid] = (
                chain_resolver(txid) if chain_resolver is not None else detect_chain_for_tx(txid)
            )
        except Exception:  # noqa: BLE001 - one tx must not stop the backfill
            cache[txid] = None
    return normalize_chain(cache.get(txid))


def _patched_meta(
    row: PendingConfirmation,
    chain: str,
    address: str,
    record: LabelRecord | None,
) -> dict[str, Any]:
    meta = dict(row.meta_data)
    meta["chain"] = chain
    meta["counterparty_address"] = cache_address(chain, address)
    if record is not None:
        meta["counterparty_label"] = record.label
        meta["counterparty_kind"] = record.kind
        meta["counterparty_label_source"] = record.source
        meta["counterparty_is_scam"] = bool(record.is_scam)
    return meta


def backfill_counterparty_labels(
    db,
    *,
    apply: bool = False,
    user_id: int | None = None,
    limit: int | None = None,
    offline: bool = False,
    chain_resolver: Callable[[str], str | None] | None = None,
    fetcher: Callable[[str, str], LabelRecord] | None = None,
    now: datetime | None = None,
) -> BackfillReport:
    """Fill pending ``meta`` chain/address and the label cache from old rows.

    Dry-run is the default, performs no writes and never touches the network.
    ``--apply`` may resolve an EVM tx chain and fetch labels, but only when
    ``--offline`` is not set.  A second apply is a no-op.  Pairs are
    deduplicated by ``(chain, address)``; ambiguous/unparseable rows are
    skipped and counted, never guessed.
    """
    query = select(PendingConfirmation).order_by(PendingConfirmation.id.asc())
    if user_id is not None:
        query = query.where(PendingConfirmation.user_id == user_id)
    all_rows = list(db.scalars(query).all())
    total = len(all_rows)
    rows = all_rows[:limit] if limit is not None else all_rows
    effective_offline = bool(offline or not apply)
    report = BackfillReport(
        total_pending_rows=total,
        scanned_rows=len(rows),
        dry_run=not apply,
        offline=effective_offline,
    )
    current = _as_utc(now) or _utcnow()
    chain_of_tx: dict[str, str | None] = {}

    # pair -> the rows that need that pair written.  Also remember the cached
    # record per pair so --apply does not fetch it twice.
    targets: dict[tuple[str, str], list[PendingConfirmation]] = {}
    reports_for_pair: dict[tuple[str, str], LabelRecord | None] = {}
    for row in rows:
        address = _pending_address(row)
        if not address:
            report.no_address_rows += 1
            continue
        if is_zero_address(address):
            report.skipped_rows += 1
            continue
        is_solana = bool(is_valid_solana_address(address))
        if not is_solana and not is_valid_evm_address(address):
            report.invalid_address_rows += 1
            # A Helius row may carry a deliberately fake/placeholder address;
            # it is still classifiable as unknown without any network call.
            if _pending_source(row) == "helius":
                is_solana = True
            else:
                report.no_address_rows += 1
                continue
        if is_solana:
            report.solana_candidate_rows += 1
        else:
            report.evm_candidate_rows += 1
        chain = _pending_chain(
            db,
            row,
            address,
            offline=effective_offline,
            chain_resolver=chain_resolver,
            cache=chain_of_tx,
        )
        if not chain:
            report.unresolved_chain += 1
            continue
        pair = (chain, cache_address(chain, address))
        if is_solana:
            report.solana_patchable_rows += 1
        else:
            if not is_valid_evm_address(address):
                report.no_address_rows += 1
                continue
            report.evm_patchable_rows += 1
        targets.setdefault(pair, []).append(row)
        if pair not in reports_for_pair:
            reports_for_pair[pair] = get_cached_label(db, chain, pair[1], now=current)

    report.unique_pairs = len(targets)
    fetch = fetcher or (lambda chain, address: fetch_address_label(chain, address, now=now))
    for pair, pair_rows in targets.items():
        chain, address = pair
        cached = reports_for_pair.get(pair)
        if cached is not None:
            report.cache_hits += 1
            report.cache_hit_rows += len(pair_rows)
            record = cached
        else:
            known_or_invalid = (
                chain == SOLANA_CHAIN
                and (
                    solana_known_program(address) is not None
                    or not is_valid_solana_address(address)
                )
            )
            if effective_offline and not known_or_invalid:
                report.unique_addresses += 1
                continue
            try:
                if known_or_invalid:
                    # Known programs and invalid placeholders are local-only;
                    # never call a (possibly network-backed) fetcher for them.
                    record = fetch_address_label(chain, address, now=now)
                else:
                    record = fetch(chain, address)
            except Exception as exc:  # noqa: BLE001 - keep the rest of the batch
                report.errors.append(
                    f"{chain}:{abbreviate_address(address)}:{type(exc).__name__}"
                )
                if not known_or_invalid:
                    report.unique_addresses += 1
                continue
            if known_or_invalid:
                report.local_only_pairs += 1
            else:
                report.fetched += 1
                report.unique_addresses += 1
            if apply and (
                chain != SOLANA_CHAIN or is_valid_solana_address(address)
            ):
                # Never cache a deliberately fake/placeholder Solana address;
                # its pending.meta still carries the explicit unknown label.
                upsert_label(db, record)
        for row in pair_rows:
            meta = _patched_meta(row, chain, address, record)
            changed = meta != row.meta_data
            report.changes.append(
                BackfillChange(
                    chain=chain,
                    address=address,
                    label=record.label,
                    kind=record.kind,
                    source=record.source,
                    expires_at=record.expires_at.isoformat() if record.expires_at else None,
                    pending_id=row.id,
                )
            )
            if apply and changed:
                row.meta = meta
                report.written += 1
    if apply:
        db.commit()
    return report


def find_protocol_deposit_withdraw_rows(
    db,
    *,
    user_id: int | None = None,
) -> list[dict[str, Any]]:
    """Read-only detection list for deposit rows still suggested as withdraw.

    Includes confirmed rows (for example row 249) so the caller can report the
    historical mismatch without ever mutating it.
    """
    query = select(PendingConfirmation).where(PendingConfirmation.source == "zerion")
    if user_id is not None:
        query = query.where(PendingConfirmation.user_id == user_id)
    rows = list(db.scalars(query).all())
    found: list[dict[str, Any]] = []
    for row, in_asset in protocol_deposit_out_rows(rows):
        found.append(
            {
                "id": row.id,
                "user_id": row.user_id,
                "status": row.status,
                "txid": _pending_txid(row),
                "asset": row.asset,
                "suggested_category": row.suggested_category,
                "in_asset": in_asset,
                "ledger_entry_id": row.ledger_entry_id,
            }
        )
    found.sort(key=lambda item: int(item["id"]))
    return found


def fix_pending_protocol_deposit_categories(
    db,
    *,
    apply: bool = False,
    user_id: int | None = None,
    backup_path: str | Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Correct only pending withdraw rows hit by the protocol-deposit rule.

    Guard rails:
    * only ``status='pending'`` and ``suggested_category='withdraw'``;
    * confirmed rows (row 249, ledger_entry 6) are never touched;
    * rerunning is a no-op because the category is no longer ``withdraw``;
    * ``--apply`` requires ``backup_path`` and writes old values before update.
    """
    if apply and backup_path is None:
        raise ValueError("--apply requires --backup so old values are preserved")
    query = select(PendingConfirmation).where(
        PendingConfirmation.source == "zerion",
        PendingConfirmation.status == "pending",
        PendingConfirmation.direction == "out",
        PendingConfirmation.suggested_category == "withdraw",
    )
    if user_id is not None:
        query = query.where(PendingConfirmation.user_id == user_id)
    candidates = list(db.scalars(query.order_by(PendingConfirmation.id.asc())).all())

    # The rule needs every leg of a tx, not only the candidate out rows.
    txids = {_pending_txid(row) for row in candidates if _pending_txid(row)}
    all_rows: list[PendingConfirmation] = []
    if txids:
        all_query = select(PendingConfirmation).where(
            PendingConfirmation.source == "zerion"
        )
        if user_id is not None:
            all_query = all_query.where(PendingConfirmation.user_id == user_id)
        all_rows = list(db.scalars(all_query).all())
    hits = protocol_deposit_hits(
        leg for leg in (leg_from_pending(row) for row in all_rows) if leg is not None
    )

    changes: list[dict[str, Any]] = []
    for row in candidates:
        txid = _pending_txid(row)
        in_asset = hits.get(txid)
        if not in_asset:
            continue
        meta = dict(row.meta_data)
        old_meta = dict(meta)
        reason = f"同 tx 收到 {in_asset} 且入账来自零地址，判定为协议存入"
        meta["protocol_deposit"] = True
        meta["protocol_deposit_asset"] = in_asset
        if meta.get("counterparty_label_source") != "manual":
            meta["counterparty_label"] = protocol_deposit_label(in_asset)
            meta["counterparty_label_source"] = "rule"
            meta["counterparty_kind"] = "contract"
        changes.append(
            {
                "id": row.id,
                "txid": txid,
                "asset": row.asset,
                "in_asset": in_asset,
                "old_suggested_category": row.suggested_category,
                "old_suggested_reason": row.suggested_reason,
                "old_meta": old_meta,
                "new_suggested_category": PROTOCOL_DEPOSIT_CATEGORY,
                "new_suggested_reason": reason,
                "new_meta": meta,
            }
        )
        if apply:
            row.suggested_category = PROTOCOL_DEPOSIT_CATEGORY
            row.suggested_reason = reason[:255]
            row.meta = meta or None

    if apply and changes:
        path = Path(backup_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "generated_at": (_as_utc(now) or _utcnow()).isoformat(),
                    "changes": changes,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        db.commit()

    return {
        "dry_run": not apply,
        "candidate_rows": len(candidates),
        "matched_rows": len(changes),
        "written": len(changes) if apply else 0,
        "backup_path": str(backup_path) if apply and changes and backup_path else None,
        "changes": changes,
    }
