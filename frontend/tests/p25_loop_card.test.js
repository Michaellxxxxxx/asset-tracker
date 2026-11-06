// node frontend/tests/p25_loop_card.test.js
// p25: loop-card formatting, missing/accumulating states, negative spread,
// split identity, real protocol icons and the drawer's a11y contract.
const fs = require("fs");
const path = require("path");
const assert = require("assert");

const staticDir = path.join(__dirname, "../static");
const loop = require(path.join(staticDir, "loop_card.js"));
const js = fs.readFileSync(path.join(staticDir, "loop_card.js"), "utf8");
const app = fs.readFileSync(path.join(staticDir, "app.js"), "utf8");
const css = fs.readFileSync(path.join(staticDir, "loop_card.css"), "utf8");

/* ---- formatting ---- */
assert.strictEqual(loop.fmtUsd(1887.14), "$1,887.14");
assert.strictEqual(loop.fmtUsd(null), "—");
assert.strictEqual(loop.fmtSignedUsd(3.6), "+$3.60");
assert.strictEqual(loop.fmtSignedUsd(-293), "−$293.00");
assert.strictEqual(loop.fmtSignedPct(0.0807935), "+8.08%");
assert.strictEqual(loop.fmtPct(-0.330474), "−33.05%");
assert.strictEqual(loop.fmtPct(null), "—");
assert.strictEqual(loop.fmtPrice(1.0987), "1.0987");
assert.strictEqual(loop.fmtPrice(0.8548), "0.8548");
assert.strictEqual(loop.fmtLeverage(6.55), "6.55×");
assert.strictEqual(loop.fmtDistance(0.074), "7.4%");
assert.strictEqual(loop.fmtBeijing("2026-10-28T12:28:00Z"), "2026-10-28 20:28");
assert.strictEqual(loop.fmtBeijing("2026-10-31T06:40:00Z"), "2026-10-31 14:40");
assert.strictEqual(loop.fmtBeijing(null), "—");

const meta1 = {
  id: 1,
  protocol: "morpho",
  chain: "ethereum",
  label: "Morpho USD3/USDC",
};
const payload1 = {
  position_id: 1,
  principal_usd: 1887.14,
  net_apy: 0.08079358631580062,
  collateral_yield_contrib: 0.411268,
  borrow_cost_contrib: -0.330474,
  health_factor: 1.08,
  liq_price: 1.0987,
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
  collateral: { usd: 12360.41, legs: [{ symbol: "USD3", amount: 10417.0, usd: 12360.41, apy: 0.06279 }] },
  borrow: { usd: 10473.27, legs: [{ symbol: "USDC", amount: 10473.27, usd: 10473.27, apy: 0.059546 }] },
  pnl: { amount: 3.6, rate: 0.001911, net_flows: 293, snapshots: 5, start: "2026-10-28T12:28:00Z" },
  pnl_breakdown: {
    price: null,
    collateral_interest: null,
    borrow_interest: null,
    interest_net: null,
  },
  apy_windows: {
    "7d": {
      min: 0.08,
      max: 0.09,
      avg: 0.085,
      points: [{ date: "2026-10-31", value: 0.08 }],
      samples: 1,
      coverage_days: 3.1,
      accumulating: true,
    },
    "30d": {
      min: 0.08,
      max: 0.09,
      avg: 0.085,
      points: [{ date: "2026-10-31", value: 0.08 }],
      samples: 1,
      coverage_days: 3.1,
      accumulating: true,
    },
  },
  missing: {
    borrow_apy_high: "数据积累中：历史快照未记录借款利率（本期起写入）",
    price_pnl: "历史快照缺少抵押/借款分项利率，无法拆分收入与支出",
    collateral_interest: "历史快照缺少抵押/借款分项利率，无法拆分收入与支出",
    borrow_interest: "历史快照缺少抵押/借款分项利率，无法拆分收入与支出",
    window_7d: "数据积累中：仅覆盖 3.1 天（需 7 天）",
    window_30d: "数据积累中：仅覆盖 3.1 天（需 30 天）",
  },
};

