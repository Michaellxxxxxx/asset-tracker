"""Phase-5 asset-class / risk classification. Pure functions, no DB, no network.

Every public classifier returns ``(asset_class, risk_level, reason)``.  The
reason is one short Chinese sentence so the UI can show the basis inline.

The rule tables at the top are intentionally simple constants: adding a new
stablecoin, mainstream coin, or tokenized stock is a one-line change and does
not require touching the classifier.
"""

from __future__ import annotations

import json
from typing import Any

ASSET_CLASSES = ("cash", "crypto", "stock", "defi", "option", "other")
RISK_LEVELS = ("low", "medium", "high")

ASSET_CLASS_LABELS = {
    "cash": "现金",
    "crypto": "加密资产",
    "stock": "股票代币",
    "defi": "DeFi",
    "option": "期权",
    "other": "未分类",
}
RISK_LABELS = {"low": "低风险", "medium": "中风险", "high": "高风险"}

RISK_RANK = {name: rank for rank, name in enumerate(RISK_LEVELS)}

# A token whose price is meant to stay at one US dollar.  ``USD*`` / ``SUSD*``
# prefixes cover long-tail wrapped dollars (USDT0, USDC+, USD3, sUSDX...).
STABLECOIN_SYMBOLS = frozenset(
    {
        "USDT",
        "USDC",
        "USDC.E",
        "USDC+",
        "DAI",
        "USDE",
        "USDS",
        "SUSDE",
        "SUSDS",
        "FDUSD",
        "TUSD",
        "USDP",
        "PYUSD",
        "BUSD",
        "GUSD",
        "LUSD",
        "CRVUSD",
        "SUSD",
        "USDT0",
        "CUSDO",
        "SUSDF",
        "HYUSD",
        "SUSDX",
        "THBILL",
    }
)
STABLECOIN_PREFIXES = ("USD", "SUSD")

# Majors only.  The spec says BTC/ETH are medium and everything else is high;
# wrapped / staked ETH forms are treated as ETH for the same reason.
MAINSTREAM_SYMBOLS = frozenset(
    {
        "BTC",
        "WBTC",
        "CBBTC",
        "TBTC",
        "ETH",
        "WETH",
        "STETH",
        "WSTETH",
        "RETH",
        "CBETH",
        "WEETH",
        "EZETH",
    }
)

# xStocks (AAPLx / TSLAx...), Ondo (AAPLon) and similar tokenized equities.
STOCK_TOKEN_BASES = frozenset(
    {
        "AAPL",
        "TSLA",
        "NVDA",
        "MSFT",
        "AMZN",
        "GOOGL",
        "GOOG",
        "META",
        "NFLX",
        "AMD",
        "AVGO",
        "COIN",
        "MSTR",
        "HOOD",
        "PLTR",
        "CRCL",
        "SPY",
        "QQQ",
        "DIA",
        "IWM",
        "VTI",
        "BRK.B",
    }
)
STOCK_TOKEN_SUFFIXES = ("X", "ON")
STOCK_TOKEN_PREFIXES = ("X",)

# Pendle principal / yield tokens and LP receipts.
_PENDLE_PT = ("PT-", "PT_")
_PENDLE_YT = ("YT-", "YT_")
_PENDLE_LP = ("PLP-", "PLP_", "KJLP-", "KJLP_")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _symbol(row: Any) -> str:
    if isinstance(row, dict):
        return _text(row.get("asset") or row.get("symbol") or row.get("name"))
    return _text(getattr(row, "asset", None) or getattr(row, "symbol", None))


def _is_stable_symbol(symbol: str) -> bool:
    text = symbol.upper().strip()
    if not text:
        return False
    if text in STABLECOIN_SYMBOLS:
        return True
    return text.startswith(STABLECOIN_PREFIXES) and text not in {"USUAL"}


def _is_stock_token(symbol: str) -> bool:
    text = symbol.upper().replace(" ", "").strip()
    if not text:
        return False
    for suffix in STOCK_TOKEN_SUFFIXES:
        if text.endswith(suffix) and text[: -len(suffix)] in STOCK_TOKEN_BASES:
            return True
    for prefix in STOCK_TOKEN_PREFIXES:
        if text.startswith(prefix) and text[len(prefix) :] in STOCK_TOKEN_BASES:
            return True
    return False


def _is_pendle(symbol: str, prefixes: tuple[str, ...]) -> bool:
    upper = symbol.upper()
    return upper.startswith(prefixes)


