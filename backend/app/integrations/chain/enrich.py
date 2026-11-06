"""Blockscout context for Zerion chain transfers.

A one-sided outflow is not necessarily a withdrawal: a DeFi deposit sends an
asset to a contract and receives a receipt / share / LP token in the same
transaction. This module looks the counterparty and the transaction's token
legs up on the chain's public Blockscout API, then returns a structured
``Enrichment`` that the attribution rules and the pending API can use.

Design constraints:

* Best effort. Every HTTP failure degrades to a partial result, never raises.
* Eight-second timeout per request, process-local TTL cache.
* The caller decides when to run it; a failure cannot abort a sync.
* No secret is logged. Blockscout is public and needs no key.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Iterable, Literal

import httpx

from ...attribution import Attribution, normalize_txid
from ..http import quiet_client
from ..types import NormalizedTransfer

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ...models import PendingConfirmation

logger = logging.getLogger(__name__)

# Verified public Blockscout instances (checked against /api/v2/stats). Some
# hosts redirect to the chain's own explorer; httpx follows the redirect. An
# unknown chain is skipped instead of querying the wrong chain.
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

_CHAIN_ALIASES = {
    "eth": "ethereum",
    "mainnet": "ethereum",
    "ethereum-mainnet": "ethereum",
    "polygon-pos": "polygon",
    "zksync-era": "zksync",
    "arbitrum-one": "arbitrum",
    "optimism-mainnet": "optimism",
}

HTTP_TIMEOUT_SECONDS = 8.0
CACHE_TTL_SECONDS = 3600.0
CACHE_MAX_ENTRIES = 512
_MAX_TOKEN_PAGES = 3

_NATIVE_OR_BASE = {
    "ETH",
    "WETH",
    "MATIC",
    "POL",
    "BNB",
    "AVAX",
    "SOL",
    "USDC",
    "USDT",
    "DAI",
    "USD3",
}

_RECEIPT_HINTS = (
    "atoken",
    "aave",
    "wrapped",
    "stata",
    "share",
    "vault",
    "receipt",
    "liquidity",
    "gauge",
    "staked",
    "steth",
    "wsteth",
    "reth",
    "sfrx",
    "lp token",
    "proof of deposit",
)

_EXCHANGE_HINTS = (
    "binance",
    "coinbase",
    "okx",
    "kraken",
    "bybit",
    "bitget",
    "gate",
    "kucoin",
    "huobi",
    "htx",
    "bitfinex",
    "gemini",
    "upbit",
    "bithumb",
    "crypto.com",
)

_DEPOSIT_ACTIONS = ("deposit", "supply", "stake", "mint", "addliquidity", "add_liquidity")
_REDEEM_ACTIONS = ("redeem", "withdraw", "unstake", "remove", "burn", "exit")
_SWAP_ACTIONS = ("swap", "exchange", "trade")

# Names that describe a generic contract role, not the protocol a user meant
# to interact with. They are kept out of the position sentence so a card never
# says "向 Helper 存入": the action line falls back to "向合约存入".
_GENERIC_PROTOCOL_NAMES = {
    "helper",
    "contract",
    "router",
    "proxy",
    "transparentupgradeableproxy",
    "wrapped",
    "token",
    "vault",
    "pool",
    "lp token",
    "unknown",
}

_TX_HASH_RE = re.compile(r"0x[0-9a-fA-F]{64}")
_DETAIL_TX_RE = re.compile(r"txid\s+(0x[0-9a-fA-F]{64})")
_DETAIL_ADDR_RE = re.compile(r"地址\s+(0x[0-9a-fA-F]{40})")
_DETAIL_TYPE_RE = re.compile(r"原始类型\s+([^\s；;]+)")


@dataclass(frozen=True)
class TokenAmount:
    """One ERC-20 leg of the transaction, from the user's point of view."""

    symbol: str
    amount: Decimal
    direction: Literal["in", "out"]
    name: str | None = None
    contract: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "amount": _decimal_text(self.amount),
            "direction": self.direction,
            "contract": self.contract,
        }


