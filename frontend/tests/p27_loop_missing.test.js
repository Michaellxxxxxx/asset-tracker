const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const api = require("../static/loop_card.js");
const css = fs.readFileSync(path.join(__dirname, "../static/loop_card.css"), "utf8");

const base = {
  position_id: 1, principal_usd: 1000, net_apy: 0.05, health_factor: 1.8, liq_price: 0.9,
  distance_to_liq_pct: 0.2, collateral_yield_contrib: 0.06, borrow_cost_contrib: -0.01,
  missing: {},
};

test("missing collateral yield reads 暂无抵押利率 without field names", () => {
  const html = api.renderCard({ ...base, net_apy: null, collateral_yield_contrib: null,
    missing: { collateral_yield_contrib: "缺少抵押收益率（apy_supply）", net_apy: "缺少 apy_supply" } }, { id: 1 });
  assert.match(html, /p25-source--missing">暂无抵押利率</);
  assert.doesNotMatch(html, /apy_supply|来源缺失|[a-z]+_[a-z]+（/);
  assert.doesNotMatch(html, /title="[^"]*[a-z]_[a-z]/);
});

test("numeric slots show an em dash instead of 缺失", () => {
  const html = api.renderCard({ ...base, net_apy: null, health_factor: null, liq_price: null, distance_to_liq_pct: null }, { id: 2 });
  assert.doesNotMatch(html, /缺失/);
  assert.ok((html.match(/p25-dash-missing"[^>]*>—</g) || []).length >= 4);
  for (const f of [api.fmtUsd, api.fmtPct, api.fmtPrice, api.fmtHealth, api.fmtDistance, api.fmtLeverage]) assert.strictEqual(f(null), "—");
});

test("zero values are neutral, never red", () => {
  assert.strictEqual(api.toneClass(0), "p25-zero");
  const html = api.renderCard({ ...base, borrow_cost_contrib: 0, net_apy: 0 }, { id: 3 });
  assert.match(html, /<b class="p25-zero">0\.00%<\/b>/);
  assert.match(html, /p25-metric-v p25-zero/);
  assert.match(css, /\.p25-zero,[^{]*\{[^}]*rgba\(255, 255, 255, 0\.48\)/);
});

test("userText strips identifiers", () => {
  assert.strictEqual(api.userText("缺少抵押收益率（apy_supply）"), "缺少抵押收益率");
});

test("r3: missing source reuses source row style, only colour 0.48", () => {
  assert.match(css, /\.p25-source\.p25-source--missing \{\s*color: rgba\(255, 255, 255, 0\.48\);\s*\}/);
  assert.match(css, /\.p25-source \{[^}]*font-size: 11px;/);
});

test("footnote aligns", () => {
  assert.match(css, /\.lend-sum-wrap \.lend-sum-note \{\s*margin-left: 0;[^}]*padding-left: 12px/);
});

test("r1: dash inherits metric size, weight and line-height", () => {
  assert.match(css, /\.p25-metric-v \.p25-dash-missing \{[^}]*font-size: inherit;[^}]*line-height: inherit;/);
});

test("r2: text state in number slot is 14px/400/0.6 and keeps slot line-height", () => {
  const html = api.renderCard({ ...base, liq_price: null, missing: { liq_price: "数据积累中：历史快照未记录" } }, { id: 9 });
  assert.match(html, /p25-state-text"[^>]*>数据积累中</);
  assert.match(css, /\.p25-metric-v \.p25-state-text \{[^}]*0\.6\)[^}]*font-size: 14px;[^}]*font-weight: 400;[^}]*line-height: inherit;/);
});
