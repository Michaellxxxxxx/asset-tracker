"""CCXT-backed exchange adapter. Read-only: balances, deposits, withdrawals, trades.

Only these client methods are ever called: ``fetch_balance``, ``fetch_deposits``,
``fetch_withdrawals``, ``fetch_my_trades``, ``load_markets``. No order, withdraw,
transfer or deposit-address call exists in this module.

Windowing follows what ccxt 4.4.50 actually sends:

* Gate deposits/withdrawals: ``params['until']`` is **seconds** (ccxt writes it
  straight into ``to``). Span must stay ≤30 days or Gate returns
  ``INVALID_PARAM_VALUE``. Paging is ``offset``, limit 100.
* Gate trades: ``symbol`` may be ``None`` (all pairs). ``until`` is milliseconds
  because ccxt divides it by 1000. Full pages advance ``page``.
* Binance deposits/withdrawals: ``until`` is milliseconds, span ≤90 days (we use
  89). ccxt's default ``endTime = since + 90d`` would otherwise keep only the
  first window. Paging is ``offset``, limit 1000.
* Binance trades require a symbol and a 24h ``startTime`` window, so history is
  walked with ``fromId`` and then filtered by ``since``. Candidate symbols are
  assets present in the current balance or in deposit/withdrawal history,
  crossed with USDT/USDC/FDUSD/BTC/ETH/BNB, keeping only active spot markets,
  capped at 80, alphabetical.

Known gaps (also returned in ``notes``): an asset that was sold out and never
deposited or withdrawn, plus Convert / Dust / Earn records, is invisible.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from ..types import NormalizedBalance, NormalizedTrade, NormalizedTransfer
from .normalize import filter_since, normalize_balance, normalize_trade, normalize_transfer
from .retry import call_with_retry

logger = logging.getLogger("app.exchanges")

# Our source name → ccxt class name. Gate's ccxt id is ``gateio``.
_CCXT_IDS = {
    "binance": "binance",
    "bitget": "bitget",
    "bybit": "bybit",
    "kraken": "kraken",
    "gate": "gateio",
}

_QUOTE_ASSETS = ("USDT", "USDC", "FDUSD", "BTC", "ETH", "BNB")
_COMPLETED = "ok"
_BINANCE_TRADE_CAP = 80


@dataclass(frozen=True)
class ExchangeProfile:
    """Per-venue window and paging. ``until_unit`` is what ccxt expects in params."""

    until_unit: str  # "s" or "ms"
    window_days: int
    page: str  # "offset" or "page" or "fromId"
    transfer_limit: int
    trade_limit: int
    # Gate trades take until in ms even though deposits take seconds.
    trade_until_unit: str
    trade_window_days: int


EXCHANGE_PROFILES: dict[str, ExchangeProfile] = {
    "gate": ExchangeProfile("s", 30, "offset", 100, 100, "ms", 30),
    "binance": ExchangeProfile("ms", 89, "offset", 1000, 1000, "ms", 89),
    "bitget": ExchangeProfile("ms", 89, "offset", 100, 100, "ms", 89),
    "bybit": ExchangeProfile("ms", 7, "offset", 50, 50, "ms", 7),
    "kraken": ExchangeProfile("ms", 89, "offset", 50, 50, "ms", 89),
}


def _profile(source: str) -> ExchangeProfile:
    return EXCHANGE_PROFILES.get(source, ExchangeProfile("ms", 30, "offset", 100, 100, "ms", 30))


def _ms(moment: datetime) -> int:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int(moment.timestamp() * 1000)


def _until_value(moment: datetime, unit: str) -> int:
    millis = _ms(moment)
    return millis // 1000 if unit == "s" else millis


def _windows(since: datetime, *, days: int, until: datetime | None = None) -> list[tuple[datetime, datetime]]:
    """Half-open windows of at most ``days`` covering ``[since, until]``."""
    start = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
    end = until or datetime.now(timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    span = timedelta(days=days)
    out: list[tuple[datetime, datetime]] = []
    cursor = start
    while cursor < end:
        nxt = min(cursor + span, end)
        out.append((cursor, nxt))
        cursor = nxt
    if not out:
        out.append((start, end))
    return out


class CCXTAdapter:
    def __init__(
        self,
        exchange_id: str,
        credentials: dict[str, str],
        *,
        client: Any | None = None,
        sleep: Any | None = None,
        page_limit: int | None = None,
    ) -> None:
        self.source = exchange_id
        self._sleep = sleep
        # Tests inject a small page so a full page + tail stays cheap. Live
        # calls leave this None and use the venue profile (Binance 1000, Gate 100).
        self._page_limit = page_limit
        ccxt_id = _CCXT_IDS.get(exchange_id, exchange_id)
        if client is not None:
            if ccxt_id == exchange_id and exchange_id not in _CCXT_IDS:
                raise ValueError(f"ccxt has no exchange '{exchange_id}'")
            self._client = client
            self.notes = []
            self._markets = None
            return
        import ccxt  # local import: keeps app importable if ccxt is optional in tests

        cls = getattr(ccxt, ccxt_id, None)
        if cls is None:
            raise ValueError(f"ccxt has no exchange '{ccxt_id}'")
        self._client = cls(
            {
                "apiKey": credentials.get("apiKey", ""),
                "secret": credentials.get("secret", ""),
                "password": credentials.get("password", ""),
                "enableRateLimit": True,
                "options": {"defaultType": "spot"},
            }
        )
        self.notes: list[str] = [
            "sold-out assets with no deposit/withdrawal, and Convert/Dust/Earn records, are not returned",
        ]
        self._markets: dict[str, Any] | None = None

    def fetch_balances(self) -> list[NormalizedBalance]:
        # Spot only. Binance funding uses a POST (sapiPostAssetGetFundingAsset); not called.
        raw = call_with_retry(lambda: self._client.fetch_balance(), sleep=self._sleep)
        return normalize_balance(self.source, raw if isinstance(raw, dict) else {})

    def fetch_transfers(self, since: datetime) -> list[NormalizedTransfer]:
        profile = _profile(self.source)
        seen: dict[str, NormalizedTransfer] = {}
        paged: dict[str, set[str]] = {}
        for start, end in _windows(since, days=profile.window_days):
            for kind, method in (("deposit", "fetch_deposits"), ("withdrawal", "fetch_withdrawals")):
                for raw in self._page_window(method, start, end, profile, paged.setdefault(method, set())):
                    raw = dict(raw)
                    raw.setdefault("type", kind)
                    # A missing status (older fixtures) is kept. An explicit
                    # pending/canceled/failed status is not.
                    if raw.get("status") is not None and str(raw.get("status")).lower() != _COMPLETED:
                        # Pending/canceled/failed stay out. A later sync re-sees the
                        # same id once the exchange flips it to ok.
                        continue
                    parsed = normalize_transfer(self.source, raw)
                    if parsed is not None:
                        seen.setdefault(parsed.external_id, parsed)
        return sorted(seen.values(), key=lambda item: (item.ts, item.external_id))

    def fetch_trades(self, since: datetime) -> list[NormalizedTrade]:
        profile = _profile(self.source)
        if self.source == "binance":
            rows = self._binance_trades(since, profile)
        else:
            rows = self._windowed_trades(since, profile, symbol=None)
        unique: dict[str, NormalizedTrade] = {}
        for raw in rows:
            parsed = normalize_trade(self.source, raw)
            if parsed is not None:
                unique.setdefault(parsed.external_id, parsed)
        return filter_since(sorted(unique.values(), key=lambda item: (item.ts, item.external_id)), since)

    def _call(self, fn: Any, *args: Any) -> list:
        """Call through the retry helper, dropping ``params`` for older fakes.

        The injected test client predates window params and only accepts
        ``(code, since, limit)``. A live ccxt client always takes ``params``.
        """
        try:
            page = call_with_retry(lambda: fn(*args), sleep=self._sleep)
        except TypeError:
            page = call_with_retry(lambda: fn(*args[:-1]), sleep=self._sleep)
        return page or []

    def _page_window(
        self,
        method: str,
        start: datetime,
        end: datetime,
        profile: ExchangeProfile,
        seen_ids: set[str] | None = None,
    ) -> list[dict]:
        fn = getattr(self._client, method)
        since_ms = _ms(start)
        until = _until_value(end, profile.until_unit)
        limit = self._page_limit or profile.transfer_limit
        collected: list[dict] = []
        offset = 0
        seen: set[str] = seen_ids if seen_ids is not None else set()
        while True:
            params: dict[str, Any] = {"until": until, "offset": offset}
            page = self._call(fn, None, since_ms, limit, params)
            if not isinstance(page, list) or not page:
                break
            fresh = [
                item
                for item in page
                if isinstance(item, dict) and str(item.get("id")) not in seen
            ]
            # A client that ignores offset (the legacy test fake, or a venue
            # that echoes the first page) must not loop forever.
            if not fresh:
                break
            for item in fresh:
                seen.add(str(item.get("id")))
            collected.extend(fresh)
            if len(page) < limit:
                break
            offset += len(fresh)
        return collected

    def _windowed_trades(self, since: datetime, profile: ExchangeProfile, symbol: str | None) -> list[dict]:
        collected: list[dict] = []
        for start, end in _windows(since, days=profile.trade_window_days):
            since_ms = _ms(start)
            until = _until_value(end, profile.trade_until_unit)
            limit = self._page_limit or profile.trade_limit
            page_no = 1
            while True:
                params: dict[str, Any] = {"until": until, "page": page_no}
                page = self._call(self._client.fetch_my_trades, symbol, since_ms, limit, params)
                if not isinstance(page, list):
                    break
                collected.extend(item for item in page if isinstance(item, dict))
                if len(page) < limit:
                    break
                page_no += 1
        return collected

    def _binance_trades(self, since: datetime, profile: ExchangeProfile) -> list[dict]:
        """``fromId`` paging per symbol. ``since`` is not passed (24h window cap)."""
        symbols = self._binance_symbols()
        collected: list[dict] = []
        for symbol in symbols:
            from_id = 0
            limit = self._page_limit or profile.trade_limit
            while True:
                params = {"fromId": from_id}
                page = self._call(self._client.fetch_my_trades, symbol, None, limit, params)
                if not isinstance(page, list) or not page:
                    break
                dicts = [item for item in page if isinstance(item, dict)]
                collected.extend(dicts)
                if len(page) < limit:
                    break
                ids = [int(item["id"]) for item in dicts if str(item.get("id", "")).isdigit()]
                if not ids:
                    break
                from_id = max(ids) + 1
        return collected

    def _binance_symbols(self) -> list[str]:
        assets: set[str] = set()
        try:
            for row in self.fetch_balances():
                assets.add(row.asset.upper())
        except Exception as exc:  # noqa: BLE001 — a balance miss must not hide trades entirely
            self.notes.append(f"balance symbols unavailable: {type(exc).__name__}")
        for method in ("fetch_deposits", "fetch_withdrawals"):
            try:
                fn = getattr(self._client, method)
                page = self._call(fn, None, None, 1000, {})
            except Exception as exc:  # noqa: BLE001
                self.notes.append(f"{method} symbols unavailable: {type(exc).__name__}")
                continue
            if isinstance(page, list):
                for item in page:
                    if isinstance(item, dict) and item.get("currency"):
                        assets.add(str(item["currency"]).upper())
        markets = self._load_markets()
        symbols: list[str] = []
        for base in sorted(assets):
            for quote in _QUOTE_ASSETS:
                symbol = f"{base}/{quote}"
                market = markets.get(symbol)
                if not isinstance(market, dict):
                    continue
                if market.get("active") is False:
                    continue
                if market.get("spot") is False:
                    continue
                symbols.append(symbol)
        symbols = sorted(set(symbols))[:_BINANCE_TRADE_CAP]
        return symbols

    def _load_markets(self) -> dict[str, Any]:
        if self._markets is None:
            if not hasattr(self._client, "load_markets"):
                self._markets = {}
                return self._markets
            # Binance load_markets calls sapi/v1/capital/config/getall. A 451 here
            # surfaces as ExchangeNotAvailable and is recorded by the caller.
            loaded = call_with_retry(lambda: self._client.load_markets(), sleep=self._sleep)
            self._markets = loaded if isinstance(loaded, dict) else {}
        return self._markets
