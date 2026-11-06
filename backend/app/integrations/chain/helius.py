"""Helius Solana adapter: balances via RPC/DAS; transfers via enhanced transactions."""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx

from ..http import quiet_client
from ..types import ChainBalance, NormalizedTransfer

logger = logging.getLogger(__name__)

FIXTURE_PATH = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "chain" / "helius_transactions.json"
BALANCE_FIXTURE_PATH = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "chain" / "helius_balances.json"

HELIUS_RPC_URL = "https://mainnet.helius-rpc.com/"
HELIUS_TX_URL = "https://api.helius.xyz/v0/addresses/{address}/transactions"
WSOL_MINT = "So11111111111111111111111111111111111111112"
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
_LAMPORTS = Decimal("1000000000")
_FUNGIBLE_IFACES = frozenset({"FungibleToken", "FungibleAsset"})

# Dust gate for Helius.  Account bookkeeping transactions and near-zero token
# movements are not user money flows; importing them creates fake withdrawals /
# airdrops.  Thresholds are deliberately conservative and token-unit based.
HELIUS_DUST_USD = Decimal("0.01")
HELIUS_DUST_SOL = Decimal("0.00005")
HELIUS_DUST_TOKEN = Decimal("0.000001")
HELIUS_ACCOUNT_OPERATION_TYPES = frozenset(
    {"INITIALIZE_ACCOUNT", "REFRESH_OBLIGATION", "UNKNOWN"}
)
_HELIUS_USD_PEGGED = frozenset(
    {
        "USDT",
        "USDC",
        "USD3",
        "USDS",
        "USDE",
        "DAI",
        "PYUSD",
        "FDUSD",
        "USDG",
        "USD1",
        "USDD",
        "TUSD",
        "BUSD",
    }
)


class HeliusError(RuntimeError):
    """Human-readable Helius failure. Message must never contain an API key or URL."""


def _decimal(value: Any) -> Decimal:
    try:
        return abs(Decimal(str(value)))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _is_dust_or_bookkeeping(
    tx_type: str,
    asset: str,
    amount: Decimal,
    *,
    is_native: bool,
) -> str | None:
    """Return a skip reason, or None when the transfer carries real value."""
    if amount <= 0:
        return "zero_amount"
    if tx_type.strip().upper() in HELIUS_ACCOUNT_OPERATION_TYPES:
        return "account_operation"
    if is_native:
        return "dust_sol" if amount < HELIUS_DUST_SOL else None
    if asset.upper() in _HELIUS_USD_PEGGED:
        return "dust_usd" if amount < HELIUS_DUST_USD else None
    return "dust_token" if amount < HELIUS_DUST_TOKEN else None


def _instruction_program_ids(row: dict[str, Any]) -> list[str]:
    """Collect program ids already present in the Helius enhanced tx."""
    ids: set[str] = set()
    stack = list(row.get("instructions") or [])
    while stack:
        instruction = stack.pop()
        if not isinstance(instruction, dict):
            continue
        program_id = str(instruction.get("programId") or "").strip()
        if program_id:
            ids.add(program_id)
        stack.extend(instruction.get("innerInstructions") or [])
    return sorted(ids)[:32]


