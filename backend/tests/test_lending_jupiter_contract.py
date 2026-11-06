"""p9a: Jupiter per-source tolerance and wire-contract regression tests.

Fixtures in ``tests/fixtures/jupiter`` are cached real responses (earn and
borrow vaults) plus position shapes built from those real payloads. No API key,
relay env or live network is used here.
"""

from __future__ import annotations

import json
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from app.auth import hash_password
from app.integrations import lending_jupiter
from app.integrations.lending_common import (
    log_failure,
    take_failures,
    take_source_failures,
)
from app.models import Position, User, UserWallet
from app.positions_core import upsert_positions
from app.sync.positions_hook import sync_positions_for_user

FIXTURES = Path(__file__).parent / "fixtures" / "jupiter"
ADDR = "So1anaWa11et11111111111111111111111111111111"
EARN_REF = "jupiter-lend-earn"
BORROW_REF = "jupiter-lend-borrow-3518"
PERPS_REF = "jupiter-perps-PerpPos111111111111111111111111111111111111"


def _load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def _fresh_state():
    lending_jupiter.reset_cooldowns()
    take_source_failures()
    take_failures()
    yield
    lending_jupiter.reset_cooldowns()
    take_source_failures()
    take_failures()


def _bundle() -> dict:
    """Valid bundle: real shape, one non-zero earn row owned by the test wallet."""
    earn = deepcopy(_load("jup_earn_sample.json"))
    earn[0]["ownerAddress"] = ADDR
    earn[0]["shares"] = "1500000000"
    earn[0]["underlyingAssets"] = "1500000000"
    return {
        "earn": earn,
        "borrow": deepcopy(_load("jup_borrow_position_sample.json")),
        "perps": deepcopy(_load("jup_perps_position_sample.json")),
    }


def _refs(rows: list[dict]) -> set[str]:
    return {row["market_ref"] for row in rows}


# --- normal parsing -------------------------------------------------------


def test_cached_earn_and_borrow_empty_samples_are_clean_empty() -> None:
    """The cached real responses keep their shape and do not read as failure."""
    earn = _load("jup_earn_sample.json")
    assert lending_jupiter.normalize(
        earn[0]["ownerAddress"], {"earn": earn}
    ) == []
    assert lending_jupiter.normalize(ADDR, {"borrow": _load("jup_borrow_sample.json")}) == []
    assert lending_jupiter.normalize(
        ADDR, {"perps": _load("jup_perps_empty_sample.json")}
    ) == []
    assert take_source_failures() == {}


def test_earn_normal_parse_from_cached_shape() -> None:
    payload = _bundle()
    rows = lending_jupiter.normalize(ADDR, {"earn": payload["earn"]})
    assert len(rows) == 1
    row = rows[0]
    assert row["market_ref"] == EARN_REF
    assert Decimal(str(row["collateral_usdt"])).quantize(Decimal("0.01")) == Decimal("1500.11")
    leg = row["details"]["collateral"][0]
    assert leg["symbol"] == "jlUSDS"
    assert Decimal(str(leg["amount"])) == Decimal("1500")
    assert take_source_failures() == {}