@dataclass(frozen=True)
class Enrichment:
    """Counterparty and same-transaction token context. Safe to serialise to JSON."""

    chain: str | None = None
    tx_hash: str | None = None
    user_address: str | None = None
    counterparty: str | None = None
    is_contract: bool | None = None
    name: str | None = None
    ens: str | None = None
    public_tags: tuple[str, ...] = ()
    method: str | None = None
    operation: str | None = None
    application: str | None = None
    sent: tuple[TokenAmount, ...] = ()
    received: tuple[TokenAmount, ...] = ()
    error: str | None = None
    fetched_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def is_exchange(self) -> bool:
        blob = " ".join(
            part
            for part in (self.name, self.ens, *self.public_tags)
            if part
        ).casefold()
        return any(hint in blob for hint in _EXCHANGE_HINTS)

    @property
    def display_name(self) -> str | None:
        if self.ens:
            return self.ens
        if self.name:
            return self.name
        if self.public_tags:
            return self.public_tags[0]
        return None

    @property
    def counterparty_label(self) -> str:
        """Chinese one-line label. Unknown addresses stay explicit."""
        name = self.display_name
        if self.is_exchange:
            return f"交易所 · {name}" if name else "交易所 · 未标记交易所"
        if self.is_contract is True:
            base = "合约"
        elif self.is_contract is False:
            base = "地址" if name else "未标记地址 · 普通钱包"
        else:
            # The lookup failed before it could tell us what the address is.
            return f"未标记地址 · {name}" if name else "未标记地址 · 普通钱包/合约"

        label = f"{base} · {name}" if name else f"{base} · 未标记合约"
        context = self._context()
        return f"{label}（{context}）" if context else label

    @property
    def protocol_name(self) -> str | None:
        """Best-effort protocol brand, e.g. "3Jane" from "3Jane USD3".

        Zerion's application field wins. Otherwise the received token name is
        read with its symbol suffix removed, then the sent token. Generic
        contract roles ("Helper", "Router") are not protocol names.
        """
        application = (self.application or "").strip()
        if (
            application
            and len(application) <= 24
            and application.casefold() not in _GENERIC_PROTOCOL_NAMES
        ):
            return application
        for token in (*self.received, *self.sent):
            name = (token.name or "").strip()
            symbol = (token.symbol or "").strip()
            if not name or not symbol:
                continue
            stripped = re.sub(
                rf"\s*{re.escape(symbol)}\s*$", "", name, flags=re.IGNORECASE
            ).strip(" ·-—:：")
            if (
                stripped
                and len(stripped) <= 24
                and stripped.casefold() not in _GENERIC_PROTOCOL_NAMES
            ):
                return stripped
        return None

    @property
    def explain(self) -> str:
        """One human sentence for the pending API and WeChat text.

        Phrasing follows the money: what left the wallet, what came back. The
        counterparty chip already carries the label, so the sentence does not
        repeat "合约 · Helper" here.
        """
        action = _action(self.method, self.operation)
        protocol = self.protocol_name
        sent = _join_tokens(self.sent)
        received = _join_tokens(self.received)

        if self.sent and self.received:
            if action == "兑换":
                return f"将 {sent} 兑换为 {received}"
            if action == "赎回":
                lead = f"从 {protocol}" if protocol else "从合约"
                gap = " " if protocol else ""
                return f"{lead}{gap}赎回 {sent}，同时收到 {received}"
            lead = f"向 {protocol}" if protocol else "向合约"
            gap = " " if protocol else ""
            verb = action or "存入"
            tail = " 凭证" if verb == "存入" else ""
            return f"{lead}{gap}{verb} {sent}，同时收到 {received}{tail}"
        if self.received:
            return f"收到 {received}"
        if self.sent:
            return f"转出 {sent}"
        return self.counterparty_label

    def _context(self) -> str | None:
        action = _action(self.method, self.operation)
        token = self.received[0] if self.received else (self.sent[0] if self.sent else None)
        if token is None:
            return action
        subject = token.name or token.symbol
        if action:
            return f"{subject} {action}助手"
        return subject

    def to_meta(self) -> dict[str, Any]:
        return {
            "version": 1,
            "source": "blockscout",
            "chain": self.chain,
            "tx_hash": self.tx_hash,
            "user_address": self.user_address,
            "counterparty": self.counterparty,
            "counterparty_label": self.counterparty_label,
            "explain": self.explain,
            "is_contract": self.is_contract,
            "is_exchange": self.is_exchange,
            "name": self.name,
            "ens": self.ens,
            "public_tags": list(self.public_tags),
            "method": self.method,
            "operation": self.operation,
            "application": self.application,
            "received_tokens": [token.as_dict() for token in self.received],
            "sent_tokens": [token.as_dict() for token in self.sent],
            "error": self.error,
            "fetched_at": self.fetched_at,
        }


