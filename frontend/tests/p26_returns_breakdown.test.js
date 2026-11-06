// node --test frontend/tests/p26_returns_breakdown.test.js
// p26 r1: block order, collapsed asset list (first 10 + remainder), range
// labels, renamed sections, and flat +0.00/-0.00 rendering.
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const src = fs.readFileSync(path.join(__dirname, "../static/returns_breakdown.js"), "utf8");
const sandbox = { window: {}, Intl };
vm.createContext(sandbox);
vm.runInContext(src, sandbox);
const rb = sandbox.window.ReturnsBreakdown;

assert.ok(rb, "ReturnsBreakdown global is missing");

// ---- formatting: signs only on real moves, flat zero has no colour ---------
assert.strictEqual(rb.formatAmount(-44.09), "-44.09");
assert.strictEqual(rb.formatAmount(1786.5), "+1,786.50");
assert.strictEqual(rb.formatAmount(0), "0.00");
// P2: +/-0.00 collapse to plain 0.00 (and get .is-flat, white 48%).
assert.strictEqual(rb.formatAmount(0.004), "0.00");
assert.strictEqual(rb.formatAmount(-0.004), "0.00");
assert.strictEqual(rb.formatAmount(null), "—");
assert.strictEqual(rb.formatRate(-0.004241), "-0.42%");
assert.strictEqual(rb.formatRate(0.011971), "+1.20%");
assert.strictEqual(rb.formatRate(0), "0.00%");
assert.strictEqual(rb.formatRate(-0.0000001), "0.00%");
assert.strictEqual(rb.formatRate(null), "—");
assert.strictEqual(rb.toneClass(-0.0042), "is-negative");
assert.strictEqual(rb.toneClass(0.00000001), "is-flat");

// ---- r1 range labels: start–end, not the raw bucket key ------------------
assert.strictEqual(
  rb.segmentLabel(
    { key: "2025-W43", start: "2025-10-19T16:00:00Z", end: "2025-10-26T16:00:00Z" },
    "week",
  ),
  "10.20–10.26",
  "week label is the inclusive ISO range (2025-W43)",
);
assert.strictEqual(rb.weekNumberLabel({ key: "2025-W43" }), "第43周");
assert.strictEqual(
  rb.segmentLabel({ key: "2025-10-28", start: "2025-10-28T00:00:00+08:00" }, "day"),
  "10.28 周二",
  "day label carries the weekday (2025-10-28 was a Tuesday)",
);
assert.strictEqual(rb.segmentLabel({ key: "2026-10" }, "month"), "2026年10月");

// ---- sorting + split: |amount| desc, >=0.01 only, first 10 ---------------
const sorted = rb.sortAssets([
  { asset: "small", amount_usdt: 0.4 },
  { asset: "big-loss", amount_usdt: -18.52 },
  { asset: "medium", amount_usdt: 1.7 },
]);
assert.deepStrictEqual(sorted.map((row) => row.asset), ["big-loss", "medium", "small"]);

const manyAssets = [];
for (let index = 0; index < 12; index += 1) {
  manyAssets.push({
    asset: "ASSET-" + index,
    asset_class_label: "加密资产",
    amount_usdt: 20 - index,
    rate: 0.001,
  });
}
const split = rb.splitAssets(manyAssets);
assert.strictEqual(split.visible.length, 10, "only the first ten rows stay visible");
assert.strictEqual(split.rest.length, 2, "the rest folds into one summary row");
assert.strictEqual(split.count, 2);
assert.ok(
  split.visible.every((row) => Math.abs(row.amount_usdt) >= rb.MIN_ASSET_AMOUNT),
  "visible rows all clear the 0.01 floor",
);
assert.strictEqual(
  Math.round(split.total * 100) / 100,
  19,
  "folded total is the exact sum of the hidden rows",
);
assert.strictEqual(rb.moreSummaryText(2, split.total), "其余 2 项 · 合计 +19.00");

// Sub-cent rows are always folded, even when fewer than ten material rows.
const tinySplit = rb.splitAssets([
  { asset: "A", amount_usdt: 5 },
  { asset: "B", amount_usdt: 4 },
  { asset: "C", amount_usdt: 3 },
  { asset: "D", amount_usdt: 2 },
  { asset: "TINY", amount_usdt: 0.004 },
]);
assert.strictEqual(tinySplit.visible.length, 4, "sub-cent holdings never count toward the ten");
assert.strictEqual(tinySplit.count, 1);
assert.strictEqual(tinySplit.rest[0].asset, "TINY");

