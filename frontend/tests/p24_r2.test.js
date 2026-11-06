// node --test frontend/tests/p24_r2.test.js
// p24 r2 (P2): hover-expanded toasts opaque + shadow, mobile 8px upward peek, Caps Lock hint.
const fs = require("fs");
const path = require("path");
const assert = require("assert");

const src = fs.readFileSync(path.join(__dirname, "../static/app.js"), "utf8");
const css = fs.readFileSync(path.join(__dirname, "../static/styles.css"), "utf8");

assert.match(
  css,
  /\.toast-stack:hover \.toast\.is-visible,\s*\.toast-stack\.is-expanded \.toast\.is-visible \{\s*opacity: 1;\s*box-shadow: 0 8px 24px rgba\(0, 0, 0, 0\.4\);/,
);
const mobile = css.slice(css.indexOf("@media (max-width: 1023px)", css.indexOf("/* ---------- Toast")));
assert.match(mobile, /\.toast \{\s*transform-origin: top center;/);
assert.match(mobile, /\.toast\[data-depth="1"\] \{\s*--toast-y: -8px;/);
assert.match(mobile, /\.toast\[data-depth="2"\] \{\s*--toast-y: -16px;/);

assert.match(src, /id="password-capslock"[^>]*hidden>大写锁定已开启<\/p>/);
assert.match(css, /\.capslock-hint \{[^}]*color: rgba\(255, 255, 255, 0\.6\);[^}]*font-size: 12px;/);

const start = src.indexOf("  function capsLockOn(");
const end = src.indexOf("\n  }\n", src.indexOf("  function syncCapsLockHint(")) + 5;
const fns = new Function(`${src.slice(start, end)}\nreturn { capsLockOn };`)();
assert.strictEqual(fns.capsLockOn({ getModifierState: (k) => k === "CapsLock" }), true);
assert.strictEqual(fns.capsLockOn({ getModifierState: () => false }), false);
assert.strictEqual(fns.capsLockOn({}), false);
assert.strictEqual(fns.capsLockOn(null), false);
console.log("p24_r2 ok");
