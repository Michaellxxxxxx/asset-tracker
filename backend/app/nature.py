"""Read-only nature/safety classification for pending rows.

The classifier answers one question for every row: is this money actually
leaving the user's control?  It is deliberately conservative—when the evidence
is incomplete the result is ``unknown`` + ``review`` rather than ``safe``.

Nothing in this module writes to the database.  ``classify_nature`` is a pure
function of a pending row plus a :class:`NatureContext`; the API and WeChat
formatter call it while reading, and historical rows are never mutated.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import UserWallet
from .integrations.chain.routes import route_kind, route_kind_from_text

Nature = Literal[
    "exchange_internal",
    "protocol_deposit",
    "protocol_redeem",
    "swap",
    "bridge",
    "self_transfer",
    "external_out",
    "dust",
    "test_data",
    "unknown",
]
Safety = Literal["safe", "review", "none"]

NATURES: tuple[str, ...] = (
    "exchange_internal",
    "protocol_deposit",
    "protocol_redeem",
    "swap",
    "bridge",
    "self_transfer",
    "external_out",
    "dust",
    "test_data",
    "unknown",
)
SAFETIES: tuple[str, ...] = ("safe", "review", "none")

# Safety is a function of nature, not a second independent guess.  This keeps
# the UI badge and the nature text from contradicting each other.
_SAFETY_BY_NATURE: dict[str, str] = {
    "exchange_internal": "safe",
    "protocol_deposit": "safe",
    "protocol_redeem": "safe",
    "swap": "review",
    "bridge": "review",
    "self_transfer": "safe",
    "external_out": "review",
    "dust": "none",
    "test_data": "review",
    "unknown": "review",
}

NATURE_LABELS: dict[str, str] = {
    "exchange_internal": "交易所内部划转",
    "protocol_deposit": "协议存入",
    "protocol_redeem": "协议赎回",
    "swap": "链上兑换",
    "bridge": "跨链转移",
    "self_transfer": "转给自己",
    "external_out": "转给外部地址",
    "dust": "尘埃转账",
    "test_data": "疑似测试数据",
    "unknown": "性质待确认",
}

# Mock identifiers used by seed/fixtures.  A hit is enough to call a row test
# data; the row still needs a human look (review), never "safe".
_MOCK_MARKERS: tuple[str, ...] = (
    "0xbinancewithdraw",
    "0xbinancedeposit",
    "txexternal",
    "txbinancedepositaddr",
    "bybittx1",
    "bybit-out",
    "wd-binance-1",
    "wd-bybit-1",
    "0xold",
)

_EXCHANGE_SOURCES = {
    "binance",
    "binance_simple_earn",
    "gate",
    "gateio",
    "okx",
    "bybit",
    "coinstats",
}

# Raw provider types that mean "inside the exchange", not "withdrawal".
_INTERNAL_TYPE_RE = re.compile(
    r"\b(?:roll[\s_-]?(?:in|out)|earn|savings?|flexible|staking|funding|"
    r"internal(?:\s+transfer)?|transfer)\b|理财|划转|内部转账|活期|定期|申购|赎回",
    re.IGNORECASE,
)
# Rewards are income, not an internal move.  Check the provider raw type before
# the broader internal keywords so "Earn reward" never becomes safe.
_INCOME_TYPE_RE = re.compile(
    r"\b(?:reward|interest|bonus|airdrop|yield|cashback)\b|奖励|利息|空投",
    re.IGNORECASE,
)

_TX_HASH_RE = re.compile(r"0x[a-fA-F0-9]{64}")
_DETAIL_TX_RE = re.compile(r"(?:txid|交易哈希|tx hash)\s*[:：=]?\s*(0x[a-fA-F0-9]{64})", re.IGNORECASE)
_DETAIL_RAW_TYPE_RE = re.compile(
    r"原始类型\s*[:：=]?\s*([^；;，,。]+)", re.IGNORECASE
)
_DETAIL_ADDRESS_RE = re.compile(
    r"(?:对方地址|地址)\s*[:：=]?\s*([^\s；;，,。]+)", re.IGNORECASE
)

_OUT_HINTS = re.compile(
    r"提现|提取|转出|流出|发送|付款|withdraw|sent|\bout\b|roll[\s_-]?out",
    re.IGNORECASE,
)
_IN_HINTS = re.compile(
    r"充值|转入|流入|收到|存入|deposit|\bin\b|roll[\s_-]?in",
    re.IGNORECASE,
)
# Raw operation types that describe a two-sided exchange of assets.
_SWAP_RAW_RE = re.compile(r"\b(?:trade|swap|exchange|buy|sell)\b|兑换|成交|交易", re.IGNORECASE)
_BRIDGE_RAW_RE = re.compile(
    r"\b(?:bridge|cross[\s_-]?chain|relay|meson|butter|cctp|layerzero|"
    r"across|stargate|wormhole|axelar|hyperlane|synapse|hop)\b|跨链|桥",
    re.IGNORECASE,
)

# Native staking moves SOL between the wallet and a stake account the user
# still controls; it is not a transfer to a third party.  Match underscore
# provider types such as STAKE_SOL / UNSTAKE_SOL as well as prose labels.
_NATIVE_STAKE_RE = re.compile(
    r"\b(?:stake(?:[\s_-]?sol)?|delegate(?:[\s_-]?stake)?)\b|质押",
    re.IGNORECASE,
)
_NATIVE_UNSTAKE_RE = re.compile(
    r"\b(?:unstake(?:[\s_-]?sol)?|undelegate(?:[\s_-]?sol)?|"
    r"deactivate(?:[\s_-]?stake)?|withdraw[\s_-]?stake)\b|解除质押",
    re.IGNORECASE,
)

# Keep these aligned with the ingestion-side Helius dust thresholds.  A quote
# is the strongest evidence; the unit thresholds are only used when the quote
# is missing and the asset is a known-value one (native SOL / USD stable).
_DUST_USD_CEILING = Decimal("0.01")
_DUST_NATIVE_SOL = Decimal("0.00005")
_DUST_STABLE = Decimal("0.01")
_SOL_ASSETS = frozenset({"SOL", "WSOL", "SO1111"})
_STABLE_ASSETS = frozenset(
    {"USDT", "USDC", "DAI", "FDUSD", "TUSD", "PYUSD", "USDS", "USDG", "USDE"}
)

_EXCHANGE_LABELS = {
    "binance": "币安",
    "binance_simple_earn": "币安",
    "gate": "Gate",
    "gateio": "Gate",
    "okx": "OKX",
    "bybit": "Bybit",
}

# Independently verified protocol contracts from the read-only event audit.
# This is deliberately a tiny allowlist, not a heuristic: the address must be
# a known protocol contract AND the provider raw type must describe a deposit
# or redemption.  It lets old rows such as #249 (meta was never backfilled)
# display the correct nature without rewriting history.
_KNOWN_PROTOCOL_CONTRACTS: dict[str, str] = {
    "0x2a66f992bf227d2e50ef19edd21503c3c4f3f682": "3Jane",
}


@dataclass(frozen=True)
class NatureContext:
    """Addresses the user controls, already normalised for comparison.

    ``exchange_addresses`` is kept separate so an exchange deposit address can
    still be recognised as a self-transfer; callers may leave it empty.
    """

    own_addresses: frozenset[str] = frozenset()
    exchange_addresses: frozenset[str] = frozenset()


@dataclass(frozen=True)
class NatureResult:
    nature: str
    safety: str
    explain: str


def normalize_address(value: object) -> str:
    """Canonical comparison form: trim, lowercase EVM addresses.

    EIP-55 checksummed input and lowercase input therefore compare equal.  A
    non-EVM address is only trimmed/casefolded if it is hex-like; Solana/base58
    is case-sensitive, so leave its body untouched.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if text.lower().startswith("0x"):
        return text.lower()
    return text