// ---- sample API payload --------------------------------------------------
const data = {
  period: "month",
  return_amount_usdt: -44.09,
  return_rate: -0.004241,
  segments: {
    month: [{ key: "2026-10", label: "2026-10", amount_usdt: -44.09, rate: -0.004241 }],
    week: [
      { key: "2026-W43", label: "W43", start: "2026-10-18T16:00:00Z", end: "2026-10-25T16:00:00Z", amount_usdt: -11.13, rate: -0.00107 },
      { key: "2026-W44", label: "W44", start: "2026-10-25T16:00:00Z", end: "2026-11-01T16:00:00Z", amount_usdt: -32.96, rate: -0.003174 },
    ],
    day: [
      { key: "2026-10-23", label: "10-23", start: "2026-10-23T00:00:00+08:00", amount_usdt: -12.56, rate: -0.001208 },
      { key: "2026-10-25", label: "10-25", start: "2026-10-25T00:00:00+08:00", amount_usdt: null, rate: null, missing: "no_snapshot" },
    ],
  },
  assets: [
    { asset: "BTC", asset_class_label: "加密资产", amount_usdt: 1.7, rate: 0.001708 },
    { asset: "PLP-SUSDX-3DEC2026", asset_class_label: "DeFi", amount_usdt: -18.52, rate: -0.00308 },
    { asset: "ZERO", asset_class_label: "未分类", amount_usdt: -1.0, rate: null },
  ],
  unattributed_usdt: 14.39,
  unattributed_reason: "数量变化、无价资产与覆盖变化区间的差额。",
  assets_missing: false,
  sum_check: {},
};

// ---- r1 terminology + order inside the widget ---------------------------
const amountMarkup = rb.widgetMarkup(data, "month", { mode: "amount", unit: "week" });
assert.ok(amountMarkup.includes("收益拆解"), "page-level block title");
assert.ok(amountMarkup.includes("分段收益"), "segment card title");
assert.ok(amountMarkup.includes("各资产涨跌"), "asset card title");
assert.ok(!amountMarkup.includes("资产涨跌明细"), "old asset title is gone");
assert.ok(!amountMarkup.includes("分段表现"), "old segment title is gone");
assert.ok(!amountMarkup.includes("价格效应 ·"), "asset kicker is removed");
assert.ok(!amountMarkup.includes("区间拆解"), "old widget kicker is removed");
assert.ok(
  amountMarkup.indexOf("rb-segments") < amountMarkup.indexOf("rb-assets"),
  "segments come before assets",
);
assert.ok(
  amountMarkup.indexOf("rb-top") < amountMarkup.indexOf("rb-segments"),
  "headline + 额/率 switch come first",
);

// ---- week/day labels + card-title toggle --------------------------------
assert.ok(amountMarkup.includes("10.19–10.25"), "week range label");
assert.ok(amountMarkup.includes("第43周"), "week number caption");
assert.ok(!amountMarkup.includes(">W43<"), "raw W43 label is gone");
assert.ok(amountMarkup.includes("按周") && amountMarkup.includes("按日"), "month sub-toggle");
assert.ok(amountMarkup.includes("按周 · 时间加权"), "week grain kicker");
const segmentHead = amountMarkup.slice(
  amountMarkup.indexOf("rb-segments"),
  amountMarkup.indexOf("rb-note"),
);
assert.ok(
  segmentHead.includes("rb-units") && segmentHead.includes("分段收益"),
  "week/day toggle sits in the segment card title row",
);
assert.ok(amountMarkup.includes('data-rb-mode="amount" aria-pressed="true"'), "amount is pressed");
assert.ok(amountMarkup.includes("-18.52"), "amount cell");
assert.ok(amountMarkup.includes("额按区间起点净值折算"), "nav0 footnote");

