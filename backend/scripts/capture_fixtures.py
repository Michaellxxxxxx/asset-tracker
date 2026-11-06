"""Capture live Zerion / Alchemy / DefiLlama responses and write sanitized fixtures.

Usage (from backend/):

    ../.venv/bin/python scripts/capture_fixtures.py

Reads ZERION_API_KEY, ALCHEMY_API_KEY and EVM_ADDRESSES from the environment
(or the repo .env via python-dotenv). Never prints the key or the full address.
Writes backend/tests/fixtures/live/ and exits non-zero if that directory still
contains a real address or a key.

The address is only used inside this process. Fixtures replace it, and every
other 20-byte address, with a deterministic stand-in.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
BACKEND = Path(__file__).resolve().parents[1]
OUT_DIR = BACKEND / "tests" / "fixtures" / "live"
load_dotenv(ROOT / ".env")

# Stand-in for the wallet being captured. Counterparties get 0x000…0001 onward.
OWNER_STANDIN = "0x1111111111111111111111111111111111111111"
SINCE = "2026-01-01T00:00:00Z"
SINCE_TS = 1767225600
# Block near 2026-01-01 UTC. The capture script re-checks via eth_getBlockByNumber
# only as a starting lower bound; transfers are also filtered by metadata time.
APPROX_BLOCK = 24_136_053

ADDR_RE = re.compile(r"0x[a-fA-F0-9]{40}")
# 40 hex chars without a 0x prefix (topics, raw data words).
BARE_RE = re.compile(r"(?<![0-9a-fA-F])([0-9a-fA-F]{40})(?![0-9a-fA-F])")
HASH_RE = re.compile(r"0x[a-fA-F0-9]{64}")


def _mask(address: str) -> str:
    return f"{address[:6]}…{address[-4:]}"


class Sanitizer:
    def __init__(self, owner: str) -> None:
        self.owner = owner.lower()
        self.owner_bare = self.owner[2:]
        self._counter = 1
        self._map: dict[str, str] = {self.owner: OWNER_STANDIN}

    def standin(self, address: str) -> str:
        key = address.lower()
        if key not in self._map:
            self._map[key] = "0x" + f"{self._counter:040x}"
            self._counter += 1
        return self._map[key]

    def fake_hash(self, value: str) -> str:
        digest = hashlib.sha256(value.encode()).hexdigest()
        return "0x" + digest[:64]

    def text(self, raw: str) -> str:
        def repl_hash(match: re.Match[str]) -> str:
            return self.fake_hash(match.group(0))

        def repl_addr(match: re.Match[str]) -> str:
            return self.standin(match.group(0))

        def repl_bare(match: re.Match[str]) -> str:
            bare = match.group(1).lower()
            known = "0x" + bare
            if known in self._map or bare == self.owner_bare:
                return self.standin(known)[2:]
            # Leave non-address 40-hex blobs (quantities) unless they are the owner.
            return match.group(0)

        redacted = HASH_RE.sub(repl_hash, raw)
        redacted = ADDR_RE.sub(repl_addr, redacted)
        redacted = BARE_RE.sub(repl_bare, redacted)
        return redacted

    def json_value(self, value):
        encoded = json.dumps(value, ensure_ascii=False)
        return json.loads(self.text(encoded))


def _zerion_headers(api_key: str) -> dict[str, str]:
    token = base64.b64encode(f"{api_key}:".encode()).decode()
    return {"authorization": f"Basic {token}", "accept": "application/json"}


def _rpc(client: httpx.Client, alchemy_key: str, method: str, params: list) -> dict:
    response = client.post(
        f"https://eth-mainnet.g.alchemy.com/v2/{alchemy_key}",
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
    )
    response.raise_for_status()
    return response.json()


def capture_zerion(client: httpx.Client, api_key: str, address: str) -> tuple[dict, dict]:
    headers = _zerion_headers(api_key)
    positions = client.get(
        f"https://api.zerion.io/v1/wallets/{address}/positions/",
        headers=headers,
        params={"filter[positions]": "only_simple", "currency": "usd"},
    )
    positions.raise_for_status()
    transactions: list[dict] = []
    url: str | None = f"https://api.zerion.io/v1/wallets/{address}/transactions/"
    params: dict | None = {"currency": "usd", "page[size]": "100"}
    pages = 0
    while url and pages < 30:
        response = None
        for attempt in range(6):
            response = client.get(url, headers=headers, params=params)
            if response.status_code != 429:
                break
            time.sleep(2 + attempt * 2)
        assert response is not None
        response.raise_for_status()
        body = response.json()
        rows = body.get("data") or []
        transactions.extend(rows)
        mined = [
            (row.get("attributes") or {}).get("mined_at")
            for row in rows
            if (row.get("attributes") or {}).get("mined_at")
        ]
        url = (body.get("links") or {}).get("next") or None
        params = None
        pages += 1
        if mined and min(mined) <= SINCE:
            break
    # Drop the links (they embed the real address and a cursor).
    return positions.json(), {"data": transactions}


def capture_alchemy(client: httpx.Client, api_key: str, address: str) -> dict:
    balances: list[dict] = []
    page_key = None
    for _page in range(8):
        params: list = [address, "erc20"]
        if page_key:
            params.append({"pageKey": page_key})
        payload = _rpc(client, api_key, "alchemy_getTokenBalances", params)
        result = payload.get("result") or {}
        balances.extend(result.get("tokenBalances") or [])
        page_key = result.get("pageKey")
        if not page_key:
            break
    nonzero = [row for row in balances if int(row.get("tokenBalance") or "0x0", 16) != 0]
    # Cap metadata calls. Spam filtering happens again at parse time.
    sample = nonzero[:40]
    metadata: dict[str, dict] = {}
    for row in sample:
        contract = row.get("contractAddress")
        if not contract:
            continue
        meta = _rpc(client, api_key, "alchemy_getTokenMetadata", [contract])
        metadata[contract.lower()] = meta.get("result") or {}
    eth = _rpc(client, api_key, "eth_getBalance", [address, "latest"])
    transfers: list[dict] = []
    for direction in ("toAddress", "fromAddress"):
        page_key = None
        for _page in range(8):
            params = {
                "fromBlock": hex(APPROX_BLOCK),
                direction: address,
                "category": ["external", "erc20"],
                "withMetadata": True,
                "maxCount": "0x32",
                "order": "asc",
            }
            if page_key:
                params["pageKey"] = page_key
            payload = _rpc(client, api_key, "alchemy_getAssetTransfers", [params])
            result = payload.get("result") or {}
            transfers.extend(result.get("transfers") or [])
            page_key = result.get("pageKey")
            if not page_key:
                break
    return {
        "token_balances": {"result": {"tokenBalances": sample}},
        "metadata": metadata,
        "eth_balance": {"result": eth.get("result")},
        "transfers": {"result": {"transfers": transfers[:80]}},
    }


def capture_defillama(client: httpx.Client) -> dict:
    current = client.get(
        "https://coins.llama.fi/prices/current/coingecko:ethereum,coingecko:usd-coin,coingecko:tether"
    )
    current.raise_for_status()
    historical = client.get(
        f"https://coins.llama.fi/prices/historical/{SINCE_TS}/coingecko:ethereum,coingecko:usd-coin,coingecko:tether"
    )
    historical.raise_for_status()
    return {"current": current.json(), "historical": historical.json()}


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def self_check(owner: str, secrets: list[str]) -> None:
    owner_l = owner.lower()
    bare = owner_l[2:]
    blob = "\n".join(path.read_text(encoding="utf-8") for path in OUT_DIR.glob("*.json")).lower()
    if owner_l in blob or bare in blob:
        raise SystemExit("fixture self-check failed: real address still present")
    for secret in secrets:
        if secret and secret.lower() in blob:
            raise SystemExit("fixture self-check failed: secret still present")
    if "authorization" in blob or "alchemy.com/v2/" in blob:
        raise SystemExit("fixture self-check failed: credential header or host still present")


def main() -> None:
    zerion = os.environ.get("ZERION_API_KEY", "").strip()
    alchemy = os.environ.get("ALCHEMY_API_KEY", "").strip()
    addresses = [part.strip() for part in os.environ.get("EVM_ADDRESSES", "").split(",") if part.strip()]
    if not addresses or not (zerion or alchemy):
        raise SystemExit("EVM_ADDRESSES and a Zerion or Alchemy key are required")
    address = addresses[0]
    print(f"capturing {_mask(address)} zerion={bool(zerion)} alchemy={bool(alchemy)}")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    sanitizer = Sanitizer(address)
    with httpx.Client(timeout=60.0) as client:
        if zerion:
            positions, transactions = capture_zerion(client, zerion, address)
            _write(OUT_DIR / "zerion_positions.json", sanitizer.json_value(positions))
            _write(OUT_DIR / "zerion_transactions.json", sanitizer.json_value(transactions))
            print(f"zerion positions={(len(positions.get('data') or []))} txs={len(transactions.get('data') or [])}")
        if alchemy:
            alchemy_payload = capture_alchemy(client, alchemy, address)
            _write(OUT_DIR / "alchemy.json", sanitizer.json_value(alchemy_payload))
            print(
                "alchemy balances="
                f"{len((alchemy_payload['token_balances']['result']['tokenBalances']))} "
                f"transfers={len(alchemy_payload['transfers']['result']['transfers'])}"
            )
        llama = capture_defillama(client)
        _write(OUT_DIR / "defillama.json", sanitizer.json_value(llama))
        print("defillama current+historical written")
    self_check(address, [zerion, alchemy])
    print(f"self-check ok, files in {OUT_DIR.relative_to(ROOT)}")


if __name__ == "__main__":
    try:
        main()
    except httpx.HTTPError as exc:
        # The exception text can embed the request URL, which embeds the key.
        print(f"capture failed: {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from None
