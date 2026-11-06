// node --test frontend/tests/p24_drawer.test.js
// p24 "more" drawer: vaul-style grabber, 30% height / 500px/s dismiss thresholds.
const fs = require("fs");
const path = require("path");
const assert = require("assert");

const src = fs.readFileSync(path.join(__dirname, "../static/app.js"), "utf8");
const css = fs.readFileSync(path.join(__dirname, "../static/styles.css"), "utf8");

function grab(name) {
  const start = src.indexOf(`  function ${name}(`);
  assert.ok(start >= 0, `function ${name} not found`);
  const next = src.indexOf("\n  }\n", start);
  assert.ok(next > start, `function ${name} end not found`);
  return src.slice(start, next + 5);
}

const code = [grab("moreSheetVelocity"), grab("shouldCloseMoreSheet")].join("\n");
const drawer = new Function(
  `${code}\nreturn { moreSheetVelocity, shouldCloseMoreSheet };`,
)();

assert.strictEqual(drawer.shouldCloseMoreSheet(149, 0, 500), false);
assert.strictEqual(drawer.shouldCloseMoreSheet(151, 0, 500), true);
assert.strictEqual(drawer.shouldCloseMoreSheet(80, 0, 0), false);
assert.strictEqual(drawer.shouldCloseMoreSheet(20, 500, 500), false);
assert.strictEqual(drawer.shouldCloseMoreSheet(20, 500.1, 500), true);
assert.strictEqual(drawer.shouldCloseMoreSheet(120, 1200, 500), true);
assert.strictEqual(drawer.shouldCloseMoreSheet(-40, -900, 500), false);

assert.strictEqual(
  drawer.moreSheetVelocity([
    { y: 100, t: 1000 },
    { y: 160, t: 1100 },
  ]),
  600,
);
assert.strictEqual(drawer.moreSheetVelocity([{ y: 100, t: 1000 }]), 0);
assert.strictEqual(drawer.moreSheetVelocity([]), 0);

const open = grab("openMoreSheet");
assert.match(grab("moreSheetMarkup"), /more-sheet-grab/);
assert.match(open, /pointerdown/);
assert.match(open, /pointermove/);
assert.match(open, /pointerup/);
assert.match(open, /pointercancel/);
assert.match(open, /moreSheetVelocity\(/);
assert.match(open, /shouldCloseMoreSheet\(dy, velocity, height\)/);
assert.match(open, /closeMoreSheet\(\{ restoreFocus: true, dragClose: true \}\)/);
assert.match(open, /dialog\.classList\.add\("is-dragging"\)/);
assert.match(open, /dialog\.style\.transform = ""/);
assert.match(open, /rawDy < 0 \? rawDy \* 0\.5 : rawDy/);
assert.match(open, /MORE_SHEET_SPRING\.stiffness/);
assert.match(open, /syncAppInert\(\)/);
assert.match(src, /MORE_SHEET_SPRING = \{ stiffness: 380, damping: 34 \}/);

assert.match(grab("closeMoreSheet"), /sheet\.trigger\.focus\(\)/);
assert.match(grab("closeMoreSheet"), /220ms ease-out/);
assert.match(grab("closeMoreSheet"), /230\)/);
assert.match(src, /role="dialog"[\s\S]{0,80}aria-modal="true"/);

assert.match(css, /\.more-sheet-grab::after\s*\{[^}]*width: 36px;[^}]*height: 5px;[^}]*border-radius: 3px;[^}]*rgba\(255, 255, 255, 0\.24\)/);
assert.match(css, /\.more-sheet-grab\s*\{[^}]*height: 44px;[^}]*padding-top: 8px/);
assert.match(css, /\.more-sheet-backdrop\s*\{[^}]*background: rgba\(0, 0, 0, 0\.55\)/);
assert.match(css, /\.more-sheet-root\s*\{[^}]*z-index: 70/);
assert.match(css, /body\.more-sheet-open \.flow-toast-root\s*\{[^}]*z-index: 40/);
assert.match(css, /\.more-sheet\.is-dragging\s*\{[^}]*transition: none/);
assert.match(css, /\.more-sheet-root\.is-open \.more-sheet\s*\{[^}]*transition-duration: 200ms/);
assert.match(
  css,
  /@media \(prefers-reduced-motion: reduce\)\s*\{[\s\S]{0,220}\.more-sheet,[\s\S]{0,140}transform: none !important/,
);

console.log("p24 drawer tests passed");
