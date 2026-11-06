// node frontend/tests/format_price.test.js
// Extracts formatPrice and its helpers from app.js: exact 0 -> "0.00", 0<v<1 keeps >=4 decimals.
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
const moneyFormatter = new Intl.NumberFormat("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const priceFormatters = new Map();
${["priceFormatter", "asNumber", "money", "formatPrice"].map(grab).join("\n")}
return formatPrice;`;
const formatPrice = new Function(code)();

assert.strictEqual(formatPrice(0), "0.00");
assert.strictEqual(formatPrice(-0), "0.00");
assert.strictEqual(formatPrice("0"), "0.00");
assert.strictEqual(formatPrice(0.855), "0.8550");
assert.strictEqual(formatPrice(0.00123), "0.001230");
assert.strictEqual(formatPrice(1), "1.00");
assert.strictEqual(formatPrice(1234.5), "1,234.50");
assert.strictEqual(formatPrice(-0.855), "-0.8550");
console.log("format_price ok");
