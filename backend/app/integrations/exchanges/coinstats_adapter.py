"""CoinStats Open API proxy for a Binance read-only key.

CoinStats holds the Binance session. We never talk to Binance directly and
never log the CoinStats key, the Binance secret, or a URL query string.

Verified against https://coinstats.app/api-docs/openapi/get-coin-by-id.md
(OpenAPI ``GET /v1/coins/{coinId}``): the 200 body is
``CoinListResponseItemDto`` and ``symbol`` is a required string (example
``BTC``).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable
from urllib.parse import urlencode

import httpx

from ...config import settings
from ..types import NormalizedBalance, NormalizedTrade, NormalizedTransfer

logger = logging.getLogger("app.exchanges")

_COIN_ID_SYMBOLS: dict[str, str] = {
    "bitcoin": "BTC",
    "ethereum": "ETH",
    "tether": "USDT",
    "usd-coin": "USDC",
    "binancecoin": "BNB",
    "binance-coin": "BNB",
    "solana": "SOL",
    "ripple": "XRP",
    "dogecoin": "DOGE",
    "cardano": "ADA",
    "tron": "TRX",
    "polkadot": "DOT",
    "polygon": "MATIC",
    "matic-network": "MATIC",
    "avalanche-2": "AVAX",
    "chainlink": "LINK",
    "litecoin": "LTC",
    "shiba-inu": "SHIB",
    "uniswap": "UNI",
    "stellar": "XLM",
    "cosmos": "ATOM",
    "monero": "XMR",
    "ethereum-classic": "ETC",
    "bitcoin-cash": "BCH",
    "filecoin": "FIL",
    "internet-computer": "ICP",
    "near": "NEAR",
    "aptos": "APT",
    "arbitrum": "ARB",
    "optimism": "OP",
    "sui": "SUI",
    "pepe": "PEPE",
    "dai": "DAI",
    "true-usd": "TUSD",
    "first-digital-usd": "FDUSD",
    "binance-usd": "BUSD",
    "wrapped-bitcoin": "WBTC",
    "the-open-network": "TON",
    "toncoin": "TON",
}

# CoinStats labels Binance Earn's automatic roll as "Roll In" / "Roll Out".
# The direction is still a real in/out leg; attribution later reclassifies the
# category as an internal transfer instead of a deposit/withdrawal.
_ROLL_IN_TYPES = frozenset({"roll in", "roll-in", "rollin"})
_ROLL_OUT_TYPES = frozenset({"roll out", "roll-out", "rollout"})
_IN_TYPES = frozenset({"deposit", "received", "receive", "in"}) | _ROLL_IN_TYPES
_OUT_TYPES = frozenset({"withdraw", "withdrawal", "sent", "send", "out"}) | _ROLL_OUT_TYPES
_TRADE_TYPES = frozenset({"swap", "buy", "sell", "trade"})

_MIN_INTERVAL = 0.55
_BACKOFF = (0.5, 1.0, 2.0, 4.0)
_MAX_429 = 4
_SYMBOL_LOOKUP_CAP = 30
_STATUS_POLLS = 12
_STATUS_INTERVAL = 1.0

# last_usage is keyed by the first 12 hex chars of sha256(api key). The key
# itself is never stored.
_last_usage: dict[str, dict[str, Any]] = {}
_usage_lock = threading.Lock()

# One lock per CoinStats key so two clients sharing a key still honour 2 req/s.
_key_locks: dict[str, threading.Lock] = {}
_key_next_at: dict[str, float] = {}
_key_state_lock = threading.Lock()

# Balance + transactions cache. Key is sha256(coinstats key + binance apiKey).
# Errors are never written here.
_response_cache: dict[str, dict[str, Any]] = {}
_cache_lock = threading.Lock()

# Process-wide coinId → symbol. A miss is also cached so a bad id is not
# looked up again inside the same process.
_symbol_cache: dict[str, str | None] = {}
_symbol_lock = threading.Lock()

_IP_PATTERNS = (
    re.compile(r"request ip:\s*([0-9]{1,3}(?:\.[0-9]{1,3}){3})", re.IGNORECASE),
    re.compile(r"IP\[([0-9]{1,3}(?:\.[0-9]{1,3}){3})\]", re.IGNORECASE),
    re.compile(r"\b([0-9]{1,3}(?:\.[0-9]{1,3}){3})\b"),
)


def _key_fingerprint(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12]


def _cache_key(coinstats_key: str, binance_api_key: str) -> str:
    blob = f"{coinstats_key}\n{binance_api_key}".encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def get_last_usage(api_key: str) -> dict[str, Any] | None:
    """Last recorded credit snapshot for this key, or None. The key is not stored."""
    with _usage_lock:
        found = _last_usage.get(_key_fingerprint(api_key))
        return dict(found) if found else None


def _record_usage(api_key: str, usage: dict[str, Any]) -> None:
    with _usage_lock:
        _last_usage[_key_fingerprint(api_key)] = dict(usage)


class CoinStatsError(Exception):
    """A CoinStats (or Binance-via-CoinStats) failure with a scrubbed Chinese message."""

    def __init__(self, kind: str, user_message: str, *, status: int | None = None) -> None:
        super().__init__(user_message)
        self.kind = kind
        self.status = status
        self.user_message = user_message

    def __str__(self) -> str:
        return self.user_message


def _message_of(payload: Any, fallback: str = "") -> str:
    if isinstance(payload, dict):
        message = payload.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
    return fallback


def _scrub_text(text: str, secrets: list[str]) -> str:
    cleaned = text
    for secret in secrets:
        if secret and len(secret) >= 4 and secret in cleaned:
            cleaned = cleaned.replace(secret, "***")
    return cleaned


def _extract_ip(text: str) -> str | None:
    for pattern in _IP_PATTERNS:
        matched = pattern.search(text)
        if matched:
            return matched.group(1)
    return None


def _is_ip_whitelist(status: int | None, message: str) -> bool:
    lowered = message.lower()
    if "whitelist" in lowered or "request ip" in lowered:
        return True
    if re.search(r"IP\[[0-9]", message):
        return True
    if "-2015" in message and "ip" in lowered:
        return True
    return False


def _classify(status: int | None, message: str, secrets: list[str]) -> CoinStatsError:
    safe = _scrub_text(message, secrets)[:240]
    if _is_ip_whitelist(status, message):
        ip = _extract_ip(message)
        extra = f"如要保留白名单，请先把报错里的 IP {ip} 加进去（IP 可能变化）" if ip else "如要保留白名单，请先把报错里的 IP 加进去（IP 可能变化）"
        return CoinStatsError(
            "ip_whitelist",
            "币安拒绝了 CoinStats 的请求：这个币安 key 开了 IP 白名单。"
            "CoinStats 出口 IP 不固定，官方建议把该只读 key 改成「不限制 IP」；"
            + extra,
            status=status,
        )
    lowered = message.lower()
    if status == 400 and ("invalid api-key" in lowered or "signature" in lowered):
        return CoinStatsError(
            "bad_request",
            "币安 API key 被拒绝：请检查 key/secret 是否正确、是否开了只读权限",
            status=status,
        )
    if status in (401, 403):
        return CoinStatsError(
            "auth",
            "CoinStats API key 无效或已被停用，请到 openapi.coinstats.app 重新生成后在设置里更新",
            status=status,
        )
    if status == 429:
        return CoinStatsError(
            "rate_limit",
            "CoinStats 请求太频繁（免费档 2 次/秒），已自动退避仍失败，稍后会自动重试",
            status=status,
        )
    if status == 409:
        return CoinStatsError("not_synced", "CoinStats 还在同步币安数据，稍后重试", status=status)
    if status == 400:
        detail = f"：{safe}" if safe else ""
        return CoinStatsError("bad_request", f"CoinStats 拒绝了请求（HTTP 400）{detail}", status=status)
    if status is not None and status >= 500:
        return CoinStatsError("upstream", f"CoinStats 服务暂不可用（HTTP {status}）", status=status)
    return CoinStatsError("upstream", f"CoinStats 服务暂不可用（HTTP {status or 'unknown'}）", status=status)


class CoinStatsClient:
    """Thin HTTP client. ``transport`` is an ``httpx.BaseTransport`` (tests use MockTransport)."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str | None = None,
        timeout: float | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = (base_url or settings.coinstats_base_url).rstrip("/")
        self._timeout = timeout if timeout is not None else float(settings.coinstats_timeout_seconds)
        self._sleep = sleep or time.sleep
        self._clock = clock or time.monotonic
        self._fingerprint = _key_fingerprint(api_key)
        self._local_next = 0.0
        self._client = httpx.Client(
            base_url=self._base_url,
            timeout=self._timeout,
            transport=transport,
            headers={"X-API-KEY": api_key, "Accept": "application/json"},
        )

    def close(self) -> None:
        self._client.close()

    def usage_credits(self) -> dict[str, Any]:
        """GET /v1/usage/credits (free). Also records the snapshot for get_last_usage."""
        payload = self._request("GET", "/v1/usage/credits")
        usage = {
            "total": int(payload.get("totalCredits") or 0),
            "used": int(payload.get("usedCredits") or 0),
            "remaining": int(payload.get("remainingCredits") or 0),
            "subscription": str(payload.get("subscription") or ""),
        }
        _record_usage(self._api_key, usage)
        return usage

    def _key_lock(self) -> threading.Lock:
        with _key_state_lock:
            lock = _key_locks.get(self._fingerprint)
            if lock is None:
                lock = threading.Lock()
                _key_locks[self._fingerprint] = lock
            return lock

    def _throttle(self) -> None:
        """Space requests from this client and from every client sharing the key by ≥0.55s."""
        lock = self._key_lock()
        with lock:
            now = self._clock()
            with _key_state_lock:
                shared = _key_next_at.get(self._fingerprint, 0.0)
            wait = max(self._local_next, shared) - now
            if wait > 0:
                self._sleep(wait)
                now = self._clock()
            nxt = now + _MIN_INTERVAL
            self._local_next = nxt
            with _key_state_lock:
                _key_next_at[self._fingerprint] = nxt

    def _request(self, method: str, path: str, *, params: dict | None = None, json_body: dict | None = None) -> Any:
        attempt = 0
        while True:
            self._throttle()
            try:
                response = self._client.request(method, path, params=params, json=json_body)
            except httpx.TimeoutException as exc:
                raise CoinStatsError("timeout", "连接 CoinStats 超时，稍后会自动重试") from exc
            except httpx.ConnectError as exc:
                raise CoinStatsError("timeout", "连接 CoinStats 超时，稍后会自动重试") from exc
            status = response.status_code
            if status == 429 and attempt < _MAX_429:
                delay = _retry_after(response) or _BACKOFF[min(attempt, len(_BACKOFF) - 1)]
                logger.info("coinstats http=429 backoff=%.2fs attempt=%s", delay, attempt + 1)
                self._sleep(delay)
                attempt += 1
                continue
            if status >= 400:
                message = _read_message(response)
                logger.info("coinstats http=%s", status)
                raise _classify(status, message, [self._api_key])
            logger.info("coinstats http=%s", status)
            if not response.content:
                return {}
            try:
                return response.json()
            except json.JSONDecodeError as exc:
                raise CoinStatsError("upstream", "CoinStats 服务暂不可用（HTTP 200）", status=status) from exc

    def post_balance(self, binance_key: str, binance_secret: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/exchange/balance",
            json_body={
                "connectionId": "binance",
                "connectionFields": {"apiKey": binance_key, "apiSecret": binance_secret},
            },
        )

    def patch_sync(self, portfolio_id: str) -> dict[str, Any]:
        return self._request("PATCH", "/v1/exchange/sync", params={"portfolioId": portfolio_id})

    def get_status(self, portfolio_id: str) -> dict[str, Any]:
        return self._request("GET", "/v1/exchange/status", params={"portfolioId": portfolio_id})

    def get_transactions(self, portfolio_id: str, *, page: int, since: datetime | None) -> dict[str, Any]:
        params: dict[str, Any] = {
            "portfolioId": portfolio_id,
            "page": page,
            "limit": 100,
            "currency": "USD",
        }
        if since is not None:
            params["from"] = _iso(since)
        return self._request("GET", "/v1/exchange/transactions", params=params)

    def get_coin(self, coin_id: str) -> dict[str, Any]:
        # Documented: GET /v1/coins/{coinId} → CoinListResponseItemDto.symbol (required).
        return self._request("GET", f"/v1/coins/{coin_id}")

    def note_usage(self) -> None:
        """Best-effort credit read after a real request. Failures are ignored."""
        try:
            self.usage_credits()
        except CoinStatsError:
            logger.info("coinstats usage read failed")


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def _read_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001 - a non-JSON error body still classifies by status
        payload = None
    if isinstance(payload, dict):
        return _message_of(payload, response.text[:240])
    return (response.text or "")[:240]