def test_borrow_normal_parse_from_real_vault_shape() -> None:
    rows = lending_jupiter.normalize(
        ADDR, {"borrow": _load("jup_borrow_position_sample.json")}
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["market_ref"] == BORROW_REF
    # 20 SOL supplied, 800 USDC borrowed; prices/decimals come from the real vault.
    assert Decimal(str(row["collateral_usdt"])).quantize(Decimal("0.01")) == Decimal("2045.12")
    assert Decimal(str(row["debt_usdt"])).quantize(Decimal("0.01")) == Decimal("799.78")
    assert row["details"]["health_factor"] is not None
    assert take_source_failures() == {}


def test_perps_equity_is_collateral_plus_pnl_not_value_usd() -> None:
    rows = lending_jupiter.normalize(
        ADDR, {"perps": _load("jup_perps_position_sample.json")}
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["market_ref"] == PERPS_REF
    # fixture: collateral 1000 + pnl 200 = 1200; valueUsd 900 is a decoy.
    assert Decimal(str(row["collateral_usdt"])) == Decimal("1200")
    assert Decimal(str(row["debt_usdt"])) == Decimal("0")
    assert Decimal(str(row["details"]["notional_usd"])) == Decimal("3000")
    assert Decimal(str(row["details"]["health_factor"])) == Decimal("1.375")
    assert take_source_failures() == {}


def test_all_sources_parse_together() -> None:
    rows = lending_jupiter.normalize(ADDR, _bundle())
    assert _refs(rows) == {EARN_REF, BORROW_REF, PERPS_REF}
    assert take_source_failures() == {}


# --- renamed/missing fields fail the source, never zero -------------------


def test_earn_renamed_field_fails_only_earn(caplog: pytest.LogCaptureFixture) -> None:
    payload = _bundle()
    item = payload["earn"][0]
    item["underlyingAsset"] = item.pop("underlyingAssets")
    with caplog.at_level("WARNING", logger="app.integrations.lending_jupiter"):
        rows = lending_jupiter.normalize(ADDR, payload)
    assert _refs(rows) == {BORROW_REF, PERPS_REF}
    assert take_source_failures() == {"jupiter": ["earn"]}
    assert "underlyingAssets" in caplog.text
    assert "1500" not in caplog.text  # logs name the field, never amounts


def test_borrow_renamed_field_fails_only_borrow(caplog: pytest.LogCaptureFixture) -> None:
    payload = _bundle()
    item = payload["borrow"][0]
    item["debt"] = item.pop("borrow")
    with caplog.at_level("WARNING", logger="app.integrations.lending_jupiter"):
        rows = lending_jupiter.normalize(ADDR, payload)
    assert _refs(rows) == {EARN_REF, PERPS_REF}
    assert take_source_failures() == {"jupiter": ["borrow"]}
    assert "borrow" in caplog.text


def test_perps_renamed_pnl_field_fails_only_perps(caplog: pytest.LogCaptureFixture) -> None:
    payload = _bundle()
    item = payload["perps"]["dataList"][0]
    item["pnlUsd"] = item.pop("pnlAfterFeesUsd")
    with caplog.at_level("WARNING", logger="app.integrations.lending_jupiter"):
        rows = lending_jupiter.normalize(ADDR, payload)
    assert _refs(rows) == {EARN_REF, BORROW_REF}
    assert take_source_failures() == {"jupiter": ["perps"]}
    assert "pnlAfterFeesUsd" in caplog.text


def test_borrow_wrong_top_level_shape_fails_source() -> None:
    payload = _bundle()
    payload["borrow"] = {"positions": payload["borrow"]}
    rows = lending_jupiter.normalize(ADDR, payload)
    assert _refs(rows) == {EARN_REF, PERPS_REF}
    assert take_source_failures() == {"jupiter": ["borrow"]}


# --- fetch-level tolerance and per-source cooldown -----------------------


def _ok_handler(calls: list[str] | None = None, earn_status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if calls is not None:
            calls.append(path)
        if "earn/positions" in path:
            if earn_status != 200:
                return httpx.Response(earn_status, json={"error": "boom"})
            earn = deepcopy(_load("jup_earn_sample.json"))
            earn[0]["ownerAddress"] = ADDR
            earn[0]["shares"] = "1500000000"
            earn[0]["underlyingAssets"] = "1500000000"
            return httpx.Response(200, json=earn)
        if "borrow/positions" in path:
            return httpx.Response(200, json=_load("jup_borrow_position_sample.json"))
        if path == "/v1/positions":
            return httpx.Response(200, json=_load("jup_perps_position_sample.json"))
        return httpx.Response(404)

    return handler


def test_fetch_malformed_earn_keeps_borrow_and_perps() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "earn/positions" in request.url.path:
            calls.append("earn")
            return httpx.Response(200, json={"error": "shape changed"})
        if "borrow/positions" in request.url.path:
            return httpx.Response(200, json=_load("jup_borrow_position_sample.json"))
        if request.url.path == "/v1/positions":
            return httpx.Response(200, json=_load("jup_perps_position_sample.json"))
        return httpx.Response(404)

    rows = lending_jupiter.fetch_positions(
        ADDR, client=_client(handler), sleep=lambda _s: None
    )
    assert _refs(rows) == {BORROW_REF, PERPS_REF}
    assert take_source_failures() == {"jupiter": ["earn"]}
    assert calls == ["earn"]


def test_429_cooldown_is_per_source_not_per_wallet() -> None:
    calls: list[str] = []
    rows = lending_jupiter.fetch_positions(
        ADDR,
        client=_client(_ok_handler(calls, earn_status=429)),
        sleep=lambda _s: None,
    )
    # Earn exhausted four tries; borrow/perps answered in one request each.
    assert sum("/earn/positions" in call for call in calls) == 4
    assert _refs(rows) == {BORROW_REF, PERPS_REF}
    assert take_source_failures() == {"jupiter": ["earn"]}
    assert lending_jupiter._cooldown_remaining(ADDR, "earn") > 0
    assert lending_jupiter._cooldown_remaining(ADDR, "borrow") == 0
    assert lending_jupiter._cooldown_remaining(ADDR, "perps") == 0

    calls.clear()
    rows = lending_jupiter.fetch_positions(
        ADDR,
        client=_client(_ok_handler(calls, earn_status=429)),
        sleep=lambda _s: None,
    )
    # The cooling source is skipped; the same wallet's healthy sources refresh.
    assert not any("/earn/positions" in call for call in calls)
    assert sum("/borrow/positions" in call for call in calls) == 1
    assert sum("/v1/positions" in call for call in calls) == 1
    assert _refs(rows) == {BORROW_REF, PERPS_REF}
    assert take_source_failures() == {"jupiter": ["earn"]}


# --- hook: sibling refresh + stale carry-forward -------------------------


def test_hook_partial_source_failure_updates_sibling_marks_stale(db, monkeypatch) -> None:
    user = User(username="p9a-jupiter", hashed_password=hash_password("pass-word"))
    db.add(user)
    db.commit()
    db.add(UserWallet(user_id=user.id, chain="solana", address=ADDR))
    db.commit()
    upsert_positions(
        db,
        user.id,
        [
            {
                "kind": "lending",
                "protocol": "jupiter",
                "chain": "solana",
                "account_ref": ADDR,
                "market_ref": EARN_REF,
                "status": "open",
                "net_value_usdt": "100",
                "collateral_usdt": "100",
                "debt_usdt": "0",
                "details": {"health_factor": None},
            },
            {
                "kind": "lending",
                "protocol": "jupiter",
                "chain": "solana",
                "account_ref": ADDR,
                "market_ref": BORROW_REF,
                "status": "open",
                "net_value_usdt": "1",
                "collateral_usdt": "1",
                "debt_usdt": "0",
                "details": {},
            },
        ],
    )

    def fake_jupiter(_address, **_kwargs):
        rows = lending_jupiter.normalize(
            ADDR,
            {
                "borrow": _load("jup_borrow_position_sample.json"),
                "perps": _load("jup_perps_position_sample.json"),
            },
        )
        log_failure("jupiter", "solana", RuntimeError("earn down"), source="earn")
        return rows

    monkeypatch.setattr("app.sync.positions_hook.fetch_kamino", lambda *_a, **_k: [])
    monkeypatch.setattr("app.sync.positions_hook.fetch_jupiter", fake_jupiter)

    sync_positions_for_user(db, user.id, None, use_mock=False)

    rows = {
        row.market_ref: row
        for row in db.scalars(select(Position).where(Position.user_id == user.id))
    }
    earn = rows[EARN_REF]
    assert earn.status == "open"
    assert earn.details["stale"] is True
    assert Decimal(str(earn.collateral_usdt)) == Decimal("100")

    borrow = rows[BORROW_REF]
    assert borrow.status == "open"
    assert not borrow.details.get("stale")
    assert Decimal(str(borrow.collateral_usdt)).quantize(Decimal("0.01")) == Decimal("2045.12")
    assert PERPS_REF in rows
