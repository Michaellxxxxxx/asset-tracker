"""Zerion wallet transactions, with an Alchemy fallback when Zerion fails or has no key."""

from __future__ import annotations

import base64
import json
import logging
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx

from ..http import quiet_client
from ..types import ChainBalance, NormalizedTransfer
from .alchemy import AlchemyAdapter, _retry_delay

logger = logging.getLogger(__name__)

FIXTURE_PATH = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "chain" / "zerion_transactions.json"
BALANCE_FIXTURE_PATH = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "chain" / "zerion_balances.json"
ZERION_URL = "https://api.zerion.io/v1/wallets/{address}/transactions/"

# Zerion chain ids that DefiLlama prices under a different name. Anything not
# listed here is used as-is when it is a known DefiLlama chain, and skipped
# otherwise — an unknown chain must not be sent as ``mystery:0x...``.
_ZERION_TO_LLAMA = {
    "binance-smart-chain": "bsc",
    "polygon-pos": "polygon",
}
_LLAMA_CHAINS = frozenset(
    {
        "ethereum",
        "arbitrum",
        "base",
        "optimism",
        "polygon",
        "bsc",
        "avalanche",
        "plasma",
        "hyperevm",
        "mantle",
        "blast",
        "berachain",
        "monad",
        "xdai",
        "scroll",
        "linea",
        "zksync",
        "zksync-era",
        "sonic",
        "ink",
        "unichain",
        "soneium",
        "celo",
        "gnosis",
        "opbnb",
        "taiko",
        "mode",
        "fraxtal",
        "manta",
        "metis",
        "aurora",
        "moonbeam",
        "moonriver",
        "cronos",
        "fantom",
        "kava",
        "harmony",
        "fuse",
        "boba",
        "okexchain",
        "heco",
        "polygon-zkevm",
        "sei",
        "katana",
    }
)


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def zerion_basic_header(api_key: str) -> str:
    """Zerion expects ``Authorization: Basic base64(key:)``. A raw key is 401."""
    token = base64.b64encode(f"{api_key}:".encode()).decode()
    return f"Basic {token}"


def _is_trash(node: dict[str, Any]) -> bool:
    flags = node.get("flags") if isinstance(node.get("flags"), dict) else {}
    return bool(flags.get("is_trash"))


def llama_chain(zerion_chain: str | None) -> str | None:
    """Map a Zerion chain id onto a DefiLlama chain name. Unknown chains are skipped."""
    if not zerion_chain:
        return None
    name = zerion_chain.strip().lower()
    mapped = _ZERION_TO_LLAMA.get(name, name)
    if mapped in _LLAMA_CHAINS or name in _LLAMA_CHAINS:
        return mapped
    return None


def mask_address(address: str | None) -> str:
    """First 6 and last 4 characters. Logs must never carry a full wallet address."""
    if not address:
        return ""
    text = address.strip()
    if len(text) <= 10:
        return text
    return f"{text[:6]}…{text[-4:]}"


def _position_chain(row: dict[str, Any]) -> str | None:
    """Chain of this position: JSON:API ``relationships.chain``, else the position id.

    Zerion ids look like ``{address}-{chain}-asset-asset`` for a token and
    ``base-{chain}-asset-asset`` for a native coin. ``chain`` itself may contain
    hyphens (``binance-smart-chain``), so the suffix is stripped rather than split.
    """
    relationships = row.get("relationships") or {}
    chain_rel = relationships.get("chain") or {}
    data = chain_rel.get("data") if isinstance(chain_rel, dict) else None
    if isinstance(data, dict) and data.get("id"):
        return str(data["id"])
    position_id = str(row.get("id") or "")
    if position_id.startswith("base-") and position_id.endswith("-asset-asset"):
        return position_id[len("base-") : -len("-asset-asset")] or None
    if position_id.endswith("-asset-asset"):
        head = position_id[: -len("-asset-asset")]
        # ``0x`` + 40 hex, then ``-``, then the chain id.
        if len(head) > 43 and head[42] == "-":
            return head[43:] or None
    return None


