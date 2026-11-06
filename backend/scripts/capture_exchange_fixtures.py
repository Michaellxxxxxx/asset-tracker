"""Capture read-only ccxt payloads for the live-exchange fixtures.

Usage (from ``backend/``)::

    python scripts/capture_exchange_fixtures.py --exchange gate
    python scripts/capture_exchange_fixtures.py --exchange binance

Calls the same windowed reads as ``CCXTAdapter`` and writes sanitized JSON to
``tests/fixtures/exchanges_live/``. Nothing here places an order, withdraws, or
transfers. The script refuses to finish if any configured key or secret survives
in the output, and it prints only 通过 or 失败 — never a secret.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import exchange_credentials, settings  # noqa: E402
from app.integrations.exchanges.ccxt_adapter import CCXTAdapter  # noqa: E402

OUT = ROOT / "tests" / "fixtures" / "exchanges_live"
_KEEP = ("currency", "amount", "timestamp", "status", "type", "fee", "symbol", "side", "price", "cost", "datetime")


def _fake_address(index: int, original: str) -> str:
    if original.startswith("0x") and len(original) >= 42:
        digest = hashlib.sha256(f"addr-{index}".encode()).hexdigest()
        return "0x" + digest[:40]
    return f"REDACTED-{index}"


def _fake_txid(index: int) -> str:
    return hashlib.sha256(f"txid-{index}".encode()).hexdigest()


def _sanitize(rows: list[dict]) -> list[dict]:
    cleaned: list[dict] = []
    for index, row in enumerate(rows, start=1):
        item = {key: row.get(key) for key in _KEEP if key in row}
        for field in ("address", "addressTo", "addressFrom", "tag", "memo"):
            if row.get(field):
                item[field] = _fake_address(index, str(row[field]))
        if row.get("txid"):
            item["txid"] = _fake_txid(index)
        item["id"] = f"id-{index}"
        if row.get("order"):
            item["order"] = f"order-{index}"
        # ``info`` is the venue's raw payload and is dropped on purpose.
        cleaned.append(item)
    return cleaned


def _top_balances(raw: dict) -> dict:
    totals = raw.get("total") if isinstance(raw, dict) else {}
    ranked = sorted(
        ((asset, amount) for asset, amount in (totals or {}).items()),
        key=lambda pair: float(pair[1] or 0),
        reverse=True,
    )[:12]
    return {"total": {asset: amount for asset, amount in ranked}}


def _secrets() -> list[str]:
    values: list[str] = []
    for cred in exchange_credentials().values():
        values.extend(value for value in cred.values() if value)
    for name in (
        "binance_api_key",
        "binance_api_secret",
        "gate_api_key",
        "gate_api_secret",
        "zerion_api_key",
        "alchemy_api_key",
        "helius_api_key",
    ):
        raw = getattr(settings, name, "") or ""
        if raw:
            values.append(raw)
    return values


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--exchange", required=True, choices=("gate", "binance"))
    args = parser.parse_args()
    creds = exchange_credentials().get(args.exchange)
    if not creds:
        print("失败")
        return 1
    adapter = CCXTAdapter(args.exchange, creds)
    since = settings.sync_since
    OUT.mkdir(parents=True, exist_ok=True)
    from app.integrations.exchanges.ccxt_adapter import _profile

    balance_raw = adapter._client.fetch_balance()
    profile = _profile(args.exchange)
    deposits = adapter._page_window("fetch_deposits", since, datetime.now(timezone.utc), profile)
    withdrawals = adapter._page_window("fetch_withdrawals", since, datetime.now(timezone.utc), profile)
    trades = adapter.fetch_trades(since)
    payloads = {
        "balances": _top_balances(balance_raw if isinstance(balance_raw, dict) else {}),
        "deposits": _sanitize(deposits),
        "withdrawals": _sanitize(withdrawals),
        "trades": _sanitize([item.raw for item in trades]),
    }
    written = []
    for name, payload in payloads.items():
        path = OUT / f"{args.exchange}_{name}.json"
        text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        path.write_text(text, encoding="utf-8")
        written.append(text)
    blob = "\n".join(written)
    for secret in _secrets():
        if secret and secret in blob:
            for path in OUT.glob(f"{args.exchange}_*.json"):
                path.unlink(missing_ok=True)
            print("失败")
            return 1
    print("通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
