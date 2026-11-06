"""Pick a CoinStats proxy or a direct CCXT adapter for one exchange."""

from __future__ import annotations

from .ccxt_adapter import CCXTAdapter
from .coinstats_adapter import CoinStatsBinanceAdapter


def build_exchange_adapter(name: str, creds: dict[str, dict[str, str]], *, ccxt_cls=None):
    """Binance goes through CoinStats only when a ``_coinstats`` key is present.

    ``_coinstats`` is ``{"apiKey": <coinstats key>}``. Any other venue, or a
    Binance entry without that key, stays on CCXT.
    """
    coinstats = creds.get("_coinstats") or {}
    coinstats_key = coinstats.get("apiKey") if isinstance(coinstats, dict) else None
    if name == "binance" and coinstats_key and isinstance(creds.get("binance"), dict):
        return CoinStatsBinanceAdapter(creds["binance"], coinstats_key)
    cls = ccxt_cls or CCXTAdapter
    return cls(name, creds[name])
