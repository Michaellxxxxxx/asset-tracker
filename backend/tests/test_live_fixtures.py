"""Parsers against sanitized live Zerion / Alchemy / DefiLlama responses.

The fixtures in ``tests/fixtures/live`` were captured by
``scripts/capture_fixtures.py``. The real wallet is rewritten to
``0x1111…1111`` and every tx hash is a sha256 stand-in, so these tests can
run without a key and without touching the network.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from app.integrations.chain.alchemy import parse_alchemy_balances, parse_alchemy_transfers
from app.integrations.chain.zerion import parse_zerion_positions, parse_zerion_transactions
from app.integrations.prices.service import PriceService, parse_defillama

LIVE = Path(__file__).resolve().parent / "fixtures" / "live"
OWNER = "0x1111111111111111111111111111111111111111"


def _load(name: str) -> dict:
    return json.loads((LIVE / name).read_text(encoding="utf-8"))


def test_live_zerion_positions_parse_quantity_and_skip_trash() -> None:
    rows = parse_zerion_positions(OWNER, _load("zerion_positions.json"))
    assert rows
    assert all(row.source == "zerion" for row in rows)
    assert all(row.amount > 0 for row in rows)
    assert all(row.address == OWNER for row in rows)
    symbols = {row.asset for row in rows}
    # The captured wallet holds Pendle principal and yield tokens.
    assert "PT-SUSDX-3DEC2026" in symbols
    assert all(len(symbol) <= 24 for symbol in symbols)
    pt = next(row for row in rows if row.asset == "PT-SUSDX-3DEC2026")
    # Chain comes from the position id; the contract is the matching implementation.
    assert pt.chain == "ethereum"
    assert pt.contract == "0x0000000000000000000000000000000000000002"
    assert pt.quote_usdt is not None and pt.quote_usdt > 0
    native = next(row for row in rows if row.asset == "ETH")
    assert native.chain == "ethereum"
    assert native.contract is None
    bsc = next(row for row in rows if row.chain == "bsc")
    assert bsc.chain == "bsc"


def test_live_zerion_transactions_parse_direction_and_dedupe() -> None:
    rows = parse_zerion_transactions(OWNER, _load("zerion_transactions.json"))
    assert rows
    assert {row.direction for row in rows} == {"in", "out"}
    ids = [row.external_id for row in rows]
    # hash + transfer index is unique even when one tx has several legs.
    assert len(ids) == len(set(ids))
    assert all(item.startswith("zerion:0x") for item in ids)
    incoming = next(row for row in rows if row.direction == "in")
    assert incoming.address != OWNER
    assert incoming.amount > 0
    assert incoming.ts >= datetime(2025, 1, 1, tzinfo=timezone.utc)


def test_live_alchemy_balances_apply_decimals_and_drop_spam() -> None:
    payload = _load("alchemy.json")
    rows = parse_alchemy_balances(OWNER, payload["token_balances"], payload["metadata"])
    assert rows
    assert all(row.amount > 0 for row in rows)
    assert all(row.address == OWNER for row in rows)
    # Hex balances were scaled by the metadata decimals, not left as raw integer units.
    raw_units = [
        int(row["tokenBalance"], 16)
        for row in _load("alchemy.json")["token_balances"]["result"]["tokenBalances"]
    ]
    assert max(row.amount for row in rows) < Decimal(max(raw_units))
    symbols = {row.asset for row in rows}
    assert "PT-SUSDX-3DEC2026" in symbols
    # Alchemy is eth-mainnet, so every token row carries the ethereum contract.
    assert all(row.chain == "ethereum" and row.contract for row in rows)


def test_live_alchemy_transfers_use_unique_id() -> None:
    rows = parse_alchemy_transfers(OWNER, _load("alchemy.json")["transfers"])
    assert rows
    ids = [row.external_id for row in rows]
    assert len(ids) == len(set(ids))
    assert all(":log:" in row.external_id for row in rows)
    assert {row.direction for row in rows} <= {"in", "out"}
    assert all(row.ts >= datetime(2026, 1, 1, tzinfo=timezone.utc) for row in rows)
    usd3 = next(row for row in rows if row.asset == "USD3")
    assert usd3.direction == "in"
    assert usd3.amount == Decimal("2307.381804")


def test_live_defillama_prices_and_stables() -> None:
    payload = _load("defillama.json")
    current = parse_defillama(payload["current"])
    historical = parse_defillama(payload["historical"])
    assert current["ETH"] > Decimal("100")
    assert historical["ETH"] > Decimal("100")
    # USDC/USDT come back from DefiLlama near a dollar; the service pins stables at 1.
    assert Decimal("0.9") < current["USDC"] < Decimal("1.1")
    service = PriceService(use_mock=False, fixture_dir=LIVE)
    assert service.quote_usdt("USDC") == Decimal("1")
    assert service.quote_usdt("USDT") == Decimal("1")
