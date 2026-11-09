// node --test frontend/tests/p45_admin_console.test.js
// p45: /admin is its own route. The admin entry exists only for admins and the
// settings page keeps 7 sections with a static account description.
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

/* ---------- Route ---------- */

const routesMatch = src.match(/const ROUTES = new Set\(\[([\s\S]*?)\]\);/);
assert.ok(routesMatch, "ROUTES set found");
const ROUTES = new Function(`return new Set([${routesMatch[1]}]);`)();
assert.ok(ROUTES.has("/admin"), "ROUTES contains /admin");
assert.ok(ROUTES.has("/"), "overview route still present");
assert.ok(ROUTES.size === 8, `expected 8 routes, got ${ROUTES.size}`);
assert.ok(src.includes('} else if (route === "/admin") {'), "renderRoute has an /admin branch");

/* ---------- Admin entry: rail and drawer ---------- */

const railCode = grab("railItemsFor");
const makeRail = new Function("RAIL_ITEMS", "ICONS", `${railCode}\nreturn railItemsFor;`);
const railItemsFor = makeRail(
  [
    ["overview", "/", "总览", "<svg-overview>"],
    ["settings", "/settings", "设置", "<svg-settings>"],
  ],
  { users: "<svg-users>" },
);

assert.deepStrictEqual(
  railItemsFor({ is_admin: false }).map((item) => item[0]),
  ["overview", "settings"],
  "member rail has no admin node",
);
assert.deepStrictEqual(
  railItemsFor(null).map((item) => item[0]),
  ["overview", "settings"],
  "missing user renders no admin node",
);
const adminRail = railItemsFor({ is_admin: true });
assert.deepStrictEqual(
  adminRail.map((item) => item[0]),
  ["overview", "settings", "admin"],
  "admin node is appended last",
);
assert.deepStrictEqual(adminRail[adminRail.length - 1], ["admin", "/admin", "管理", "<svg-users>"]);

const makeSheet = new Function(
  "state",
  "ICONS",
  "escapeHtml",
  "moreSheetSyncStamp",
  "moreSheetSyncText",
  `${grab("moreSheetMarkup")}\nreturn moreSheetMarkup;`,
);
const sheetIcons = { pending: "p", settings: "s", users: "u", chevron: "c" };
const sheetFor = (isAdmin) =>
  makeSheet(
    {
      user: { username: "avery", is_admin: isAdmin },
      syncBusy: false,
      pendingCount: 0,
      moreSheet: false,
    },
    sheetIcons,
    (value) => String(value ?? ""),
    () => null,
    () => "",
  )();

const adminSheet = sheetFor(true);
assert.ok(adminSheet.includes('data-more-nav="admin"'), "admin drawer row exists");
assert.ok(adminSheet.includes("账号管理"), "drawer row label is 账号管理");
assert.ok(
  adminSheet.indexOf('data-more-nav="admin"') > adminSheet.indexOf('data-more-nav="settings"'),
  "账号管理 sits below 设置",
);
assert.ok(!sheetFor(false).includes('data-more-nav="admin"'), "member drawer has no admin row");

/* ---------- Settings page keeps its 7 sections + static copy ---------- */

const sectionsStart = src.indexOf("  const SETTINGS_SECTIONS = ");
assert.ok(sectionsStart >= 0, "SETTINGS_SECTIONS found");
const sectionsEnd = src.indexOf("\n  ];", sectionsStart);
assert.ok(sectionsEnd > sectionsStart, "SETTINGS_SECTIONS end found");
const sectionsCode = src.slice(sectionsStart, sectionsEnd + 5);
const SETTINGS_SECTIONS = new Function(`${sectionsCode}\nreturn SETTINGS_SECTIONS;`)();
assert.strictEqual(SETTINGS_SECTIONS.length, 7, "settings still has exactly 7 sections");
const account = SETTINGS_SECTIONS.find((section) => section.id === "account");
assert.ok(account, "account section exists");
assert.strictEqual(account.copy, "当前登录账号", "account copy is the static string");
assert.ok(!/\bget\s+copy\b/.test(sectionsCode), "account copy is not a getter");
assert.ok(!sectionsCode.includes("成员管理"), "no 成员管理 in the settings inventory");

/* ---------- Admin page markup and guards ---------- */

const forbiddenCode = grab("renderAdminForbiddenPage");
assert.ok(forbiddenCode.includes("无权访问"), "forbidden page renders its h1 copy");
assert.ok(!forbiddenCode.includes("api("), "forbidden page issues no API request");
assert.ok(!forbiddenCode.includes("usersCardMarkup"), "forbidden page shows no account UI");

const adminPageCode = grab("renderAdminPage");
assert.ok(/if \(!state\.user\?\.is_admin\) \{[\s\S]*?return;/.test(adminPageCode),
  "renderAdminPage bails out before loading data for members");

assert.ok(src.includes("这个页面只有管理员能打开。"), "forbidden sentence present");
assert.ok(src.includes("还没有成员账号。"), "empty state copy");
assert.ok(src.includes("开账号、重置密码"), "card subtitle copy");
assert.ok(!src.includes("成员管理"), "app.js never says 成员管理");
assert.ok(!src.includes("settings-admin"), "the users card left the settings page");
assert.ok(!src.includes('<span class="users-badge">admin</span>'), "badge text is Chinese");

const formCode = grab("usersFormMarkup");
assert.ok(formCode.includes('for="users-username"'), "username label bound");
assert.ok(formCode.includes('id="users-username"'), "username input id present");
assert.ok(formCode.includes('for="users-password"'), "password label bound");
assert.ok(formCode.includes('id="users-password"'), "password input id present");

const listCode = grab("usersListMarkup");
assert.ok(listCode.includes("管理员"), "admin badge text");
assert.ok(listCode.includes("成员"), "member badge text");
assert.ok(listCode.includes("这是你"), "self annotation");
assert.ok(listCode.includes("<ul class=\"users-list\">"), "member list uses ul");
assert.ok(listCode.includes("<li class=\"users-item"), "member list uses li");

console.log("p45_admin_console: all assertions passed");
