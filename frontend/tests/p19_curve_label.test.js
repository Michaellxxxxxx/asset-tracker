// node frontend/tests/p19_curve_label.test.js
// p19: the 完整同步 label sits 6px right of the jump line with a 2px-padded
// #141416 chip and 60% white text, and fixture points can be hidden on the
// client without touching the snapshot API.
const fs = require("fs");
const path = require("path");
const assert = require("assert");

const src = fs.readFileSync(path.join(__dirname, "../static/app.js"), "utf8");
const css = fs.readFileSync(path.join(__dirname, "../static/styles.css"), "utf8");

function grab(name) {
  const start = src.indexOf(`  function ${name}(`);
  assert.ok(start >= 0, `function ${name} not found`);
  const next = src.indexOf("\n  }\n", start);
  return src.slice(start, next + 5);
}

function rule(selector) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = css.match(new RegExp(`${escaped}\\s*\\{([^}]*)\\}`));
  assert.ok(match, `${selector} rule not found`);
  return match[1];
}

assert.ok(src.includes('class="chart-jump-label-bg"'), "label has a background rect");
assert.ok(src.includes("const labelPad = 2;"), "background padding is 2px");
assert.ok(src.includes("const labelY = top + 14;"), "label moved down 4px from top + 10");
assert.ok(src.includes("const textX = flip ? x - 6 : x + 6;"), "label sits 6px beside the line");
assert.ok(
  src.includes('width="${(labelWidth + labelPad * 2).toFixed(2)}"'),
  "background is text width plus both 2px paddings",
);
assert.ok(
  src.indexOf("${jumpMarkup}") > src.indexOf("chart-line--reveal"),
  "label is painted after the area/line so the fill cannot tint it gold",
);

const label = rule(".chart-jump-label");
assert.match(label, /fill:\s*rgba\(255,\s*255,\s*255,\s*0\.6\);/);
assert.match(label, /stroke:\s*none;/);
const chip = rule(".chart-jump-label-bg");
assert.match(chip, /fill:\s*#141416;/);
assert.ok(!css.includes(".chart-jump,\n.chart-jump-label"), "label is no longer in the gold rule");

const normalize = new Function(
  "asNumber",
  "dateObject",
  "INCLUDE_FIXTURE_POINTS",
  `${grab("normalizeOverviewSeries")}; return normalizeOverviewSeries;`,
)(
  (value) => {
    const n = Number(value);
    return Number.isFinite(n) ? n : 0;
  },
  (value) => {
    if (value == null || value === "") return null;
    const d = new Date(value);
    return Number.isNaN(d.getTime()) ? null : d;
  },
  false,
);

const points = [
  { ts: "2026-10-28T12:30:46Z", nav_usdt: 29792.65, provenance: null, live: null },
  { ts: "2026-10-28T12:36:23Z", nav_usdt: 16483.1, provenance: "fixture", live: false },
  { ts: "2026-10-28T13:18:48Z", nav_usdt: 29745.39, provenance: "live", live: true },
  { ts: "2026-10-28T14:18:48Z", nav_usdt: 29700, provenance: "demo", live: false },
  { ts: "not-a-date", nav_usdt: 1, provenance: "fixture", live: false },
];
const hidden = normalize({ series: points });
assert.deepStrictEqual(
  hidden.map((p) => p.nav_usdt),
  [29792.65, 29745.39, 29700],
  "fixture points and invalid timestamps are skipped",
);
assert.strictEqual(hidden[1].provenance, "live");
assert.strictEqual(hidden[1].live, true);
assert.strictEqual(hidden[2].provenance, "demo", "demo points stay visible");

const normalizeAll = new Function(
  "asNumber",
  "dateObject",
  "INCLUDE_FIXTURE_POINTS",
  `${grab("normalizeOverviewSeries")}; return normalizeOverviewSeries;`,
)(
  (value) => Number(value) || 0,
  (value) => {
    if (value == null || value === "") return null;
    const d = new Date(value);
    return Number.isNaN(d.getTime()) ? null : d;
  },
  true,
);
assert.strictEqual(normalizeAll({ series: points }).length, 4, "show_fixtures=1 keeps fixture points");
assert.ok(src.includes('get("show_fixtures") === "1"'), "switch is a front-end URL flag only");

// p19 add-on: half snapshots flagged partial by the API are skipped too.
const withPartial = normalize({
  series: [
    { ts: "2026-10-28T12:30:46Z", nav_usdt: 29792.65, provenance: null, live: null, partial: false },
    { ts: "2026-10-28T12:36:23Z", nav_usdt: 16483.1, provenance: null, live: null, partial: true },
    { ts: "2026-10-28T13:18:48Z", nav_usdt: 29745.39, provenance: null, live: null },
  ],
});
assert.deepStrictEqual(withPartial.map((p) => p.nav_usdt), [29792.65, 29745.39], "partial (id 665-like) point skipped");
assert.match(rule(".chart-jump-label"), /font-size:\s*max\(var\(--fs-min\),\s*12px\);/);

// p19 P1: y axis min span and dashed clamp.
const extent = new Function(`${grab("chartYExtent")}; return chartYExtent;`)();
{
  const e = extent([29700, 29800, 29750]); // raw span 100 < 4% of 29750 = 1190
  const span = 29750 * 0.04;
  assert.ok(Math.abs(e.max - e.min - span * 1.16) < 1e-6, "min span 4% of last value plus 8% each side");
  assert.ok(Math.abs((e.max + e.min) / 2 - 29750) < 1e-6, "centred on data midpoint");
  const wide = extent([10000, 20000]);
  assert.ok(Math.abs(wide.min - (10000 - 800)) < 1e-6 && Math.abs(wide.max - (20000 + 800)) < 1e-6, "real span used when wider");
}
assert.ok(
  src.includes("jumpIndex > 0 && index < jumpIndex ? min : value") &&
    src.includes("clampY(yRaw(plotValue(value, index)))"),
  "pre-sync dashed values are clamped to the axis floor",
);
{
  // dashed clamp behaviour: pre-jump values map to the bottom edge
  const min = 100, jumpIndex = 3;
  const plotValue = (value, index) => (jumpIndex > 0 && index < jumpIndex ? min : value);
  assert.deepStrictEqual([5, 500, 99999, 120].map(plotValue), [100, 100, 100, 120]);
}

console.log("p19_curve_label: ok");
