// node frontend/tests/p19_polish.test.js
// p19: login copy wraps before 登录后 at 393, field labels share the form's
// left line with the title and inputs, and the recent-errors box fades its
// right edge under its own horizontal scroll.
const fs = require("fs");
const path = require("path");
const assert = require("assert");

const css = fs.readFileSync(path.join(__dirname, "../static/styles.css"), "utf8");

function rule(selector) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = css.match(new RegExp(`${escaped}\\s*\\{([^}]*)\\}`));
  assert.ok(match, `${selector} rule not found`);
  return match[1];
}

const sub = rule(".login-sub");
assert.match(sub, /max-width:\s*20em;/, "login copy is capped at 20em so 登录 is not split");
assert.ok(!/max-width:\s*280px/.test(sub), "old 280px cap that split 登录/录 is gone");

const label = rule(".field label");
assert.match(label, /(?:^|\n)\s*margin:\s*0;/, "field label has no extra left margin");
assert.ok(!/margin:\s*0 0 0 3px/.test(label), "old 3px label indent is gone");

const code = rule(".wechat-details code");
assert.match(
  code,
  /-webkit-mask-image:\s*linear-gradient\(to right, #000 calc\(100% - 24px\), transparent\);/,
  "recent-errors box has a 24px right fade for WebKit",
);
assert.match(
  code,
  /(?:^|\n)\s*mask-image:\s*linear-gradient\(to right, #000 calc\(100% - 24px\), transparent\);/,
  "recent-errors box has a 24px right fade",
);

console.log("p19_polish: ok");