class _TTLCache:
    def __init__(self, ttl: float = CACHE_TTL_SECONDS, max_entries: int = CACHE_MAX_ENTRIES) -> None:
        self._ttl = ttl
        self._max = max_entries
        self._items: dict[tuple[str, str, str], tuple[float, Enrichment]] = {}
        self._lock = threading.Lock()

    def get(self, key: tuple[str, str, str]) -> Enrichment | None:
        now = time.monotonic()
        with self._lock:
            row = self._items.get(key)
            if row is None:
                return None
            expires, value = row
            if expires <= now:
                self._items.pop(key, None)
                return None
            return value

    def put(self, key: tuple[str, str, str], value: Enrichment) -> None:
        with self._lock:
            if len(self._items) >= self._max:
                # A tiny cache: drop the entry closest to expiry.
                oldest = min(self._items, key=lambda item: self._items[item][0])
                self._items.pop(oldest, None)
            self._items[key] = (time.monotonic() + self._ttl, value)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


_CACHE = _TTLCache()
_DEFAULT_CLIENT: httpx.Client | None = None
_DEFAULT_CLIENT_LOCK = threading.Lock()


def clear_cache() -> None:
    """Drop the process-local cache. Tests use this; production does not need it."""
    _CACHE.clear()


def _default_client() -> httpx.Client:
    global _DEFAULT_CLIENT
    with _DEFAULT_CLIENT_LOCK:
        if _DEFAULT_CLIENT is None:
            _DEFAULT_CLIENT = quiet_client(
                timeout=HTTP_TIMEOUT_SECONDS,
                follow_redirects=True,
                headers={"User-Agent": "asset-tracker/1.0 (+blockscout-enrich)"},
            )
        return _DEFAULT_CLIENT


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _format_amount(value: Decimal) -> str:
    """Human amount: 2307.381804 -> 2,307.38; tiny units keep six decimals."""
    if abs(value) >= 1:
        return f"{value:,.2f}"
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text or "0"


def _join_tokens(tokens: Iterable[TokenAmount]) -> str:
    return "、".join(f"{_format_amount(token.amount)} {token.symbol}" for token in tokens)


def _action(method: str | None, operation: str | None) -> str | None:
    hints = f"{method or ''} {operation or ''}".casefold()
    if any(word in hints for word in _REDEEM_ACTIONS):
        return "赎回"
    if any(word in hints for word in _SWAP_ACTIONS):
        return "兑换"
    if any(word in hints for word in _DEPOSIT_ACTIONS):
        return "存入"
    return None


def _receipt_like(token: TokenAmount) -> bool:
    blob = f"{token.symbol} {token.name or ''}".casefold()
    if any(hint in blob for hint in _RECEIPT_HINTS):
        return True
    symbol = token.symbol.casefold()
    return symbol.endswith("-lp") or symbol.endswith("lp") and len(symbol) > 2


def _chain_key(chain: str | None) -> str | None:
    if not chain:
        return None
    name = chain.strip().casefold()
    name = _CHAIN_ALIASES.get(name, name)
    return name if name in BLOCKSCOUT_HOSTS else None


