"""Known cross-chain / DEX route contracts and label hints.

This is a deliberately small, extendable table.  A route hit is *context*, not
proof: the nature classifier only calls a paired swap/bridge ``safe`` when the
same transaction also shows the counter-leg.  Blockscout labels are accepted as
a second signal via :func:`route_kind_from_text`.
"""

from __future__ import annotations

import re

# Address -> "swap" | "bridge".  Checksummed input is lower-cased by callers.
ROUTE_CONTRACTS: dict[str, str] = {
    # CoW Protocol settlement (DEX / batch auction).
    "0x9008d19f58aabd9ed0d60971565aa8510560ab41": "swap",
    # Relay depository (cross-chain).
    "0x4cd00e387622c35bddb9b4c962c68c65124a8e17": "bridge",
    # Meson proxy (cross-chain).
    "0x25ab3efd52e6470681ce037cd546dc60726948d3": "bridge",
    # Circle CCTP TokenMessenger (cross-chain).
    "0xbd3fa81b58ba92a82136038b25adec7066af3155": "bridge",
    # Sushi router (DEX).
    "0xd9e1ce17f2641f24ae83637ab66a2cca9c378b9f": "swap",
    # Uniswap V3 router / Universal Router (DEX).
    "0xe592427a0aece92de3edee1f18e0157c05861564": "swap",
    "0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad": "swap",
    # 1inch / Odos aggregators (DEX).
    "0x1111111254eeb25477b68fb85ed929f73a960582": "swap",
    "0xcf5540fffcdc3d510b18bfca6d2b9987b0772559": "swap",
    # Across SpokePool / LayerZero endpoints (cross-chain).
    "0x5c7bcd6e7de5423a257d81b442095a1a6ced35c5": "bridge",
    "0x66a71dcef29a0ffbdbe3c6a460a3b5bc225cd675": "bridge",
    "0x1a44076050125825900e736c501f859c50fe728c": "bridge",
    # 3Jane helper is a protocol, classified elsewhere; kept here only so the
    # table can be extended without changing call sites.
}

_BRIDGE_TEXT = re.compile(
    r"\b(?:bridge|cross[\s_-]?chain|relay|meson|butter|cctp|layerzero|stargate|"
    r"across|wormhole|axelar|hyperlane|synapse|hop)\b|跨链|桥",
    re.IGNORECASE,
)
_SWAP_TEXT = re.compile(
    r"\b(?:cow|gpv2|settlement|uniswap|sushi|1inch|odos|paraswap|kyber|"
    r"dex|swap|exchange|aggregator)\b|兑换|去中心化交易",
    re.IGNORECASE,
)


def normalize_route_address(address: str | None) -> str:
    text = str(address or "").strip()
    return text.lower() if text.lower().startswith("0x") else text


def route_kind(address: str | None) -> str | None:
    """Return ``"swap"``/``"bridge"`` for a known route contract, else None."""
    return ROUTE_CONTRACTS.get(normalize_route_address(address))


def route_kind_from_text(*values: object) -> str | None:
    """Infer a route kind from Blockscout labels / provider notes.

    Bridge words win over generic DEX words so "Relay swap" is still a bridge.
    """
    blob = " ".join(str(value or "") for value in values)
    if _BRIDGE_TEXT.search(blob):
        return "bridge"
    if _SWAP_TEXT.search(blob):
        return "swap"
    return None
