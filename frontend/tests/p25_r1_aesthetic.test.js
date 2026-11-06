// node frontend/tests/p25_r1_aesthetic.test.js
// p25 r1 aesthetic rework: health colours, <3-snapshot empty rows, dash
// breakdown, risk copy/alert, protocol chip geometry and the leg header.
const fs = require("fs");
const path = require("path");
const assert = require("assert");

const staticDir = path.join(__dirname, "../static");
const loop = require(path.join(staticDir, "loop_card.js"));
const js = fs.readFileSync(path.join(staticDir, "loop_card.js"), "utf8");
const css = fs.readFileSync(path.join(staticDir, "loop_card.css"), "utf8");

const base = {
  position_id: 1,
  principal_usd: 1887.14,
  net_apy: 0.0807935,
  collateral_yield_contrib: 0.411268,
  borrow_cost_contrib: -0.330474,
  health_factor: 1.5,
  liq_price: 1.0987,
  liq_price_basis: "market",
  current_price: 1.1865,
  distance_to_liq_pct: 0.074,
  leverage: 6.55,
  spread: 0.003244,
  spread_negative: false,
  weighted_apy_supply: 0.06279,
  weighted_apy_borrow: 0.059546,
  depeg_liq_price: 1.0987,
  depeg_pair: "USDC/USD3",
  depeg_applicable: true,
  borrow_apy_high: null,
  collateral: { usd: 12360.41, legs: [{ symbol: "USD3", amount: 10417, usd: 12360.41, apy: 0.06279 }] },
  borrow: { usd: 10473.27, legs: [{ symbol: "USDC", amount: 10473.27, usd: 10473.27, apy: 0.059546 }] },
  pnl: { amount: 3.6, rate: 0.0019, net_flows: 293, snapshots: 5, start: "2026-10-28T12:28:00Z" },
  pnl_breakdown: { price: null, collateral_interest: null, borrow_interest: null, interest_net: null },
  apy_windows: {
    "7d": { points: [{ date: "2026-10-31", value: 0.08 }], coverage_days: 0.4, accumulating: true, min: 0.08, max: 0.09, avg: 0.085 },
    "30d": { points: [{ date: "2026-10-31", value: 0.08 }], coverage_days: 0.4, accumulating: true, min: 0.08, max: 0.09, avg: 0.085 },
  },
  missing: {
    borrow_apy_high: "数据积累中：历史快照未记录借款利率（本期起写入）",
    price_pnl: "历史快照缺少抵押/借款分项利率，无法拆分收入与支出",
    collateral_interest: "历史快照缺少抵押/借款分项利率，无法拆分收入与支出",
    borrow_interest: "历史快照缺少抵押/借款分项利率，无法拆分收入与支出",
  },
};
const meta = { id: 1, protocol: "morpho", chain: "ethereum", label: "Morpho USD3/USDC" };

function rendered(overrides) {
  return loop.renderCard({ ...base, ...overrides }, meta);
}