def build_context(db: Session, user_id: int | None) -> NatureContext:
    """Load the user's wallets for read-only classification.

    Only the address column is selected.  No row is inserted or updated.
    """
    if user_id is None or db is None:
        return NatureContext()
    rows = db.scalars(
        select(UserWallet.address).where(UserWallet.user_id == user_id)
    ).all()
    own = frozenset(
        address for address in (normalize_address(row) for row in rows) if address
    )
    return NatureContext(own_addresses=own)


def classify_nature(
    pending: Any,
    ctx: NatureContext | None = None,
) -> NatureResult:
    """Classify one pending row without touching the database.

    Order matters: mock data wins, then paired swap/bridge rows, then
    non-outflows, then exchange-internal labels, then address ownership,
    dust, native staking, protocol evidence, and finally the
    external/unknown split.
    """
    context = ctx or NatureContext()
    meta = _pending_meta(pending)
    direction = _direction(pending, meta)
    counterparty = _counterparty(pending, meta)
    txid = _txid(pending)
    raw_type = _raw_type(pending)
    source = str(getattr(pending, "source", "") or "").strip().casefold()
    category = str(
        getattr(pending, "suggested_category", None)
        or getattr(pending, "final_category", None)
        or ""
    ).strip().casefold()
    if _looks_like_test_data(pending, counterparty):
        return _result(
            "test_data",
            "疑似测试数据（mock 占位），不是真实资金转出，请核对",
        )

    # A swap/bridge is one two-sided event.  Provider feeds often surface both
    # legs (out + in) and a naive mapping would create a fake withdrawal *and* a
    # fake deposit.  Recognise the pair before looking at direction so the
    # inflow leg is not reported as an outflow either.
    pair_kind = str(meta.get("pair_kind") or "").strip().casefold()
    route_hint = _route_hint(pending, meta, counterparty)
    if _is_chain_context(source, txid, counterparty):
        is_bridge = (
            pair_kind == "bridge"
            or route_hint == "bridge"
            or bool(_BRIDGE_RAW_RE.search(raw_type))
        )
        is_swap = (
            pair_kind in {"swap", "bridge"}
            or route_hint in {"swap", "bridge"}
            or category == "swap"
            or bool(_SWAP_RAW_RE.search(raw_type))
        )
        # A protocol deposit/redeem label is stronger than a generic router name
        # (e.g. adding liquidity through a DEX router) and stays with the
        # protocol branch below.
        if category in {"defi_deposit", "defi_redeem"} and pair_kind not in {
            "swap",
            "bridge",
        }:
            is_swap = False
            is_bridge = False
        if is_swap or is_bridge:
            nature = "bridge" if is_bridge else "swap"
            if direction == "in":
                return _result(
                    nature,
                    _swap_explanation(nature, meta, pending, "in", paired=False),
                    "none",
                )
            if direction is None:
                return _result(
                    nature,
                    _swap_explanation(nature, meta, pending, None, paired=False),
                    "review",
                )
            paired = _same_tx_counterleg(pending, meta, txid)
            return _result(
                nature,
                _swap_explanation(nature, meta, pending, "out", paired=paired),
                "safe" if paired else "review",
            )

    # An inflow has no outflow risk.  Preserve any richer enrichment sentence
    # instead of replacing it with a generic one.
    if direction != "out":
        existing = _meta_explain(meta)
        if direction == "in":
            # A confirmed inflow cannot be an outflow; "none" means safety is
            # not applicable, not that the row was proven safe.
            if existing:
                return _result("unknown", existing, "none")
            return _result("unknown", "流入记录，未发现资金外流", "none")
        # A row without a direction is genuinely under-evidenced.  Keep the
        # review flag instead of silently treating it as benign.
        if existing:
            return _result("unknown", existing, "review")
        return _result(
            "unknown",
            "未提供资金方向，无法确认是否外流，请核对",
            "review",
        )

    if _is_exchange_internal(pending, meta, source, raw_type, counterparty, txid):
        label = _exchange_label(source)
        return _result(
            "exchange_internal",
            f"{label}理财/内部划转，资金仍在你的账户",
        )

    own = context.own_addresses | context.exchange_addresses
    if counterparty and normalize_address(counterparty) in own:
        return _result(
            "self_transfer",
            f"转入你自己的钱包 {_mask_address(counterparty)}",
        )

    # Historical Helius rows predate the parser-level dust filter.  Classifying
    # them as external outflows creates a review item for every 1e-9 SOL leg.
    if _is_dust_outflow(pending):
        amount = _pending_amount(pending)
        detail = f"金额 {amount} 低于尘埃阈值" if amount else "金额低于尘埃阈值"
        return _result(
            "dust",
            f"{detail}，视为链上噪声，不是有效资金外流",
        )

    # Native staking has a provider-side "stake" operation and a stake account
    # as counterparty.  The asset stays under the user's control, so this is a
    # protocol deposit/redeem rather than an external transfer.
    if _is_native_stake(raw_type):
        if _NATIVE_UNSTAKE_RE.search(raw_type):
            return _result(
                "protocol_redeem",
                "解除 SOL 原生质押，资产回到你的钱包",
            )
        return _result(
            "protocol_deposit",
            "SOL 原生质押，资产仍在你的质押账户",
        )

    received = _tokens(meta.get("received_tokens"))

    # A swap is a two-sided transaction, not a one-sided theft, but without the
    # counter-leg we cannot call it safe.
    if category == "swap":
        return _result(
            "swap",
            "同 tx 兑换/转换，未发现单边外流，请核对",
        )

    known_protocol = _KNOWN_PROTOCOL_CONTRACTS.get(normalize_address(counterparty))
    if known_protocol and _protocol_action(raw_type, category):
        nature = "protocol_redeem" if _is_redeem(raw_type, category) else "protocol_deposit"
        existing = _meta_explain(meta)
        if existing:
            return _result(nature, existing)
        return _result(
            nature,
            _protocol_explanation(
                nature,
                {"application": known_protocol},
                pending,
            ),
        )
    if category in {"defi_deposit", "defi_redeem"} or (received and _is_contract(meta)):
        nature = "protocol_redeem" if category == "defi_redeem" else "protocol_deposit"
        existing = _meta_explain(meta)
        if existing:
            return _result(nature, existing)
        return _result(nature, _protocol_explanation(nature, meta, pending))

    if counterparty:
        return _result(
            "external_out",
            f"转给未知外部地址 {_mask_address(counterparty)}，请核对",
        )

    return _result(
        "unknown",
        "证据不足，无法确认这笔资金去向，请核对",
    )


