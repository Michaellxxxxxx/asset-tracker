const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const src = fs.readFileSync(path.join(__dirname, "../static/app.js"), "utf8");
const start = src.indexOf("function counterpartySourceLabel(");
const end = src.indexOf("function counterpartyKindLabel(", start);
const fn = vm.runInNewContext(`(${src.slice(start, end).trim()})`);

test("known_owner source reads 按所属程序识别", () => {
  assert.strictEqual(fn("helius.rpc+known_owner"), "按所属程序识别");
  assert.strictEqual(fn("known_program"), "按所属程序识别");
  assert.strictEqual(fn("tx_program"), "按调用程序识别");
});

test("known sources map to friendly words", () => {
  assert.strictEqual(fn("manual"), "手动标签");
  assert.strictEqual(fn("rule"), "规则标签");
  assert.strictEqual(fn("fallback"), "未验证标签");
  for (const s of ["helius.rpc", "helius", "rpc.name", "ens", "blockscout.token", "zerion", "sourcify", "routescan"]) {
    assert.strictEqual(fn(s), "链上标签");
  }
  assert.strictEqual(fn(""), "");
});

test("unknown sources are hidden (empty), never the raw string", () => {
  for (const s of ["weird.src+foo", "some_internal"]) assert.strictEqual(fn(s), "");
  assert.ok(!src.includes("其它来源"));
});

test("party-source uses body sans 12px white 48%", () => {
  const css = fs.readFileSync(path.join(__dirname, "../static/styles.css"), "utf8");
  const m = css.match(/\.party-source \{([^}]*)\}/);
  assert.ok(m);
  assert.match(m[1], /font-family:\s*var\(--font\)/);
  assert.match(m[1], /font-size:\s*12px/);
  assert.match(m[1], /rgba\(255, 255, 255, 0\.48\)/);
});

test("r3: counterparty text is pulled left only once (chip, not stack)", () => {
  const css = fs.readFileSync(path.join(__dirname, "../static/styles.css"), "utf8");
  assert.match(css, /\.pending-info \.party-chip \{[^}]*margin-left:\s*-8px/);
  const stack = [...css.matchAll(/\.pending-info \.party-stack \{([^}]*)\}/g)].map((m) => m[1]).join("");
  assert.doesNotMatch(stack, /margin-left/);
});
