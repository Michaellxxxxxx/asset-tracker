"""P19: sync points written without the position fold are flagged partial at read time."""
import json
from datetime import datetime, timezone
from types import SimpleNamespace

from app.routers.overview import partial_snapshot_ids


def _row(minute, nav, folded, source="sync"):
    payload = {"holdings": [], "nav_usdt": str(nav)}
    if folded:
        payload["positions_included"] = True
    return SimpleNamespace(
        ts=datetime(2026, 10, 28, 12, minute, tzinfo=timezone.utc),
        nav_usdt=nav,
        source=source,
        breakdown=json.dumps(payload),
    )


def test_half_snapshot_like_665_is_partial():
    legacy = _row(0, 16000, False)  # before the fold existed: left alone
    before = _row(30, 29792.65, True)  # id 631
    spike = _row(36, 16483.10, False)  # id 665
    after = _row(59, 29745.39, True)  # id 667
    ids = partial_snapshot_ids([after, spike, legacy, before])
    assert ids == {id(spike)}


def test_backfill_and_unfolded_users_not_partial():
    rows = [_row(1, 10, False), _row(2, 10, False), _row(3, 10, False, source="backfill")]
    assert partial_snapshot_ids(rows) == set()