def classify_exchange_internal(pending: Any) -> NatureResult | None:
    """Return an ``exchange_internal`` result when provider evidence proves it.

    ``classify_nature`` returns early for inflows because they carry no outflow
    risk, so a historical Roll In row stays ``unknown`` even though the provider
    raw type says the money never left the exchange.  This read-only helper
    applies the same evidence without a direction gate; it returns ``None``
    whenever the row is not provably internal (real withdrawal, reward, …).
    """
    meta = _pending_meta(pending)
    source = str(getattr(pending, "source", "") or "").strip().casefold()
    if not _is_exchange_internal(
        pending,
        meta,
        source,
        _raw_type(pending),
        _counterparty(pending, meta),
        _txid(pending),
    ):
        return None
    label = _exchange_label(source)
    return _result(
        "exchange_internal",
        f"{label}理财/内部划转，资金仍在你的账户",
    )


def format_nature_tag(result: NatureResult) -> str:
    """Short badge used by WeChat: nature + safety in one phrase."""
    label = NATURE_LABELS.get(result.nature, "性质待确认")
    if result.safety == "safe":
        return f"性质：✅ 已识别 · 非外流（{label}）"
    if result.safety == "review":
        return f"性质：⚠️ 请核对（{label}）"
    # "none" is used for confirmed inflows, where there is no outflow risk.
    # Keep a neutral badge so every WeChat card still carries a nature line.
    return f"性质：ℹ️ 非外流（{label}）"