def _address_hash(node: Any) -> str | None:
    if not isinstance(node, dict):
        return None
    value = node.get("hash")
    return str(value) if value else None


def _tag_names(node: Any) -> tuple[str, ...]:
    if not isinstance(node, dict):
        return ()
    names: list[str] = []
    for tag in (node.get("metadata") or {}).get("tags") or []:
        if isinstance(tag, dict) and tag.get("name"):
            names.append(str(tag["name"]))
    for tag in node.get("public_tags") or []:
        if isinstance(tag, dict) and tag.get("display_name"):
            names.append(str(tag["display_name"]))
        elif isinstance(tag, str):
            names.append(tag)
    return tuple(dict.fromkeys(names))


def _token_from_transfer(item: dict[str, Any]) -> TokenAmount | None:
    token = item.get("token") or {}
    total = item.get("total") or {}
    symbol = str(token.get("symbol") or "").strip().upper()
    if not symbol:
        return None
    raw_value = total.get("value", item.get("value"))
    decimals = total.get("decimals", token.get("decimals", 0))
    try:
        decimals_int = int(decimals or 0)
    except (TypeError, ValueError):
        decimals_int = 0
    amount = _decimal(raw_value)
    if decimals_int > 0:
        amount = amount / (Decimal(10) ** decimals_int)
    if amount <= 0:
        return None
    return TokenAmount(
        symbol=symbol,
        name=str(token.get("name") or "") or None,
        amount=amount,
        direction="in",
        contract=str(token.get("address_hash") or "") or None,
    )


class BlockscoutClient:
    """Minimal read-only Blockscout v2 client with per-call degradation."""

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        timeout: float = HTTP_TIMEOUT_SECONDS,
    ) -> None:
        self._client = client
        self._timeout = timeout

    def _get(
        self,
        chain: str,
        path: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any] | None:
        host = BLOCKSCOUT_HOSTS.get(chain)
        if host is None:
            return None
        client = self._client or _default_client()
        try:
            response = client.get(
                f"{host}{path}", params=params, timeout=timeout or self._timeout
            )
            if response.status_code >= 400:
                logger.info("blockscout %s returned %s", chain, response.status_code)
                return None
            payload = response.json()
            return payload if isinstance(payload, dict) else None
        except Exception as exc:  # noqa: BLE001 - degradation is the contract
            # Do not log the URL: it carries a wallet address and tx hash.
            logger.info("blockscout %s request failed (%s)", chain, type(exc).__name__)
            return None

    def fetch(
        self,
        *,
        chain: str,
        tx_hash: str,
        user_address: str,
        counterparty: str | None = None,
        operation: str | None = None,
        application: str | None = None,
    ) -> Enrichment:
        canonical = _chain_key(chain)
        if canonical is None:
            return Enrichment(
                chain=chain,
                tx_hash=tx_hash,
                user_address=user_address,
                counterparty=counterparty,
                operation=operation,
                application=application,
                error=f"unsupported chain {chain!r}",
            )

        user = user_address.casefold()
        tx_hash = tx_hash.casefold()
        errors: list[str] = []
        deadline = time.monotonic() + self._timeout

        def remaining() -> float:
            # The whole lookup, not each request, is bounded by the timeout.
            return max(0.05, deadline - time.monotonic())

        tx_doc = self._get(
            canonical, f"/api/v2/transactions/{tx_hash}", timeout=remaining()
        )
        if tx_doc is None:
            errors.append("transaction unavailable")

        if not counterparty and tx_doc:
            counterparty = _address_hash(tx_doc.get("to"))
        counterparty = counterparty.casefold() if counterparty else None

        address_doc = (
            self._get(
                canonical,
                f"/api/v2/addresses/{counterparty}",
                timeout=remaining(),
            )
            if counterparty
            else None
        )
        if counterparty and address_doc is None:
            errors.append("counterparty unavailable")

        sent: list[TokenAmount] = []
        received: list[TokenAmount] = []
        params: dict[str, Any] | None = None
        for _ in range(_MAX_TOKEN_PAGES):
            page = self._get(
                canonical,
                f"/api/v2/transactions/{tx_hash}/token-transfers",
                params=params,
                timeout=remaining(),
            )
            if page is None:
                errors.append("token transfers unavailable")
                break
            for item in page.get("items") or []:
                if not isinstance(item, dict):
                    continue
                token = _token_from_transfer(item)
                if token is None:
                    continue
                sender = (_address_hash(item.get("from")) or "").casefold()
                recipient = (_address_hash(item.get("to")) or "").casefold()
                if recipient == user and sender != user:
                    received.append(TokenAmount(**{**token.__dict__, "direction": "in"}))
                elif sender == user and recipient != user:
                    sent.append(TokenAmount(**{**token.__dict__, "direction": "out"}))
            params = page.get("next_page_params")
            if not params:
                break

        name = None
        ens = None
        public_tags: tuple[str, ...] = ()
        is_contract: bool | None = None
        if address_doc:
            name = str(address_doc.get("name") or "") or None
            ens = str(address_doc.get("ens_domain_name") or "") or None
            public_tags = _tag_names(address_doc)
            flag = address_doc.get("is_contract")
            is_contract = flag if isinstance(flag, bool) else None

        method = None
        if tx_doc:
            method = str(tx_doc.get("method") or "") or None
            decoded = tx_doc.get("decoded_input")
            if method is None and isinstance(decoded, dict):
                call = str(decoded.get("method_call") or "")
                method = call.split("(", 1)[0] or None
            if not name:
                name = str((tx_doc.get("to") or {}).get("name") or "") or None
            if not public_tags:
                public_tags = _tag_names(tx_doc.get("to"))
            if is_contract is None:
                flag = (tx_doc.get("to") or {}).get("is_contract")
                is_contract = flag if isinstance(flag, bool) else None

        return Enrichment(
            chain=canonical,
            tx_hash=tx_hash,
            user_address=user,
            counterparty=counterparty,
            is_contract=is_contract,
            name=name,
            ens=ens,
            public_tags=public_tags,
            method=method,
            operation=operation,
            application=application,
            sent=tuple(sent),
            received=tuple(received),
            error="；".join(errors) or None,
        )


