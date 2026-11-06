"""Re-encrypt api_credentials from an old MASTER_KEY to a new one.

Keys come only from --old-key / --new-key, or OLD_MASTER_KEY / NEW_MASTER_KEY.
This script never reads settings.master_key.

Usage (from backend/):

    python -m scripts.rotate_master_key --old-key <64 hex> --new-key <64 hex> --yes
    python -m scripts.rotate_master_key --dry-run

A row already decryptable with the new key is skipped. A row that matches
neither key aborts the transaction. --dry-run reports counts and writes nothing.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.crypto import CryptoConfigError, decrypt_with_key, encrypt_with_key, key_bytes
from app.models import ApiCredential


@dataclass(frozen=True)
class RotateResult:
    migrated: int
    skipped: int
    total: int

    def render(self, *, dry_run: bool) -> str:
        if dry_run:
            return f"would_migrate={self.migrated} skipped={self.skipped} total={self.total}"
        return f"migrated={self.migrated} skipped={self.skipped} total={self.total}"


class RotateError(RuntimeError):
    pass


def _resolve_key(cli_value: str | None, env_name: str, label: str) -> str:
    raw = cli_value if cli_value else os.environ.get(env_name, "")
    raw = (raw or "").strip()
    if not raw:
        raise RotateError(f"{label} is required (CLI or {env_name})")
    try:
        key_bytes(raw)
    except CryptoConfigError as exc:
        raise RotateError(f"{label} is not valid 64-hex / 32 bytes") from exc
    return raw


def _try_decrypt(key_hex: str, payload: bytes) -> str | None:
    try:
        return decrypt_with_key(key_hex, payload)
    except (InvalidTag, ValueError):
        return None


def rotate_credentials(
    session: Session,
    old_key: str,
    new_key: str,
    *,
    dry_run: bool = False,
) -> RotateResult:
    """Re-encrypt every api_credentials row. Caller owns the transaction.

    On a row that neither key can open, raises RotateError before any flush
    when dry_run is set; otherwise the caller should roll the session back.
    """
    rows = list(session.scalars(select(ApiCredential).order_by(ApiCredential.id)))
    migrated = 0
    skipped = 0
    for row in rows:
        payload = bytes(row.encrypted_payload)
        opened_with_new = _try_decrypt(new_key, payload)
        if opened_with_new is not None:
            skipped += 1
            continue
        plaintext = _try_decrypt(old_key, payload)
        if plaintext is None:
            raise RotateError(
                f"api_credentials id={row.id} cannot be decrypted with old or new key"
            )
        if not dry_run:
            row.encrypted_payload = encrypt_with_key(new_key, plaintext)
        migrated += 1
    return RotateResult(migrated=migrated, skipped=skipped, total=len(rows))


def _database_url(explicit: str | None) -> str:
    url = (explicit or os.environ.get("DATABASE_URL") or "").strip()
    if not url:
        raise RotateError("DATABASE_URL is required (CLI --database-url or env)")
    return url


def run(
    *,
    old_key: str,
    new_key: str,
    database_url: str,
    dry_run: bool,
) -> RotateResult:
    engine = create_engine(database_url, future=True)
    try:
        with Session(engine) as session:
            try:
                result = rotate_credentials(session, old_key, new_key, dry_run=dry_run)
                if dry_run:
                    session.rollback()
                else:
                    session.commit()
            except Exception:
                session.rollback()
                raise
        return result
    finally:
        engine.dispose()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Rotate MASTER_KEY for api_credentials")
    parser.add_argument("--old-key", default=None, help="64 hex chars; else OLD_MASTER_KEY")
    parser.add_argument("--new-key", default=None, help="64 hex chars; else NEW_MASTER_KEY")
    parser.add_argument("--database-url", default=None, help="defaults to DATABASE_URL")
    parser.add_argument("--dry-run", action="store_true", help="report counts, write nothing")
    parser.add_argument("--yes", action="store_true", help="required to write unless --dry-run")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        old_key = _resolve_key(args.old_key, "OLD_MASTER_KEY", "old key")
        new_key = _resolve_key(args.new_key, "NEW_MASTER_KEY", "new key")
        if old_key == new_key:
            raise RotateError("old key and new key must differ")
        if not args.dry_run and not args.yes:
            raise RotateError("refusing to write without --yes (or pass --dry-run)")
        result = run(
            old_key=old_key,
            new_key=new_key,
            database_url=_database_url(args.database_url),
            dry_run=args.dry_run,
        )
    except RotateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(result.render(dry_run=args.dry_run))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
