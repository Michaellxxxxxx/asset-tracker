// node frontend/tests/p20h_no_undef_calls.test.js
// p20h: p19 removed the alert menu; nothing may still call closeAlertMenu or
// any other bare helper that app.js no longer defines.
const fs = require("fs");
const path = require("path");
const assert = require("assert");

const src = fs.readFileSync(path.join(__dirname, "../static/app.js"), "utf8");
const code = src.replace(/\/\/[^\n]*/g, "");

assert.ok(!/closeAlertMenu|toggleAlertMenu/.test(code), "no alert-menu helpers referenced");

const defined = new Set();
for (const m of code.matchAll(/function\s*\*?\s*([A-Za-z_$][\w$]*)\s*\(/g)) defined.add(m[1]);
for (const m of code.matchAll(/(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=/g)) defined.add(m[1]);

const missing = new Set();
for (const m of code.matchAll(/(^|[^.\w$])((?:close|open|toggle|render|show|hide|collapse|expand)[A-Z][\w$]*)\s*\(/g)) {
  if (!defined.has(m[2])) missing.add(m[2]);
}
assert.deepStrictEqual([...missing], [], `undefined helpers called: ${[...missing].join(", ")}`);

const open = code.slice(code.indexOf("function openNotifyPanel("));
const body = open.slice(0, open.indexOf("\n  }\n"));
for (const m of body.matchAll(/(^|[^.\w$])([a-z][\w$]*)\s*\(/g)) {
  const name = m[2];
  if (["if", "for", "while", "switch", "return", "catch", "function"].includes(name)) continue;
  assert.ok(defined.has(name), `openNotifyPanel calls undefined ${name}`);
}
console.log("p20h no-undef calls ok");
