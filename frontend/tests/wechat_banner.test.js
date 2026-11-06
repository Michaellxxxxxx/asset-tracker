// node frontend/tests/wechat_banner.test.js
// p15b2: the site banner keeps one human conclusion and a gold settings
// action. The raw send error is never rendered here; it only appears inside
// the settings WeChat card disclosure.
const fs = require("fs");
const path = require("path");
const assert = require("assert");

const src = fs.readFileSync(path.join(__dirname, "../static/app.js"), "utf8");
const css = fs.readFileSync(path.join(__dirname, "../static/styles.css"), "utf8");
function grab(name) {
  const start = src.indexOf(`  function ${name}(`);
  assert.ok(start >= 0, `function ${name} not found`);
  const next = src.indexOf("\n  }\n", start);
  assert.ok(next > start, `function ${name} terminator not found`);
  return src.slice(start, next + 5);
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => {
    const entities = {
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    };
    return entities[character];
  });
}

const state = {
  overviewData: null,
  wechatBannerDismissTier: null,
  wechatBannerStaleSeen: false,
  wechatBannerTier: null,
};
const banner = new Function(
  "state",
  "escapeHtml",
  "PF_WARN_SVG",
  `${grab("wechatAgeTier")}
${grab("wechatBannerMarkup")}
return wechatBannerMarkup;`,
)(
  state,
  escapeHtml,
  '<svg viewBox="0 0 12 11"></svg>',
);

const healthy = banner({ wechat_token_stale: false, wechat_send_stale: false });
assert.strictEqual(healthy, "", "healthy state renders no banner");
assert.strictEqual(state.wechatBannerDismissTier, null);
assert.strictEqual(state.wechatBannerStaleSeen, false);

const error = 'unverified: <ret=-2> & "prepare failed"';
const sendStale = banner({
  wechat_token_stale: false,
  wechat_send_stale: true,
  wechat_send_error: error,
  wechat_send_since: "2026-10-29T05:10:42Z",
});
assert.ok(sendStale.includes("data-wechat-banner"), "banner marker present");
assert.ok(sendStale.includes('role="alert"'), "banner is announced as an alert");
assert.ok(sendStale.includes("微信告警暂时发不出去"), "plain send-stale conclusion");
assert.ok(sendStale.includes("告警会留在站内，不会丢"), "plain reassurance");
assert.ok(sendStale.includes('href="/settings"'), "gold action goes to settings");
assert.ok(sendStale.includes(">去设置</a>"), "gold action label");
assert.ok(sendStale.includes('data-wechat-tier="send"'), "send incident tier");
assert.ok(sendStale.includes("data-wechat-dismiss"), "close control present");
assert.ok(!sendStale.includes("unverified"), "technical error is not in the banner");
assert.ok(!sendStale.includes("prepare failed"), "error text is not in the banner");
assert.ok(!sendStale.includes("wechat-banner-detail"), "no error detail node");
assert.strictEqual(state.wechatBannerTier, "send");

const tokenStale = banner({ wechat_token_stale: true, wechat_token_age_hours: 30 });
assert.ok(tokenStale.includes("微信告警已中断 30 小时"), "token-age conclusion");
assert.ok(tokenStale.includes("给微信机器人发一句话即可恢复"), "recovery instruction");
assert.ok(tokenStale.includes('data-wechat-tier="24to48"'), "24-48h age tier");
assert.ok(tokenStale.includes('data-wechat-cta'), "token tier keeps the settings CTA");
assert.ok(tokenStale.includes('href="/settings"'), "token CTA goes to settings");
assert.ok(tokenStale.includes(">去设置</a>"), "token CTA is the gold 去设置 action");
assert.ok(!tokenStale.includes("wechat-banner-detail"), "no send detail without send stale");

const unknownAge = banner({ wechat_token_stale: true, wechat_token_age_hours: null });
assert.ok(unknownAge.includes("微信告警还没连上"), "unknown-age conclusion");

state.wechatBannerDismissTier = "24to48";
assert.strictEqual(
  banner({ wechat_token_stale: true, wechat_token_age_hours: 30 }),
  "",
  "dismissal is scoped to the incident tier",
);

