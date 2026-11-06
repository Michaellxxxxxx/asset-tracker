// node frontend/tests/provenance.test.js
// Demo detection must come from the backend (is_demo / username), never id===2.
// The chart badge must understand the P13 provenance values.
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

function demo(user) {
  const fn = new Function("state", `${grab("isDemoAccount")}; return isDemoAccount;`)({ user });
  return fn();
}

assert.strictEqual(demo({ id: 2, username: "alice" }), false, "id 2 alone is not demo");
assert.strictEqual(demo({ id: 2, username: "alice", is_demo: false }), false);
assert.strictEqual(demo({ username: "demo" }), true);
assert.strictEqual(demo({ username: "DEMO" }), false, "backend owns the canonical flag");
assert.strictEqual(demo({ username: "alice", is_demo: true }), true);
assert.strictEqual(demo(null), false);

const chartBadge = new Function(`${grab("chartBadge")}; return chartBadge;`)();
assert.strictEqual(chartBadge("demo"), "USDT · 示例数据");
assert.strictEqual(chartBadge("fixture"), "USDT · 示例数据");
assert.strictEqual(chartBadge("live"), "USDT · 实时估值");
assert.strictEqual(chartBadge("sync"), "USDT · 同步估值");
assert.strictEqual(chartBadge("backfill"), "USDT · 历史回填");

assert.ok(!src.includes("user?.id === 2"), "legacy id===2 demo check must be gone");
console.log("provenance: all assertions passed");
