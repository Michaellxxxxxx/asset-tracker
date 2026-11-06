"""Phase-4 A, no-key: option valuation, PnL, APR, and the public Deribit mark.

No network except the one test that hits Deribit public market data, and that
test skips cleanly when the host is unreachable. No private method, no key.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.integrations import options_deribit, options_mock
from app.integrations.options_deribit import DeribitPublicError
from app.options_calc import (
    capital_of,
    holding_days,
    mark_value,
    option_apr,
    premium_cost,
    realized_pnl,
    summarize,
    unrealized_pnl,
    value_position,
)

FIXTURES = Path(__file__).parent / "fixtures" / "options"


def _book() -> dict:
    return json.loads((FIXTURES / "mock_book.json").read_text(encoding="utf-8"))


def _approx(value: object, expected: object) -> None:
    assert Decimal(str(value)).quantize(Decimal("0.00000001")) == Decimal(str(expected)).quantize(
        Decimal("0.00000001")
    )


# --- pure functions -------------------------------------------------------


def test_mark_value_short_is_negative() -> None:
    assert mark_value(1, 100) == Decimal("100")
    assert mark_value(-2, 210) == Decimal("-420")
    # Linear multiplier: 3 contracts × $10 mark × $2 multiplier.
    assert mark_value(3, 10, 2) == Decimal("60")
    assert mark_value(-1, 10, 5) == Decimal("-50")


def test_premium_sign_follows_position() -> None:
    assert premium_cost(0.5, 1800) == Decimal("900")
    assert premium_cost(-2, 150) == Decimal("-300")  # sold: income, negative cost


def test_unrealized_is_mark_minus_premium() -> None:
    # Long: value up.
    assert unrealized_pnl(2500, 900) == Decimal("1600")
    # Short: mark more negative than the credit received → loss.
    assert unrealized_pnl(-420, -300) == Decimal("-120")
    # Short whose mark fell below the credit → gain.
    assert unrealized_pnl(-600, -800) == Decimal("200")


def test_apr_zero_capital_is_none() -> None:
    assert option_apr(100, 0, 10) is None
    assert option_apr(100, -5, 10) is None
    assert option_apr(100, 500, 0) is None
    assert option_apr(0, 0, 0) is None
    got = option_apr(1600, 900, Decimal("22.5"))
    assert got is not None
    _approx(got, "28.839506172839506")


def test_capital_is_margin_plus_buyer_premium_only() -> None:
    # Long: the premium posted is capital. No double count with margin.
    assert capital_of({"margin_usd": 0, "premium_usd": 900}) == Decimal("900")
    # Short: premium is income, so only the margin is occupied capital.
    assert capital_of({"margin_usd": 800, "premium_usd": -300}) == Decimal("800")
    assert capital_of({"margin_usd": 0, "premium_usd": -300}) == Decimal("0")


def test_holding_days() -> None:
    assert holding_days("2026-10-01T00:00:00Z", "2026-10-23T12:00:00Z") == Decimal("22.5")
    assert holding_days("2026-10-23T12:00:00Z", "2026-10-01T00:00:00Z") is None
    assert holding_days(None, "2026-10-23T12:00:00Z") is None


def test_realized_exercise_and_partial_close() -> None:
    trades = _book()["trades"]
    exercised = [row for row in trades if row["position_ref"] == "ETH-23OCT26-1800-P"]
    # Bought 5 @ 40, exercised 5 @ 70. 350 cash in − 200 cost = 150.
    assert realized_pnl(exercised) == Decimal("150")

    partial = [row for row in trades if row["kind"] == "close"]
    # The close fixture has no matching open, so the function must not invent a
    # cost basis. Pair it with the open the note describes.
    paired = [
        {"ts": "2026-10-20T12:00:00Z", "side": "buy", "qty": 1, "price": 3200, "fee": 0, "kind": "open"},
        *partial,
    ]
    # Sold 0.25 @ 3600, fee 1, cost 800 → 900 − 1 − 800 = 99.
    assert realized_pnl(paired) == Decimal("99")


def test_realized_short_cover() -> None:
    trades = [
        {"ts": "t1", "side": "sell", "qty": 2, "price": 150, "fee": 0, "kind": "open"},
        {"ts": "t2", "side": "buy", "qty": 2, "price": 100, "fee": 1, "kind": "close"},
    ]
    # Credit 300, paid 200 to close, fee 1 → +99.
    assert realized_pnl(trades) == Decimal("99")


def test_realized_expire_worthless_long() -> None:
    trades = [
        {"ts": "t1", "side": "buy", "qty": 1, "price": 40, "fee": 0, "kind": "open"},
        {"ts": "t2", "side": "sell", "qty": 1, "price": 0, "fee": 0, "kind": "expire"},
    ]
    assert realized_pnl(trades) == Decimal("-40")


# --- mock book ------------------------------------------------------------


def test_mock_book_shape() -> None:
    book = options_mock.load_mock_book()
    positions = book["positions"]
    assert len(positions) >= 6
    underlyings = {row["underlying"] for row in positions}
    assert underlyings == {"BTC", "ETH"}
    sides = {row["side"] for row in positions}
    assert {"long", "short"} <= sides
    assert any(row.get("near_expiry") for row in positions)
    assert any(row.get("exercised") for row in positions)
    shorts = [row for row in positions if row["size"] < 0]
    assert len(shorts) >= 2
    assert any(row["underlying"] == "BTC" and row["size"] > 0 for row in positions)
    assert any(row["underlying"] == "ETH" and row["size"] > 0 for row in positions)


def test_value_position_matches_hand_numbers() -> None:
    """Every fixture row's expect block is reproduced by the pure function."""
    book = _book()
    trades_by_ref: dict[str, list] = {}
    for trade in book["trades"]:
        # The partial-close trade belongs to a row that is still fully open in
        # the fixture; only the exercised (flat) row carries its fills.
        if trade["kind"] == "close":
            continue
        trades_by_ref.setdefault(trade["position_ref"], []).append(trade)

    valued = []
    for row in book["positions"]:
        got = value_position(row, trades=trades_by_ref.get(row["instrument"]))
        valued.append(got)
        expected = row["expect"]
        for key, want in expected.items():
            if want is None:
                assert got[key] is None, key
            else:
                _approx(got[key], want)

    # Book: marks 2500+4100-420+285-600+0 = 5865
    # premiums 900+3200-300+240-800+0 = 3240
    # unrealized 1600+900-120+45+200+0 = 2625
    # realized 150
    summary = summarize(valued)
    _approx(summary["total_mark_value_usd"], "5865")
    _approx(summary["total_premium_usd"], "3240")
    _approx(summary["unrealized_pnl_usd"], "2625")
    _approx(summary["realized_pnl_usd"], "150")
    assert summary["pnl_apr"] is not None  # capital is not zero


