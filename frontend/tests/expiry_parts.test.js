// node frontend/tests/expiry_parts.test.js
// Extracts dateObject / formatDate / expiryParts from app.js and checks countdown text
// against a fixed "now" (2026-10-28 19:54 Asia/Shanghai).
const fs = require("fs");
const path = require("path");
const assert = require("assert");

const src = fs.readFileSync(path.join(__dirname, "../static/app.js"), "utf8");
function grab(name) {
  const start = src.indexOf(`  function ${name}(`);
  assert.ok(start >= 0, `function ${name} not found`);
  const next = src.indexOf("\n  }\n", start);
  return src.slice(start, next + 5);
}
const NOW = Date.parse("2026-10-28T19:54:00+08:00");
const code = ["dateObject", "formatDate", "expiryParts"].map(grab).join("\n");
const expiryParts = new Function("Date", `${code}; return expiryParts;`)(
  class extends Date {
    static now() { return NOW; }
  },
);

const iso = (s) => new Date(s).toISOString();
const cases = [
  ["2027-03-26T08:00:00Z", "149 天后"],
  ["2026-10-28T23:00:00+08:00", "3 小时后"],
  ["2026-10-29T23:00:00+08:00", "明天"],
  ["2026-10-27T08:00:00Z", "已到期"],
  ["2025-01-01T08:00:00Z", "已到期"],
  ["not-a-date", "—"],
];
const results = [];
for (const [value, want] of cases) {
  const got = expiryParts(value).countdown;
  results.push([value, got]);
}
// explicit expectations
assert.strictEqual(expiryParts("2027-03-26T08:00:00Z").countdown, "149 天后");
assert.strictEqual(expiryParts("2026-10-28T23:00:00+08:00").countdown, "3 小时后");
assert.strictEqual(expiryParts("2026-10-28T20:30:00+08:00").countdown, "1 小时后");
assert.strictEqual(expiryParts("2026-10-29T20:00:00+08:00").countdown, "明天"); // 24h+6min ahead, next calendar day
assert.strictEqual(expiryParts("2026-10-30T10:00:00+08:00").countdown, "2 天后");
assert.strictEqual(expiryParts("2026-10-27T08:00:00Z").countdown, "已到期");
assert.strictEqual(expiryParts("2025-01-01T08:00:00Z").countdown, "已到期");
assert.strictEqual(expiryParts("not-a-date").countdown, "—");
assert.strictEqual(expiryParts("2027-03-26T08:00:00Z").label, "2027.03.26");
for (const [v] of cases.concat([["2020-01-01T00:00:00Z"]])) {
  assert.ok(!/-\d/.test(expiryParts(v).countdown), `negative countdown for ${v}`);
}
console.log("expiry_parts: all assertions passed");
results.forEach((r) => console.log(" ", r[0], "->", r[1]));
