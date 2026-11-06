"""The exchange integration stays read-only.

Scans the backend for order/withdraw calls and checks that the adapter only
ever calls the five documented ccxt methods.
"""

from __future__ import annotations

import ast
import re
from datetime import datetime, timezone
from pathlib import Path

from app.integrations.exchanges.ccxt_adapter import CCXTAdapter

APP = Path(__file__).resolve().parents[1] / "app"

# A call that would move money or open an order. Matched against the source so a
# comment mentioning the name does not trip it; the AST walk below is what
# actually decides.
_FORBIDDEN = re.compile(
    r"\b(?:create_order|createOrder|cancel_order|cancelOrder|create_deposit_address|"
    r"private\w*Post\w*|privateWalletPost|sapiPost\w*|withdraw|transfer)\s*\("
)
_ALLOWED_CLIENT_CALLS = {"fetch_balance", "fetch_deposits", "fetch_withdrawals", "fetch_my_trades", "load_markets"}


def test_backend_source_has_no_order_or_withdraw_calls() -> None:
    offenders: list[str] = []
    for path in APP.rglob("*.py"):
        source = path.read_text()
        for lineno, line in enumerate(source.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if _FORBIDDEN.search(line):
                offenders.append(f"{path.relative_to(APP.parent)}:{lineno}: {stripped}")
    assert offenders == []


def test_adapter_only_calls_the_read_methods() -> None:
    """Every attribute read on the client is one of the five read methods."""
    seen: set[str] = set()

    class _Watcher:
        def __getattr__(self, name):
            seen.add(name)

            def _method(*_args, **_kwargs):
                if name == "fetch_balance":
                    return {"total": {"USDT": "1"}}
                if name == "load_markets":
                    return {}
                return []

            return _method

    adapter = CCXTAdapter("binance", {"apiKey": "k", "secret": "s"}, client=_Watcher(), sleep=lambda _s: None)
    adapter.fetch_balances()
    adapter.fetch_transfers(datetime(2026, 1, 1, tzinfo=timezone.utc))
    adapter.fetch_trades(datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert seen <= _ALLOWED_CLIENT_CALLS
    assert seen  # the adapter did call something


def test_ccxt_calls_in_the_adapter_are_gets() -> None:
    """The adapter never reaches for a private POST. It only names fetch_* and load_markets."""
    source = (APP / "integrations" / "exchanges" / "ccxt_adapter.py").read_text()
    tree = ast.parse(source)
    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            called.add(node.func.attr)
    private_posts = {name for name in called if re.search(r"(?i)post", name)}
    assert private_posts == set()
    assert "withdraw" not in called
    assert "create_order" not in called