const html1 = loop.renderCard(payload1, meta1);
assert.ok(html1.includes("$1,887.14"), "principal is rendered");
assert.ok(html1.includes("+8.08%"), "net APY is rendered");
assert.ok(html1.includes("抵押 <b class=\"p25-positive\">+41.13%</b>"), "supply contribution");
assert.ok(html1.includes("借款 <b class=\"p25-negative\">−33.05%</b>"), "borrow contribution");
assert.ok(html1.includes("1.080"), "health factor");
assert.ok(html1.includes("1.0987"), "liquidation price");
assert.ok(html1.includes("7.4%"), "distance to liquidation");
assert.ok(html1.includes("6.55×"), "leverage");
assert.ok(html1.includes("数据积累中"), "borrow high / windows accumulate");
assert.ok(html1.includes("累计收益缺失") === false, "pnl exists");
assert.ok(html1.includes("历史快照缺少抵押/借款分项利率"), "split missing reason");
assert.ok(html1.includes("+$3.60"), "cumulative pnl amount");
assert.ok(html1.includes("已扣除 $293.00 充提"), "deposit/withdrawal line");
assert.ok(html1.includes("起点 2026-10-28 20:28"), "start time is Beijing time");

/* id2 shape: oracle-based PT loop, depeg applies to the stable leg. */
const html2 = loop.renderCard(
  {
    ...payload1,
    position_id: 2,
    principal_usd: 2254.78,
    net_apy: 0.20474320244103633,
    collateral_yield_contrib: 0.561563,
    borrow_cost_contrib: -0.35682,
    health_factor: 1.149,
    liq_price: 0.8548,
    distance_to_liq_pct: 0.12962,
    leverage: 3.74,
    spread: 0.01994,
    depeg_liq_price: 0.8548,
    depeg_pair: "USDC/PT-USD3-17DEC2026",
    collateral: {
      usd: 8438.26,
      legs: [{ symbol: "PT-USD3-17DEC2026", amount: 8592.06, usd: 8438.26, apy: 0.15006 }],
    },
    borrow: { usd: 6183.48, legs: [{ symbol: "USDC", amount: 6183.48, usd: 6183.48, apy: 0.13012 }] },
    pnl: { amount: 3.63, rate: 0.001612, net_flows: 0, snapshots: 4, start: "2026-10-28T18:00:00Z" },
  },
  { id: 2, protocol: "morpho", chain: "ethereum", label: "Morpho PT-USD3-17DEC2026/USDC" },
);
assert.ok(html2.includes("0.8548"), "oracle liquidation price");
assert.ok(html2.includes("USDC/PT-USD3-17DEC2026"), "depeg pair");
assert.ok(html2.includes("20.47%"), "id2 net APY");
assert.ok(html2.includes("13.0%"), "id2 distance");

/* Negative spread is red and explicitly flagged. */
const negative = loop.renderCard(
  { ...payload1, spread: -0.06, spread_negative: true, missing: {} },
  meta1,
);
assert.ok(negative.includes("负利差"), "negative spread flag");
assert.match(negative, /class="p25-negative">−6\.00pp/, "negative spread is red");

/* Accumulating window renders the label and coverage. */
assert.ok(html1.includes("数据积累中 · 已覆盖 3.1 天"), "coverage days in accumulating state");

/* Split identity: price + income - expense = total. */
const split = loop.renderCard(
  {
    ...payload1,
    pnl: { amount: 115, rate: 0.0191, net_flows: 0, snapshots: 3, start: "2026-10-24T00:00:00Z" },
    pnl_breakdown: {
      price: 100,
      collateral_interest: 20,
      borrow_interest: 5,
      interest_net: 15,
    },
    missing: {},
  },
  meta1,
);
assert.ok(
  split.includes("价格涨跌 +$100.00 + 收入 +$20.00 − 支出 $5.00 = 累计收益 +$115.00"),
  "split equation is explicit",
);
assert.ok(split.includes("净利息 +$15.00"), "net interest");
assert.ok(
  split.includes('借款利息支出</dt><dd class="p25-negative">−$5.00'),
  "borrow interest is displayed as a negative expense",
);
assert.ok(js.includes("价格涨跌（含价格与其它）"), "price label says it includes other effects");
assert.ok(js.includes("已接入 ${PROTOCOLS.length} 个协议"), "protocol count comes from the icon list");
assert.ok(
  !/@media \(min-width: 900px\)[\s\S]{0,200}align-items: center/.test(css),
  "desktop protocol panel stays a bottom sheet",
);