def _iso(moment: datetime) -> str:
    aware = moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(value: Any) -> datetime:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    text = str(value or "").strip()
    if not text:
        return datetime.now(timezone.utc)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _type_tokens(raw_type: Any) -> set[str]:
    text = str(raw_type or "").strip().lower().replace("_", "-")
    parts = {piece for piece in re.split(r"[|\s/]+", text) if piece}
    parts.add(text)
    return parts


def _direction_of(raw_type: Any) -> str | None:
    tokens = _type_tokens(raw_type)
    if tokens & _TRADE_TYPES and not (tokens & (_IN_TYPES | _OUT_TYPES)):
        return "trade"
    # "withdraw|sent" carries both words; either is enough to call it out.
    if tokens & _OUT_TYPES:
        return "out"
    if tokens & _IN_TYPES:
        return "in"
    if tokens & _TRADE_TYPES:
        return "trade"
    return None


def _item_amount(item: dict[str, Any], coin_data: dict[str, Any] | None) -> Decimal:
    count = item.get("count")
    if count is None and isinstance(coin_data, dict):
        count = coin_data.get("count")
    try:
        return abs(Decimal(str(count)))
    except Exception:  # noqa: BLE001
        return Decimal(0)


def _item_symbol(item: dict[str, Any], coin_data: dict[str, Any] | None) -> str:
    coin = item.get("coin") if isinstance(item.get("coin"), dict) else {}
    symbol = coin.get("symbol") or (coin_data or {}).get("symbol") or ""
    return str(symbol).upper()