def _result(
    nature: str,
    explain: str,
    safety: str | None = None,
) -> NatureResult:
    resolved = safety or _SAFETY_BY_NATURE.get(nature, "review")
    return NatureResult(nature=nature, safety=resolved, explain=explain)


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


def _meta_explain(meta: dict[str, Any]) -> str | None:
    value = meta.get("explain")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _direction(pending: Any, meta: dict[str, Any]) -> str | None:
    raw = str(getattr(pending, "direction", "") or "").strip().casefold()
    if raw in {"in", "out"}:
        return raw
    blob = " ".join(
        str(part or "")
        for part in (
            getattr(pending, "title", ""),
            getattr(pending, "detail", ""),
            getattr(pending, "external_id", ""),
            meta.get("operation"),
        )
    )
    if _OUT_HINTS.search(blob):
        return "out"
    if _IN_HINTS.search(blob):
        return "in"
    return None


def _counterparty(pending: Any, meta: dict[str, Any]) -> str:
    # Enrich always stores the canonical address under counterparty_address.
    # Checking the legacy keys first let a detail label ("…自有地址→transfer")
    # win over the real address and turned #305 into an external outflow.
    for key in ("counterparty_address", "counterparty", "to", "address"):
        value = meta.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    detail = str(getattr(pending, "detail", "") or "")
    # The detail can contain prose such as "已知自有地址→transfer" before the
    # actual "地址 <addr>" field.  Skip arrow/punctuation labels instead of
    # accepting the first textual match.
    for match in _DETAIL_ADDRESS_RE.finditer(detail):
        value = match.group(1).strip().strip("，,。；;")
        if value and value[0] not in "→>-":
            return value
    # enrich's historical detail format includes a 0x address without the
    # "地址" label; accept that too, but never a 64-char tx hash.
    for candidate in re.findall(r"0x[a-fA-F0-9]{8,64}", detail):
        if len(candidate) != 66:
            return candidate
    return ""


