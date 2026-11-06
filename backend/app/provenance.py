"""Independent data provenance for NAV snapshots and positions.

``source`` answers *how* a row entered the history (seed / sync / backfill).
``provenance`` answers *what* the numbers are (demo / fixture / live).  The two
are intentionally separate: a sync run on the demo account still writes
``source="sync"`` so the January seed curve is not selected again, while its
provenance is ``demo`` and ``live=False``.

Rows written before p13 have a NULL provenance.  Read paths fall back to the
old ``source`` value so those rows keep rendering; nothing backfills them.
"""

from __future__ import annotations

from typing import Literal

Provenance = Literal["demo", "fixture", "live"]

PROVENANCES = ("demo", "fixture", "live")


def is_demo_user(user: object | None) -> bool:
    """True for the built-in demo account, identified by username only."""
    if user is None:
        return False
    username = user if isinstance(user, str) else getattr(user, "username", None)
    return isinstance(username, str) and username.strip().lower() == "demo"


def classify_provenance(
    *,
    mode: str,
    use_live: bool,
    user: object | None = None,
    has_fixture: bool = False,
) -> str:
    """Classify one snapshot / position batch.

    ``demo`` is reserved for the built-in demo account.  Any other account whose
    batch contains fixture rows (or ran in mock mode) is ``fixture``.  Only a
    fully live read is ``live``.  ``mode`` is accepted for call-site clarity and
    future modes; the decision is driven by ``use_live``/``has_fixture``.
    """
    del mode  # explicit mock/live is already folded into use_live at call sites
    if not use_live or has_fixture:
        return "demo" if is_demo_user(user) else "fixture"
    return "live"


def snapshot_provenance(row: object | None) -> str | None:
    """Stored provenance of a snapshot, falling back to its legacy source."""
    if row is None:
        return None
    value = getattr(row, "provenance", None)
    if value:
        return str(value)
    source = getattr(row, "source", None)
    if source in ("seed", "sync", "backfill"):
        return str(source)
    return None


def data_provenance(row: object | None) -> str | None:
    """The P13 provenance value, or None for legacy rows (no invented value)."""
    value = snapshot_provenance(row)
    return value if value in PROVENANCES else None


def snapshot_live(row: object | None) -> bool | None:
    """Stored live flag; legacy rows with unknown provenance stay NULL.

    A pre-P13 ``source="sync"`` row may itself have been a mock write (the bug
    this phase fixes), so the old source is not evidence of a live read. Only
    seed rows are certainly not live.
    """
    if row is None:
        return None
    value = getattr(row, "live", None)
    if value is not None:
        return bool(value)
    provenance = snapshot_provenance(row)
    if provenance in ("demo", "fixture", "seed"):
        return False
    if provenance == "live":
        return True
    return None