def _external_id(item: dict[str, Any], raw_type: str, date: Any, symbol: str, count: Any) -> str:
    item_id = item.get("id")
    if item_id:
        return str(item_id)
    blob = f"{raw_type}|{date}|{symbol}|{count}"
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


class CoinStatsBinanceAdapter:
    """Binance balances and transfers fetched through CoinStats. ``source`` is always binance."""

    source = "binance"

    def __init__(
        self,
        binance_creds: dict[str, str],
        coinstats_key: str,
        *,
        client: CoinStatsClient | None = None,
    ) -> None:
        self._binance_key = binance_creds.get("apiKey") or ""
        self._binance_secret = binance_creds.get("secret") or binance_creds.get("apiSecret") or ""
        self._coinstats_key = coinstats_key
        self._owns_client = client is None
        self._client = client or CoinStatsClient(coinstats_key)
        self._cache_id = _cache_key(coinstats_key, self._binance_key)
        self._symbol_lookups = 0

    def fetch_balances(self) -> list[NormalizedBalance]:
        cached = _cache_get(self._cache_id, "balances")
        if cached is not None:
            return list(cached)
        payload = self._client.post_balance(self._binance_key, self._binance_secret)
        self._client.note_usage()
        portfolio = payload.get("portfolio") if isinstance(payload, dict) else None
        portfolio_id = portfolio.get("id") if isinstance(portfolio, dict) else None
        rows = self._map_balances(payload.get("balances") if isinstance(payload, dict) else None)
        _cache_put(self._cache_id, balances=rows, portfolio_id=str(portfolio_id) if portfolio_id else None)
        return list(rows)

    def fetch_transfers(self, since: datetime) -> list[NormalizedTransfer]:
        cached = _cache_get(self._cache_id, "transfers")
        if cached is not None:
            return [row for row in cached if row.ts >= _aware(since)]
        transfers, _trades = self._load_transactions(since)
        return transfers

    def fetch_trades(self, since: datetime) -> list[NormalizedTrade]:
        cached = _cache_get(self._cache_id, "trades")
        if cached is not None:
            return [row for row in cached if row.ts >= _aware(since)]
        try:
            _transfers, trades = self._load_transactions(since)
        except CoinStatsError:
            return []
        return trades

    def _portfolio_id(self) -> str:
        cached = _cache_meta(self._cache_id)
        if cached and cached.get("portfolio_id"):
            return str(cached["portfolio_id"])
        self.fetch_balances()
        cached = _cache_meta(self._cache_id)
        portfolio_id = (cached or {}).get("portfolio_id")
        if not portfolio_id:
            raise CoinStatsError("upstream", "CoinStats 服务暂不可用（HTTP unknown）")
        return str(portfolio_id)

    def _load_transactions(self, since: datetime) -> tuple[list[NormalizedTransfer], list[NormalizedTrade]]:
        cached_transfers = _cache_get(self._cache_id, "transfers")
        cached_trades = _cache_get(self._cache_id, "trades")
        if cached_transfers is not None and cached_trades is not None:
            start = _aware(since)
            return (
                [row for row in cached_transfers if row.ts >= start],
                [row for row in cached_trades if row.ts >= start],
            )
        portfolio_id = self._portfolio_id()
        self._client.patch_sync(portfolio_id)
        self._wait_synced(portfolio_id)
        transfers: list[NormalizedTransfer] = []
        trades: list[NormalizedTrade] = []
        page = 1
        max_pages = max(1, int(settings.coinstats_max_pages))
        while page <= max_pages:
            payload = self._client.get_transactions(portfolio_id, page=page, since=since)
            result = payload.get("result") if isinstance(payload, dict) else None
            if not result:
                break
            for entry in result:
                if not isinstance(entry, dict):
                    continue
                kind = _direction_of(entry.get("type"))
                if kind == "trade":
                    trade = _map_trade(entry)
                    if trade is not None:
                        trades.append(trade)
                elif kind in ("in", "out"):
                    transfers.extend(_map_transfer(entry, kind))
            page += 1
        self._client.note_usage()
        _cache_put(self._cache_id, transfers=transfers, trades=trades)
        return transfers, trades

    def _wait_synced(self, portfolio_id: str) -> None:
        for _ in range(_STATUS_POLLS):
            payload = self._client.get_status(portfolio_id)
            status = str(payload.get("status") or "").lower() if isinstance(payload, dict) else ""
            if status == "synced":
                return
            self._client._sleep(_STATUS_INTERVAL)
        raise CoinStatsError("not_synced", "CoinStats 还在同步币安数据，稍后重试")

    def _map_balances(self, rows: Any) -> list[NormalizedBalance]:
        if not isinstance(rows, list):
            return []
        out: list[NormalizedBalance] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                amount = Decimal(str(row.get("amount")))
            except Exception:  # noqa: BLE001
                continue
            if amount <= 0:
                continue
            coin_id = str(row.get("coinId") or "")
            symbol = self._symbol_for(coin_id)
            price = row.get("price")
            out.append(
                NormalizedBalance(
                    source="binance",
                    asset=symbol,
                    amount=amount,
                    raw={"coinId": coin_id, "price": price},
                )
            )
        return out

    def _symbol_for(self, coin_id: str) -> str:
        if not coin_id:
            return ""
        static = _COIN_ID_SYMBOLS.get(coin_id.lower())
        if static:
            return static
        with _symbol_lock:
            if coin_id in _symbol_cache and _symbol_cache[coin_id]:
                return str(_symbol_cache[coin_id])
        if self._symbol_lookups >= _SYMBOL_LOOKUP_CAP:
            return coin_id.upper()
        self._symbol_lookups += 1
        try:
            payload = self._client.get_coin(coin_id)
        except CoinStatsError:
            with _symbol_lock:
                _symbol_cache.setdefault(coin_id, None)
            return coin_id.upper()
        symbol = ""
        if isinstance(payload, dict) and payload.get("symbol"):
            symbol = str(payload["symbol"]).upper()
        with _symbol_lock:
            _symbol_cache[coin_id] = symbol or None
        return symbol or coin_id.upper()


