"""Alchemy ``alchemy_getAssetTransfers`` fallback and token-balance reader."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx

from ..http import quiet_client
from ..types import ChainBalance, NormalizedTransfer

logger = logging.getLogger(__name__)

# A symbol this long, or one full of punctuation, is a spam airdrop, not a holding.
_SPAM_SYMBOL = 24

FIXTURE_PATH = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "chain" / "alchemy_transfers.json"


def _decimal(value: Any) -> Decimal:
    try:
        return abs(Decimal(str(value)))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _parse_metadata_ts(metadata: dict[str, Any] | None, fallback_ms: Any) -> datetime | None:
    block = (metadata or {}).get("blockTimestamp")
    if isinstance(block, str) and block:
        try:
            ts = datetime.fromisoformat(block.replace("Z", "+00:00"))
            return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    if fallback_ms:
        try:
            return datetime.fromtimestamp(int(fallback_ms) / 1000, tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            return None
    return None


def parse_alchemy_transfers(address: str, payload: dict[str, Any]) -> list[NormalizedTransfer]:
    """Parse an Alchemy JSON-RPC result (``result.transfers``)."""
    owner = address.lower()
    result = payload.get("result") or payload
    rows = result.get("transfers") if isinstance(result, dict) else None
    items: list[NormalizedTransfer] = []
    for index, row in enumerate(rows or []):
        ts = _parse_metadata_ts(row.get("metadata"), row.get("timestamp"))
        if ts is None:
            continue
        asset = str(row.get("asset") or "UNKNOWN").upper()
        amount = _decimal(row.get("value"))
        # Spam airdrops use the token name as `asset` and run far past a symbol.
        if amount == 0 or is_spam_symbol(asset):
            continue
        to_addr = str(row.get("to") or "").lower()
        from_addr = str(row.get("from") or "").lower()
        direction = "in" if to_addr == owner else "out"
        category = str(row.get("category") or "external")
        tx_hash = str(row.get("hash") or "")
        unique = str(row.get("uniqueId") or "")
        # uniqueId is hash + log index. Without it, fall back to hash:position so
        # older fixtures and a tx with several legs still dedupe.
        external = unique or f"{tx_hash}:{index}"
        items.append(
            NormalizedTransfer(
                source="alchemy",
                external_id=f"alchemy:{external}",
                ts=ts,
                asset=asset,
                amount=amount,
                direction=direction,  # type: ignore[arg-type]
                address=from_addr if direction == "in" else to_addr,
                txid=tx_hash,
                raw_type=category,
                note=category,
                raw={"address": owner},
            )
        )
    return items


def _hex_nonzero(raw: str) -> bool:
    try:
        return int(raw, 16) != 0
    except (TypeError, ValueError):
        return False


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    """Honor ``Retry-After`` when it is a number of seconds; otherwise 1, 2, 4, 8."""
    header = response.headers.get("Retry-After") or response.headers.get("retry-after")
    if header:
        try:
            parsed = float(header)
        except ValueError:
            parsed = 0.0
        if parsed > 0:
            return min(parsed, 60.0)
    return float(min(8, 2**attempt))


def _hex_amount(raw: str, decimals: int) -> Decimal:
    try:
        units = int(raw, 16)
    except (TypeError, ValueError):
        return Decimal("0")
    scale = Decimal(10) ** int(decimals)
    return Decimal(units) / scale


def is_spam_symbol(symbol: str) -> bool:
    text = symbol.strip()
    if not text or len(text) > _SPAM_SYMBOL:
        return True
    if any(ord(ch) < 32 or ch in "$.\u0000" for ch in text):
        return True
    # URL-like or claim-your-reward names are airdrop spam.
    lowered = text.lower()
    return "http" in lowered or "visit" in lowered or "claim" in lowered


def parse_alchemy_balances(
    address: str,
    balances_payload: dict[str, Any],
    metadata: dict[str, dict[str, Any]],
) -> list[ChainBalance]:
    """Convert ``alchemy_getTokenBalances`` hex rows using ``alchemy_getTokenMetadata``.

    ``metadata`` is keyed by lower-case contract address. Zero balances and
    spam symbols are dropped. Native ETH is not in this payload.
    """
    owner = address.lower()
    result = balances_payload.get("result") or balances_payload
    rows = result.get("tokenBalances") if isinstance(result, dict) else None
    items: list[ChainBalance] = []
    for row in rows or []:
        contract = str(row.get("contractAddress") or "").lower()
        raw = str(row.get("tokenBalance") or "0x0")
        meta = metadata.get(contract) or {}
        symbol = str(meta.get("symbol") or "").upper()
        decimals = meta.get("decimals")
        if decimals is None or is_spam_symbol(symbol):
            continue
        amount = _hex_amount(raw, int(decimals))
        if amount <= 0:
            continue
        items.append(
            ChainBalance(
                source="alchemy",
                asset=symbol,
                amount=amount,
                address=owner,
                chain="ethereum",
                contract=contract or None,
            )
        )
    return items


class AlchemyAdapter:
    source = "alchemy"

    def __init__(
        self,
        addresses: list[str] | tuple[str, ...],
        *,
        api_key: str = "",
        client: httpx.Client | None = None,
        fixture_path: Path | None = None,
        allow_fixture: bool = True,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.addresses = [addr for addr in addresses if addr]
        self.api_key = api_key
        self._client = client
        self._fixture = fixture_path or FIXTURE_PATH
        # A real wallet must never read recorded fixtures. Callers that already
        # have a live key pass False so a failed request raises instead.
        self._allow_fixture = allow_fixture
        self._sleep = sleep or time.sleep

    def fetch_transfers(self, since: datetime) -> list[NormalizedTransfer]:
        if not self.api_key:
            if not self._allow_fixture:
                raise RuntimeError("alchemy key missing")
            return self._from_fixture(since)
        client = self._client or quiet_client(timeout=30.0)
        close = self._client is None
        collected: list[NormalizedTransfer] = []
        since_utc = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
        try:
            for address in self.addresses:
                for direction_key in ("toAddress", "fromAddress"):
                    page_key = None
                    for _page in range(20):
                        params: dict[str, Any] = {
                            "fromBlock": "0x0",
                            direction_key: address,
                            "category": ["external", "erc20"],
                            "withMetadata": True,
                            "maxCount": "0x64",
                            "order": "desc",
                        }
                        if page_key:
                            params["pageKey"] = page_key
                        payload = self._rpc(client, "alchemy_getAssetTransfers", [params])
                        parsed = parse_alchemy_transfers(address, payload)
                        collected.extend(parsed)
                        page_key = ((payload.get("result") or {}).get("pageKey"))
                        if not page_key:
                            break
                        if parsed and min(item.ts for item in parsed) < since_utc:
                            break
        finally:
            if close:
                client.close()
        return [item for item in collected if item.ts >= since_utc]

    def fetch_balances(self) -> list[ChainBalance]:
        """Native ETH plus ERC-20 balances. Spam symbols and zero balances are dropped.

        Raises when the key is missing or a live call fails and fixtures are
        forbidden. A mock caller (``allow_fixture``) gets an empty list: this
        adapter has no balance fixture of its own.
        """
        if not self.api_key:
            if not self._allow_fixture:
                raise RuntimeError("alchemy key missing")
            return []
        client = self._client or quiet_client(timeout=30.0)
        close = self._client is None
        collected: list[ChainBalance] = []
        try:
            for address in self.addresses:
                eth_payload = self._rpc(client, "eth_getBalance", [address, "latest"])
                eth = _hex_amount(str(eth_payload.get("result") or "0x0"), 18)
                if eth > 0:
                    collected.append(
                        ChainBalance(
                            source="alchemy",
                            asset="ETH",
                            amount=eth,
                            address=address.lower(),
                            chain="ethereum",
                            contract=None,
                        )
                    )
                rows: list[dict[str, Any]] = []
                page_key = None
                for _page in range(8):
                    params: list[Any] = [address, "erc20"]
                    if page_key:
                        params.append({"pageKey": page_key})
                    payload = self._rpc(client, "alchemy_getTokenBalances", params)
                    result = payload.get("result") or {}
                    rows.extend(result.get("tokenBalances") or [])
                    page_key = result.get("pageKey")
                    if not page_key:
                        break
                nonzero = [row for row in rows if _hex_nonzero(str(row.get("tokenBalance") or "0x0"))]
                metadata: dict[str, dict[str, Any]] = {}
                for row in nonzero:
                    contract = str(row.get("contractAddress") or "")
                    if not contract:
                        continue
                    meta = self._rpc(client, "alchemy_getTokenMetadata", [contract])
                    metadata[contract.lower()] = meta.get("result") or {}
                collected.extend(
                    parse_alchemy_balances(
                        address,
                        {"result": {"tokenBalances": nonzero}},
                        metadata,
                    )
                )
        finally:
            if close:
                client.close()
        return collected

    def _rpc(self, client: httpx.Client, method: str, params: list[Any]) -> dict[str, Any]:
        """POST one JSON-RPC call. 429 waits for Retry-After, then backs off."""
        last: httpx.Response | None = None
        for attempt in range(4):
            response = client.post(
                f"https://eth-mainnet.g.alchemy.com/v2/{self.api_key}",
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            )
            last = response
            if response.status_code != 429:
                response.raise_for_status()
                return response.json()
            if attempt == 3:
                break
            self._sleep(_retry_delay(response, attempt))
        assert last is not None
        last.raise_for_status()
        return last.json()

    def _from_fixture(self, since: datetime) -> list[NormalizedTransfer]:
        if not self._fixture.is_file():
            return []
        with self._fixture.open(encoding="utf-8") as handle:
            payload = json.load(handle)
        address = self.addresses[0] if self.addresses else "0xdemo"
        since_utc = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
        return [item for item in parse_alchemy_transfers(address, payload) if item.ts >= since_utc]
