// node frontend/tests/returns_excluded.test.js
// The excluded-flow line must never call newly counted assets a deposit.
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
const excludedFlowLabel = new Function(`${grab("excludedFlowLabel")}; return excludedFlowLabel;`)();

// Coverage-only: existing assets newly enter the measured universe.
assert.strictEqual(excludedFlowLabel("coverage", 600), "新计入资产");
assert.ok(!excludedFlowLabel("coverage", 600).includes("充值"));
assert.strictEqual(excludedFlowLabel("coverage", -600), "资产口径调整");

// Cash-only: a real ledger deposit/withdrawal may use deposit wording.
assert.strictEqual(excludedFlowLabel("cash", 200), "净充值");
assert.strictEqual(excludedFlowLabel("cash", -200), "净流出");

// Mixed uses the neutral wording, never "充值".
assert.strictEqual(excludedFlowLabel("mixed", 800), "净流入");
assert.strictEqual(excludedFlowLabel("mixed", -800), "净流出");
assert.ok(!excludedFlowLabel("mixed", 800).includes("充值"));

// Nothing removed: no extra line.
assert.strictEqual(excludedFlowLabel("none", 0), "");
assert.strictEqual(excludedFlowLabel("coverage", 0), "");
assert.strictEqual(excludedFlowLabel("mixed", null), "");

// The markup and the 60% white style are wired in.
assert.ok(src.includes('class="return-excluded"'));
assert.ok(src.includes("（已剔除）"));