def build_enrichment(
    *,
    chain: str | None,
    tx_hash: str | None,
    user_address: str | None,
    counterparty: str | None = None,
    operation: str | None = None,
    application: str | None = None,
    client: BlockscoutClient | None = None,
) -> Enrichment | None:
    """Cached lookup. Returns None when there is not enough data to look up.

    Errors are returned as a partial ``Enrichment``; they are not cached, so a
    later sync can retry. Successful results are cached per process for an hour.
    """
    canonical = _chain_key(chain)
    tx = normalize_txid(tx_hash)
    if canonical is None or not tx or not user_address:
        return None
    key = (canonical, tx, user_address.casefold())
    cached = _CACHE.get(key)
    if cached is not None:
        return cached

    enrichment = (client or BlockscoutClient()).fetch(
        chain=canonical,
        tx_hash=tx,
        user_address=user_address,
        counterparty=counterparty,
        operation=operation,
        application=application,
    )
    if enrichment.error is None:
        _CACHE.put(key, enrichment)
    return enrichment


def enrich_transfer(
    transfer: NormalizedTransfer,
    *,
    own_addresses: set[str] | None = None,
    chain: str | None = None,
    client: BlockscoutClient | None = None,
) -> Enrichment | None:
    """Look up one Zerion transfer. Never raises; unknown data returns None."""
    raw = transfer.raw or {}
    tx_hash = transfer.txid or raw.get("hash")
    user_address = raw.get("address")
    if not user_address and own_addresses and len(own_addresses) == 1:
        user_address = next(iter(own_addresses))
    return build_enrichment(
        chain=chain or raw.get("chain"),
        tx_hash=tx_hash,
        user_address=user_address,
        counterparty=transfer.address,
        operation=str(raw.get("operation") or transfer.raw_type or "") or None,
        application=str(raw.get("application") or "") or None,
        client=client,
    )


