"""Parse a short confirm command. Unknown text never touches the database."""

from __future__ import annotations

import re
from dataclasses import dataclass

from ...models import LEDGER_KINDS

_CATEGORY_ZH = {
    "空投": "airdrop",
    "充值": "deposit",
    "入金": "deposit",
    "提现": "withdraw",
    "出金": "withdraw",
    "划转": "transfer",
    "转账": "transfer",
    "交易": "trade",
    "成交": "trade",
    "资金费": "funding",
    "kol": "kol_reward",
    "其他": "other",
    "存入": "defi_deposit",
    "协议存入": "defi_deposit",
    "defi": "defi_deposit",
    "赎回": "defi_redeem",
    "协议赎回": "defi_redeem",
    "兑换": "swap",
    "swap": "swap",
}

_USAGE = (
    "用法：确认 #123 空投，或 123 充值；review 行需回复「确认入账 #123」二次确认。"
    "类别：空投/充值/提现/划转/交易/资金费/KOL/其他/存入/赎回/兑换。"
)

_CONFIRM = re.compile(
    r"^\s*(确认入账|确认|入账)?\s*#?\s*(\d+)\s*(?:确认)?\s*([A-Za-z_\u4e00-\u9fff]+)?\s*$"
)


@dataclass(frozen=True)
class ParsedCommand:
    kind: str  # confirm | help | unknown
    pending_id: int | None = None
    category: str | None = None
    reply: str = ""
    # "确认入账 #123": explicit second confirmation for a review row.
    strong: bool = False


def parse_command(text: str) -> ParsedCommand:
    raw = (text or "").strip()
    if not raw:
        return ParsedCommand(kind="unknown", reply=_USAGE)
    folded = raw.casefold()
    if folded in {"帮助", "help", "?"}:
        return ParsedCommand(kind="help", reply=_USAGE)

    match = _CONFIRM.match(raw)
    if match is None:
        return ParsedCommand(kind="unknown", reply=_USAGE)
    # Bare number without a verb or a category is not a confirm.
    verb = match.group(1)
    has_verb = verb is not None
    category_raw = match.group(3)
    if not has_verb and not category_raw:
        return ParsedCommand(kind="unknown", reply=_USAGE)

    category = _normalize_category(category_raw) if category_raw else None
    if category_raw and category is None:
        return ParsedCommand(kind="unknown", reply=f"不认识的类别。{_USAGE}")
    return ParsedCommand(
        kind="confirm",
        pending_id=int(match.group(2)),
        category=category,
        reply="",
        strong=verb == "确认入账",
    )


def _normalize_category(raw: str) -> str | None:
    key = raw.strip()
    mapped = _CATEGORY_ZH.get(key) or _CATEGORY_ZH.get(key.casefold())
    if mapped:
        return mapped
    lowered = key.casefold()
    if lowered in LEDGER_KINDS:
        return lowered
    return None
