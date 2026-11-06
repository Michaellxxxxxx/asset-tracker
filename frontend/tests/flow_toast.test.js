// node frontend/tests/flow_toast.test.js
// p16 top prompt: collapsed/expanded card copy, amount/direction, escaping, reduced motion.
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

const code = [
  "formatCardDate",
  "parseOccurredAt",
  "notifyDirection",
  "notifyPartyLine",
  "notifyAmountMarkup",
  "notifyDateTime",
  "notifyCardBodyMarkup",
  "flowToastMarkup",
].map(grab).join("\n");
const build = new Function(
  "asNumber",
  "money",
  "escapeHtml",
  "shortAddress",
  "counterpartyAddressOf",
  "selectedCategoryOf",
  "categoryLabel",
  "ICONS",
  "dateObject",
  `const NOTIFY_DOWN_SVG = "<svg></svg>";
${code}
return { notifyDirection, notifyPartyLine, notifyAmountMarkup, notifyDateTime, notifyCardBodyMarkup, flowToastMarkup };`,
)(
  (value) => (value === null || value === undefined || value === "" ? NaN : Number(value)),
  (value) => Number(value).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }),
  (value) => String(value).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;"),
  (value) => (String(value || "").length > 12 ? `${String(value).slice(0, 6)}…${String(value).slice(-4)}` : String(value || "")),
  (item) => item.counterparty_address || "",
  (row) => row.suggested_category || null,
  (value) => (value === "transfer_in" ? "转入" : "未分类"),
  { check: "<svg class=check></svg>" },
  (value) => (value ? new Date(value) : null),
);

const item = {
  id: 41,
  status: "pending",
  direction: "in",
  amount_usdt: 1200,
  counterparty_label: "vitalik.eth",
  counterparty_address: "0x8da4c0ffee4045",
  counterparty_chain: "Ethereum",
  nature: "test_data",
  suggested_category: "transfer_in",
  ts: "2026-10-29T08:23:00Z",
};
assert.strictEqual(build.notifyDirection(item), "转入");
assert.strictEqual(build.notifyDirection({ direction: "out" }), "转出");
assert.strictEqual(build.notifyAmountMarkup(item), '<span class="notify-sign">+</span>1,200.00');
assert.strictEqual(build.notifyAmountMarkup({ amount_usdt: -8, direction: "out" }), '<span class="notify-sign">−</span>8.00');
assert.strictEqual(build.notifyAmountMarkup({}), '<span class="notify-amount-missing">待估值</span>');

const body = build.notifyCardBodyMarkup(item, "toast");
for (const needle of ["待确认流水 · 转入", "1,200.00", "USDT", "vitalik.eth", "Ethereum", "发生于 2026-10-29 16:23", "选择分类", "确认", ">编辑<"]) {
  assert.ok(body.includes(needle), `card body missing ${needle}`);
}
const unclassified = build.notifyCardBodyMarkup({ ...item, suggested_category: undefined }, "panel");
assert.ok(/data-notify-confirm="41"[^>]*disabled/.test(unclassified), "confirm is inert without a category");
assert.ok(unclassified.includes("未分类"), "no-category chip stays honest");
assert.ok(unclassified.includes("先选分类"), "disabled confirm explains the next step");
assert.ok(!/data-notify-confirm="41"[^>]*disabled/.test(body), "suggested category keeps confirm actionable");
assert.strictEqual(build.notifyDateTime("2026-10-29T08:23:00Z"), "2026-10-29 16:23", "card time is Beijing time");
assert.strictEqual(build.notifyDateTime("2026-10-29T08:23:00"), "2026-10-29 16:23", "zone-less time is treated as UTC");
assert.strictEqual(build.notifyDateTime(null), "");
const panel = build.flowToastMarkup(item, 3);
assert.ok(panel.includes("还有 2 条 · 上滑收起"), "queue footer count");
assert.ok(panel.includes('data-flow-expand'), "collapsed summary is a button");
assert.ok(!/data-flow-confirm="41"[^>]*disabled/.test(panel), "quick confirm is live with a category");
const quickUnclassified = build.flowToastMarkup({ ...item, suggested_category: undefined }, 3);
assert.ok(/data-flow-confirm="41"[^>]*disabled/.test(quickUnclassified), "quick confirm is inert without a category");
assert.ok(quickUnclassified.includes("先选分类"), "quick confirm uses the same disabled copy");
assert.ok(css.includes(".flow-quick:disabled"), "quick confirm has a disabled style");
assert.ok(panel.includes('aria-expanded="false"'));
assert.ok(build.flowToastMarkup(item, 1).includes("上滑收起"));
assert.ok(!build.flowToastMarkup(item, 1).includes("还有 0 条"));

const escaped = build.notifyPartyLine({ counterparty_label: "<b>x</b>", counterparty_chain: "A&B" });
assert.ok(escaped.includes("&lt;b&gt;x&lt;/b&gt;"));
assert.ok(escaped.includes("A&amp;B"));
assert.ok(!escaped.includes("<b>x</b>"));

const reduced = new Function("window", `${grab("reducedMotion")}; return reducedMotion;`)({ matchMedia: () => ({ matches: true }) });
assert.strictEqual(reduced(), true);
const normal = new Function("window", `${grab("reducedMotion")}; return reducedMotion;`)({ matchMedia: () => ({ matches: false }) });
assert.strictEqual(normal(), false);

// Reduced motion is a real CSS fallback, not only a JS branch.
assert.ok(css.includes("@media (prefers-reduced-motion: reduce)"));
assert.ok(/\.notify-card\.is-rising,[\s\S]{0,80}animation: none/.test(css));
assert.ok(/\.flow-toast-root\.is-flying\s*\{[^}]*transform: none/s.test(css));
assert.ok(/\.flow-toast\s*\{[^}]*transition:/s.test(css));
assert.ok(src.includes('window.matchMedia?.("(prefers-reduced-motion: reduce)")'));
console.log("flow_toast: all assertions passed");
