"""Helius RPC / DAS balance adapter (三期·10). All network via httpx.MockTransport."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest

from app.integrations.chain.helius import (
    WSOL_MINT,
    HeliusError,
    SolanaHeliusAdapter,
    describe_helius_error,
    parse_das_fungibles,
    parse_helius_balances,
    parse_token_accounts,
)
from app.logging_redact import redact_text

ADDR = "85Rtk5mEwieg9g3G1ALRRHUGxyCYpC9fAmViCe355yEQ"
ADDR2 = "E2yWHeDemoAddress111111111111111111111111111"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SINCE = datetime(2026, 10, 23, tzinfo=timezone.utc)
API_KEY = "test-helius-key-do-not-leak"


def _rpc_ok(result: object) -> httpx.Response:
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})


def _rpc_err(message: str, code: int = -32000) -> httpx.Response:
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": code, "message": message}})


def test_parse_fixture_sol_gets_wsol_contract() -> None:
    rows = parse_helius_balances(ADDR, {"balances": [{"symbol": "SOL", "amount": "0.5"}]})
    assert len(rows) == 1
    assert rows[0].asset == "SOL"
    assert rows[0].contract == WSOL_MINT
    assert rows[0].chain == "solana"


def test_parse_das_fungibles_skips_nft_and_zero() -> None:
    payload = {
        "result": {
            "items": [
                {
                    "id": USDC_MINT,
                    "interface": "FungibleToken",
                    "token_info": {
                        "symbol": "USDC",
                        "balance": 1_100_000,
                        "decimals": 6,
                        "price_info": {"price_per_token": 1.0},
                    },
                },
                {
                    "id": "NftMint111",
                    "interface": "V1_NFT",
                    "token_info": {"symbol": "NFT", "balance": 1, "decimals": 0},
                },
                {
                    "id": "ZeroMint",
                    "interface": "FungibleToken",
                    "token_info": {"symbol": "ZERO", "balance": 0, "decimals": 6},
                },
                {
                    "id": "Dec0Mint",
                    "interface": "FungibleToken",
                    "token_info": {"symbol": "BAD", "balance": 1, "decimals": 0},
                },
            ]
        }
    }
    rows = parse_das_fungibles(ADDR, payload)
    assert len(rows) == 1
    assert rows[0].asset == "USDC"
    assert rows[0].amount == Decimal("1.1")
    assert rows[0].contract == USDC_MINT
    assert rows[0].quote_usdt == Decimal("1.0")


def test_parse_token_accounts_uses_ui_amount() -> None:
    payload = {
        "result": {
            "value": [
                {
                    "account": {
                        "data": {
                            "parsed": {
                                "info": {
                                    "mint": USDC_MINT,
                                    "tokenAmount": {
                                        "amount": "2500000",
                                        "decimals": 6,
                                        "uiAmount": 2.5,
                                    },
                                }
                            }
                        }
                    }
                },
                {
                    "account": {
                        "data": {
                            "parsed": {
                                "info": {
                                    "mint": "EmptyMint",
                                    "tokenAmount": {"amount": "0", "decimals": 6, "uiAmount": 0},
                                }
                            }
                        }
                    }
                },
            ]
        }
    }
    rows = parse_token_accounts(ADDR, payload)
    assert len(rows) == 1
    assert rows[0].amount == Decimal("2.5")
    assert rows[0].contract == USDC_MINT


def test_fetch_balances_sol_and_spl_via_das() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert "api-key=" in str(request.url)
        body = request.read()
        import json

        payload = json.loads(body)
        method = payload["method"]
        calls.append(method)
        if method == "getBalance":
            return _rpc_ok({"value": 479_818_494})
        if method == "getAssetsByOwner":
            return _rpc_ok(
                {
                    "nativeBalance": {"lamports": 479_818_494},
                    "items": [
                        {
                            "id": USDC_MINT,
                            "interface": "FungibleToken",
                            "token_info": {
                                "symbol": "USDC",
                                "balance": 1100,
                                "decimals": 6,
                                "price_info": {"price_per_token": 1},
                            },
                        },
                        {
                            "id": "NftX",
                            "interface": "V1_NFT",
                            "token_info": {"balance": 1, "decimals": 0},
                        },
                    ],
                }
            )
        return _rpc_err(f"unexpected {method}")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = SolanaHeliusAdapter([ADDR], api_key=API_KEY, client=client)
    rows = adapter.fetch_balances()
    by_asset = {row.asset: row for row in rows}
    assert "SOL" in by_asset
    assert by_asset["SOL"].amount == Decimal("479818494") / Decimal("1000000000")
    assert by_asset["SOL"].contract == WSOL_MINT
    assert by_asset["USDC"].amount == Decimal("0.0011")
    assert by_asset["USDC"].quote_usdt == Decimal("1")
    assert calls == ["getBalance", "getAssetsByOwner"]


def test_fetch_balances_falls_back_to_token_accounts_when_das_fails() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        method = json.loads(request.read())["method"]
        calls.append(method)
        if method == "getBalance":
            return _rpc_ok({"value": 1_000_000_000})
        if method == "getAssetsByOwner":
            return _rpc_err("DAS unavailable", code=-32601)
        if method == "getTokenAccountsByOwner":
            return _rpc_ok(
                {
                    "value": [
                        {
                            "account": {
                                "data": {
                                    "parsed": {
                                        "info": {
                                            "mint": USDC_MINT,
                                            "tokenAmount": {
                                                "amount": "5000000",
                                                "decimals": 6,
                                                "uiAmount": 5,
                                            },
                                        }
                                    }
                                }
                            }
                        }
                    ]
                }
            )
        return _rpc_err("nope")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = SolanaHeliusAdapter([ADDR], api_key=API_KEY, client=client)
    rows = adapter.fetch_balances()
    by_asset = {row.asset: row.amount for row in rows}
    assert by_asset["SOL"] == Decimal("1")
    assert Decimal("5") in by_asset.values()
    assert "getTokenAccountsByOwner" in calls


def test_fetch_balances_rpc_http_error_is_humanized() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = SolanaHeliusAdapter([ADDR], api_key=API_KEY, client=client)
    with pytest.raises(HeliusError) as raised:
        adapter.fetch_balances()
    msg = str(raised.value)
    assert "API Key" in msg or "无效" in msg
    assert API_KEY not in msg
    assert "api-key" not in msg.lower()


def test_fetch_balances_rpc_json_error_is_humanized() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _rpc_err("Pubkey Validation Err: bad")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = SolanaHeliusAdapter([ADDR], api_key=API_KEY, client=client)
    with pytest.raises(HeliusError) as raised:
        adapter.fetch_balances()
    assert "Pubkey" in str(raised.value) or "RPC" in str(raised.value)
    assert API_KEY not in str(raised.value)


def test_unpriced_spl_still_returned_with_amount() -> None:
    """Missing price_info must not abort; quote_usdt stays None (priced later as 0)."""

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        method = json.loads(request.read())["method"]
        if method == "getBalance":
            return _rpc_ok({"value": 0})
        return _rpc_ok(
            {
                "items": [
                    {
                        "id": "UnknownMint1111111111111111111111111111111",
                        "interface": "FungibleToken",
                        "token_info": {"symbol": "XYZ", "balance": 10**9, "decimals": 9},
                    }
                ]
            }
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    rows = SolanaHeliusAdapter([ADDR], api_key=API_KEY, client=client).fetch_balances()
    assert len(rows) == 1
    assert rows[0].asset == "XYZ"
    assert rows[0].amount == Decimal("1")
    assert rows[0].quote_usdt is None


def test_transfers_filter_since_and_404_skips() -> None:
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(1)
        if ADDR in str(request.url):
            return httpx.Response(
                200,
                json=[
                    {
                        "signature": "sigOld",
                        "timestamp": int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()),
                        "type": "TRANSFER",
                        "description": "old",
                        "nativeTransfers": [
                            {
                                "fromUserAccount": "other",
                                "toUserAccount": ADDR,
                                "amount": 1_000_000_000,
                            }
                        ],
                    },
                    {
                        "signature": "sigNew",
                        "timestamp": int(datetime(2026, 10, 24, tzinfo=timezone.utc).timestamp()),
                        "type": "TRANSFER",
                        "description": "new",
                        "nativeTransfers": [
                            {
                                "fromUserAccount": "other",
                                "toUserAccount": ADDR,
                                "amount": 2_000_000_000,
                            }
                        ],
                    },
                ],
            )
        return httpx.Response(404, text="Not Found")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = SolanaHeliusAdapter([ADDR, ADDR2], api_key=API_KEY, client=client)
    rows = adapter.fetch_transfers(SINCE)
    assert len(rows) == 1
    assert rows[0].txid == "sigNew"
    assert rows[0].amount == Decimal("2")
    assert len(seen) == 2


def test_describe_helius_error_timeout() -> None:
    assert "超时" in describe_helius_error(httpx.TimeoutException("x"))


def test_redact_helius_api_key_query_string() -> None:
    leaked = f"POST https://mainnet.helius-rpc.com/?api-key={API_KEY} failed"
    cleaned = redact_text(leaked)
    assert API_KEY not in cleaned
    assert "api-key=***" in cleaned or "***" in cleaned


def test_mock_fixture_balances_still_work() -> None:
    rows = SolanaHeliusAdapter(["So1DemoAddress"], use_mock=True).fetch_balances()
    by_asset = {row.asset: row.amount for row in rows}
    assert by_asset["SOL"] == Decimal("3.5")
    assert by_asset["USDC"] == Decimal("25")