def _txid(pending: Any) -> str:
    external_id = str(getattr(pending, "external_id", "") or "")
    match = _TX_HASH_RE.search(external_id)
    if match:
        return match.group(0)
    if re.fullmatch(r"0x[a-fA-F0-9]{64}", external_id):
        return external_id
    detail = str(getattr(pending, "detail", "") or "")
    match = _DETAIL_TX_RE.search(detail) or _TX_HASH_RE.search(detail)
    return match.group(1) if match and match.lastindex else (match.group(0) if match else "")


def _raw_type(pending: Any) -> str:
    detail = str(getattr(pending, "detail", "") or "")
    match = _DETAIL_RAW_TYPE_RE.search(detail)
    if match:
        return match.group(1).strip()
    # Some rows carry the provider type in the external id only.
    external_id = str(getattr(pending, "external_id", "") or "")
    return external_id.replace("_", " ").replace("-", " ").strip()


def _is_exchange_internal(
    pending: Any,
    meta: dict[str, Any],
    source: str,
    raw_type: str,
    counterparty: str,
    txid: str,
) -> bool:
    raw = raw_type.casefold()
    # Rewards/interest can be labelled "transfer" by a provider; inspect the
    # row text too so income never becomes a safe internal transfer.
    income_blob = " ".join(
        (
            raw,
            str(getattr(pending, "title", "") or ""),
            str(getattr(pending, "detail", "") or ""),
        )
    )
    if _INCOME_TYPE_RE.search(income_blob):
        return False
    if not _INTERNAL_TYPE_RE.search(raw):
        return False
    if source not in _EXCHANGE_SOURCES and not meta.get("is_exchange"):
        return False
    # A transaction hash or a counterparty address means this left the
    # exchange's internal book.  Self-transfer to the user's own wallet is
    # handled by address ownership, not by the provider label.
    if txid or counterparty:
        return False
    return True


def _protocol_action(raw_type: str, category: str) -> bool:
    if category in {"defi_deposit", "defi_redeem"}:
        return True
    return bool(
        re.search(
            r"\b(?:deposit|supply|stake|mint|addliquidity|redeem|unstake|withdraw|remove)\b|"
            r"存入|赎回|质押|申购",
            raw_type,
            re.IGNORECASE,
        )
    )