state.wechatBannerDismissTier = null;
const both = banner({
  wechat_token_stale: true,
  wechat_token_age_hours: 30,
  wechat_send_stale: true,
  wechat_send_error: error,
});
assert.ok(both.includes("微信告警已中断 30 小时"));
assert.ok(!both.includes("prepare failed"), "even with both flags the error stays in settings");

const withSlot = new Function(
  `${grab("withWechatBannerSlot")}
return withWechatBannerSlot;`,
)();
const slotted = withSlot('<header class="topbar"></header><div id="content"></div>');
assert.ok(
  slotted.startsWith(
    '<header class="topbar"></header><div class="wechat-banner-slot" data-wechat-banner-slot></div>',
  ),
  "banner slot follows the topbar on every shell page",
);
assert.ok(slotted.endsWith('<div id="content"></div>'), "page content is preserved");
const noHeader = withSlot("<div>login</div>");
assert.ok(noHeader.startsWith('<div class="wechat-banner-slot"'), "headerless pages still get the slot");

// The settings card owns the full error, folded behind 查看详情.
const settingsState = { wechatDetailsOpen: false };
const card = new Function(
  "state",
  "settingsState",
  "escapeHtml",
  "ICONS",
  "formatDateTime",
  `${grab("wechatCardModel")}
${grab("wechatSettingsMarkup")}
return { wechatCardModel, wechatSettingsMarkup };`,
)(
  state,
  settingsState,
  escapeHtml,
  { bell: '<svg aria-hidden="true"></svg>' },
  () => "2026.10.29 05:10",
);

state.overviewData = {
  wechat_token_stale: false,
  wechat_send_stale: true,
  wechat_send_error: error,
  wechat_send_since: "2026-10-29T05:10:42Z",
};
const folded = card.wechatSettingsMarkup();
assert.ok(folded.includes("微信推送"), "settings card title");
assert.ok(folded.includes("查看详情"), "details are collapsed behind a disclosure");
assert.ok(folded.includes("hidden"), "details start hidden");
assert.ok(folded.includes("发送失败"), "settings card names the failed send");
assert.ok(
  folded.includes("unverified: &lt;ret=-2&gt; &amp; &quot;prepare failed&quot;"),
  "the full error is escaped inside the settings card",
);
assert.ok(!folded.includes("<ret=-2>"), "raw error markup is never injected");

settingsState.wechatDetailsOpen = true;
const opened = card.wechatSettingsMarkup();
assert.ok(opened.includes("收起详情"), "open disclosure offers a close action");
assert.ok(!opened.includes('id="wechat-details" data-wechat-details-panel hidden'), "panel opens");

state.overviewData = { wechat_token_stale: false, wechat_send_stale: false };
settingsState.wechatDetailsOpen = false;
const calm = card.wechatSettingsMarkup();
assert.ok(calm.includes("正常"), "healthy status is named");
assert.ok(calm.includes("查看详情"), "details remain available when healthy");

// The banner contract is also pinned in CSS: near-black base, 8% white
// border, amber reserved for the icon, and a white 14/13 two-row type scale.
assert.ok(css.includes("background: #1C1C1E"), "banner base is #1C1C1E");
assert.ok(
  css.includes("border: 1px solid rgba(255, 255, 255, 0.08)"),
  "banner border is white 8%",
);
assert.ok(
  /\.wechat-banner-lead \{[^}]*font-size: 14px/.test(css),
  "banner title is 14px",
);
assert.ok(
  /\.wechat-banner-action \{[^}]*font-size: 13px/.test(css),
  "banner description is 13px",
);
assert.ok(
  /\.wechat-banner-cta \{[^}]*color: var\(--accent-bright\)/.test(css),
  "banner action is gold text",
);

// p15b3: the raw recent error is a single monospace line that scrolls
// sideways instead of reflowing the settings card.
const errorCodeRule = css.match(/\.wechat-details code \{([^}]*)\}/);
assert.ok(errorCodeRule, "recent-error code rule exists");
assert.ok(/font-size: 12px/.test(errorCodeRule[1]), "recent error is 12px");
assert.ok(
  /color: rgba\(255, 255, 255, 0\.6\)/.test(errorCodeRule[1]),
  "recent error is white 60%",
);
assert.ok(/white-space: nowrap/.test(errorCodeRule[1]), "recent error never wraps");
assert.ok(/overflow-x: auto/.test(errorCodeRule[1]), "recent error scrolls sideways");

console.log("wechat_banner: all assertions passed");
