// node frontend/tests/p31_settings.test.js
// p31: iOS-style settings — section inventory, mobile status summaries,
// linear icon discipline, danger group, and fixed layout/motion tokens.
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
function grabConst(name) {
  const start = src.indexOf(`  const ${name} = `);
  assert.ok(start >= 0, `const ${name} not found`);
  const end = src.indexOf(name === "SETTINGS_SECTIONS" ? "\n  ];" : "\n  };", start);
  assert.ok(end > start, `const ${name} end not found`);
  return src.slice(start, end + (name === "SETTINGS_SECTIONS" ? 5 : 5));
}

const sectionsCode = grabConst("SETTINGS_SECTIONS");
const iconsCode = grabConst("SETTINGS_LINE_ICONS");
const functionsCode = [
  "settingsRelativeTime",
  "settingsSummaryFor",
  "settingsHomeItems",
  "settingsHomeRowsMarkup",
  "settingsDangerItems",
  "settingsDangerMarkup",
  "settingsNavMarkup",
].map(grab).join("\n");
const settingsState = {
  wallets: [{ id: 1 }, { id: 2 }, { id: 3 }],
  key: { configured: true, last4: "9f3c" },
};
const escapeHtml = (value) =>
  String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
const ICONS = { chevron: '<svg viewBox="0 0 24 24" fill="none"><path d="m9 6 6 6-6 6"/></svg>' };
const factory = new Function(
  "ICONS",
  "escapeHtml",
  "settingsState",
  `${sectionsCode}\n${iconsCode}\n${functionsCode}
  return {
    SETTINGS_SECTIONS,
    SETTINGS_LINE_ICONS,
    settingsRelativeTime,
    settingsSummaryFor,
    settingsHomeItems,
    settingsHomeRowsMarkup,
    settingsDangerItems,
    settingsDangerMarkup,
    settingsNavMarkup,
  };`,
);
const settings = factory(ICONS, escapeHtml, settingsState);
const NOW = Date.parse("2026-11-02T12:00:00+08:00");

assert.deepStrictEqual(
  settings.SETTINGS_SECTIONS.map((section) => section.id),
  ["sync", "source", "wallet", "alert", "wechat", "account", "privacy"],
  "seven sections in wireframe order",
);

const base = {
  key: { configured: true, last4: "9f3c" },
  wallets: [{}, {}, {}],
  alertRules: [{ enabled: true }],
  sync: { last_run_at: new Date(NOW - 2 * 60 * 1000).toISOString(), last_status: "ok" },
  wechat: { label: "正常", tone: "is-on" },
  user: { username: "avery", is_admin: true },
  now: NOW,
};
assert.strictEqual(settings.settingsSummaryFor("sync", base, NOW).text, "2 分钟前同步");
assert.strictEqual(
  settings.settingsSummaryFor("sync", { ...base, sync: { ...base.sync, last_status: "partial" } }, NOW).text,
  "部分来源失败",
);
assert.strictEqual(
  settings.settingsSummaryFor("sync", { ...base, sync: { ...base.sync, last_status: "partial" } }, NOW).warn,
  true,
);
assert.strictEqual(settings.settingsSummaryFor("source", base, NOW).text, "已配置 · 9f3c");
assert.strictEqual(settings.settingsSummaryFor("wallet", base, NOW).text, "3 个地址");
assert.strictEqual(settings.settingsSummaryFor("alert", base, NOW).text, "已开启");
assert.strictEqual(
  settings.settingsSummaryFor("wechat", { ...base, wechat: { label: "未连接", tone: "is-warn" } }, NOW).text,
  "未连接",
);
assert.strictEqual(
  settings.settingsSummaryFor("wechat", { ...base, wechat: { label: "未连接", tone: "is-warn" } }, NOW).warn,
  true,
);
assert.strictEqual(settings.settingsSummaryFor("account", base, NOW).text, "管理员");
assert.strictEqual(settings.settingsSummaryFor("privacy", base, NOW).text, "只读");

