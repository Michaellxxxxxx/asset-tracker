// node frontend/tests/notify_center.test.js
// p16 notification centre: queue/filter, alert dedupe/recover, unread badge count.
const fs = require("fs");
const path = require("path");
const assert = require("assert");

const src = fs.readFileSync(path.join(__dirname, "../static/app.js"), "utf8");
const css = fs.readFileSync(path.join(__dirname, "../static/styles.css"), "utf8");
function grab(name) {
  const start = src.indexOf(`  function ${name}(`);
  assert.ok(start >= 0, `function ${name} not found`);
  const next = src.indexOf("\n  }\n", start);
  return src.slice(start, next + 5);
}
const kinds = src.match(/const NOTIFY_ALERT_KINDS[^;]+;/);
assert.ok(kinds, "NOTIFY_ALERT_KINDS missing");

const code = [
  "parseOccurredAt",
  "formatCardDate",
  "notifyPendingItems",
  "notifyPositionsMap",
  "notifyRuleFor",
  "notifyProtocolLabel",
  "notifyStamp",
  "notifyDemoSuffix",
  "notifyActiveAlertRows",
  "notifyUnreadCount",
].map(grab).join("\n");

function build(state, seen = state.notify.seen) {
  return new Function(
    "state",
    "dateObject",
    "isDemoAccount",
    "notifySeenStore",
    `${kinds[0]}\n${code}\nreturn { notifyPendingItems, notifyActiveAlertRows, notifyUnreadCount };`,
  )(
    state,
    (value) => (value ? new Date(value) : null),
    () => false,
    () => seen,
  );
}

const events = [
  { id: 10, event_kind: "hf", position_id: 1, level: "recover", hf: 1.6, created_at: "2026-10-29T06:10:00Z" },
  { id: 9, event_kind: "hf", position_id: 1, level: "warn", hf: 1.32, created_at: "2026-10-29T06:05:00Z" },
  { id: 8, event_kind: "hf", position_id: 2, level: "warn", hf: 1.3, created_at: "2026-10-29T06:00:00Z" },
  { id: 7, event_kind: "hf_missing", position_id: 3, level: "warn", hf: null, created_at: "2026-10-29T05:58:00Z" },
  { id: 6, event_kind: null, position_id: null, source_name: "币安 API", source_kind: "stale", level: "warn", created_at: "2026-10-29T05:52:00Z" },
  { id: 5, event_kind: null, position_id: null, source_name: "币安 API", source_kind: "error", level: "warn", created_at: "2026-10-29T05:50:00Z" },
  { id: 4, event_kind: "wechat_send_health", position_id: null, level: "warn", created_at: "2026-10-29T05:40:00Z" },
  { id: 11, event_kind: null, position_id: null, source_name: "Coinbase", source_kind: "sync", level: "recover", created_at: "2026-10-29T06:20:00Z" },
  { id: 12, event_kind: null, position_id: null, source_name: "Coinbase", source_kind: "sync", level: "warn", created_at: "2026-10-29T06:15:00Z" },
];
const state = {
  user: { id: 7 },
  notify: {
    pending: [
      { id: 1, status: "pending", amount_usdt: 1200 },
      { id: 2, status: "pending", amount_usdt: 20 },
      { id: 3, status: "confirmed", amount_usdt: 5 },
      { id: 4, status: "pending", amount_usdt: 8 },
    ],
    skip: [2],
    positions: [
      { id: 1, protocol: "aave_v3", label: "Aave V3" },
      { id: 2, protocol: "aave_v3", label: "Aave V3" },
    ],
    rules: [{ protocol: "aave_v3", warn_hf: 1.25, danger_hf: 1.1, enabled: true }],
    events,
    overview: {
      latest_snapshot_ts: "2026-10-29T05:40:00Z",
      wechat_send_stale: true,
      wechat_send_error: "token expired",
      wechat_send_since: "2026-10-29T04:00:00Z",
    },
    reconcile: { status: "failed", latest: { id: 3, created_at: "2026-10-29T05:30:00Z", failed_sources: ["okx"] } },
    seen: new Set(),
    seenUser: 7,
  },
  pendingItems: [],
};

const api = build(state);
const queue = api.notifyPendingItems();
assert.deepStrictEqual(queue.map((item) => item.id), [1, 4, 2], "confirmed filtered, skipped sorted last");
assert.deepStrictEqual(queue.map((item) => item.id), [1, 4, 2], "queue is not mutated");

