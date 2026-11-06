// node frontend/tests/returns_readout.test.js
// Extracts the returns date/drawdown helpers from app.js so the readout maths
// is checked without a browser.
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
const code = `
${["dateObject", "formatDateSH", "formatShortDateSH", "returnsDrawdown"].map(grab).join("\n")}
return { formatDateSH, formatShortDateSH, returnsDrawdown };`;
const { formatDateSH, formatShortDateSH, returnsDrawdown } = new Function(code)();

assert.strictEqual(formatDateSH("2026-10-22T23:59:59Z"), "2026.10.23");
assert.strictEqual(formatDateSH("2026-10-28T12:30:46Z"), "2026.10.28");
assert.strictEqual(formatShortDateSH("2026-10-22T23:59:59Z"), "10月23日");
assert.strictEqual(formatDateSH(null), "—");

// Rate mode: cumulative +186.58% -> +58.55% is a 44.67% relative drawdown,
// not -128.03% (the old gap-in-percentage-points bug).
const rateDrawdown = returnsDrawdown(
  [
    { ts: "2026-10-22T23:59:59Z", value: 0 },
    { ts: "2026-10-28T12:30:46Z", value: 1.865794 },
    { ts: "2026-10-28T12:36:23Z", value: 0.585531 },
  ],
  true,
);
assert.strictEqual(rateDrawdown.ts, "2026-10-28T12:36:23Z");
assert.ok(Math.abs(rateDrawdown.rate + 0.4467) < 0.0001, String(rateDrawdown.rate));

// NAV mode: 150 -> 120 is -20% of the peak level.
const navDrawdown = returnsDrawdown(
  [
    { ts: "a", value: 100 },
    { ts: "b", value: 150 },
    { ts: "c", value: 120 },
  ],
  false,
);
assert.ok(Math.abs(navDrawdown.rate + 0.2) < 1e-12, String(navDrawdown.rate));

const monotonic = returnsDrawdown(
  [
    { ts: "a", value: 100 },
    { ts: "b", value: 110 },
  ],
  false,
);
assert.strictEqual(monotonic.rate, 0);

console.log("returns_readout ok");
