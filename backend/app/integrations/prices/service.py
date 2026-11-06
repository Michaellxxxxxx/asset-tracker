"""CoinGecko Demo API with a DefiLlama fallback. Quotes are USDT."""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx

from ..http import quiet_client
from .kamino_liquidity import quote_share_price

logger = logging.getLogger(__name__)

FIXTURE_DIR = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "prices"

# Symbols we know how to ask CoinGecko / DefiLlama about. Unknown symbols stay unpriced.
_COINGECKO_IDS = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "BNB": "binancecoin",
    "USDT": "tether",
    "USDC": "usd-coin",
}
_STABLES = {"USDT", "USDC", "USD", "DAI", "BUSD"}
# Wrapped or bridged stand-ins priced as the underlying only when the contract
# lookup misses. USDT0 is a USDT-class stable, not a distinct volatile asset.
_SYMBOL_ALIASES = {"WETH": "ETH", "USDT0": "USDT"}
_USDT0_FALLBACK = Decimal("1")

# Pendle yield tokens: ``YT-<UNDERLYING>-<DDMONYYYY>`` (e.g. YT-SUSDAI-20NOV2025).
# After expiry the claim is worth nothing, but DefiLlama still quotes the
# contract near 1.0. PT tokens share the date suffix and are left alone.
_YT_EXPIRY = re.compile(r"^YT-.*-(\d{1,2}[A-Z]{3}\d{4})$")
_YT_MONTHS = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}


def yt_expiry(symbol: str) -> date | None:
    """Maturity of a Pendle YT symbol, or None when the symbol is not one.

    The suffix is ``DDMONYYYY`` (day may be one digit). An unparseable date
    returns None so the caller prices the token normally.
    """
    match = _YT_EXPIRY.match(symbol.strip().upper())
    if match is None:
        return None
    raw = match.group(1)
    month = _YT_MONTHS.get(raw[-7:-4])
    if month is None:
        return None
    try:
        return date(int(raw[-4:]), month, int(raw[:-7]))
    except ValueError:
        return None


def expired_yt_price(symbol: str, on: date) -> Decimal | None:
    """``0`` when ``symbol`` is a YT that has matured on or before ``on``.

    PT symbols and YTs that are still alive return None. Callers that get
    ``0`` must mark the holding priced with ``price_source="expired_yt"``.
    """
    expiry = yt_expiry(symbol)
    if expiry is None or expiry > on:
        return None
    return Decimal("0")


# ERC-20 contracts for symbols CoinGecko ids do not cover. DefiLlama accepts
# ``ethereum:0x...`` alongside ``coingecko:{id}``. Native ETH stays on the id.
_CONTRACTS: dict[str, str] = {}

# Our exchange source id → ccxt class id. Gate's public ticker lives on gateio.
# Used only for unauthenticated ``fetch_ticker``; never for signed calls.
_CCXT_TICKER_IDS = {
    "binance": "binance",
    "bitget": "bitget",
    "bybit": "bybit",
    "kraken": "kraken",
    "gate": "gateio",
    "gateio": "gateio",
}

# Binance public REST is geo-blocked (HTTP 451) from this host. The same spot
# symbols trade on Gate, which is reachable without a key. Only these bases
# fall through; everything else stays on its own venue.
_TICKER_FALLBACK_VENUES = {
    "binance": ("gateio",),
}


def _contract_key(chain: str | None, contract: str | None) -> str | None:
    """``chain:address`` lower-cased, or None when either side is missing."""
    if not chain or not contract:
        return None
    chain_name = chain.strip().lower()
    address = contract.strip().lower()
    if not chain_name or not address:
        return None
    return f"{chain_name}:{address}"


def _contract_quotes(parsed: dict[str, Decimal], keys: list[str]) -> dict[str, Decimal]:
    """Keep prices whose key is one we asked for. Symbol entries are ignored."""
    wanted = {key.lower() for key in keys}
    return {key: price for key, price in parsed.items() if key in wanted}


def _coin_key(symbol: str) -> str | None:
    contract = _CONTRACTS.get(symbol)
    if contract:
        return f"ethereum:{contract}"
    cg_id = _COINGECKO_IDS.get(symbol)
    return f"coingecko:{cg_id}" if cg_id else None