def test_zero_capital_row_has_null_apr() -> None:
    row = {
        "size": -1,
        "avg_price": 10,
        "mark_price": 4,
        "margin_usd": 0,
        "multiplier": 1,
        "opened_at": "2026-10-01T00:00:00Z",
        "asof": "2026-10-11T00:00:00Z",
        "capital_samples": [0, 0],
    }
    got = value_position(row)
    # Mark -4, premium -10, unrealized +6, but nothing was posted as capital.
    _approx(got["unrealized_pnl_usd"], "6")
    assert got["pnl_apr"] is None
    assert summarize([got])["pnl_apr"] is None


def test_value_position_does_not_mutate_input() -> None:
    row = {"size": 1, "avg_price": 10, "mark_price": 12, "details": {"iv": 0.5}}
    snapshot = json.dumps(row)
    value_position(row)
    assert json.dumps(row) == snapshot


# --- Deribit public quote normalization (no network) ----------------------


def test_inverse_mark_converts_with_index() -> None:
    # 0.1458 BTC × 80783.22 = 11778.193476
    usd = options_deribit.quote_to_usd(0.1458, quote_currency="BTC", index_usd="80783.22")
    _approx(usd, "11778.193476")
    # Linear USDC mark is already dollars.
    assert options_deribit.quote_to_usd("132.61926934", quote_currency="USDC", index_usd=2372) == Decimal(
        "132.61926934"
    )


