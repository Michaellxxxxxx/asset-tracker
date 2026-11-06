// node --test frontend/tests/p24_focus.test.js
// p24 dialogs: Tab/Shift+Tab cycle, Escape wiring, focus restore.
const fs = require("fs");
const path = require("path");
const assert = require("assert");

const src = fs.readFileSync(path.join(__dirname, "../static/app.js"), "utf8");

function grab(name) {
  const start = src.indexOf(`  function ${name}(`);
  assert.ok(start >= 0, `function ${name} not found`);
  const next = src.indexOf("\n  }\n", start);
  assert.ok(next > start, `function ${name} end not found`);
  return src.slice(start, next + 5);
}

function node(name) {
  return {
    name,
    disabled: false,
    offsetParent: {},
    focused: false,
    focus() {
      this.focused = true;
    },
  };
}

function buildTrap(nodes) {
  let focused = null;
  const focusable = nodes.map((entry) => {
    entry.focus = () => {
      focused = entry.name;
    };
    return entry;
  });
  const dialog = {
    name: "dialog",
    focus() {
      focused = "dialog";
    },
    contains(candidate) {
      return candidate === dialog || focusable.includes(candidate);
    },
    querySelectorAll() {
      return focusable;
    },
  };
  const document = { activeElement: dialog };
  const trap = new Function(
    "document",
    `${grab("trapFocusables")}\n${grab("trapDialogTab")}\nreturn trapDialogTab;`,
  )(document);
  return {
    dialog,
    document,
    trap,
    focused: () => focused,
    setActive(candidate) {
      document.activeElement = candidate;
    },
  };
}

function keyEvent(shiftKey) {
  return {
    key: "Tab",
    shiftKey,
    prevented: false,
    preventDefault() {
      this.prevented = true;
    },
  };
}

{
  const first = node("first");
  const middle = node("middle");
  const last = node("last");
  const ctx = buildTrap([first, middle, last]);
  ctx.setActive(last);
  const event = keyEvent(false);
  ctx.trap(event, ctx.dialog);
  assert.strictEqual(ctx.focused(), "first");
  assert.strictEqual(event.prevented, true);
}

{
  const first = node("first");
  const last = node("last");
  const ctx = buildTrap([first, last]);
  ctx.setActive(first);
  const event = keyEvent(true);
  ctx.trap(event, ctx.dialog);
  assert.strictEqual(ctx.focused(), "last");
  assert.strictEqual(event.prevented, true);
}

{
  const first = node("first");
  const last = node("last");
  const ctx = buildTrap([first, last]);
  ctx.setActive(node("trigger-outside"));
  const event = keyEvent(false);
  ctx.trap(event, ctx.dialog);
  assert.strictEqual(ctx.focused(), "first");
  assert.strictEqual(event.prevented, true);
}

{
  const first = node("first");
  const last = node("last");
  const ctx = buildTrap([first, last]);
  ctx.setActive(ctx.dialog);
  const event = keyEvent(true);
  ctx.trap(event, ctx.dialog);
  assert.strictEqual(ctx.focused(), "last");
  assert.strictEqual(event.prevented, true);
}

{
  const ctx = buildTrap([]);
  const event = keyEvent(false);
  ctx.trap(event, ctx.dialog);
  assert.strictEqual(ctx.focused(), "dialog");
  assert.strictEqual(event.prevented, true);
}

{
  const first = node("first");
  const middle = node("middle");
  const last = node("last");
  const ctx = buildTrap([first, middle, last]);
  ctx.setActive(middle);
  const event = keyEvent(false);
  ctx.trap(event, ctx.dialog);
  assert.strictEqual(event.prevented, false);
  assert.strictEqual(ctx.focused(), null);
}

const keydownStart = src.indexOf('document.addEventListener("keydown"');
assert.ok(keydownStart > 0, "keydown listener missing");
const keydown = src.slice(keydownStart, keydownStart + 4200);
assert.match(keydown, /event\.key === "Escape"/);
assert.match(keydown, /if \(state\.moreSheet\)[\s\S]{0,160}closeMoreSheet\(\)/);
assert.match(keydown, /if \(state\.notify\.open\)[\s\S]{0,160}closeNotifyPanel\(\)/);
assert.match(keydown, /trapDialogTab\(event, dialog\)/);

assert.match(grab("openMoreSheet"), /dialog\?\.focus\(\)/);
assert.match(grab("openMoreSheet"), /syncAppInert\(\)/);
assert.match(grab("closeMoreSheet"), /sheet\.trigger\.focus\(\)/);
assert.match(grab("openNotifyPanel"), /focusNotifyFirst\(panel\)/);
assert.match(grab("focusNotifyFirst"), /trapFocusables\(panel\)\[0\] \|\| panel/);
assert.match(grab("openNotifyPanel"), /syncAppInert\(\)/);
assert.match(grab("closeNotifyPanel"), /state\.notify\.trigger\.focus/);
assert.match(grab("closeNotifyPanel"), /reducedMotion\(\) \? 200 : 160/);
assert.match(src, /app\.setAttribute\("inert", ""\)/);
assert.match(src, /app\.setAttribute\("aria-hidden", "true"\)/);

console.log("p24 focus tests passed");