def parse_helius_transactions(address: str, payload: list[dict[str, Any]] | dict[str, Any]) -> list[NormalizedTransfer]:
    """Parse a Helius enhanced-transactions array.

    Zero/near-zero bookkeeping rows are dropped here (never imported into the
    pending table); the per-reason counts are logged once per response.
    """
    rows = payload if isinstance(payload, list) else payload.get("transactions") or payload.get("data") or []
    owner = address
    items: list[NormalizedTransfer] = []
    skipped: dict[str, int] = {}
    for row in rows:
        stamp = row.get("timestamp")
        if stamp is None:
            continue
        try:
            ts = datetime.fromtimestamp(int(stamp), tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            continue
        signature = str(row.get("signature") or "")
        tx_type = str(row.get("type") or row.get("description") or "")
        description = str(row.get("description") or "")
        transfers = row.get("tokenTransfers") or row.get("nativeTransfers") or []
        for index, transfer in enumerate(transfers):
            mint = str(transfer.get("mint") or "")
            asset = str(transfer.get("tokenSymbol") or transfer.get("symbol") or ("SOL" if not mint else mint[:6])).upper()
            raw_amount = transfer.get("tokenAmount")
            if raw_amount is None:
                lamports = transfer.get("amount")
                raw_amount = (Decimal(str(lamports)) / _LAMPORTS) if lamports is not None else 0
            amount = _decimal(raw_amount)
            reason = _is_dust_or_bookkeeping(
                tx_type,
                asset,
                amount,
                is_native=not mint,
            )
            if reason:
                skipped[reason] = skipped.get(reason, 0) + 1
                continue
            to_user = str(transfer.get("toUserAccount") or transfer.get("toUser") or "")
            from_user = str(transfer.get("fromUserAccount") or transfer.get("fromUser") or "")
            direction = "in" if to_user == owner else "out"
            items.append(
                NormalizedTransfer(
                    source="helius",
                    external_id=f"helius:{signature}:{index}",
                    ts=ts,
                    asset=asset,
                    amount=amount,
                    direction=direction,  # type: ignore[arg-type]
                    address=from_user if direction == "in" else to_user,
                    txid=signature or None,
                    raw_type=tx_type or "transfer",
                    note=description[:500],
                    raw={
                        "address": owner,
                        "chain": "solana",
                        "program_ids": _instruction_program_ids(row),
                        "tx_type": tx_type,
                        "helius_source": str(row.get("source") or ""),
                    },
                )
            )
    if skipped:
        detail = ", ".join(f"{key}={value}" for key, value in sorted(skipped.items()))
        logger.info("helius ignored %d dust/bookkeeping transfers (%s)", sum(skipped.values()), detail)
    return items


def parse_helius_balances(address: str, payload: dict[str, Any]) -> list[ChainBalance]:
    """Parse a compact balance fixture or a legacy Helius balances response.

    Accepted shapes:
    - ``{"balances": [{"symbol", "amount"}]}``
    - ``{"nativeBalance": <lamports>, "tokens": [{"symbol"|"mint", "amount"}]}``
    """
    items: list[ChainBalance] = []
    compact = payload.get("balances")
    if isinstance(compact, list):
        for row in compact:
            if not isinstance(row, dict):
                continue
            asset = str(row.get("symbol") or row.get("mint") or "").upper()
            amount = _decimal(row.get("amount"))
            if asset and amount > 0:
                mint = str(row.get("mint") or "").strip() or None
                items.append(
                    ChainBalance(
                        source="helius",
                        asset=asset,
                        amount=amount,
                        address=address,
                        chain="solana",
                        contract=mint if mint else (WSOL_MINT if asset == "SOL" else None),
                    )
                )
        return items

    lamports = payload.get("nativeBalance")
    if lamports is not None:
        sol = _decimal(Decimal(str(lamports)) / _LAMPORTS)
        if sol > 0:
            items.append(
                ChainBalance(
                    source="helius",
                    asset="SOL",
                    amount=sol,
                    address=address,
                    chain="solana",
                    contract=WSOL_MINT,
                )
            )
    for row in payload.get("tokens") or []:
        if not isinstance(row, dict):
            continue
        asset = str(row.get("symbol") or row.get("mint") or "").upper()
        amount = _decimal(row.get("amount"))
        mint = str(row.get("mint") or "").strip() or None
        if asset and amount > 0:
            items.append(
                ChainBalance(
                    source="helius",
                    asset=asset,
                    amount=amount,
                    address=address,
                    chain="solana",
                    contract=mint,
                )
            )
    return items


def parse_das_fungibles(address: str, payload: dict[str, Any]) -> list[ChainBalance]:
    """Parse DAS ``getAssetsByOwner`` result items into SPL balances (no native SOL)."""
    result = payload.get("result") if "result" in payload else payload
    if not isinstance(result, dict):
        return []
    items: list[ChainBalance] = []
    for row in result.get("items") or []:
        if not isinstance(row, dict):
            continue
        iface = str(row.get("interface") or "")
        token_info = row.get("token_info") if isinstance(row.get("token_info"), dict) else {}
        if iface not in _FUNGIBLE_IFACES and not token_info:
            continue
        if iface and iface not in _FUNGIBLE_IFACES:
            # NFTs / compressed NFTs even when token_info is present.
            continue
        decimals_raw = token_info.get("decimals")
        try:
            decimals = int(decimals_raw) if decimals_raw is not None else -1
        except (TypeError, ValueError):
            decimals = -1
        if decimals < 0:
            continue
        # decimals==0 with balance 1 is almost always an NFT mis-tagged as fungible.
        raw_balance = token_info.get("balance")
        if raw_balance is None:
            continue
        try:
            raw_int = int(str(raw_balance))
        except (TypeError, ValueError):
            continue
        if raw_int <= 0:
            continue
        if decimals == 0:
            continue
        amount = _decimal(Decimal(raw_int) / (Decimal(10) ** decimals))
        if amount <= 0:
            continue
        mint = str(row.get("id") or token_info.get("mint") or "").strip()
        symbol = str(
            token_info.get("symbol")
            or ((row.get("content") or {}).get("metadata") or {}).get("symbol")
            or (mint[:6] if mint else "")
        ).upper()
        if not symbol:
            continue
        quote: Decimal | None = None
        price_info = token_info.get("price_info") if isinstance(token_info.get("price_info"), dict) else {}
        if price_info.get("price_per_token") is not None:
            quote = _decimal(price_info.get("price_per_token"))
            if quote <= 0:
                quote = None
        items.append(
            ChainBalance(
                source="helius",
                asset=symbol,
                amount=amount,
                address=address,
                chain="solana",
                contract=mint or None,
                quote_usdt=quote,
            )
        )
    return items


def parse_token_accounts(address: str, payload: dict[str, Any]) -> list[ChainBalance]:
    """Parse ``getTokenAccountsByOwner`` jsonParsed result into SPL balances."""
    result = payload.get("result") if "result" in payload else payload
    if not isinstance(result, dict):
        return []
    items: list[ChainBalance] = []
    for row in result.get("value") or []:
        if not isinstance(row, dict):
            continue
        info = (
            ((row.get("account") or {}).get("data") or {}).get("parsed") or {}
        ).get("info") or {}
        if not isinstance(info, dict):
            continue
        mint = str(info.get("mint") or "").strip()
        token_amount = info.get("tokenAmount") if isinstance(info.get("tokenAmount"), dict) else {}
        decimals_raw = token_amount.get("decimals")
        try:
            decimals = int(decimals_raw) if decimals_raw is not None else -1
        except (TypeError, ValueError):
            decimals = -1
        if decimals <= 0:
            continue
        ui = token_amount.get("uiAmount")
        if ui is not None:
            amount = _decimal(ui)
        else:
            raw = token_amount.get("amount")
            if raw is None:
                continue
            try:
                amount = _decimal(Decimal(int(str(raw))) / (Decimal(10) ** decimals))
            except (TypeError, ValueError, InvalidOperation):
                continue
        if amount <= 0 or not mint:
            continue
        asset = mint[:6].upper()
        items.append(
            ChainBalance(
                source="helius",
                asset=asset,
                amount=amount,
                address=address,
                chain="solana",
                contract=mint,
            )
        )
    return items


def describe_helius_error(exc: BaseException) -> str:
    """Human-readable error without URLs, query strings, or API keys."""
    if isinstance(exc, HeliusError):
        return str(exc)
    if isinstance(exc, httpx.TimeoutException):
        return "Solana 余额查询超时，请稍后再试"
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code if exc.response is not None else 0
        if status in {401, 403}:
            return "Helius API Key 无效或已过期，请检查配置"
        if status == 429:
            return "Helius 请求过于频繁，请稍后再试"
        if status == 404:
            return "Helius 接口不可用（已下线或地址无效）"
        return f"Solana 余额查询失败（HTTP {status}）"
    if isinstance(exc, httpx.RequestError):
        return "无法连接 Helius，请检查网络后重试"
    return f"Solana 余额查询失败（{type(exc).__name__}）"


class SolanaHeliusAdapter:
    source = "solana"

    def __init__(
        self,
        addresses: list[str] | tuple[str, ...],
        *,
        api_key: str = "",
        client: httpx.Client | None = None,
        fixture_path: Path | None = None,
        balance_fixture_path: Path | None = None,
        use_mock: bool = False,
    ) -> None:
        self.addresses = [addr for addr in addresses if addr]
        self._api_key = api_key
        self._client = client
        self._fixture = fixture_path or FIXTURE_PATH
        self._balance_fixture = balance_fixture_path or BALANCE_FIXTURE_PATH
        self._use_mock = use_mock or not api_key
        self.warnings: list[str] = []

    def fetch_transfers(self, since: datetime) -> list[NormalizedTransfer]:
        """Enhanced transactions since ``since``. Pre-``since`` rows are dropped.

        The legacy balances endpoint is gone (404), but ``/transactions`` still
        works. Callers must pass a ``since`` at or after the user's returns
        start (admin: 2026-10-23) so historical SOL flow does not flood pending.
        """
        since_utc = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
        if self._use_mock or not self._api_key:
            parsed = self._from_fixture()
            return [item for item in parsed if item.ts >= since_utc]

        client = self._client or quiet_client(timeout=20.0)
        close = self._client is None
        collected: list[NormalizedTransfer] = []
        try:
            for address in self.addresses:
                try:
                    response = client.get(
                        HELIUS_TX_URL.format(address=address),
                        params={"api-key": self._api_key, "limit": 100},
                    )
                    if response.status_code in {404, 410}:
                        logger.warning(
                            "solana transfers unavailable for %s… (%s); balances-only this run",
                            address[:6],
                            response.status_code,
                        )
                        continue
                    response.raise_for_status()
                    collected.extend(parse_helius_transactions(address, response.json()))
                except httpx.HTTPError as exc:
                    # Do not put request.url into the log — it carries api-key=.
                    logger.warning(
                        "solana transfers failed for %s… (%s)",
                        address[:6],
                        describe_helius_error(exc),
                    )
                    raise HeliusError(describe_helius_error(exc)) from None
        finally:
            if close:
                client.close()
        return [item for item in collected if item.ts >= since_utc]

    def fetch_balances(self) -> list[ChainBalance]:
        """Current SOL (getBalance) + SPL (DAS, fallback token accounts).

        Live mode never falls back to fixtures. Mock mode reads the fixture.
        """
        self.warnings = []
        if self._use_mock or not self._api_key:
            return self._balances_from_fixture()
        client = self._client or quiet_client(timeout=20.0)
        close = self._client is None
        collected: list[ChainBalance] = []
        try:
            for address in self.addresses:
                collected.extend(self._live_balances_for(client, address))
        except Exception as exc:  # noqa: BLE001 - rewrap without leaking URL/key
            if isinstance(exc, HeliusError):
                raise
            raise HeliusError(describe_helius_error(exc)) from None
        finally:
            if close:
                client.close()
        return collected

    def _live_balances_for(self, client: httpx.Client, address: str) -> list[ChainBalance]:
        rows: list[ChainBalance] = []
        sol = self._rpc_get_balance(client, address)
        if sol > 0:
            rows.append(
                ChainBalance(
                    source="helius",
                    asset="SOL",
                    amount=sol,
                    address=address,
                    chain="solana",
                    contract=WSOL_MINT,
                )
            )
        try:
            spl = self._rpc_das_fungibles(client, address)
        except HeliusError as exc:
            logger.warning(
                "DAS getAssetsByOwner failed for %s… (%s); falling back to token accounts",
                address[:6],
                exc,
            )
            spl = self._rpc_token_accounts(client, address)
        rows.extend(spl)
        return rows

    def _rpc_url(self) -> str:
        # Key stays in the query string (Helius convention). Never log this URL.
        return f"{HELIUS_RPC_URL}?api-key={self._api_key}"

    def _rpc(self, client: httpx.Client, method: str, params: Any) -> dict[str, Any]:
        try:
            response = client.post(
                self._rpc_url(),
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            )
        except httpx.TimeoutException as exc:
            raise HeliusError(describe_helius_error(exc)) from None
        except httpx.RequestError as exc:
            raise HeliusError(describe_helius_error(exc)) from None
        if response.status_code in {401, 403, 429}:
            raise HeliusError(describe_helius_error(httpx.HTTPStatusError(
                f"status {response.status_code}",
                request=response.request,
                response=response,
            )))
        if response.status_code >= 400:
            raise HeliusError(describe_helius_error(httpx.HTTPStatusError(
                f"status {response.status_code}",
                request=response.request,
                response=response,
            )))
        try:
            payload = response.json()
        except ValueError as exc:
            raise HeliusError("Helius 返回了无法解析的响应") from None
        if not isinstance(payload, dict):
            raise HeliusError("Helius 返回了无法解析的响应")
        error = payload.get("error")
        if isinstance(error, dict):
            message = str(error.get("message") or "RPC error")[:120]
            # Strip anything that looks like a key just in case.
            if self._api_key and self._api_key in message:
                message = message.replace(self._api_key, "***")
            raise HeliusError(f"Solana RPC 调用失败：{message}")
        return payload

    def _rpc_get_balance(self, client: httpx.Client, address: str) -> Decimal:
        payload = self._rpc(client, "getBalance", [address])
        result = payload.get("result") or {}
        lamports = result.get("value") if isinstance(result, dict) else None
        if lamports is None:
            return Decimal("0")
        return _decimal(Decimal(str(lamports)) / _LAMPORTS)

    def _rpc_das_fungibles(self, client: httpx.Client, address: str) -> list[ChainBalance]:
        payload = self._rpc(
            client,
            "getAssetsByOwner",
            {
                "ownerAddress": address,
                "page": 1,
                "limit": 1000,
                "displayOptions": {"showFungible": True, "showNativeBalance": True},
            },
        )
        return parse_das_fungibles(address, payload)

    def _rpc_token_accounts(self, client: httpx.Client, address: str) -> list[ChainBalance]:
        payload = self._rpc(
            client,
            "getTokenAccountsByOwner",
            [
                address,
                {"programId": TOKEN_PROGRAM_ID},
                {"encoding": "jsonParsed"},
            ],
        )
        return parse_token_accounts(address, payload)

    def _balances_from_fixture(self) -> list[ChainBalance]:
        if not self._balance_fixture.is_file():
            return []
        with self._balance_fixture.open(encoding="utf-8") as handle:
            payload = json.load(handle)
        address = self.addresses[0] if self.addresses else "So1DemoAddress"
        rows = parse_helius_balances(address, payload)
        return [replace(row, fixture=True) for row in rows]

    def _from_fixture(self) -> list[NormalizedTransfer]:
        if not self._fixture.is_file():
            return []
        with self._fixture.open(encoding="utf-8") as handle:
            payload = json.load(handle)
        address = self.addresses[0] if self.addresses else "So1DemoAddress"
        return parse_helius_transactions(address, payload)