def test_normalize_ticker_iv_is_fraction_and_greeks_pass_through() -> None:
    raw = _book()["quotes"]["BTC-27NOV26-70000-C"]
    got = options_deribit.normalize_ticker(raw)
    _approx(got["mark_iv"], "0.4274")  # 42.74% → fraction, matching the spec's iv
    _approx(got["mark_price_usd"], "11778.193476")
    assert got["delta"] == pytest.approx(0.88438)
    assert got["greeks"]["theta"] == pytest.approx(-30.18945)
    assert got["quote_currency"] == "BTC"


def test_normalize_linear_usdc_mark_is_not_multiplied() -> None:
    raw = _book()["quotes"]["ETH_USDC-6NOV26-2450-P"]
    got = options_deribit.normalize_ticker(raw)
    _approx(got["mark_price_usd"], "132.61926934")
    _approx(got["mark_iv"], "0.4923")
    assert got["delta"] == pytest.approx(-0.42)


def test_apply_mark_copies_usd_mark_without_touching_source() -> None:
    position = {"size": 0.1, "avg_price": 1000, "mark_price": 0, "details": {"underlying": "BTC"}}
    quote = options_deribit.normalize_ticker(_book()["quotes"]["BTC-27NOV26-70000-C"])
    updated = options_deribit.apply_mark(position, quote)
    assert position["mark_price"] == 0  # source untouched
    _approx(updated["mark_price"], "11778.193476")
    assert updated["details"]["iv"] == pytest.approx(0.4274)
    assert updated["details"]["underlying"] == "BTC"
    valued = value_position(updated)
    # 0.1 × 11778.193476 − 0.1 × 1000
    _approx(valued["mark_value_usd"], "1177.8193476")
    _approx(valued["unrealized_pnl_usd"], "1077.8193476")


def test_book_summary_parses_mocked_transport() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["currency"] == "BTC"
        assert "private" not in str(request.url)
        return httpx.Response(200, json={"jsonrpc": "2.0", "result": [_book()["quotes"]["BTC-27NOV26-70000-C"]]})

    rows = options_deribit.book_summary("BTC", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert rows[0]["instrument_name"] == "BTC-27NOV26-70000-C"
    got = options_deribit.normalize_book_row(rows[0], index_usd="80783.22")
    _approx(got["mark_price_usd"], "11778.193476")
    assert got["greeks"] is None  # book summary has no greeks; ticker does


def test_public_error_hides_query_string() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "down"})

    with pytest.raises(DeribitPublicError) as caught:
        options_deribit.ticker(
            "BTC-27NOV26-70000-C",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
    assert "instrument_name" not in str(caught.value)
    assert "api_key" not in str(caught.value).lower()
    assert "www.deribit.com" in str(caught.value)


def test_expiry_from_instrument_name() -> None:
    assert options_deribit.expiry_iso("BTC-27MAR26-100000-C") == "2026-03-27T08:00:00Z"
    assert options_deribit.expiry_iso("ETH-25OCT26-2340-P") == "2026-10-25T08:00:00Z"
    assert options_deribit.expiry_iso("not-an-instrument") is None


def test_live_public_ticker_shape() -> None:
    """One real public call. Skips if Deribit is unreachable from this sandbox."""
    try:
        row = options_deribit.ticker("BTC-27NOV26-70000-C")
    except Exception as exc:  # noqa: BLE001 — skip, don't fail the suite on egress
        pytest.skip(f"deribit public unreachable: {type(exc).__name__}")
    got = options_deribit.normalize_ticker(row)
    assert got["instrument_name"] == "BTC-27NOV26-70000-C"
    assert got["mark_price_usd"] > 0
    assert 0 < got["mark_iv"] < 5
    assert got["greeks"]["delta"] is not None