// ---- r1 collapsed asset list -------------------------------------------
// A 12-row payload folds exactly two rows.
const foldData = {
  ...data,
  assets: manyAssets.map((row) => ({ ...row, asset_class_label: "加密资产" })),
  unattributed_usdt: 0.004,
};
const foldMarkup = rb.widgetMarkup(foldData, "month", { mode: "amount", unit: "week" });
assert.ok(foldMarkup.includes("rb-assets is-collapsed"), "asset list opens collapsed");
assert.ok(foldMarkup.includes("data-rb-expand"), "collapsed row has a toggle");
assert.ok(foldMarkup.includes('aria-expanded="false"'), "toggle starts collapsed");
assert.ok(foldMarkup.includes('data-rb-more'), "rest panel exists");
assert.ok(foldMarkup.includes('aria-hidden="true"'), "rest panel starts hidden");
assert.ok(foldMarkup.includes("rb-more-text"), "fold summary row is present");
assert.match(foldMarkup, /其余 2 项 · 合计[\s\S]*?[+\-]?[\d,]+\.\d{2}/, "其余 N 项 · 合计 ±x.xx");
assert.ok(
  foldMarkup.indexOf("ASSET-10") > foldMarkup.indexOf("rb-more-panel"),
  "folded assets live inside the expansion panel",
);
assert.ok(
  foldMarkup.indexOf("rb-unattributed") > foldMarkup.indexOf("rb-more-row"),
  "unattributed stays pinned below the fold row",
);
// Three material assets need no summary row at all.
assert.ok(!amountMarkup.includes("rb-more-text"), "no fold row when everything fits");

const expandedMarkup = rb.widgetMarkup(foldData, "month", {
  mode: "amount",
  unit: "week",
  assetsExpanded: true,
});
assert.ok(expandedMarkup.includes("rb-assets"), "expanded asset list");
assert.ok(!expandedMarkup.includes("rb-assets is-collapsed"), "expanded list is not collapsed");
assert.ok(expandedMarkup.includes('aria-expanded="true"'), "toggle reflects expanded state");
assert.ok(expandedMarkup.includes("rb-more-panel is-expanded"), "panel reflects expanded state");
assert.ok(expandedMarkup.includes('aria-hidden="false"'), "expanded panel is exposed");

// ---- rate mode + missing render -----------------------------------------
const rateMarkup = rb.widgetMarkup(data, "month", { mode: "rate", unit: "day" });
assert.ok(rateMarkup.includes('data-rb-mode="rate" aria-pressed="true"'), "rate is pressed");
assert.ok(rateMarkup.includes("-0.31%"), "asset rate");
assert.ok(rateMarkup.includes("10.23 周五"), "day range label");
assert.ok(rateMarkup.includes("无快照"), "missing bucket text");
assert.ok(rateMarkup.includes("缺失"), "zero-denominator asset rate is missing");
const missingRow = rateMarkup.match(/<tr class="rb-row is-missing"[^>]*>[\s\S]*?<\/tr>/)[0];
assert.strictEqual(
  (missingRow.match(/<t[dh][ >]/g) || []).length,
  2,
  "missing row has exactly the two columns of the table",
);
assert.ok(!rateMarkup.includes("-18.52"), "rate mode hides amounts");

// ---- year uses months, week uses days ----------------------------------
const yearMarkup = rb.widgetMarkup(data, "year", { mode: "amount", unit: "day" });
assert.ok(yearMarkup.includes("2026年10月"), "year renders month labels");
assert.ok(yearMarkup.includes("按月"), "year labels the month grain");
assert.ok(!yearMarkup.includes("第43周"), "year never shows week rows");
const weekMarkup = rb.widgetMarkup(data, "week", { mode: "amount", unit: "week" });
assert.ok(weekMarkup.includes("10.23 周五"), "week renders day labels");
assert.ok(weekMarkup.includes("按日"), "week labels the day grain");
assert.ok(!weekMarkup.includes("第43周"), "week never shows week rows");
assert.ok(!weekMarkup.includes("data-rb-unit"), "week has no sub-toggle");

// ---- day view has no segment table --------------------------------------
const dayMarkup = rb.widgetMarkup(data, "day", { mode: "amount", unit: "day" });
assert.ok(!dayMarkup.includes("rb-segments"), "day view has no segment section");
assert.ok(!dayMarkup.includes("分段收益"), "day view has no segment title");
assert.ok(!dayMarkup.includes("按周"), "day view has no unit switch");
assert.ok(dayMarkup.includes("各资产涨跌"), "day view still shows assets");

