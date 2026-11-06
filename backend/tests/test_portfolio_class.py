"""Phase-5 A: pure classification rules and stable keys."""

from __future__ import annotations

from types import SimpleNamespace

from app.portfolio_class import (
    classify_holding,
    classify_position,
    holding_key,
    merge_override,
    position_key,
)


def _pos(**overrides):
    base = {
        "id": 1,
        "kind": "lending",
        "protocol": "aave",
        "market_ref": "USDC",
        "label": None,
        "debt_usdt": 0,
        "details": {},
    }
    base.update(overrides)
    return base


def test_stablecoin_is_cash_low():
    assert classify_holding({"asset": "USDC"})[:2] == ("cash", "low")
    assert classify_holding({"asset": "USDT0"})[:2] == ("cash", "low")
    assert classify_holding({"asset": "DAI"})[1] == "low"


def test_mainstream_coin_is_crypto_medium_and_alt_is_high():
    assert classify_holding({"asset": "BTC"})[:2] == ("crypto", "medium")
    assert classify_holding({"asset": "WETH"})[:2] == ("crypto", "medium")
    assert classify_holding({"asset": "ARB"})[:2] == ("crypto", "high")
    assert classify_holding({"asset": "PEPE"})[1] == "high"


def test_stock_token_is_stock_medium():
    assert classify_holding({"asset": "QQQx"})[:2] == ("stock", "medium")
    assert classify_holding({"asset": "AAPLx"})[:2] == ("stock", "medium")
    assert classify_holding({"asset": "TSLAon"})[:2] == ("stock", "medium")


def test_aave_stable_deposit_without_debt_is_low():
    row = _pos(
        protocol="aave",
        debt_usdt=0,
        details={"collateral": [{"symbol": "USDC", "usd": 1000}]},
    )
    assert classify_position(row)[:2] == ("defi", "low")


def test_morpho_loop_is_high():
    row = _pos(
        protocol="morpho",
        debt_usdt=400,
        details={
            "health_factor": 1.7,
            "collateral": [{"symbol": "WETH", "usd": 1400}],
            "borrow": [{"symbol": "USDC", "usd": 400}],
        },
    )
    assert classify_position(row)[:2] == ("defi", "high")


def test_borrow_hf_below_1_5_upgrades_one_level():
    row = _pos(
        protocol="aave",
        debt_usdt=500,
        details={
            "health_factor": 1.4,
            "collateral": [{"symbol": "WETH", "usd": 1000}],
            "borrow": [{"symbol": "USDC", "usd": 500}],
        },
    )
    asset_class, risk, reason = classify_position(row)
    assert (asset_class, risk) == ("defi", "high")
    assert "升一档" in reason


def test_borrow_hf_below_1_2_is_high():
    row = _pos(
        protocol="aave",
        debt_usdt=900,
        details={
            "health_factor": 1.1,
            "collateral": [{"symbol": "WETH", "usd": 1000}],
            "borrow": [{"symbol": "USDC", "usd": 900}],
        },
    )
    _, risk, reason = classify_position(row)
    assert risk == "high"
    assert "1.2" in reason


def test_pendle_pt_medium_and_yt_high():
    pt = _pos(protocol="pendle", market_ref="PT-sUSDx-3DEC2026", debt_usdt=0)
    yt = _pos(protocol="pendle", market_ref="YT-sUSDx-3DEC2026", debt_usdt=0)
    assert classify_position(pt)[:2] == ("defi", "medium")
    assert classify_position(yt)[:2] == ("defi", "high")


def test_option_is_high():
    row = _pos(kind="option", protocol="deribit", market_ref="BTC-20261225-100000-C")
    assert classify_position(row)[:2] == ("option", "high")


def test_unknown_is_other_medium_with_reason():
    assert classify_holding({}) == ("other", "medium", "未识别，按中风险")
    assert classify_position({"kind": "mystery"}) == ("other", "medium", "未识别，按中风险")


def test_holding_key_prefers_chain_contract_and_is_case_stable():
    row = {"chain": "EVM", "contract": "0xABCDEF", "asset": "USDC", "source": "zerion"}
    assert holding_key(row) == "evm:0xabcdef"
    assert holding_key({**row, "asset": "USDC.e", "source": "other"}) == "evm:0xabcdef"
    no_contract = {"chain": None, "contract": None, "asset": "ARB", "source": "Binance"}
    assert holding_key(no_contract) == "symbol:binance:arb"
    assert holding_key(no_contract) == holding_key(dict(no_contract))


def test_position_key_prefers_id_and_falls_back_to_protocol_market():
    assert position_key({"id": 7, "protocol": "morpho", "market_ref": "PT-USD3"}) == "pos:7"
    assert (
        position_key({"id": None, "protocol": "Morpho", "market_ref": "PT-USD3"})
        == "pos:morpho:pt-usd3"
    )


def test_merge_override_is_partial_and_marks_overridden():
    row = SimpleNamespace(asset_class="crypto", risk_level="medium", reason="default")
    out = merge_override("crypto", "medium", "default", row)
    assert out == ("crypto", "medium", "default", True)
    only_risk = SimpleNamespace(asset_class=None, risk_level="low", reason="人工降风险")
    assert merge_override("crypto", "high", "default", only_risk) == (
        "crypto",
        "low",
        "人工降风险",
        True,
    )