def _implementation_address(fungible: dict[str, Any], zerion_chain: str | None) -> str | None:
    """Contract on ``zerion_chain``. A null address is the native coin."""
    if not zerion_chain:
        return None
    wanted = zerion_chain.strip().lower()
    for impl in fungible.get("implementations") or []:
        if not isinstance(impl, dict):
            continue
        if str(impl.get("chain_id") or "").strip().lower() != wanted:
            continue
        raw = impl.get("address")
        if raw is None or str(raw).strip() == "":
            return None
        return str(raw)
    return None


def _fungible_icon_url(fungible: dict[str, Any]) -> str | None:
    """Zerion logo: ``icon`` is either a URL string or ``{"url": "..."}``."""
    icon = fungible.get("icon")
    raw: str | None = None
    if isinstance(icon, str):
        raw = icon
    elif isinstance(icon, dict):
        url = icon.get("url")
        if isinstance(url, str):
            raw = url
    if not raw:
        return None
    text = raw.strip()
    if not text.startswith(("http://", "https://")):
        return None
    return text


def _zerion_quote(attrs: dict[str, Any]) -> Decimal | None:
    price = attrs.get("price")
    if price is None:
        return None
    try:
        quote = Decimal(str(price))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if quote <= 0:
        return None
    return quote


def parse_zerion_transactions(address: str, payload: dict[str, Any]) -> list[NormalizedTransfer]:
    """Parse a Zerion ``/transactions`` JSON:API document into transfers."""
    owner = address.lower()
    items: list[NormalizedTransfer] = []
    for row in payload.get("data") or []:
        attrs = row.get("attributes") or {}
        mined = attrs.get("mined_at") or attrs.get("sent_at")
        if not mined:
            continue
        try:
            ts = datetime.fromisoformat(str(mined).replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        operation = str(attrs.get("operation_type") or "")
        transfers = attrs.get("transfers") or []
        tx_hash = ((attrs.get("hash")) or row.get("id") or "")
        for index, transfer in enumerate(transfers):
            fungible = transfer.get("fungible_info") or {}
            if _is_trash(transfer) or _is_trash(fungible) or _is_trash(attrs):
                continue
            asset = str(fungible.get("symbol") or "UNKNOWN").upper()
            quantity = transfer.get("quantity") or {}
            amount = abs(_decimal(quantity.get("numeric") or quantity.get("float")))
            if amount == 0:
                continue
            direction_raw = str(transfer.get("direction") or "").lower()
            direction = "in" if direction_raw in {"in", "incoming"} else "out"
            sender = str(transfer.get("sender") or "")
            recipient = str(transfer.get("recipient") or "")
            counterparty = sender if direction == "in" else recipient
            raw_type = operation or direction_raw or "transfer"
            note = " ".join(
                part
                for part in (
                    operation,
                    str((attrs.get("application_metadata") or {}).get("name") or ""),
                    str(attrs.get("status") or ""),
                )
                if part
            )
            items.append(
                NormalizedTransfer(
                    source="zerion",
                    external_id=f"zerion:{tx_hash}:{index}",
                    ts=ts,
                    asset=asset,
                    amount=amount,
                    direction=direction,  # type: ignore[arg-type]
                    address=counterparty or owner,
                    txid=str(tx_hash) or None,
                    raw_type=raw_type,
                    note=note[:500],
                    raw={
                        "address": owner,
                        "operation": operation,
                        "chain": _position_chain(row),
                        "application": (attrs.get("application_metadata") or {}).get("name"),
                    },
                )
            )
    return items


def parse_zerion_positions(address: str, payload: dict[str, Any]) -> list[ChainBalance]:
    """Parse a Zerion positions document: ``{"data": [{"attributes": {...}}]}``.

    Also accepts the compact fixture shape ``{"balances": [{"symbol", "amount"}]}``.
    Dust and zero quantities are dropped. DeFi protocol positions are not modeled.
    """
    owner = address.lower()
    compact = payload.get("balances")
    rows = compact if isinstance(compact, list) else payload.get("data") or []
    items: list[ChainBalance] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        attrs = row.get("attributes") or row
        fungible = attrs.get("fungible_info") or {}
        if _is_trash(attrs) or _is_trash(fungible):
            continue
        asset = str(attrs.get("symbol") or fungible.get("symbol") or "").upper()
        quantity = attrs.get("quantity") if isinstance(attrs.get("quantity"), dict) else {}
        raw_amount = attrs.get("amount", quantity.get("numeric") or quantity.get("float"))
        amount = _decimal(raw_amount)
        if not asset or amount <= 0:
            continue
        zerion_chain = _position_chain(row)
        chain = llama_chain(zerion_chain)
        contract = _implementation_address(fungible, zerion_chain) if chain else None
        items.append(
            ChainBalance(
                source="zerion",
                asset=asset,
                amount=amount,
                address=owner,
                chain=chain,
                contract=contract,
                quote_usdt=_zerion_quote(attrs),
                icon_url=_fungible_icon_url(fungible),
            )
        )
    return items


class ZerionAdapter:
    """Prefer Zerion. A live wallet falls back to Alchemy, never to fixtures.

    ``use_mock`` (or a missing Zerion key with no Alchemy key) reads the recorded
    fixture. That path is only for a user who has no live wallet at all. Once a
    wallet is live, a Zerion failure — including a 429 that survives Retry-After
    and exponential backoff — goes to Alchemy, and if Alchemy also fails the
    error propagates so the caller can skip the snapshot.
    """

    source = "evm"

    def __init__(
        self,
        addresses: list[str] | tuple[str, ...],
        *,
        zerion_api_key: str = "",
        alchemy_api_key: str = "",
        client: httpx.Client | None = None,
        fixture_path: Path | None = None,
        balance_fixture_path: Path | None = None,
        use_mock: bool = False,
        allow_fixture: bool = True,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.addresses = [addr for addr in addresses if addr]
        self._zerion_key = zerion_api_key
        self._alchemy_key = alchemy_api_key
        # A live wallet forbids the fixture path on both adapters. Alchemy is
        # still constructed without a key so a missing key raises instead of
        # silently reading recorded transfers.
        live = not allow_fixture
        # Alchemy gets its own client. Sharing the Zerion client would replay a
        # one-shot mock transport and, on a real socket, mix two hosts' state.
        self._alchemy = (
            AlchemyAdapter(
                addresses,
                api_key=alchemy_api_key,
                allow_fixture=not live,
                sleep=sleep,
            )
            if alchemy_api_key or live
            else None
        )
        self._client = client
        self._fixture = fixture_path or FIXTURE_PATH
        self._balance_fixture = balance_fixture_path or BALANCE_FIXTURE_PATH
        self._allow_fixture = allow_fixture
        self._sleep = sleep or time.sleep
        # Mock is explicit, or the only option when neither key exists and the
        # caller still allows fixtures (demo / --mode mock).
        self._use_mock = use_mock or (not zerion_api_key and not alchemy_api_key and allow_fixture)

    def fetch_transfers(self, since: datetime) -> list[NormalizedTransfer]:
        if self._use_mock:
            parsed = self._from_fixture(since)
            return [item for item in parsed if item.ts >= _aware(since)]
        if not self._zerion_key:
            return self._from_alchemy(since, reason="no zerion key")

        collected: list[NormalizedTransfer] = []
        try:
            client = self._client or quiet_client(timeout=20.0)
            close = self._client is None
            try:
                for address in self.addresses:
                    url: str | None = ZERION_URL.format(address=address)
                    params: dict[str, str] | None = {"currency": "usd", "page[size]": "100"}
                    pages = 0
                    while url and pages < 40:
                        response = self._get(client, url, params)
                        payload = response.json()
                        collected.extend(parse_zerion_transactions(address, payload))
                        url = str((payload.get("links") or {}).get("next") or "") or None
                        # links.next already carries the query string.
                        params = None
                        pages += 1
                        oldest = min((item.ts for item in collected), default=None)
                        if oldest is not None and oldest < _aware(since):
                            break
            finally:
                if close:
                    client.close()
        except Exception as exc:  # noqa: BLE001 - fallback is the contract
            logger.warning("Zerion request failed (%s); falling back to Alchemy", type(exc).__name__)
            return self._from_alchemy(since, reason="zerion failed")
        return [item for item in collected if item.ts >= _aware(since)]

    def fetch_balances(self) -> list[ChainBalance]:
        """Current wallet token balances.

        Mock mode reads the recorded fixture. Live mode calls Zerion
        ``/positions`` and, on failure (including a 429 that survives
        Retry-After plus exponential backoff), Alchemy
        ``eth_getBalance`` + ``alchemy_getTokenBalances``. If both fail the
        error propagates — a real wallet must never be filled from fixtures.
        """
        if self._use_mock:
            return self._balances_from_fixture()
        if not self._zerion_key:
            return self._balances_from_alchemy(reason="no zerion key")
        collected: list[ChainBalance] = []
        try:
            client = self._client or quiet_client(timeout=20.0)
            close = self._client is None
            try:
                for address in self.addresses:
                    response = self._get(
                        client,
                        f"https://api.zerion.io/v1/wallets/{address}/positions/",
                        {"filter[positions]": "only_simple", "currency": "usd", "filter[trash]": "only_non_trash"},
                    )
                    collected.extend(parse_zerion_positions(address, response.json()))
            finally:
                if close:
                    client.close()
        except Exception as exc:  # noqa: BLE001 - Alchemy is the live fallback, not the fixture
            logger.warning("Zerion balances failed (%s); falling back to Alchemy", type(exc).__name__)
            return self._balances_from_alchemy(reason="zerion failed")
        return collected

    def _get(
        self,
        client: httpx.Client,
        url: str,
        params: dict[str, str] | None,
    ) -> httpx.Response:
        """GET with 429 handling: honor Retry-After, else exponential backoff."""
        headers = {"authorization": zerion_basic_header(self._zerion_key)}
        response = client.get(url, headers=headers, params=params)
        # Up to three waits (Retry-After, else 1s, 2s, 4s) and four requests.
        for attempt in range(3):
            if response.status_code != 429:
                break
            self._sleep(_retry_delay(response, attempt))
            response = client.get(url, headers=headers, params=params)
        response.raise_for_status()
        return response

    def _from_alchemy(self, since: datetime, *, reason: str) -> list[NormalizedTransfer]:
        if self._alchemy is not None and self._alchemy.api_key:
            logger.info("EVM transfers via Alchemy (%s)", reason)
            return self._alchemy.fetch_transfers(since)
        if not self._allow_fixture:
            raise RuntimeError(f"zerion and alchemy unavailable ({reason})")
        return [item for item in self._from_fixture(since) if item.ts >= _aware(since)]

    def _balances_from_alchemy(self, *, reason: str) -> list[ChainBalance]:
        if self._alchemy is not None and self._alchemy.api_key:
            logger.info("EVM balances via Alchemy (%s)", reason)
            return self._alchemy.fetch_balances()
        if not self._allow_fixture:
            raise RuntimeError(f"zerion and alchemy unavailable ({reason})")
        return self._balances_from_fixture()

    def _balances_from_fixture(self) -> list[ChainBalance]:
        if not self._allow_fixture or not self._balance_fixture.is_file():
            return []
        with self._balance_fixture.open(encoding="utf-8") as handle:
            payload = json.load(handle)
        address = self.addresses[0] if self.addresses else "0xdemo"
        rows = parse_zerion_positions(address, payload)
        return [replace(row, fixture=True) for row in rows]

    def _from_fixture(self, since: datetime) -> list[NormalizedTransfer]:
        if not self._allow_fixture or not self._fixture.is_file():
            return []
        with self._fixture.open(encoding="utf-8") as handle:
            payload = json.load(handle)
        address = self.addresses[0] if self.addresses else "0xdemo"
        return parse_zerion_transactions(address, payload)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
