"""One-shot / idempotent archive of pre-baseline pending confirmations.

Usage (from repo root, with DATABASE_URL set or docker env):
  cd backend && ../.venv/bin/python -m scripts.archive_prestart_pending
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow ``python -m scripts.archive_prestart_pending`` from backend/
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.archive_prestart import archive_prestart_pending  # noqa: E402
from app.db import SessionLocal  # noqa: E402


def main() -> int:
    db = SessionLocal()
    try:
        result = archive_prestart_pending(db)
    finally:
        db.close()
    print(f"users_scanned={result.users_scanned} archived={result.archived}")
    for name, count in sorted(result.by_user.items()):
        print(f"  {name}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