/* P1-1: health uses red/amber/white, never the gold interactive accent. */
assert.strictEqual(loop.healthTone(1.19), "danger");
assert.strictEqual(loop.healthTone(1.2), "warn");
assert.strictEqual(loop.healthTone(1.49), "warn");
assert.strictEqual(loop.healthTone(1.5), "safe");
assert.strictEqual(loop.healthTone(null), "");
assert.strictEqual(loop.healthClass(1.08), "p25-hf--danger");
assert.strictEqual(loop.healthClass(1.35), "p25-hf--warn");
assert.strictEqual(loop.healthClass(2.1), "p25-hf--safe");
assert.ok(rendered({ health_factor: 1.08 }).includes('class="p25-metric-v p25-hf--danger"'));
assert.ok(rendered({ health_factor: 1.35 }).includes('class="p25-metric-v p25-hf--warn"'));
assert.ok(rendered({ health_factor: 2.1 }).includes('class="p25-metric-v p25-hf--safe"'));
assert.ok(!rendered({ health_factor: 1.08 }).includes("p25-warn-text"));
assert.match(css, /\.p25-hf--danger\s*\{\s*color:\s*#ff453a/i);
assert.match(css, /\.p25-hf--warn\s*\{\s*color:\s*#ffb340/i);
assert.match(css, /\.p25-hf--safe\s*\{\s*color:\s*var\(--text\)/i);

/* P1-2: fewer than 3 snapshots -> one 44px line, no chart, no min/max/avg. */
const twoPoints = [
  { date: "2026-10-30", value: 0.08 },
  { date: "2026-10-31", value: 0.09 },
];
const emptyWin = rendered({
  apy_windows: {
    "7d": { points: twoPoints, coverage_days: 2.4, accumulating: true, min: 0.08, max: 0.09, avg: 0.085 },
    "30d": { points: [], coverage_days: 2.4, accumulating: true, min: null, max: null, avg: null },
  },
});
assert.ok(emptyWin.includes("数据积累中 · 已覆盖 2.4 天，满 3 个快照后显示"), "empty row copy");
assert.ok(emptyWin.includes("p25-window--empty"), "empty row class");
assert.ok(!emptyWin.includes("p25-spark"), "no sparkline under 3 snapshots");
assert.ok(!emptyWin.includes("最低 "), "min hidden under 3 snapshots");
assert.ok(!emptyWin.includes("最高 "), "max hidden under 3 snapshots");
assert.ok(!emptyWin.includes("均值 "), "avg hidden under 3 snapshots");
assert.match(css, /\.p25-window--empty\s*\{[^}]*min-height:\s*44px/);
assert.match(css, /\.p25-window--empty\s*\{[^}]*border:\s*0/);
assert.match(css, /\.p25-window--empty\s*\{[^}]*background:\s*none/);
assert.match(css, /\.p25-window--empty p\s*\{[^}]*font-size:\s*14px/);
assert.match(css, /\.p25-window--empty p\s*\{[^}]*color:\s*rgba\(255,\s*255,\s*255,\s*0\.6\)/);

const threePoints = [
  { date: "2026-10-29", value: 0.25 },
  { date: "2026-10-30", value: 0.3 },
  { date: "2026-10-31", value: 0.28 },
];
const chartWin = rendered({
  apy_windows: {
    "7d": { points: threePoints, coverage_days: 2.5, accumulating: true, min: 0.25, max: 0.3, avg: 0.276 },
    "30d": { points: threePoints, coverage_days: 2.5, accumulating: true, min: 0.25, max: 0.3, avg: 0.276 },
  },
});
assert.ok(chartWin.includes("p25-spark"), "chart returns at 3 snapshots");
assert.ok(!chartWin.includes("p25-window--empty"), "no empty row at 3 snapshots");

/* P1-3: three dashes, no underline, no gold sentence, note moved to header. */
const breakdownHtml = emptyWin.match(/<dl class="p25-breakdown">[\s\S]*?<\/dl>/)[0];
const dashes = breakdownHtml.match(/class="p25-dash"[^>]*>—<\/span>/g) || [];
assert.strictEqual(dashes.length, 3, "three breakdown values become em dashes");
assert.ok(emptyWin.includes("<h5>收益拆解</h5>"));
assert.ok(emptyWin.includes("历史不足，暂无法拆分"));
assert.ok(!emptyWin.includes("分项缺失："));
assert.ok(!js.includes("分项缺失："), "gold warning sentence removed");
assert.ok(!css.includes("p25-equation--missing"), "gold warning style removed");
assert.match(css, /\.p25-dash\s*\{[^}]*color:\s*rgba\(255,\s*255,\s*255,\s*0\.48\)/);
assert.match(css, /\.p25-dash\s*\{[^}]*border-bottom:\s*0/);
assert.match(css, /\.p25-dash\s*\{[^}]*text-decoration:\s*none/);
assert.match(css, /\.p25-breakdown-head\s*\{[^}]*justify-content:\s*space-between/);
assert.match(css, /\.p25-breakdown-head span\s*\{[^}]*font-size:\s*12px/);

/* P1-4: market -> 市场价, accumulating -> —, no duplicate health, amber alert. */
assert.strictEqual(loop.liqBasisLabel("market"), "市场价");
assert.ok(emptyWin.includes("<dt>清算价基准</dt><dd>市场价"));
const riskHtml = rendered({ health_factor: 2.1 });
const riskGrid = riskHtml.slice(riskHtml.indexOf('<dl class="p25-risk-grid">'));
assert.ok(!riskGrid.includes("健康度"), "duplicate health row removed from risk");
assert.ok(!riskGrid.includes(">数据积累中<"), "risk accumulating text replaced by dash");
assert.ok(riskGrid.includes('<dt>借款利率近期高点</dt><dd><span class="p25-dash"'));
const alertHtml = rendered({ health_factor: 1.08 });
assert.ok(alertHtml.includes("p25-alert"), "risk warning strip present");
assert.ok(!js.includes("⚠"), "no warning emoji");
assert.ok(/p25-alert[\s\S]{0,400}viewBox="0 0 16 16"/.test(alertHtml), "16px linear icon");
assert.match(css, /\.p25-alert\s*\{[^}]*background:\s*rgba\(255,\s*179,\s*64,\s*0\.12\)/);
assert.match(css, /\.p25-alert\s*\{[^}]*color:\s*#ffb340/i);
assert.match(css, /\.p25-alert \.pos-liq-ico svg\s*\{[^}]*width:\s*16px/);

/* P1-5: 18px icons, -6px stack, 2px black ring, 32px pill / 44px tap target. */
assert.match(css, /\.p25-stack img\s*\{[^}]*width:\s*18px/);
assert.match(css, /\.p25-stack img\s*\{[^}]*margin-left:\s*-6px/);
assert.match(css, /\.p25-stack img\s*\{[^}]*border:\s*2px solid #000/);
assert.match(css, /\.p25-title-row\s*\{[^}]*height:\s*32px/);
assert.match(css, /\.p25-title-row::after\s*\{[^}]*inset:\s*-6px/);
assert.match(css, /\.page--lending \.page-title-row\s*\{[^}]*align-items:\s*center/);
assert.match(css, /\.page--lending \.page-head\s*\{[^}]*display:\s*block/);
assert.match(css, /\.page--lending \.page-note\s*\{[^}]*font-size:\s*12px/);
assert.match(css, /\.page--lending \.page-note\s*\{[^}]*color:\s*rgba\(255,\s*255,\s*255,\s*0\.48\)/);
assert.match(css, /\.page--lending \.page-note\s*\{[^}]*text-align:\s*left/);

/* P2: numeric leg columns are tabular and right aligned, with a header. */
assert.ok(emptyWin.includes('<span class="p25-leg-amount">数量</span>'));
assert.ok(emptyWin.includes('<span class="p25-leg-usd">价值</span>'));
assert.match(css, /\.p25-leg-amount,\s*\n\.p25-leg-usd,\s*\n\.p25-leg-apy\s*\{[^}]*font-variant-numeric:\s*tabular-nums/);
assert.match(css, /\.p25-leg-amount,\s*\n\.p25-leg-usd,\s*\n\.p25-leg-apy\s*\{[^}]*text-align:\s*right/);
assert.ok(emptyWin.includes("<span>杠杆</span>"), "leverage cell retained");
assert.ok(emptyWin.includes("<span>利差</span>"), "spread cell retained");

console.log("p25_r1_aesthetic ok");