def _is_receipt(symbol: str) -> bool:
    """Aave receipts / debt tokens, Jupiter Earn and Morpho share tokens."""
    if not symbol:
        return False
    folded = symbol.lower()
    if folded.startswith(("variabledebt", "stabledebt", "aeth", "jl")):
        return True
    # aEthWETH / aUSDC: lower-case a + mixed case. All-caps tickers such as
    # ARB must not be mistaken for an Aave receipt.
    if (
        len(symbol) >= 2
        and symbol[0] in ("a", "A")
        and symbol[1].isupper()
        and not symbol.isupper()
    ):
        return True
    # Morpho share symbols are m + a known underlying (mwETH, mUSDC).
    morpho_bases = {"WETH", "USDC", "USDT", "DAI", "WBTC", "USDE"}
    if len(symbol) >= 3 and symbol[0] == "m" and symbol[1:].upper() in morpho_bases:
        return True
    return False


def _is_lp(symbol: str) -> bool:
    upper = symbol.upper()
    if upper.startswith(("LP-", "LP_", "UNI-V", "SLP", "KJLP", "PLP")):
        return True
    return "-LP" in upper or "_LP" in upper


def _risk_up(risk: str) -> str:
    rank = RISK_RANK.get(risk, 1)
    return RISK_LEVELS[min(rank + 1, len(RISK_LEVELS) - 1)]


def classify_holding(row: Any, ctx: dict | None = None) -> tuple[str, str, str]:
    """Classify one flat holding row (dict or ORM-like object).

    Holding rows come from ``snapshot.breakdown.holdings[]``: they carry
    ``asset``, ``source``, ``chain``, ``contract`` and ``covered_by_position``.
    """
    del ctx  # reserved for future context (prices, protocol map)
    symbol = _symbol(row)
    upper = symbol.upper().strip()
    if not upper:
        return "other", "medium", "未识别，按中风险"

    if _is_pendle(upper, _PENDLE_YT):
        return "defi", "high", "Pendle YT 收益代币，价格与收益波动大"
    if _is_pendle(upper, _PENDLE_PT):
        return "defi", "medium", "Pendle PT 本金代币，到期前流动性有限"
    if _is_pendle(upper, _PENDLE_LP) or _is_lp(upper):
        return "defi", "medium", "LP / 理财凭证，依赖协议流动性"

    if _is_stock_token(upper):
        return "stock", "medium", "股票代币，价格随正股波动"

    if _is_stable_symbol(upper):
        return "cash", "low", "稳定币，价格锚定美元"

    covered = bool(row.get("covered_by_position")) if isinstance(row, dict) else False
    if _is_receipt(upper) or covered:
        if any(token in upper for token in ("USDC", "USDT", "DAI", "USDE", "USD3")):
            return "defi", "low", "协议稳定币存款凭证，无借款"
        return "defi", "medium", "协议存款 / 份额凭证，价值依赖协议"

    if upper in MAINSTREAM_SYMBOLS:
        return "crypto", "medium", "主流币（BTC/ETH），波动中等"
    return "crypto", "high", "非主流币，波动与流动性风险高"


def _get(pos: Any, name: str) -> Any:
    if isinstance(pos, dict):
        return pos.get(name)
    return getattr(pos, name, None)


def _details(pos: Any) -> dict:
    raw = _get(pos, "details")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _legs(details: dict, name: str) -> list[dict]:
    raw = details.get(name)
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _leg_symbols(details: dict) -> list[str]:
    symbols: list[str] = []
    for key in ("collateral", "borrow"):
        for leg in _legs(details, key):
            symbol = _text(leg.get("symbol"))
            if symbol:
                symbols.append(symbol)
    return symbols