def _is_redeem(raw_type: str, category: str) -> bool:
    if category == "defi_redeem":
        return True
    return bool(
        re.search(
            r"\b(?:redeem|unstake|withdraw|remove)\b|赎回|提取",
            raw_type,
            re.IGNORECASE,
        )
    )


def _route_hint(pending: Any, meta: dict[str, Any], counterparty: str) -> str | None:
    """Known route address, then Blockscout/provider label hints."""
    direct = route_kind(counterparty)
    if direct:
        return direct
    return route_kind_from_text(
        meta.get("counterparty_label"),
        meta.get("name"),
        meta.get("application"),
        meta.get("public_tags"),
        meta.get("operation"),
        getattr(pending, "title", ""),
    )


def _is_chain_context(source: str, txid: str, counterparty: str) -> bool:
    """Exchange book entries are not on-chain swaps; a tx/address makes it chain."""
    return source not in _EXCHANGE_SOURCES or bool(txid or counterparty)


def _positive_amount(value: Any) -> bool:
    try:
        return Decimal(str(value)) > 0
    except (InvalidOperation, ValueError):
        return False


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _is_dust_outflow(pending: Any) -> bool:
    """True for a positive out leg whose value is below the dust threshold.

    This mirrors the parser-side Helius filter for rows that entered the
    database before that filter existed.  Without a USD quote we only trust
    assets whose unit value is known (SOL and USD stables); an arbitrary token
    amount is left as review rather than hidden as dust.
    """
    amount = _decimal_or_none(getattr(pending, "amount_raw", None))
    if amount is None or amount <= 0:
        return False
    quote = _decimal_or_none(getattr(pending, "amount_usdt", None))
    if quote is not None:
        return quote <= _DUST_USD_CEILING
    asset = str(getattr(pending, "asset", "") or "").strip().upper()
    if asset in _SOL_ASSETS:
        return amount < _DUST_NATIVE_SOL
    if asset in _STABLE_ASSETS:
        return amount < _DUST_STABLE
    return False


def _is_native_stake(raw_type: str) -> bool:
    raw = str(raw_type or "").strip()
    return bool(_NATIVE_STAKE_RE.search(raw) or _NATIVE_UNSTAKE_RE.search(raw))


def _paired_received(meta: dict[str, Any], *, same_tx_only: bool = True) -> list[dict[str, Any]]:
    if same_tx_only and meta.get("paired_same_tx") is not True:
        return []
    return [
        leg
        for leg in _tokens(meta.get("paired_legs"))
        if str(leg.get("direction") or "").strip().casefold() == "in"
        and _positive_amount(leg.get("amount"))
    ]


def _same_tx_counterleg(pending: Any, meta: dict[str, Any], txid: str) -> bool:
    """True only when a positive counter-leg is proven to be in the same tx.

    Two independent pieces of evidence count: sync's same-tx pairing
    (``paired_legs`` + ``paired_same_tx``) and enrich's ``received_tokens``,
    which are read from the same tx.  A near-window bridge pairing is *not*
    same-tx evidence and therefore stays ``review``.
    """
    paired = _paired_received(meta, same_tx_only=True)
    if paired:
        return True
    received = [token for token in _tokens(meta.get("received_tokens")) if _positive_amount(token.get("amount"))]
    if not received:
        return False
    meta_tx = str(meta.get("tx_hash") or "").strip().casefold()
    if txid and meta_tx and txid.strip().casefold() != meta_tx:
        return False
    return True


def _swap_explanation(
    nature: str,
    meta: dict[str, Any],
    pending: Any,
    direction: str | None,
    *,
    paired: bool,
) -> str:
    noun = "跨链转移" if nature == "bridge" else "链上兑换"
    sent = _join_tokens(_tokens(meta.get("sent_tokens")))
    received = _join_tokens(_tokens(meta.get("received_tokens")))
    if paired and not received:
        received = _join_tokens(_paired_received(meta, same_tx_only=True))
    if not sent:
        sent = _pending_amount(pending)
    if direction == "in":
        amount = received or sent
        suffix = f"（收到 {amount}）" if amount else ""
        return f"{noun}流入腿{suffix}，未发现资金外流"
    if direction is None:
        return f"{noun}记录，未提供资金方向，无法确认是否单边外流，请核对"
    sent_text = f"转出 {sent}" if sent else "转出资产"
    if paired:
        if received:
            return f"{noun}：{sent_text}，同 tx 收到 {received}，非单边外流"
        return f"{noun}：{sent_text}，同 tx 存在回流腿，非单边外流"
    return f"{noun}：{sent_text}，缺少同 tx 回流证据，请核对"