const items = settings.settingsHomeItems(base);
assert.strictEqual(items.length, 7);
const rows = settings.settingsHomeRowsMarkup(items);
assert.strictEqual((rows.match(/data-settings-open=/g) || []).length, 7, "seven tappable rows");
assert.ok(rows.includes('class="settings-home-summary" data-settings-summary="sync"'));
assert.ok(rows.includes("2 分钟前同步"));
assert.ok(rows.includes('data-settings-summary="wallet"'));
assert.ok(rows.includes("3 个地址"));
assert.ok(rows.includes('class="settings-warn-dot"') === false, "healthy summaries have no amber dot");
assert.ok(!/<img|📊|🔔|👤|🔑|💼|🛡/u.test(rows), "no image icons or emoji");
assert.ok(rows.includes('fill="none"'), "icons are linear");
for (const section of settings.SETTINGS_SECTIONS) {
  const icon = settings.SETTINGS_LINE_ICONS[section.id];
  assert.ok(icon && icon.includes("<svg") && icon.includes('fill="none"'), `linear icon for ${section.id}`);
}

const warnRows = settings.settingsHomeRowsMarkup([
  { ...items[0], summary: "同步失败", warn: true },
]);
assert.ok(warnRows.includes('class="settings-home-summary is-warn"'));
assert.ok(warnRows.includes('class="settings-warn-dot"'));

const danger = settings.settingsDangerMarkup("home");
assert.strictEqual((danger.match(/data-danger=/g) || []).length, 3);
assert.ok(danger.includes("删除全部钱包"));
assert.ok(danger.includes("删除 CoinStats key"));
assert.ok(danger.includes("退出登录"));
assert.ok(danger.includes("危险操作"));
assert.ok(!danger.includes("cs-btn--primary"), "danger never renders a gold primary button");

// r2: danger confirm is portalled to body, never nested in the settings page.
assert.ok(src.includes('"settings-portal-root"'), "dedicated body portal root");
assert.ok(src.includes("settingsPortalRoot().append(root)"), "dialog mounts into portal");
assert.ok(src.includes("portal.parentElement !== document.body"), "portal stays a body child");
assert.ok(src.includes("settings-danger-open"), "body/html scroll lock class");
assert.ok(src.includes("settingsLockBackgroundScroll"), "background scroll lock");
assert.ok(src.includes("settingsUnlockBackgroundScroll"), "background scroll unlock");
assert.ok(src.includes("document.body.style.top"), "iOS scrollY offset is stored and restored");
assert.ok(src.includes("window.scrollTo(0, y)"), "scrollY restored on close");
assert.ok(src.includes('root.id = "settings-danger-dialog"'), "dialog root id preserved");
assert.ok(settings.settingsDangerMarkup("desktop", { id: "danger" }).includes('id="danger"'),
  "desktop danger group is the #danger anchor target");
assert.ok(!settings.settingsDangerMarkup("home").includes('id="danger"'), "duplicate #danger id avoided");
assert.ok(src.includes("settingsScrollTarget"), "danger anchor scroll target supported");
assert.ok(src.includes('id !== "danger" && !SETTINGS_SECTION_IDS.includes(id)'), "danger is navigable");
assert.ok(/html\.settings-danger-open,\s*body\.settings-danger-open\s*\{[^}]*overflow:\s*hidden/s.test(css),
  "html/body lock scroll while dialog is open");
