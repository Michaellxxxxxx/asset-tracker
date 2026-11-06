// node --test frontend/tests/p24_login.test.js
// p24 login: field errors, password visibility, loading and focus styling.
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

function classList() {
  const values = new Set();
  return {
    toggle(name, on) {
      if (on) values.add(name);
      else values.delete(name);
      return on;
    },
    add(name) {
      values.add(name);
    },
    remove(name) {
      values.delete(name);
    },
    contains(name) {
      return values.has(name);
    },
  };
}

function buildHelpers() {
  const input = {
    type: "password",
    value: "s3cret",
    attrs: new Map(),
    focused: false,
    selection: null,
    setAttribute(name, value) {
      this.attrs.set(name, String(value));
    },
    removeAttribute(name) {
      this.attrs.delete(name);
    },
    focus() {
      this.focused = true;
    },
    setSelectionRange(start, end) {
      this.selection = [start, end];
    },
  };
  const error = { textContent: "", hidden: true, classList: classList() };
  const button = {
    attrs: new Map(),
    classList: classList(),
    setAttribute(name, value) {
      this.attrs.set(name, String(value));
    },
    getAttribute(name) {
      return this.attrs.get(name);
    },
  };
  const document = {
    getElementById(id) {
      if (id === "password") return input;
      if (id === "password-error") return error;
      return null;
    },
    querySelector(selector) {
      return selector === "[data-password-toggle]" ? button : null;
    },
  };
  const code = [
    grab("loginFieldErrors"),
    grab("setLoginFieldError"),
    grab("clearLoginFieldErrors"),
    grab("setPasswordVisible"),
  ].join("\n");
  const helpers = new Function(
    "document",
    `${code}\nreturn { loginFieldErrors, setLoginFieldError, clearLoginFieldErrors, setPasswordVisible };`,
  )(document);
  return { helpers, input, error, button };
}

const { helpers, input, error, button } = buildHelpers();

assert.deepStrictEqual(helpers.loginFieldErrors("", ""), {
  username: "请输入用户名",
  password: "请输入密码",
});
assert.deepStrictEqual(helpers.loginFieldErrors(" demo ", ""), { password: "请输入密码" });
assert.deepStrictEqual(helpers.loginFieldErrors("demo", "demo123"), {});

helpers.setLoginFieldError("password", "请输入密码");
assert.strictEqual(input.attrs.get("aria-invalid"), "true");
assert.strictEqual(error.textContent, "请输入密码");
assert.strictEqual(error.hidden, false);
helpers.setLoginFieldError("password", "");
assert.strictEqual(input.attrs.has("aria-invalid"), false);
// The red state is kept one 120ms beat so it fades instead of snapping off.
assert.strictEqual(error.hidden, false);
assert.ok(error.classList.contains("is-clearing"));

helpers.setPasswordVisible(true);
assert.strictEqual(input.type, "text");
assert.strictEqual(button.attrs.get("aria-pressed"), "true");
assert.strictEqual(button.attrs.get("aria-label"), "隐藏密码");
assert.ok(button.classList.contains("is-visible"));
assert.ok(input.focused);
assert.deepStrictEqual(input.selection, [6, 6]);

helpers.setPasswordVisible(false);
assert.strictEqual(input.type, "password");
assert.strictEqual(button.attrs.get("aria-pressed"), "false");
assert.strictEqual(button.attrs.get("aria-label"), "显示密码");
assert.ok(!button.classList.contains("is-visible"));

assert.match(src, /aria-describedby="username-error"/);
assert.match(src, /aria-describedby="password-error"/);
assert.match(src, /data-password-toggle/);
assert.match(src, /aria-label="显示密码"/);
assert.match(src, /aria-pressed="false"/);
assert.match(src, /正在进入…/);
assert.doesNotMatch(src, /正在验证/);
assert.match(src, /setLoginFieldError\("username", errors\.username \|\| ""\)/);
assert.match(src, /setLoginFieldError\("password", "用户名或密码不对"\)/);
assert.match(src, /button\.setAttribute\("aria-busy", loading \? "true" : "false"\)/);
assert.match(src, /addEventListener\("input"/);
assert.match(src, /event\.target\.id === "username" \|\| event\.target\.id === "password"/);
assert.match(src, /setLoginFieldError\(event\.target\.id, ""\)/);

assert.match(css, /\.field input\[aria-invalid="true"\]\s*\{[^}]*rgba\(255, 69, 58, 0\.6\)/);
assert.match(
  css,
  /\.field input:focus-visible\s*\{[^}]*rgba\(212, 165, 116, 0\.6\)[^}]*0 0 0 3px rgba\(212, 165, 116, 0\.1\)/,
);
assert.match(css, /\.field input\s*\{[^}]*border-color 120ms ease-out/);
assert.doesNotMatch(css, /\.field input:focus\s*\{/);
assert.match(css, /\.field-error\s*\{[^}]*margin: 6px 2px 0;[^}]*color: #ff6961;[^}]*font-size: 12px/);
assert.match(css, /\.field-error\.is-clearing\s*\{[^}]*opacity: 0/);
assert.match(css, /\.pw-toggle\s*\{[^}]*width: 44px;[^}]*height: 44px;/);
assert.match(css, /\.pw-toggle\s*\{[^}]*color: rgba\(255, 255, 255, 0\.6\)/);
assert.match(css, /\.pw-toggle svg\s*\{[^}]*width: 18px;[^}]*height: 18px/);
assert.match(css, /\.pw-toggle:hover,[\s\S]{0,120}color: rgba\(255, 255, 255, 0\.8\)/);
assert.doesNotMatch(css, /\.pw-toggle:hover,[\s\S]{0,120}color: var\(--accent\)/);
assert.doesNotMatch(css, /\.login-submit:disabled\s*\{[^}]*opacity:\s*0\.7/);
assert.match(css, /\.login-submit:disabled,[\s\S]{0,40}\.login-submit\.is-loading\s*\{[^}]*opacity: 1;[^}]*pointer-events: none;[^}]*background: #d4a574;[^}]*color: #1b130a/);
assert.match(css, /\.login-submit\.is-loading \[data-submit-label\]\s*\{[^}]*color: #1b130a/);
assert.match(css, /\.login-submit\.is-loading:hover,[\s\S]{0,80}background: #d4a574/);
assert.match(css, /\.submit-progress\s*\{[^}]*width: 16px;[^}]*height: 16px/);
assert.match(css, /animation: spin 0\.9s linear infinite/);

setTimeout(() => {
  assert.strictEqual(error.textContent, "");
  assert.strictEqual(error.hidden, true);
  assert.ok(!error.classList.contains("is-clearing"));
  console.log("p24 login tests passed");
}, 150);