// ---- assets missing ------------------------------------------------------
const missingMarkup = rb.widgetMarkup(
  { ...data, assets: [], assets_missing: true, unattributed_usdt: null },
  "week",
  { mode: "amount", unit: "day" },
);
assert.ok(missingMarkup.includes("缺失"), "missing assets state");
assert.ok(missingMarkup.includes("持仓"), "missing reason is rendered");

// ---- P2 flat zero inside a table cell -----------------------------------
const flatMarkup = rb.widgetMarkup(
  { ...data, assets: [{ asset: "FLAT", asset_class_label: "现金", amount_usdt: 0.004, rate: -0.0000001 }] },
  "month",
  { mode: "amount", unit: "week" },
);
assert.match(flatMarkup, /<span class="is-flat">0\.00<\/span>/, "flat amount is colourless 0.00");
assert.ok(!flatMarkup.includes("+0.00") && !flatMarkup.includes("-0.00"), "no signed zero");

// ---- wiring / styling ----------------------------------------------------
const css = fs.readFileSync(path.join(__dirname, "../static/returns_breakdown.css"), "utf8");
assert.match(css, /font-variant-numeric:\s*tabular-nums/);
assert.match(css, /\.rb-table tbody td\s*\{[^}]*text-align:\s*right/);
assert.match(css, /\.rb-headline\s*\{[^}]*font-size:\s*20px/);
assert.match(css, /\.rb-units\.segmented\s*\{[^}]*width:\s*132px/);
assert.match(css, /\.rb-units \.segment\[aria-pressed="true"\][\s\S]*?background:\s*var\(--accent-soft\)/);
assert.match(css, /\.rb-table--assets tbody tr\s*\{[^}]*height:\s*52px/);
assert.match(css, /\.rb-asset\s*\{[^}]*text-overflow:\s*ellipsis/);
assert.match(css, /\.rb-asset-cell\s*\{[^}]*flex-wrap:\s*nowrap/);
assert.match(css, /\.rb-unattributed th,\s*\.rb-unattributed td\s*\{[^}]*rgba\(255, 255, 255, 0\.08\)/);
assert.match(
  css,
  /\.rb-unattributed \.rb-tag\s*\{[^}]*color:\s*rgba\(255, 255, 255, 0\.6\)[^}]*font-size:\s*15px/s,
);
assert.match(css, /\.rb-more-panel\s*\{[^}]*grid-template-rows:\s*0fr[^}]*var\(--t-base\)/s);
assert.match(css, /\.rb-table \.is-flat\s*\{[^}]*rgba\(255, 255, 255, 0\.48\)/);
assert.match(
  css,
  /\.rb-week-no\s*\{[^}]*color:\s*rgba\(255, 255, 255, 0\.48\)[^}]*font-size:\s*12px/s,
);
assert.match(css, /prefers-reduced-motion: reduce/);

const app = fs.readFileSync(path.join(__dirname, "../static/app.js"), "utf8");
assert.ok(app.includes('data-returns-breakdown'));
assert.ok(app.includes("ReturnsBreakdown.mount"));
// P0-1: period switch -> hero -> breakdown (segments/assets) -> right rail.
assert.ok(
  app.indexOf('<div class="return-content" id="returns-content">') > app.indexOf("${segmentedMarkup()}"),
  "period switch precedes the returns content",
);
assert.ok(
  app.indexOf('<section class="return-hero">') < app.indexOf('data-returns-breakdown'),
  "hero precedes the breakdown",
);
assert.ok(
  app.indexOf('data-returns-breakdown') < app.indexOf("${returnsSideMarkup(data, series, trend)}"),
  "breakdown precedes the range profile / readout rail",
);
assert.ok(app.indexOf('<div class="returns-main">') < app.indexOf("${returnsSideMarkup"), "main column before side rail");
assert.ok(!app.includes('class="return-note"'), "old standalone readout note is folded into the side rail");
const index = fs.readFileSync(path.join(__dirname, "../index.html"), "utf8");
assert.ok(index.includes("/static/returns_breakdown.js"));
assert.ok(index.includes("/static/returns_breakdown.css"));

console.log("p26_returns_breakdown: ok");
