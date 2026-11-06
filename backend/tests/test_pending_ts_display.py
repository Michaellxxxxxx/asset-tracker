"""Phase-4: the pending card's Beijing-time formatter.

app.js has no JS test runner, so this test evaluates the exact helper slice
from the shipped file under Node. Skipped where Node is unavailable.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[2] / "frontend" / "static" / "app.js"

_HARNESS = r"""
const failures = [];
function eq(actual, expected, label) {
  if (actual !== expected) failures.push(`${label}: ${JSON.stringify(actual)} != ${JSON.stringify(expected)}`);
}
function ok(value, label) {
  if (!value) failures.push(label);
}

eq(formatOccurredAt("2026-01-06T04:20:00Z"), "2026-01-06 12:20", "Z suffix");
eq(formatOccurredAt("2026-01-06T04:20:00+00:00"), "2026-01-06 12:20", "offset suffix");
eq(formatOccurredAt("2026-01-06T04:20:00"), "2026-01-06 12:20", "zone-less treated as UTC");
eq(formatOccurredAt("2026-01-06T12:20:00+08:00"), "2026-01-06 12:20", "already Beijing");
eq(formatOccurredAt("2026-01-06 04:20:00"), "2026-01-06 12:20", "space separator");
eq(formatOccurredAt(""), "", "empty ts renders nothing");
eq(formatOccurredAt(null), "", "null ts renders nothing");
eq(formatOccurredAt("not-a-date"), "", "invalid ts renders nothing");
eq(parseOccurredAt("not-a-date"), null, "invalid parse is null");

eq(formatCardDate("2026-10-23T15:27:00Z"), "2026-10-23", "card day is hyphenated UTC");
eq(formatCardDate("2026-10-23T15:27:00Z", true), "2026-10-23 15:27", "card datetime is hyphenated UTC");
eq(formatCardDate("2026-10-23T20:00:00Z", true, true), "2026-10-24 04:00", "Beijing datetime crosses midnight");
eq(formatCardDate(null), "—", "missing card day is a dash");

const markup = occurredMarkup({ ts: "2026-01-06T04:20:00Z" });
ok(markup.includes("发生于"), "markup has 发生于");
ok(markup.includes("2026-01-06 12:20"), "markup has formatted instant");
ok(markup.includes(" · 北京时间"), "markup has unparenthesized Beijing label");
ok(!markup.includes("（北京时间）"), "markup drops the parenthesized hint");
eq(occurredMarkup({ ts: null }), "", "missing ts omits row");

const old = new Date(Date.now() - 12 * 86400000).toISOString();
ok(occurredMarkup({ ts: old }).includes("12 天前"), "stale row shows relative days");
ok(!occurredMarkup({ ts: new Date().toISOString() }).includes("天前"), "fresh row has no relative days");

if (failures.length) {
  console.error(failures.join("\n"));
  process.exit(1);
}
"""


def test_frontend_occurred_time_formatter() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    source = APP_JS.read_text(encoding="utf-8")
    start = source.index("function parseOccurredAt(value) {")
    end = source.index("function pendingSkeleton()")
    script = source[start:end] + _HARNESS
    result = subprocess.run(
        [node, "-"],
        input=script,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
