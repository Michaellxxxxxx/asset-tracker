"""Rule-based attribution for normalized transfers.

Categories: deposit, withdraw, airdrop, kol_reward, transfer.
Each suggestion carries a confidence in [0, 1] and a Chinese reason.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from .integrations.types import NormalizedTransfer

# EVM txids sometimes arrive as ``<hash>-<logIndex>`` or ``<hash>:<index>``.
_TXID_SUFFIX = re.compile(r"[-:][0-9a-f]{1,8}$", re.IGNORECASE)
_LINKED_REASON = "交易所↔自有钱包同一笔链上转账→transfer"

CATEGORIES = (
    "deposit",
    "withdraw",
    "airdrop",
    "kol_reward",
    "transfer",
    "defi_deposit",
    "defi_redeem",
    "swap",
    # Suggestion-only pseudo category: an on-chain outflow whose operation is
    # unknown/insufficient.  It is deliberately *not* a ledger kind.
    "chain_out",
)

# Dust / claim heuristic. Amounts at or below this (in token units, before pricing)
# from an unknown contract interaction are treated as airdrops.
AIRDROP_AMOUNT_THRESHOLD = Decimal("50")

_KOL_HINTS = ("kol", "reward", "referral", "返佣", "推荐", "邀请")
_CLAIM_HINTS = ("claim", "airdrop", "air drop", "空投")
_EXCHANGE_SOURCES = {"binance", "bitget", "bybit", "kraken", "gate"}

# Exchange Earn / savings moves are internal wallet transfers, never withdrawals.
# CoinStats labels Binance's automatic Earn roll as ``Roll In`` / ``Roll Out``.
_ROLL_TRANSFER = re.compile(r"\broll[\s_-]?(?:in|out)\b")
_EARN_TRANSFER = re.compile(r"\b(?:earn|savings|flexible|auto[\s_-]?subscribe)\b")
# Interest / reward rows are income, not an internal principal move.
_EARN_INCOME = re.compile(r"\b(?:reward|interest|yield|bonus)\b")

# Semantic operation types.  A raw ``trade``/``swap`` is never a withdrawal or
# a deposit, even when the provider only gives us one leg of the exchange.
_SWAP_TYPE = re.compile(r"\b(?:trade|swap|exchange|buy|sell)\b|兑换|成交|交易")
_BRIDGE_TYPE = re.compile(
    r"\b(?:bridge|cross[\s_-]?chain|relay|meson|butter|cctp|layerzero|"
    r"across|stargate|wormhole)\b|跨链|桥"
)
_DEPOSIT_TYPE = re.compile(r"\b(?:deposit|supply|stake|mint|add[\s_-]?liquidity)\b")
_REDEEM_TYPE = re.compile(r"\b(?:redeem|unstake|remove|burn|exit|withdraw_liquidity)\b")
_WITHDRAW_TYPE = re.compile(r"\b(?:withdraw|withdrawal|send|transfer|out)\b")
_RECEIVE_TYPE = re.compile(r"\b(?:receive|receiving|receive_native)\b")


@dataclass(frozen=True)
class Attribution:
    category: str
    confidence: float
    reason: str


def normalize_txid(txid: str | None) -> str:
    """Lower-case bare hash. A trailing ``-logIndex`` / ``:index`` is dropped."""
    if not txid:
        return ""
    text = txid.strip().lower()
    return _TXID_SUFFIX.sub("", text)


def linked_transfer_txids(transfers: list[NormalizedTransfer], own_addresses: set[str]) -> set[str]:
    """Txids that are an exchange leg and a self-custody leg of one on-chain move.

    A txid qualifies when (a) an exchange withdrawal's address is one of the
    user's own wallets, or (b) the same txid appears on both an exchange record
    and a chain record. Both legs then attribute as ``transfer``.
    """
    owned = {addr.lower() for addr in own_addresses if addr}
    exchange: dict[str, list[NormalizedTransfer]] = {}
    chain: set[str] = set()
    linked: set[str] = set()
    for transfer in transfers:
        txid = normalize_txid(transfer.txid)
        if not txid:
            continue
        if transfer.source in _EXCHANGE_SOURCES:
            exchange.setdefault(txid, []).append(transfer)
            if (
                transfer.direction == "out"
                and transfer.address
                and transfer.address.lower() in owned
            ):
                linked.add(txid)
        else:
            chain.add(txid)
    linked.update(set(exchange) & chain)
    return linked


def attribute(
    transfer: NormalizedTransfer,
    *,
    own_addresses: set[str] | None = None,
    linked_txids: set[str] | None = None,
) -> Attribution:
    """Suggest a ledger category for one transfer."""
    owned = {addr.lower() for addr in (own_addresses or set()) if addr}
    blob = " ".join(
        part.lower()
        for part in (transfer.note, transfer.raw_type, transfer.source, transfer.asset)
        if part
    )

    if any(hint in blob for hint in _KOL_HINTS):
        return Attribution("kol_reward", 0.86, "备注/来源含 KOL/reward/referral → kol_reward")

    txid = normalize_txid(transfer.txid)
    if txid and linked_txids and txid in linked_txids:
        return Attribution("transfer", 0.9, _LINKED_REASON)

    counterparty = (transfer.address or "").lower()
    # An exchange withdrawal paid straight to a bound wallet is an internal move.
    if counterparty and counterparty in owned and (
        transfer.source not in _EXCHANGE_SOURCES or transfer.direction == "out"
    ):
        reason = (
            _LINKED_REASON
            if transfer.source in _EXCHANGE_SOURCES
            else "链上流入来自已知自有地址→transfer"
        )
        return Attribution("transfer", 0.9, reason)

    if transfer.source in _EXCHANGE_SOURCES:
        raw_type = transfer.raw_type or ""
        lowered = raw_type.lower()
        is_internal_move = bool(
            _ROLL_TRANSFER.search(lowered) or _EARN_TRANSFER.search(lowered)
        ) and not _EARN_INCOME.search(lowered)
        if is_internal_move:
            label = "币安" if transfer.source == "binance" else "交易所"
            return Attribution(
                "transfer",
                0.9,
                f"{label}理财划转（{raw_type}）→transfer（内部转账）",
            )
        if _SWAP_TYPE.search(lowered):
            return Attribution(
                "trade",
                0.8,
                f"交易所成交记录（{raw_type}）→trade（非提现/充值）",
            )
        if transfer.direction == "in" or lowered in {"deposit", "in"}:
            return Attribution("deposit", 0.93, "交易所充值记录→deposit")
        return Attribution("withdraw", 0.93, "交易所提现记录→withdraw")

    if any(hint in blob for hint in _CLAIM_HINTS) or (
        transfer.direction == "in"
        and transfer.amount <= AIRDROP_AMOUNT_THRESHOLD
        and transfer.asset not in {"USDT", "USDC", "BTC", "ETH", "SOL"}
    ):
        return Attribution("airdrop", 0.72, "小额未知代币/合约 claim → airdrop")

    # Zerion's own operation is already a protocol hint: a DeFi deposit is an
    # outflow with ``operation_type=deposit``. This is the offline fallback when
    # Blockscout is unavailable; the enricher refines it with same-tx token legs.
    if transfer.source not in _EXCHANGE_SOURCES:
        raw_type = transfer.raw_type or ""
        lowered = raw_type.strip().casefold()
        semantic = f"{lowered} {transfer.note or ''}".casefold()

        # A swap/bridge (or a trade-like raw type) is an exchange of assets.  It
        # must never be booked as a one-sided withdrawal/deposit.
        if _SWAP_TYPE.search(lowered) or _BRIDGE_TYPE.search(semantic):
            label = "跨链/兑换" if _BRIDGE_TYPE.search(semantic) else "兑换"
            return Attribution(
                "swap",
                0.72,
                f"链上{label}记录（{raw_type or '交易'}）→swap（非提现/充值）",
            )

        if transfer.direction == "out":
            if _DEPOSIT_TYPE.search(lowered):
                return Attribution(
                    "defi_deposit",
                    0.68,
                    f"链上流出且操作标记为 {raw_type} → 协议存入（非提现）",
                )
            if _REDEEM_TYPE.search(lowered):
                return Attribution(
                    "defi_redeem",
                    0.68,
                    f"链上流出且操作标记为 {raw_type} → 协议赎回（非提现）",
                )
            if _WITHDRAW_TYPE.search(lowered):
                return Attribution(
                    "withdraw",
                    0.55,
                    f"链上流出（原始类型 {raw_type}），暂记为提现（低置信）",
                )
            detail = f"原始类型 {raw_type}" if raw_type else "无 operation_type"
            return Attribution(
                "chain_out",
                0.4,
                f"链上流出（{detail}），无法确认提现 → 待核实的链上转出",
            )

        if transfer.direction == "in":
            if _RECEIVE_TYPE.search(lowered):
                return Attribution("deposit", 0.6, f"链上接收（{raw_type}）→充值（待核实）")
            if _DEPOSIT_TYPE.search(lowered):
                return Attribution("deposit", 0.55, f"链上流入（{raw_type}）→充值（待核实）")
            return Attribution(
                "deposit",
                0.5,
                f"链上流入（{raw_type or '无类型'}），暂记为充值（待核实）",
            )

    if transfer.direction == "in":
        return Attribution("deposit", 0.5, "链上流入，暂记为充值（待核实）")
    return Attribution(
        "chain_out",
        0.4,
        "链上流出，缺少 operation_type/对手方证据 → 待核实的链上转出",
    )