const rows = api.notifyActiveAlertRows();
assert.strictEqual(rows.health.length, 2, "newer recover closes older warning; missing HF still shown");
assert.ok(rows.health.every((row) => !row.title.includes("Aave V3 健康度 1.32")), "recovered warning is gone");
assert.ok(rows.health[0].title.includes("Aave V3 健康度 1.30"), "danger warning uses rule text");
assert.ok(rows.health[0].title.includes("接近 1.25 警戒线"));
assert.ok(rows.health[1].title.includes("健康度暂时无法计算"));
assert.ok(rows.health[0].sub.startsWith("10-29 14:00"), "alert time is Beijing time");
assert.strictEqual(rows.sync.length, 3, "wechat stale + newest binance + reconcile");
assert.strictEqual(rows.sync[0].title, "微信推送通道异常");
assert.strictEqual(rows.sync[0].sub, "最近一次推送失败 · 10-29 12:00", "sync subtitle is human, no raw error");
assert.ok(!JSON.stringify(rows).includes("token expired"), "raw send error never reaches alert rows");
assert.strictEqual(rows.sync[0].kind, "sync", "wechat row is a sync row even without retry");
assert.strictEqual(rows.sync[1].kind, "sync", "source rows are sync rows");
assert.ok(rows.health.every((row) => row.kind === "health"), "health rows keep the warning dot");
assert.ok(rows.sync[1].title.includes("已沿用 10-29 13:40 数据"));
assert.strictEqual(rows.sync[2].title, "账户对账未通过");
assert.ok(rows.sync[2].sub.includes("okx"));

assert.strictEqual(api.notifyUnreadCount(), 8, "3 pending + 5 unread alerts");
state.notify.seen.add(rows.health[0].key);
assert.strictEqual(api.notifyUnreadCount(), 7, "seen alert drops out of badge");
state.notify.skip = [];
assert.deepStrictEqual(api.notifyPendingItems().map((item) => item.id), [1, 2, 4]);

assert.ok(src.includes('row.kind === "sync" ? "notify-dot--sync"'), "sync dot follows the row kind");
assert.ok(src.includes("installNotifyGestures();"), "notify swipe gestures are installed at boot");
assert.ok(/data-notify-confirm="\$\{id\}"[^>]*disabled aria-disabled="true"/.test(src), "confirm renders disabled without a category");
assert.ok(src.includes("先选分类"), "disabled confirm says 先选分类");
assert.ok(!src.includes("系统推荐分类"), "card no longer claims a system recommendation");
assert.ok(src.includes("<span>选择分类</span>"), "category picker label is 选择分类");
assert.ok(src.includes(">编辑</button>"), "edit action is labelled 编辑");
assert.ok(!src.includes(">改</button>"), "old 改 label is gone");
assert.ok(/\.notify-row-sub\s*\{[^}]*text-overflow:\s*ellipsis/s.test(css), "sync subtitle truncates with an ellipsis");
assert.ok(/\.notify-row-sub\s*\{[^}]*white-space:\s*nowrap/s.test(css), "sync subtitle stays on one line");
assert.ok(/\.notify-row-sub\s*\{[^}]*overflow:\s*hidden/s.test(css), "sync subtitle clips overflow");
assert.ok(/\.notify-scrim\s*\{[^}]*background:\s*rgba\(0, 0, 0, 0\.55\)/s.test(css), "page scrim is 55% black");
assert.ok(/\.notify-panel\s*\{[^}]*max-height:[^;]*100dvh - 232px/s.test(css), "panel reserves space above page text");
assert.ok(/\.notify-panel\s*\{[^}]*overflow:\s*auto/s.test(css), "panel scrolls internally");
assert.ok(/\.notify-cat\s*\{[^}]*border:\s*1px solid rgba\(212, 165, 116, 0\.4\)/s.test(css), "category picker has a 1px gold 40% border");
assert.ok(/\.notify-change\s*\{[^}]*min-width:\s*88px/s.test(css), "edit action is at least 88px wide");
assert.ok(/\.notify-retry\s*\{[^}]*height:\s*44px/s.test(css), "retry keeps 44px height");
assert.ok(/\.notify-retry\s*\{[^}]*background:\s*transparent/s.test(css), "retry is a text-only gold button");
assert.ok(src.includes("最近错误"), "settings page owns the raw last-error section");
assert.ok(src.includes("loadWechatError(seq)"), "settings loads the last-error block");
assert.ok(!src.includes("detail.slice(0, 48)"), "sync row no longer truncates raw error into the subtitle");
assert.ok(src.includes("左滑跳过"), "pending skip affordance present");
assert.ok(src.includes("查看全部待确认"), "view-all link present");
assert.ok(src.includes('data-more-nav="pending"'), "more drawer keeps the all-pending entry");
assert.ok(!src.includes("待确认</span>\n            <span class=\"more-sheet-row-chevron\""));
console.log("notify_center: all assertions passed");