def _as_float(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _has_debt(pos: Any, details: dict) -> bool:
    if _as_float(_get(pos, "debt_usdt")) > 0:
        return True
    return any(_as_float(leg.get("usd")) > 0 for leg in _legs(details, "borrow"))


def _health_factor(pos: Any, details: dict) -> float | None:
    raw = details.get("health_factor")
    if raw is None:
        raw = _get(pos, "health_factor")
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _collateral_all_stable(details: dict) -> bool:
    legs = _legs(details, "collateral")
    symbols = [_text(leg.get("symbol")) for leg in legs if _text(leg.get("symbol"))]
    return bool(symbols) and all(_is_stable_symbol(symbol) for symbol in symbols)


def _position_text(pos: Any, details: dict) -> str:
    parts = [
        _text(_get(pos, "market_ref")),
        _text(_get(pos, "label")),
        _text(_get(pos, "asset")),
        _text(details.get("type")),
        _text(details.get("name")),
    ]
    parts.extend(_leg_symbols(details))
    return " | ".join(part for part in parts if part).upper()


def _hf_adjust(risk: str, reason: str, hf: float | None) -> tuple[str, str]:
    if hf is None:
        return risk, reason
    if hf < 1.2:
        return "high", f"{reason}；HF {hf:.2f} < 1.2，直接高风险"
    if hf < 1.5:
        return _risk_up(risk), f"{reason}；HF {hf:.2f} < 1.5，自动升一档"
    return risk, reason


def classify_position(pos: Any, ctx: dict | None = None) -> tuple[str, str, str]:
    """Classify one open position (dict or ORM ``Position``)."""
    del ctx
    kind = _text(_get(pos, "kind")).lower()
    protocol = _text(_get(pos, "protocol")).lower()
    details = _details(pos)
    text = _position_text(pos, details)
    hf = _health_factor(pos, details)
    has_debt = _has_debt(pos, details)

    if kind == "option":
        return "option", "high", "期权仓位，杠杆与到期风险"

    if kind != "lending":
        return "other", "medium", "未识别，按中风险"

    if protocol in {"morpho", "kamino"}:
        if has_debt:
            return "defi", "high", "Morpho/Kamino 循环贷，抵押借款放大风险"
        if _collateral_all_stable(details):
            return "defi", "low", f"{protocol.capitalize()} 稳定币存款，无借款"
        return "defi", "medium", f"{protocol.capitalize()} 存款，无借款但非稳定币"

    if protocol == "pendle" or "YT-" in text or "PT-" in text:
        if "YT-" in text:
            risk, reason = "high", "Pendle YT 收益代币，价格波动大"
        else:
            risk, reason = "medium", "Pendle PT 本金代币，到期前流动性有限"
        if has_debt:
            risk = "high" if hf is not None and hf < 1.5 else risk
            reason = f"{reason}；有借款"
        return "defi", risk, reason

    if protocol == "aave":
        if not has_debt:
            if _collateral_all_stable(details):
                return "defi", "low", "Aave 稳定币存款，无借款"
            return "defi", "medium", "Aave 存款，无借款但抵押品非稳定币"
        risk, reason = "medium", "Aave 抵押借款"
        return ("defi", *_hf_adjust(risk, reason, hf))

    if not has_debt:
        if _collateral_all_stable(details):
            return "defi", "low", f"{protocol.capitalize() or '协议'} 稳定币存款，无借款"
        return "defi", "medium", f"{protocol.capitalize() or '协议'} 存款，无借款但非稳定币"

    risk, reason = "medium", "借贷仓位，有借款"
    return ("defi", *_hf_adjust(risk, reason, hf))


def holding_key(row: Any) -> str:
    """Stable key for a flat holding.

    ``chain:contract`` when both are known (survives symbol/source changes),
    otherwise ``symbol:<source>:<symbol>``.  Values are case-folded so a sync
    that changes letter case does not orphan a user override.
    """
    if isinstance(row, dict):
        chain = _text(row.get("chain")).lower()
        contract = _text(row.get("contract")).lower()
        source = _text(row.get("source")).lower() or "unknown"
        symbol = _symbol(row).casefold()
    else:
        chain = _text(getattr(row, "chain", None)).lower()
        contract = _text(getattr(row, "contract", None)).lower()
        source = _text(getattr(row, "source", None)).lower() or "unknown"
        symbol = _symbol(row).casefold()
    if chain and contract:
        return f"{chain}:{contract}"
    if symbol:
        return f"symbol:{source}:{symbol}"
    if contract:
        return f"contract:{contract}"
    return f"symbol:{source}:unknown"


def position_key(pos: Any) -> str:
    """Stable key for a position: ``pos:<id>`` or ``pos:<protocol>:<market>``."""
    raw_id = _get(pos, "id")
    if raw_id not in (None, ""):
        return f"pos:{raw_id}"
    protocol = _text(_get(pos, "protocol")).lower() or "unknown"
    market = (
        _text(_get(pos, "market_ref"))
        or _text(_get(pos, "account_ref"))
        or _text(_get(pos, "label"))
    ).lower()
    return f"pos:{protocol}:{market or 'unknown'}"


def merge_override(
    asset_class: str,
    risk_level: str,
    reason: str,
    override: Any | None,
) -> tuple[str, str, str, bool]:
    """Apply a user override. Partial overrides fall back to the default field."""
    if override is None:
        return asset_class, risk_level, reason, False
    override_class = _text(_get(override, "asset_class")).lower()
    override_risk = _text(_get(override, "risk_level")).lower()
    override_reason = _text(_get(override, "reason"))
    final_class = override_class if override_class in ASSET_CLASSES else asset_class
    final_risk = override_risk if override_risk in RISK_LEVELS else risk_level
    final_reason = override_reason or reason
    return final_class, final_risk, final_reason, True


def resolve_holding(
    row: Any, overrides: dict[str, Any] | None = None
) -> tuple[str, str, str, bool]:
    """Default classification + user override for a holding row."""
    asset_class, risk, reason = classify_holding(row)
    override = (overrides or {}).get(holding_key(row))
    return merge_override(asset_class, risk, reason, override)


def resolve_position(
    pos: Any, overrides: dict[str, Any] | None = None
) -> tuple[str, str, str, bool]:
    """Default classification + user override for a position row."""
    asset_class, risk, reason = classify_position(pos)
    override = (overrides or {}).get(position_key(pos))
    return merge_override(asset_class, risk, reason, override)
