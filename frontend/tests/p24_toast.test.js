// node --test frontend/tests/p24_toast.test.js
// p24 toasts: Sonner-style top/bottom stack, depth scale, auto/swipe dismissal.
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

const code = [
  grab("toastStackDepth"),
  grab("toastVelocity"),
  grab("shouldDismissToast"),
  grab("toastIconMarkup"),
  grab("showToast"),
].join("\n");
const toast = new Function(
  `${code}\nreturn { toastStackDepth, toastVelocity, shouldDismissToast, toastIconMarkup };`,
)();

assert.strictEqual(toast.toastStackDepth(0, 3), 2);
assert.strictEqual(toast.toastStackDepth(1, 3), 1);
assert.strictEqual(toast.toastStackDepth(2, 3), 0);
assert.strictEqual(toast.toastStackDepth(0, 4), 2);
assert.strictEqual(toast.toastStackDepth(3, 4), 0);
assert.strictEqual(toast.toastStackDepth(0, 1), 0);
assert.strictEqual(toast.toastStackDepth(2, 2), 0);

assert.strictEqual(
  toast.toastVelocity([
    { y: 100, t: 1000 },
    { y: 40, t: 1100 },
  ]),
  -600,
);
assert.strictEqual(
  toast.toastVelocity(
    [
      { x: 10, t: 1000 },
      { x: 70, t: 1100 },
    ],
    "x",
  ),
  600,
);
assert.strictEqual(toast.toastVelocity([{ y: 100, t: 1000 }]), 0);
assert.strictEqual(toast.toastVelocity([]), 0);

// Phones swipe up; desktop swipes right. 40px or 500px/s dismisses.
assert.strictEqual(toast.shouldDismissToast(0, -40), false);
assert.strictEqual(toast.shouldDismissToast(0, -41), true);
assert.strictEqual(toast.shouldDismissToast(0, -10, 0, -500), false);
assert.strictEqual(toast.shouldDismissToast(0, -10, 0, -501), true);
assert.strictEqual(toast.shouldDismissToast(12, 0), false);
assert.strictEqual(toast.shouldDismissToast(41, 0), true);
assert.strictEqual(toast.shouldDismissToast(0, 0, 501, 0), true);

assert.match(toast.toastIconMarkup("success"), /class="toast-icon"/);
assert.match(toast.toastIconMarkup("error"), /m8 8 8 8/);
assert.match(toast.toastIconMarkup("info"), /<circle/);

const show = grab("showToast");
assert.match(show, /ensureToastStack\(\)/);
assert.match(grab("ensureToastStack"), /className = "toast-stack"/);
assert.match(show, /toast--\$\{safeTone\}/);
assert.match(show, /toastIconMarkup\(safeTone\)/);
assert.match(show, /role", safeTone === "error" \? "alert" : "status"/);
assert.match(show, /aria-live", safeTone === "error" \? "assertive" : "polite"/);
assert.match(show, /textContent = message/);
assert.doesNotMatch(show, /document\.querySelectorAll\("\.toast"\)\.forEach/);
assert.match(src, /TOAST_VISIBLE_LIMIT = 3/);
assert.match(src, /TOAST_LIFETIME = 4000/);
assert.match(src, /TOAST_EXIT = 160/);

const swipe = grab("bindToastSwipe");
assert.match(swipe, /pointerdown/);
assert.match(swipe, /pointermove/);
assert.match(swipe, /shouldDismissToast\(dx, dy, velocityX, velocityY\)/);
assert.match(swipe, /--drag-x/);
assert.match(swipe, /--drag-y/);
assert.match(swipe, /pointerenter/);
assert.match(swipe, /pointerleave/);
assert.match(swipe, /is-expanded/);

assert.match(css, /\.toast-stack\s*\{[^}]*right: 24px;[^}]*bottom: 24px/);
assert.match(css, /@media \(max-width: 1023px\)[\s\S]{0,160}\.toast-stack\s*\{[^}]*top: calc\(8px \+ env\(safe-area-inset-top\)\)/);
assert.match(css, /\.toast\s*\{[^}]*padding: 12px 16px;[^}]*border-radius: 14px;[^}]*background: #1c1c1e;[^}]*font-size: 14px/);
assert.match(css, /\.toast\s*\{[^}]*border: 1px solid rgba\(255, 255, 255, 0\.08\)/);
assert.match(css, /\.toast\[data-depth="1"\]\s*\{[^}]*--toast-y: -8px;[^}]*--toast-scale: 0\.96/);
assert.match(css, /\.toast\[data-depth="2"\]\s*\{[^}]*--toast-y: -16px;[^}]*--toast-scale: 0\.92/);
assert.match(css, /\.toast\.is-visible\[data-depth="1"\],[^{]*\{[^}]*opacity: 0\.7/);
assert.match(css, /\.toast\.is-visible\s*\{[^}]*opacity: 1/);
assert.match(css, /\.toast\s*\{[^}]*opacity 160ms ease-out,[^}]*transform 200ms cubic-bezier/);
assert.match(css, /\.toast\.is-leaving\s*\{[^}]*opacity 160ms ease-out/);
assert.match(css, /\.toast-icon\s*\{[^}]*width: 16px;[^}]*height: 16px/);
assert.match(css, /\.toast--success \.toast-icon\s*\{[^}]*color: var\(--positive\)/);
assert.match(css, /\.toast--error \.toast-icon\s*\{[^}]*color: var\(--negative\)/);
assert.match(css, /\.toast--info \.toast-icon\s*\{[^}]*rgba\(255, 255, 255, 0\.6\)/);
assert.doesNotMatch(css, /\.toast-dot/);
assert.match(
  css,
  /@media \(prefers-reduced-motion: reduce\)\s*\{[\s\S]{0,260}\.toast,[\s\S]{0,200}transform: none !important;/,
);

console.log("p24 toast tests passed");