/* Missing fields render 缺失 with the backend's reason in the title. */
const missing = loop.renderCard(
  {
    position_id: 11,
    principal_usd: null,
    net_apy: null,
    health_factor: null,
    liq_price: null,
    distance_to_liq_pct: null,
    leverage: null,
    spread: null,
    depeg_applicable: true,
    depeg_liq_price: null,
    borrow_apy_high: null,
    pnl: { amount: null, rate: null, net_flows: null, snapshots: 0 },
    pnl_breakdown: { price: null, collateral_interest: null, borrow_interest: null },
    apy_windows: { "7d": { accumulating: true, points: [] }, "30d": { accumulating: true, points: [] } },
    missing: {
      principal: "缺少抵押或借款美元价值",
      net_apy: "本金不为正或缺少抵押/借款价值",
      health_factor: "协议未提供健康度",
      liq_price: "协议未提供清算价",
      distance_to_liq_pct: "缺少清算价",
      leverage: "本金不为正或缺少抵押价值",
      pnl: "暂无仓位快照，无法计算累计收益",
      price_pnl: "暂无仓位快照，无法拆分",
      borrow_apy_high: "数据积累中：历史快照未记录借款利率（本期起写入）",
    },
  },
  { id: 11, protocol: "aave", chain: "ethereum", label: "Aave 缺失样例" },
);
assert.ok(missing.includes("—") && !missing.includes(">缺失<"), "missing renders em dash");
assert.ok(missing.includes("累计收益暂不可用"), "pnl missing state");
assert.ok(missing.includes("数据积累中"), "accumulating is distinguished from missing");

/* Real protocol icons, no letter placeholders. */
assert.strictEqual(loop.PROTOCOLS.length, 5);
for (const item of loop.PROTOCOLS) {
  assert.ok(item.icon.endsWith(".png"), `${item.key} points at a local image`);
  const disk = path.join(staticDir, item.icon.replace("/static/", ""));
  assert.ok(fs.existsSync(disk), `${item.key} icon exists: ${disk}`);
}
assert.strictEqual(loop.protocolIcon("morpho"), "/static/icons/protocols/morpho.png");
assert.strictEqual(loop.protocolIcon("AAVE"), "/static/icons/protocols/aave.png");
assert.ok(!/neutralLetterIcon/.test(app), "app.js has no letter-placeholder helpers");
assert.ok(app.includes("PROTOCOL_ICON_FILES"), "app.js maps protocols to real icons");
assert.ok(!app.includes("lendingProtocolsMarkup"), "side protocol card was removed");
assert.ok(!/lendingProtocolsMarkup/.test(js), "loop card does not recreate the side card");
assert.ok(css.includes("font-variant-numeric: tabular-nums"), "numbers use tabular figures");

/* Drawer contract: Esc, focus trap, drag threshold, focus return. */
assert.ok(/event\.key === "Escape"/.test(js), "Esc closes the drawer");
assert.ok(/event\.key !== "Tab"/.test(js) && /trapFocusIndex/.test(js), "Tab is trapped");
assert.ok(/aria-modal="true"/.test(js), "dialog is modal");
assert.ok(/lastFocus/.test(js) && /\.focus\(/.test(js), "focus returns to trigger");
assert.ok(/data-p25-grip/.test(js), "drag handle is present");
assert.ok(js.includes("只读，不发起交易"), "read-only footer");
assert.ok(js.includes("data-p25-protocols"), "title row trigger exists");

assert.strictEqual(loop.drawerShouldClose(97, 400), true);
assert.strictEqual(loop.drawerShouldClose(95, 400), false);
assert.strictEqual(loop.drawerShouldClose(60, 200), true, "28% of a short panel");
assert.strictEqual(loop.drawerShouldClose(-40, 400), false);
assert.strictEqual(loop.trapFocusIndex(0, 1, true), 0);
assert.strictEqual(loop.trapFocusIndex(0, 3, true), 2);
assert.strictEqual(loop.trapFocusIndex(2, 3, false), 0);
assert.strictEqual(loop.trapFocusIndex(-1, 3, false), 0);

assert.ok(
  loop.renderSkeleton(meta1).includes("data-loop-card"),
  "loading skeleton keeps the selector hook used by the fetch completion",
);

/* The card starts folded: only the top block is visible. */
assert.ok(html1.includes('aria-expanded="false"'), "card defaults to collapsed");
assert.ok(html1.includes("p25-loop-more\" id=") && !html1.includes("p25-loop-more is-open"), "more panel hidden");
const expanded = loop.renderCard(payload1, { ...meta1, expanded: true });
assert.ok(expanded.includes("p25-loop-more is-open"), "expanded state supported");
assert.ok(!expanded.includes('aria-expanded="false"'), "expanded aria state");

console.log("p25_loop_card ok");