assert.ok(/\.settings-confirm-root\s*\{[^}]*z-index:\s*(\d+)/s.test(css), "confirm layer has z-index");
const confirmZ = Number((css.match(/\.settings-confirm-root\s*\{[^}]*z-index:\s*(\d+)/s) || [])[1]);
assert.ok(confirmZ >= 1000, `confirm z-index ${confirmZ} sits above app layers`);
assert.ok(/#settings-portal-root\s*\{[^}]*z-index:\s*(\d+)/s.test(css), "portal layer has z-index");
const portalZ = Number((css.match(/#settings-portal-root\s*\{[^}]*z-index:\s*(\d+)/s) || [])[1]);
assert.ok(portalZ >= 1000, `portal z-index ${portalZ} sits above app layers`);
assert.ok(/\.settings-confirm-backdrop\s*\{[^}]*background:\s*rgba\(0,\s*0,\s*0,\s*0?\.6\)/s.test(css),
  "scrim is rgba(0,0,0,.6)");
assert.ok(/\.settings-confirm-backdrop\s*\{[^}]*backdrop-filter:\s*blur\(8px\)/s.test(css), "scrim blur 8px");
assert.ok(/\.settings-confirm-backdrop\s*\{[^}]*\-webkit-backdrop-filter:\s*blur\(8px\)/s.test(css),
  "scrim keeps the -webkit blur fallback");
assert.ok(/\.settings-confirm\s*\{[^}]*background:\s*#1c1c1e/s.test(css), "card is solid #1C1C1E");
assert.ok(/\.settings-confirm\s*\{[^}]*border-radius:\s*22px/s.test(css), "card radius 22px");
assert.ok(!/\.settings-confirm\s*\{[^}]*backdrop-filter/s.test(css), "card is not glass");

// r3: danger copy balances its wrap at 1280 so the last line is never just "加。".
assert.ok(/\.settings-confirm p\s*\{[^}]*text-wrap:\s*pretty/s.test(css), "danger copy keeps pretty fallback");
assert.ok(/\.settings-confirm p\s*\{[^}]*text-wrap:\s*balance/s.test(css), "danger copy balances line wrap");

// r2: only home/section decorative icons are white 60%; gold stays interactive.
assert.ok(/\.settings-home-icon\s*\{[^}]*color:\s*rgba\(255,\s*255,\s*255,\s*0?\.6\)/s.test(css),
  "home icon is white 60%");
assert.ok(/\.settings-section-icon\s*\{[^}]*color:\s*rgba\(255,\s*255,\s*255,\s*0?\.6\)/s.test(css),
  "section icon is white 60%");
assert.ok(/\.settings-warn-dot\s*\{[^}]*background:\s*#ffb454/s.test(css), "amber warn dot unchanged");

// r2: desktop danger footer is a real 44px anchor to #danger.
const nav = settings.settingsNavMarkup();
assert.ok(nav.includes('class="settings-nav-danger"'), "danger nav link class");
assert.ok(nav.includes('href="#danger"'), "danger nav points at #danger");
assert.ok(nav.includes('data-settings-anchor="danger"'), "danger nav is a settings anchor");
assert.ok(!nav.includes("settings-nav-foot"), "old static footer text removed");
assert.ok(/\.settings-nav-danger\s*\{[^}]*min-height:\s*44px/s.test(css), "danger anchor tap area >= 44px");
assert.ok(/\.settings-nav-danger\s*\{[^}]*color:\s*rgba\(255,\s*255,\s*255,\s*0?\.48\)/s.test(css),
  "danger anchor text is white 48%");

// Layout contract: 232px sticky nav, selected #1C1C1E + gold #D4A574.
assert.ok(/grid-template-columns: 232px minmax\(0, 1fr\)/.test(css));
assert.ok(/\.settings-nav\s*\{[^}]*position: sticky/s.test(css));
assert.ok(/\.settings-nav-link\.is-active\s*\{[^}]*background: #1c1c1e/s.test(css));
assert.ok(/\.settings-nav-link\.is-active\s*\{[^}]*color: #d4a574/s.test(css));
assert.ok(css.includes("settings-push-in 280ms"), "280ms mobile push");
assert.ok(css.includes("prefers-reduced-motion: reduce"));
assert.ok(/#ff453a/i.test(css), "danger red #FF453A");
assert.ok(/\.settings-danger-row\s*\{[^}]*color: #ff453a/s.test(css));
assert.ok(/\.settings-home-summary\s*\{[^}]*font-size: 14px/s.test(css));
assert.ok(/\.settings-home-row\s*\{[^}]*min-height: 56px/s.test(css));
assert.ok(/\.settings-nav-link\s*\{[^}]*min-height: 44px/s.test(css));
assert.ok(!src.includes("settingsAsideMarkup"), "right overview aside removed");
assert.ok(!src.includes('id="settings-side"'), "no desktop aside DOM");
assert.ok(src.includes("settingsCloseSection"), "back/Esc restores home");
assert.ok(src.includes("settingsView.homeScroll"), "home scroll position preserved");
assert.ok(src.includes('data-settings-danger-confirm'), "danger confirmation action exists");

console.log("p31_settings: all assertions passed");
console.log(`  sections=${items.length} danger=${3} rows=7`);