def _pending_meta(pending: Any) -> dict[str, Any]:
    meta = getattr(pending, "meta", None)
    if isinstance(meta, dict):
        return meta
    if isinstance(meta, str) and meta.strip():
        try:
            parsed = json.loads(meta)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


def _pending_tx_hash(pending: Any) -> str | None:
    match = _TX_HASH_RE.search(str(getattr(pending, "external_id", "") or ""))
    if match:
        return match.group(0)
    detail = str(getattr(pending, "detail", "") or "")
    match = _DETAIL_TX_RE.search(detail) or _TX_HASH_RE.search(detail)
    return match.group(0) if match else None


def _pending_counterparty(pending: Any) -> str | None:
    meta = _pending_meta(pending)
    value = meta.get("counterparty")
    if value:
        return str(value)
    match = _DETAIL_ADDR_RE.search(str(getattr(pending, "detail", "") or ""))
    return match.group(1) if match else None


def enrich_pending(
    pending: "PendingConfirmation",
    *,
    chain: str | None = None,
    own_address: str | None = None,
    client: BlockscoutClient | None = None,
) -> Enrichment | None:
    """Read-only enrichment for an existing pending row (dry-run helper)."""
    meta = _pending_meta(pending)
    detail = str(getattr(pending, "detail", "") or "")
    type_match = _DETAIL_TYPE_RE.search(detail)
    return build_enrichment(
        chain=chain or meta.get("chain") or "ethereum",
        tx_hash=_pending_tx_hash(pending),
        user_address=own_address or meta.get("user_address"),
        counterparty=_pending_counterparty(pending),
        operation=str(meta.get("operation") or (type_match.group(1) if type_match else "") or "") or None,
        application=str(meta.get("application") or "") or None,
        client=client,
    )


def refine_attribution(
    base: Attribution,
    transfer: NormalizedTransfer,
    enrichment: Enrichment,
) -> Attribution:
    """Replace a one-sided ``withdraw`` guess with a DeFi / swap category.

    Only the outflow leg is refined. A same-transaction inflow that is not the
    asset itself (receipt, LP, share, another token) means this was an internal
    transformation, not money leaving the portfolio.
    """
    if transfer.direction != "out" or not enrichment.received:
        return base

    received = enrichment.received
    hints = f"{enrichment.method or ''} {enrichment.operation or ''}".casefold()
    received_text = _join_tokens(received)
    sent_receipt = any(_receipt_like(token) for token in enrichment.sent)
    received_base = any(token.symbol.upper() in _NATIVE_OR_BASE for token in received)

    if any(word in hints for word in _REDEEM_ACTIONS) or (sent_receipt and received_base):
        return Attribution(
            "defi_redeem",
            0.9,
            f"同 tx 收到 {received_text}，转出份额 → 协议赎回（非提现）",
        )
    if any(word in hints for word in _SWAP_ACTIONS):
        return Attribution(
            "swap",
            0.86,
            f"同 tx 收到 {received_text}，链上兑换 → swap（非提现）",
        )
    if any(word in hints for word in _DEPOSIT_ACTIONS) or any(
        _receipt_like(token) for token in received
    ):
        return Attribution(
            "defi_deposit",
            0.9,
            f"同 tx 收到 {received_text}，合约存入 → 协议存入（非提现）",
        )
    if enrichment.is_contract:
        return Attribution(
            "defi_deposit",
            0.82,
            f"同 tx 收到 {received_text}，对方是合约 → 协议存入/兑换（非提现）",
        )
    return Attribution(
        "swap",
        0.75,
        f"同 tx 收到 {received_text}，并非单边流出 → swap（非提现）",
    )


def describe_pending(pending: "PendingConfirmation") -> str | None:
    """One-line Chinese description shared with the nature classifier.

    Kept as a compatibility entry point for callers that only have a pending
    row; the actual wording lives in :func:`app.nature.classify_nature` so the
    API, WeChat and enrichment layers cannot drift into separate explanations.
    """
    from ...nature import classify_nature

    return classify_nature(pending).explain