def _exchange_label(source: str) -> str:
    return _EXCHANGE_LABELS.get(source, "交易所")


def _looks_like_test_data(pending: Any, counterparty: str) -> bool:
    blob = " ".join(
        str(part or "")
        for part in (
            getattr(pending, "external_id", ""),
            getattr(pending, "txid", ""),
            getattr(pending, "detail", ""),
            getattr(pending, "title", ""),
            counterparty,
        )
    ).casefold()
    return any(marker in blob for marker in _MOCK_MARKERS)


def _is_contract(meta: dict[str, Any]) -> bool:
    return meta.get("is_contract") is True


def _tokens(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _protocol_explanation(nature: str, meta: dict[str, Any], pending: Any) -> str:
    protocol = str(meta.get("application") or "").strip()
    if not protocol:
        # Derive a brand from "3Jane USD3" without a separate dependency on
        # enrich's private helpers.
        for token in _tokens(meta.get("received_tokens")) + _tokens(meta.get("sent_tokens")):
            name = str(token.get("name") or "").strip()
            symbol = str(token.get("symbol") or "").strip()
            if name and symbol:
                stripped = re.sub(
                    rf"\s*{re.escape(symbol)}\s*$", "", name, flags=re.IGNORECASE
                ).strip(" ·-—:：")
                if stripped and len(stripped) <= 24:
                    protocol = stripped
                    break
    sent = _join_tokens(_tokens(meta.get("sent_tokens")))
    received = _join_tokens(_tokens(meta.get("received_tokens")))
    if not sent:
        sent = _pending_amount(pending)
    gap = " " if protocol else ""
    if nature == "protocol_redeem":
        lead = f"从 {protocol}" if protocol else "从协议"
        if sent and received:
            return f"{lead}{gap}赎回 {sent}，同时收到 {received}"
        if received:
            return f"{lead}{gap}赎回，收到 {received}"
        return f"{lead}{gap}赎回，资金回到你的钱包"
    lead = f"向 {protocol}" if protocol else "向协议"
    if sent and received:
        return f"{lead}{gap}存入 {sent}，同时收到 {received} 凭证"
    if sent:
        return f"{lead}{gap}存入 {sent}，资金仍在你的 DeFi 仓位"
    return f"{lead}{gap}存入，资金仍在你的 DeFi 仓位"


def _join_tokens(tokens: Iterable[dict[str, Any]]) -> str:
    parts: list[str] = []
    for token in tokens:
        symbol = str(token.get("symbol") or "").strip()
        amount = _format_amount(token.get("amount"))
        if amount and symbol:
            parts.append(f"{amount} {symbol}")
    return "、".join(parts)


def _format_amount(value: Any) -> str:
    if value is None or value == "":
        return ""
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return ""
    if abs(number) >= 1:
        return f"{number:,.2f}"
    # 6 decimals turned 1e-9 SOL into "0" in the dust explanation.  Keep enough
    # precision for sub-cent Solana amounts while still trimming trailing zeros.
    text = f"{number:.12f}".rstrip("0").rstrip(".")
    return text or "0"


def _pending_amount(pending: Any) -> str:
    amount = getattr(pending, "amount_raw", None)
    asset = str(getattr(pending, "asset", "") or "").strip()
    text = _format_amount(amount)
    return f"{text} {asset}".strip() if text else ""


def _mask_address(address: str) -> str:
    text = address.strip()
    if len(text) <= 12:
        return text
    if text.lower().startswith("0x"):
        text = text.lower()
        return f"{text[:6]}…{text[-4:]}"
    return f"{text[:4]}…{text[-4:]}"