def _decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def parse_coingecko(payload: dict[str, Any]) -> dict[str, Decimal]:
    """Parse ``/simple/price``: ``{"bitcoin": {"usd": 1.0}, ...}`` into SYMBOL→USDT.

    The caller passes ``id_to_symbol`` by embedding it as ``payload["__symbols__"]``
    optionally; when absent, known ids are reversed.
    """
    reverse = {cg_id: symbol for symbol, cg_id in _COINGECKO_IDS.items()}
    extra = payload.get("__symbols__") or {}
    if isinstance(extra, dict):
        reverse.update({str(cg_id): str(symbol).upper() for cg_id, symbol in extra.items()})
    quotes: dict[str, Decimal] = {}
    for cg_id, body in payload.items():
        if cg_id.startswith("__") or not isinstance(body, dict):
            continue
        price = _decimal(body.get("usd"))
        symbol = reverse.get(cg_id)
        if price is None or symbol is None:
            continue
        quotes[symbol] = price
    return quotes


def _nearest_price(points: list[Any], target: int) -> Decimal | None:
    """Pick the price closest to ``target`` from ``[[ts, price], ...]`` points."""
    best: Decimal | None = None
    best_gap: int | None = None
    for point in points:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            continue
        try:
            stamp = int(point[0])
        except (TypeError, ValueError):
            continue
        price = _decimal(point[1])
        if price is None:
            continue
        gap = abs(stamp - target)
        if best_gap is None or gap < best_gap:
            best = price
            best_gap = gap
    return best


def parse_defillama(payload: dict[str, Any]) -> dict[str, Decimal]:
    """Parse DefiLlama ``/prices/current``: ``{"coins": {"coingecko:bitcoin": {"price": 1}}}``.

    Contract keys (``ethereum:0x...``) are also stored under the lower-cased
    ``chain:address`` so a caller can price a holding without trusting the
    symbol DefiLlama sends back. Symbol keys stay upper-case.
    """
    reverse = {cg_id: symbol for symbol, cg_id in _COINGECKO_IDS.items()}
    coins = payload.get("coins") or {}
    quotes: dict[str, Decimal] = {}
    for key, body in coins.items():
        if not isinstance(body, dict):
            continue
        prefix, _, rest = str(key).partition(":")
        price = _decimal(body.get("price"))
        if price is None:
            continue
        if prefix == "coingecko":
            symbol = str(body.get("symbol") or reverse.get(rest) or "").upper()
            if symbol:
                quotes[symbol] = price
            continue
        # ``ethereum:0x...`` — keep the request key so two tokens that share a
        # symbol (two WETH deployments) do not overwrite each other.
        quotes[str(key).lower()] = price
        symbol = str(body.get("symbol") or "").upper()
        if symbol and symbol not in quotes:
            quotes[symbol] = price
    return quotes