def _aware(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _map_transfer(entry: dict[str, Any], direction: str) -> list[NormalizedTransfer]:
    raw_type = str(entry.get("type") or "")
    date = entry.get("date")
    coin_data = entry.get("coinData") if isinstance(entry.get("coinData"), dict) else {}
    txid = None
    digest = entry.get("hash")
    if isinstance(digest, dict) and digest.get("id"):
        txid = str(digest["id"])
    groups = entry.get("transactions")
    items: list[dict[str, Any]] = []
    if isinstance(groups, list):
        for group in groups:
            if isinstance(group, dict) and isinstance(group.get("items"), list):
                items.extend(item for item in group["items"] if isinstance(item, dict))
    if not items:
        items = [{}]
    out: list[NormalizedTransfer] = []
    for item in items:
        symbol = _item_symbol(item, coin_data)
        amount = _item_amount(item, coin_data)
        if amount <= 0 or not symbol:
            continue
        address = item.get("toAddress") if direction == "in" else item.get("fromAddress")
        if not address:
            address = item.get("toAddress") or item.get("fromAddress")
        out.append(
            NormalizedTransfer(
                source="binance",
                external_id=_external_id(item, raw_type, date, symbol, item.get("count")),
                ts=_parse_ts(date),
                asset=symbol,
                amount=amount,
                direction=direction,  # type: ignore[arg-type]
                address=str(address) if address else None,
                txid=txid,
                raw_type=raw_type,
                raw=entry,
            )
        )
    return out


def _map_trade(entry: dict[str, Any]) -> NormalizedTrade | None:
    """Best effort. A Swap/Buy/Sell/Trade we cannot read is dropped, never raised."""
    try:
        raw_type = str(entry.get("type") or "")
        tokens = _type_tokens(raw_type)
        if "sell" in tokens:
            side = "sell"
        elif "buy" in tokens:
            side = "buy"
        else:
            side = "swap"
        coin_data = entry.get("coinData") if isinstance(entry.get("coinData"), dict) else {}
        groups = entry.get("transactions") if isinstance(entry.get("transactions"), list) else []
        received: dict[str, Any] | None = None
        sent: dict[str, Any] | None = None
        for group in groups:
            if not isinstance(group, dict):
                continue
            action = str(group.get("action") or "").lower()
            items = [item for item in group.get("items") or [] if isinstance(item, dict)]
            if not items:
                continue
            if action in {"received", "receive", "buy"}:
                received = items[0]
            elif action in {"sent", "send", "sell"}:
                sent = items[0]
        base_item = received or sent
        if base_item is None:
            return None
        base = _item_symbol(base_item, coin_data)
        quote_item = sent if base_item is received else received
        quote = _item_symbol(quote_item, None) if quote_item else "USDT"
        if not base:
            return None
        amount = _item_amount(base_item, coin_data)
        cost = None
        worth = base_item.get("totalWorth")
        if worth is None and quote_item is not None:
            worth = quote_item.get("totalWorth")
        if worth is not None:
            cost = abs(Decimal(str(worth)))
        external = _external_id(base_item, raw_type, entry.get("date"), base, base_item.get("count"))
        return NormalizedTrade(
            source="binance",
            external_id=external,
            ts=_parse_ts(entry.get("date")),
            symbol=f"{base}/{quote or 'USDT'}",
            side=side,
            amount=amount,
            cost_usdt=cost,
            raw=entry,
        )
    except Exception:  # noqa: BLE001 - unreadable trades are skipped
        return None


def _ttl() -> float:
    try:
        return float(settings.coinstats_sync_interval_seconds)
    except Exception:  # noqa: BLE001
        return 21600.0


def _cache_get(cache_id: str, field: str) -> Any:
    now = time.time()
    with _cache_lock:
        entry = _response_cache.get(cache_id)
        if not entry:
            return None
        if now >= float(entry.get("expires", 0)):
            _response_cache.pop(cache_id, None)
            return None
        if field not in entry:
            return None
        return entry[field]


def _cache_meta(cache_id: str) -> dict[str, Any] | None:
    now = time.time()
    with _cache_lock:
        entry = _response_cache.get(cache_id)
        if not entry or now >= float(entry.get("expires", 0)):
            return None
        return dict(entry)


def _cache_put(cache_id: str, **fields: Any) -> None:
    with _cache_lock:
        entry = _response_cache.get(cache_id)
        now = time.time()
        if entry is None or now >= float(entry.get("expires", 0)):
            entry = {"expires": now + _ttl()}
            _response_cache[cache_id] = entry
        else:
            entry["expires"] = entry.get("expires", now + _ttl())
        entry.update(fields)


def reset_state_for_tests() -> None:
    """Drop module caches. Tests only."""
    with _usage_lock:
        _last_usage.clear()
    with _key_state_lock:
        _key_next_at.clear()
    with _cache_lock:
        _response_cache.clear()
    with _symbol_lock:
        _symbol_cache.clear()