class PriceService:
    """USDT quotes with a process-local TTL cache. No key → recorded fixtures."""

    def __init__(
        self,
        *,
        coingecko_api_key: str = "",
        client: httpx.Client | None = None,
        ttl_seconds: float = 60.0,
        now: Any | None = None,
        use_mock: bool = False,
        fixture_dir: Path | None = None,
        ticker_client: Any | None = None,
    ) -> None:
        self._key = coingecko_api_key
        self._client = client
        self._ttl = ttl_seconds
        self._now = now or time.monotonic
        # No CoinGecko key is not an error: spot and history go straight to DefiLlama.
        self._use_mock = use_mock
        self._fixture_dir = fixture_dir or FIXTURE_DIR
        self._ticker_client = ticker_client
        self._cache: dict[str, tuple[float, Decimal]] = {}
        # Contract quotes keyed by ``chain:address`` (lower-case). Separate from
        # the symbol cache so WETH on two chains cannot collide.
        self._contract_cache: dict[str, tuple[float, Decimal]] = {}
        # Public spot tickers keyed by ``ccxt_id:SYMBOL`` (e.g. ``gateio:GT``).
        # One snapshot asks each symbol once. A miss is cached as None so a
        # later row with the same symbol does not hit the exchange again.
        self._ticker_cache: dict[str, tuple[float, Decimal | None]] = {}
        # Historical quotes keyed by (symbol or chain:address, iso day). One
        # DefiLlama call covers every coin for a day, so a backfill of hundreds
        # of days does not repeat it.
        self._history_cache: dict[tuple[str, str], Decimal] = {}

    def quote_exchange_ticker(self, source: str, asset: str) -> Decimal | None:
        """Public spot last/close for ``source``'s ``ASSET/USDT`` (else ``/USD``).

        ``source`` is our venue id (``gate`` → ccxt ``gateio``). No api key.
        Mock mode never touches the network. A failure or a missing market
        returns None and is cached for the TTL so one bad symbol cannot be
        retried for every row, and cannot abort the snapshot.
        """
        symbol = (asset or "").strip().upper()
        venue = (source or "").strip().lower()
        if not symbol or not venue or symbol in _STABLES:
            return None
        ccxt_id = _CCXT_TICKER_IDS.get(venue)
        if ccxt_id is None:
            return None
        cache_key = f"{ccxt_id}:{symbol}"
        cached = self._ticker_cache.get(cache_key)
        if cached is not None and self._now() - cached[0] < self._ttl:
            return cached[1]
        # No injected client and mock mode: tests must not open a socket.
        # A dict (even empty) is an explicit client map, so keep going.
        if self._use_mock and not isinstance(self._ticker_client, dict) and self._ticker_client is None:
            return None
        price = self._fetch_exchange_ticker(ccxt_id, symbol)
        if price is None:
            for fallback in _TICKER_FALLBACK_VENUES.get(venue, ()):
                price = self._fetch_exchange_ticker(fallback, symbol)
                if price is not None:
                    break
        self._ticker_cache[cache_key] = (self._now(), price)
        return price

    def _fetch_exchange_ticker(self, ccxt_id: str, symbol: str) -> Decimal | None:
        client = self._ticker_client_for(ccxt_id)
        if client is None:
            return None
        for quote in ("USDT", "USD"):
            market = f"{symbol}/{quote}"
            try:
                ticker = client.fetch_ticker(market)
            except Exception as exc:  # noqa: BLE001 - one symbol must not abort pricing
                logger.debug(
                    "exchange ticker failed %s %s (%s)",
                    ccxt_id,
                    market,
                    type(exc).__name__,
                )
                # BadSymbol / 451: try the other quote, then give up.
                continue
            if not isinstance(ticker, dict):
                continue
            price = _decimal(ticker.get("last"))
            if price is None or price <= 0:
                price = _decimal(ticker.get("close"))
            if price is not None and price > 0:
                return price
        return None

    def _ticker_client_for(self, ccxt_id: str) -> Any | None:
        injected = self._ticker_client
        if isinstance(injected, dict) and ccxt_id in injected:
            return injected.get(ccxt_id)
        if injected is not None and not isinstance(injected, dict):
            return injected
        if self._use_mock:
            return None
        try:
            import ccxt
        except Exception as exc:  # noqa: BLE001
            logger.debug("ccxt import failed (%s)", type(exc).__name__)
            return None
        cls = getattr(ccxt, ccxt_id, None)
        if cls is None:
            return None
        try:
            client = cls({"enableRateLimit": True, "options": {"defaultType": "spot"}})
        except Exception as exc:  # noqa: BLE001
            logger.debug("ccxt client failed %s (%s)", ccxt_id, type(exc).__name__)
            return None
        # Remember it so the next symbol on this venue reuses load_markets.
        if not isinstance(self._ticker_client, dict):
            self._ticker_client = {}
        self._ticker_client[ccxt_id] = client
        return client

    def quote_usdt(self, asset: str) -> Decimal | None:
        symbol = asset.upper()
        if symbol in _STABLES:
            return Decimal("1")
        cached = self._cache.get(symbol)
        if cached is not None and self._now() - cached[0] < self._ttl:
            return cached[1]
        price = self._fetch(symbol)
        if price is None:
            # WETH tracks ETH. USDT0 is a USDT-class stable only after DefiLlama
            # (and CoinGecko) had nothing under its own symbol.
            alias = _SYMBOL_ALIASES.get(symbol)
            if alias is not None and alias != symbol:
                price = Decimal("1") if alias in _STABLES else self.quote_usdt(alias)
                if price is None and symbol == "USDT0":
                    price = _USDT0_FALLBACK
        if price is not None:
            self._cache[symbol] = (self._now(), price)
        return price

    def quote_contract(self, chain: str, contract: str) -> Decimal | None:
        """Spot USDT price for ``chain:contract`` via DefiLlama. No symbol fallback."""
        key = _contract_key(chain, contract)
        if key is None:
            return None
        cached = self._contract_cache.get(key)
        if cached is not None and self._now() - cached[0] < self._ttl:
            return cached[1]
        if self._use_mock:
            return None
        try:
            found = self._defillama_contracts([key])
        except Exception as exc:  # noqa: BLE001 - caller falls through to the symbol
            logger.debug("DefiLlama contract price failed (%s)", type(exc).__name__)
            return None
        price = found.get(key)
        if price is not None:
            self._contract_cache[key] = (self._now(), price)
        return price

    def quote_kamino_share(self, mint: str) -> Decimal | None:
        """Kamino Liquidity share NAV for an allow-listed ``KJLP-*`` mint.

        Only the two known share mints are requested. Mock mode with no injected
        client never opens a socket, matching the ticker path.
        """
        if self._use_mock and self._client is None:
            return None
        return quote_share_price(mint, client=self._client)

    def quote_contracts(self, keys: list[tuple[str, str]]) -> dict[str, Decimal]:
        """Batch ``coins.llama.fi/prices/current/{chain}:{address}``.

        Returns ``chain:address`` → price. A key DefiLlama does not know is
        simply absent. Mock mode returns nothing so tests never touch the network.
        """
        pending: list[str] = []
        found: dict[str, Decimal] = {}
        now = self._now()
        for chain, contract in keys:
            key = _contract_key(chain, contract)
            if key is None or key in pending:
                continue
            cached = self._contract_cache.get(key)
            if cached is not None and now - cached[0] < self._ttl:
                found[key] = cached[1]
                continue
            pending.append(key)
        if not pending or self._use_mock:
            return found
        try:
            fresh = self._defillama_contracts(pending)
        except Exception as exc:  # noqa: BLE001
            logger.debug("DefiLlama contract batch failed (%s)", type(exc).__name__)
            return found
        for key, price in fresh.items():
            self._contract_cache[key] = (now, price)
            found[key] = price
        return found

    def to_usdt(self, asset: str, amount: Decimal) -> Decimal | None:
        price = self.quote_usdt(asset)
        if price is None:
            return None
        return (amount * price).quantize(Decimal("0.01"))

    def quote_usdt_on(self, asset: str, day: date) -> Decimal | None:
        """Historical USDT quote for ``day``. Stables stay at 1.

        Live mode asks CoinGecko ``market_chart`` and falls back to DefiLlama
        ``chart``. Both failures (or mock mode) read the recorded daily curve,
        then the spot fixture. Unknown symbols return None — callers must not
        invent a price.
        """
        symbol = asset.upper()
        if symbol in _STABLES:
            return Decimal("1")
        if isinstance(day, datetime):
            day = day.date()
        alias = _SYMBOL_ALIASES.get(symbol)
        if alias is not None and alias != symbol:
            # Same day, underlying's history. USDT0 stays a dollar only when
            # that history is also missing — never today's spot.
            price = self.quote_usdt_on(alias, day)
            if price is not None:
                return price
            return _USDT0_FALLBACK if symbol == "USDT0" else None
        if self._use_mock:
            return self._historical_from_fixtures(symbol, day)
        price = None
        if self._key:
            try:
                price = self._coingecko_history(symbol, day)
            except Exception as exc:  # noqa: BLE001 - fallback is the contract
                logger.warning("CoinGecko history failed (%s); trying DefiLlama", type(exc).__name__)
        if price is None:
            cached = self._history_cache.get((symbol, day.isoformat()))
            if cached is not None:
                return cached
            try:
                price = self._defillama_history(symbol, day)
            except Exception as exc:  # noqa: BLE001
                logger.debug("DefiLlama history failed (%s)", type(exc).__name__)
        if price is not None:
            self._history_cache[(symbol, day.isoformat())] = price
            return price
        return self._historical_from_fixtures(symbol, day)

    def quote_contract_on(self, chain: str, contract: str, day: date) -> Decimal | None:
        """Historical USDT price for ``chain:contract``. Never the spot price.

        A miss returns None. Callers must record the holding as unpriced rather
        than substitute today's quote for a day in the past.
        """
        key = _contract_key(chain, contract)
        if key is None:
            return None
        if isinstance(day, datetime):
            day = day.date()
        cached = self._history_cache.get((key, day.isoformat()))
        if cached is not None:
            return cached
        if self._use_mock:
            return None
        try:
            found = self._defillama_contracts_on([key], day)
        except Exception as exc:  # noqa: BLE001
            logger.debug("DefiLlama contract history failed (%s)", type(exc).__name__)
            return None
        price = found.get(key)
        if price is not None:
            self._history_cache[(key, day.isoformat())] = price
        return price

    def _coingecko_history(self, symbol: str, day: date) -> Decimal | None:
        cg_id = _COINGECKO_IDS.get(symbol)
        if cg_id is None:
            return None
        # A 2-day window around UTC midnight of ``day``; we pick the nearest point.
        start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        start_s = int(start.timestamp())
        end_s = start_s + 86_400
        client = self._client or quiet_client(timeout=20.0)
        close = self._client is None
        try:
            response = client.get(
                f"https://api.coingecko.com/api/v3/coins/{cg_id}/market_chart/range",
                params={"vs_currency": "usd", "from": start_s, "to": end_s},
                headers={"x-cg-demo-api-key": self._key},
            )
            response.raise_for_status()
            payload = response.json()
        finally:
            if close:
                client.close()
        return _nearest_price(payload.get("prices") or [], start_s * 1000)

    def _defillama_history(self, symbol: str, day: date) -> Decimal | None:
        coin = _coin_key(symbol)
        if coin is None:
            return None
        start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        payload = self._defillama_historical_raw(coin, int(start.timestamp()))
        return parse_defillama(payload).get(symbol)

    def _defillama_historical_raw(self, coins: str, timestamp: int) -> dict[str, Any]:
        """GET ``/prices/historical/{ts}/{coins}``. ``coins`` may be comma-separated."""
        client = self._client or quiet_client(timeout=20.0)
        close = self._client is None
        try:
            response = client.get(f"https://coins.llama.fi/prices/historical/{timestamp}/{coins}")
            response.raise_for_status()
            payload = response.json()
        finally:
            if close:
                client.close()
        return payload if isinstance(payload, dict) else {}

    def prefetch_contracts_on(self, keys: list[tuple[str, str]], day: date) -> None:
        """One historical call for every ``chain:address`` not already cached.

        A failed call is logged and leaves the contracts unpriced. It does not
        fall back to the current price.
        """
        if isinstance(day, datetime):
            day = day.date()
        pending: list[str] = []
        for chain, contract in keys:
            key = _contract_key(chain, contract)
            if key is None or (key, day.isoformat()) in self._history_cache or key in pending:
                continue
            pending.append(key)
        if not pending or self._use_mock:
            return
        try:
            found = self._defillama_contracts_on(pending, day)
        except Exception as exc:  # noqa: BLE001
            logger.debug("DefiLlama contract history batch failed (%s)", type(exc).__name__)
            return
        for key, price in found.items():
            self._history_cache[(key, day.isoformat())] = price

    def prefetch_history(self, symbols: list[str], day: date) -> None:
        """One ``/prices/historical/{ts}/{coins}`` call for every unknown symbol.

        Stables and anything already cached are skipped. A failed call is logged
        at debug and leaves the symbols unpriced; it never raises.
        """
        if isinstance(day, datetime):
            day = day.date()
        pending = []
        for symbol in {item.upper() for item in symbols}:
            if symbol in _STABLES or (symbol, day.isoformat()) in self._history_cache:
                continue
            if _COINGECKO_IDS.get(symbol) or _CONTRACTS.get(symbol):
                pending.append(symbol)
        if not pending or self._use_mock:
            return
        start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        coins = ",".join(_coin_key(symbol) for symbol in pending if _coin_key(symbol))
        if not coins:
            return
        try:
            payload = self._defillama_historical_raw(coins, int(start.timestamp()))
        except Exception as exc:  # noqa: BLE001 - one day's prices must not abort the curve
            logger.debug("DefiLlama history batch failed (%s)", type(exc).__name__)
            return
        for symbol, price in parse_defillama(payload).items():
            self._history_cache[(symbol, day.isoformat())] = price

    def _historical_from_fixtures(self, symbol: str, day: date) -> Decimal | None:
        path = self._fixture_dir / "historical.json"
        if path.is_file():
            with path.open(encoding="utf-8") as handle:
                payload = json.load(handle)
            series = (payload.get("prices") or {}).get(symbol) or {}
            raw = series.get(day.isoformat())
            if raw is None:
                # Use the latest recorded point at or before the requested day.
                earlier = [key for key in series if key <= day.isoformat()]
                raw = series[max(earlier)] if earlier else None
            price = _decimal(raw) if raw is not None else None
            if price is not None:
                return price
        return self._from_fixtures(symbol)

    def _fetch(self, symbol: str) -> Decimal | None:
        if self._use_mock:
            return self._from_fixtures(symbol)
        price = None
        if self._key:
            try:
                price = self._coingecko(symbol)
            except Exception as exc:  # noqa: BLE001 - fallback is the contract
                logger.warning("CoinGecko failed (%s); falling back to DefiLlama", type(exc).__name__)
        if price is None:
            try:
                price = self._defillama(symbol)
            except Exception as fallback_exc:  # noqa: BLE001
                logger.debug("DefiLlama failed (%s)", type(fallback_exc).__name__)
        if price is not None:
            return price
        return self._from_fixtures(symbol)

    def _coingecko(self, symbol: str) -> Decimal | None:
        cg_id = _COINGECKO_IDS.get(symbol)
        if cg_id is None:
            return None
        client = self._client or quiet_client(timeout=15.0)
        close = self._client is None
        try:
            response = client.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": cg_id, "vs_currencies": "usd"},
                headers={"x-cg-demo-api-key": self._key},
            )
            response.raise_for_status()
            payload = response.json()
        finally:
            if close:
                client.close()
        return parse_coingecko(payload).get(symbol)

    def _defillama(self, symbol: str) -> Decimal | None:
        coin = _coin_key(symbol)
        if coin is None:
            return None
        client = self._client or quiet_client(timeout=15.0)
        close = self._client is None
        try:
            response = client.get(f"https://coins.llama.fi/prices/current/{coin}")
            response.raise_for_status()
            payload = response.json()
        finally:
            if close:
                client.close()
        return parse_defillama(payload).get(symbol)

    def _defillama_contracts(self, keys: list[str]) -> dict[str, Decimal]:
        """GET ``/prices/current/{coins}`` and keep only the requested contract keys."""
        payload = self._defillama_current_raw(",".join(keys))
        return _contract_quotes(parse_defillama(payload), keys)

    def _defillama_current_raw(self, coins: str) -> dict[str, Any]:
        client = self._client or quiet_client(timeout=20.0)
        close = self._client is None
        try:
            response = client.get(f"https://coins.llama.fi/prices/current/{coins}")
            response.raise_for_status()
            payload = response.json()
        finally:
            if close:
                client.close()
        return payload if isinstance(payload, dict) else {}

    def _defillama_contracts_on(self, keys: list[str], day: date) -> dict[str, Decimal]:
        start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        payload = self._defillama_historical_raw(",".join(keys), int(start.timestamp()))
        return _contract_quotes(parse_defillama(payload), keys)

    def _from_fixtures(self, symbol: str) -> Decimal | None:
        gecko = self._fixture_dir / "coingecko.json"
        if gecko.is_file():
            with gecko.open(encoding="utf-8") as handle:
                price = parse_coingecko(json.load(handle)).get(symbol)
            if price is not None:
                return price
        llama = self._fixture_dir / "defillama.json"
        if llama.is_file():
            with llama.open(encoding="utf-8") as handle:
                return parse_defillama(json.load(handle)).get(symbol)
        return None
