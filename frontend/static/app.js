(() => {
  "use strict";

  const API_BASE = "/api";
  const TOKEN_KEY = "asset_tracker_token";
  const PERIODS = ["day", "week", "month", "year"];
  const PERIOD_LABELS = {
    day: "日",
    week: "周",
    month: "月",
    year: "年",
  };
  const PERIOD_TITLES = {
    day: "近一日",
    week: "近一周",
    month: "近一月",
    year: "近一年",
  };
  const PERIOD_DAYS = {
    day: 1,
    week: 7,
    month: 30,
    year: 365,
  };
  const ROUTES = new Set(["/", "/login", "/returns", "/pending", "/settings", "/lending", "/options"]);
  const ROUTE_ALIASES = { "/positions": "/lending" };

  // Full-page captures opt out of the fixed dock's 148px reserve so the
  // screenshot ends with the content instead of a band of empty black.
  const CAPTURE_MODE = (() => {
    try {
      return new URLSearchParams(window.location.search).get("capture") === "1";
    } catch {
      return false;
    }
  })();
  if (CAPTURE_MODE) document.documentElement.classList.add("is-capture");

  // Dev-only visual mock for the WeChat token/send banner.  Read once at boot
  // so the flag survives the login redirect (which drops the query string);
  // with no explicit mock params this is null and changes nothing.
  const WECHAT_BANNER_MOCK = (() => {
    let params = null;
    try {
      params = new URLSearchParams(window.location.search);
    } catch {
      return null;
    }
    const token = params.get("mock_wechat_stale");
    const send = params.get("mock_wechat_send_stale");
    const mock = {};
    if (token === "1") {
      mock.wechat_token_stale = true;
      mock.wechat_token_age_hours = 30;
    } else if (token === "0") {
      mock.wechat_token_stale = false;
      mock.wechat_token_age_hours = null;
    }
    if (send === "1") {
      mock.wechat_send_stale = true;
      mock.wechat_send_error = "unverified: ret=-2 errmsg=prepare failed";
      mock.wechat_send_since = "2026-10-29T05:10:42Z";
    } else if (send === "0") {
      mock.wechat_send_stale = false;
      mock.wechat_send_error = null;
      mock.wechat_send_since = null;
    }
    return Object.keys(mock).length ? mock : null;
  })();

  let wechatBannerRequest = null;

  const CATEGORY_ORDER = [
    "deposit",
    "defi_deposit",
    "defi_redeem",
    "swap",
    "withdraw",
    "transfer",
    "airdrop",
    "kol_reward",
    "trade",
    "funding",
    "other",
  ];
  const CATEGORY_LABELS = {
    deposit: "充值",
    defi_deposit: "协议存入",
    defi_redeem: "协议赎回",
    swap: "兑换",
    withdraw: "提现",
    transfer: "转账",
    airdrop: "空投",
    kol_reward: "KOL 奖励",
    trade: "交易",
    funding: "资金费",
    other: "其他收益",
  };
  // Category pills carry meaning through a restrained tint. Red/green stay
  // reserved for profit/loss and KOL rewards; deposits are teal, protocol
  // moves blue, withdrawals amber, airdrops violet, and so on.
  const CATEGORY_TONES = {
    deposit: "tone-deposit",
    defi_deposit: "tone-defi",
    defi_redeem: "tone-defi",
    swap: "tone-defi",
    withdraw: "tone-withdraw",
    transfer: "tone-transfer",
    airdrop: "tone-airdrop",
    kol_reward: "tone-kol",
    trade: "tone-trade",
    funding: "tone-neutral",
    other: "tone-neutral",
  };
  const PENDING_PAGE_SIZE = 30;

  const state = {
    token: localStorage.getItem(TOKEN_KEY) || "",
    user: null,
    period: "month",
    overviewSeries: null,
    overviewData: null,
    overviewMetrics: null,
    wechatBannerDismissTier: null,
    wechatBannerStaleSeen: false,
    wechatBannerTier: null,
    holdingsMode: "asset",
    holdingsOpenAssets: new Set(),
    holdingsOpenSources: new Set(),
    holdingsOpenMisc: new Set(),
    holdingsSourceSeeded: false,
    renderSeq: 0,
    pendingChoices: new Map(),
    pendingExpanded: new Set(),
    pendingVisible: PENDING_PAGE_SIZE,
    pendingMixed: false,
    pendingCount: null,
    notify: {
      open: false,
      root: null,
      trigger: null,
      previousFocus: null,
      timer: 0,
      pending: null,
      events: null,
      rules: null,
      positions: null,
      overview: null,
      reconcile: null,
      loading: false,
      error: "",
      lastLoadedAt: 0,
      loadPromise: null,
      seen: null,
      seenUser: null,
      skip: [],
      toast: null,
      toastSuppressed: false,
      confirming: new Set(),
    },
    moreSheet: null,
    moreSheetTimer: 0,
    confirmedOpen: false,
    categorySheet: null,
    pf: {
      structure: null,
      allItems: [],
      view: { mode: "overview", tab: "all", class: null, risk: null },
      viewItems: [],
      cache: new Map(),
      loading: false,
      error: null,
      itemsError: null,
      seq: 0,
      gen: 0,
      openMisc: new Set(),
      sheet: null,
    },
    syncBusy: false,
  };

  const app = document.getElementById("app");
  const moneyFormatter = new Intl.NumberFormat("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  // Unit prices (coin price, oracle/liq price, option mark) need sub-dollar
  // precision; USDT amounts keep the two-decimal moneyFormatter.
  const priceFormatters = new Map();
  function priceFormatter(digits) {
    if (!priceFormatters.has(digits)) {
      priceFormatters.set(
        digits,
        new Intl.NumberFormat("en-US", {
          minimumFractionDigits: digits,
          maximumFractionDigits: digits,
        }),
      );
    }
    return priceFormatters.get(digits);
  }
  const TOAST_VISIBLE_LIMIT = 3;
  const TOAST_LIFETIME = 4000;
  const TOAST_EXIT = 160;

  const ICONS = {
    logo: `
      <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <path d="M4.5 16.5 9.25 11.8l3.2 2.9 6.8-7"/>
        <path d="M15.25 7.7h4v4"/>
      </svg>`,
    overview: `
      <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <path d="M4 19V5"/>
        <path d="M4 19h16"/>
        <path d="m7 15 3.5-4 3 2.3L20 7"/>
      </svg>`,
    returns: `
      <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <path d="M4 16.5 9 11l3.4 3 6.6-7"/>
        <path d="M15 7h4v4"/>
      </svg>`,
    pending: `
      <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <rect x="4" y="4" width="16" height="16" rx="4.5"/>
        <path d="m8.4 12.1 2.2 2.2 5-5.1"/>
      </svg>`,
    lending: `
      <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <path d="M8 7H4.5L7.5 4"/>
        <path d="M4.5 7h11"/>
        <path d="M16 17h3.5L16.5 20"/>
        <path d="M19.5 17H8.5"/>
      </svg>`,
    options: `
      <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <path d="M6 4.5h9.5L19 8v11.5H6z"/>
        <path d="M15 4.5V8h4"/>
        <path d="M9 12h7M9 15.5h5"/>
      </svg>`,
    more: `
      <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <circle cx="5" cy="12" r="1.35" fill="currentColor" stroke="none"/>
        <circle cx="12" cy="12" r="1.35" fill="currentColor" stroke="none"/>
        <circle cx="19" cy="12" r="1.35" fill="currentColor" stroke="none"/>
      </svg>`,
    chevron: `
      <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <path d="m9 6 6 6-6 6"/>
      </svg>`,
    logout: `
      <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <path d="M14.5 8V6.8c0-1-.8-1.8-1.8-1.8H6.8C5.8 5 5 5.8 5 6.8v10.4c0 1 .8 1.8 1.8 1.8h5.9c1 0 1.8-.8 1.8-1.8V16"/>
        <path d="M11 12h9"/>
        <path d="m17 9 3 3-3 3"/>
      </svg>`,
    back: `
      <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <path d="M15 6 9 12l6 6"/>
      </svg>`,
    settings: `
      <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <ellipse cx="12" cy="6.5" rx="6.5" ry="2.8"/>
        <path d="M5.5 6.5v5.2c0 1.55 2.9 2.8 6.5 2.8s6.5-1.25 6.5-2.8V6.5"/>
        <path d="M5.5 11.7v5.3c0 1.55 2.9 2.8 6.5 2.8s6.5-1.25 6.5-2.8v-5.3"/>
      </svg>`,
    copy: `
      <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <rect x="8" y="8" width="11" height="11" rx="2"/>
        <path d="M5.5 15.5V6.8A1.8 1.8 0 0 1 7.3 5h8.7"/>
      </svg>`,
    wallet: `
      <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <rect x="3.5" y="6" width="17" height="12.5" rx="2.5"/>
        <path d="M3.5 10h17"/>
        <circle cx="16.2" cy="14.2" r="1.1"/>
      </svg>`,
    key: `
      <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <circle cx="8.5" cy="14.5" r="3.7"/>
        <path d="m11.2 11.8 7.1-7.1"/>
        <path d="m15.4 7.6 2.2 2.2"/>
        <path d="m17.6 5.4 1.8 1.8"/>
      </svg>`,
    bell: `
      <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <path d="M6.2 16.2h11.6"/>
        <path d="M7.4 16.2V11a4.6 4.6 0 0 1 9.2 0v5.2"/>
        <path d="M10.2 16.6a1.8 1.8 0 0 0 3.6 0"/>
      </svg>`,
    users: `
      <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <circle cx="9" cy="9" r="2.6"/>
        <path d="M4.8 17.2c.5-2.1 2.2-3.2 4.2-3.2s3.7 1.1 4.2 3.2"/>
        <circle cx="16.2" cy="9.4" r="2.1"/>
        <path d="M14.6 14.2c1.4-.3 2.7.2 3.5 1.4.5.8.8 1.6 1 2.2"/>
      </svg>`,
    eye: `
      <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <path d="M2.9 12S6.3 5.9 12 5.9 21.1 12 21.1 12 17.7 18.1 12 18.1 2.9 12 2.9 12Z"/>
        <circle cx="12" cy="12" r="2.7"/>
      </svg>`,
    arrow: `
      <svg class="submit-arrow" viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <path d="M5 12h14"/>
        <path d="m13 6 6 6-6 6"/>
      </svg>`,
    check: `
      <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <path d="m5 12.5 4.2 4.2L19 7"/>
      </svg>`,
    safety: `
      <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <path d="m4.5 12.4 4.6 4.6L19.5 6.6"/>
      </svg>`,
    warn: `
      <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <path d="M12 4.6 21 20H3L12 4.6Z"/>
        <path d="M12 10.2v4.4"/>
        <path d="M12 17.5h.01"/>
      </svg>`,
    empty: `
      <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <path d="m5 12.5 4.2 4.2L19 7"/>
        <circle cx="12" cy="12" r="9"/>
      </svg>`,
  };

  const LOGO = `
    <span class="brand-mark" aria-hidden="true">${ICONS.logo}</span>`;

  /* ---------- Utilities ---------- */

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

  // Display-only. Shorten wallet addresses and txids (0x + 12+ hex) to
  // first-6 / last-4. Already-shortened strings are not matched (the ellipsis
  // breaks the hex run), so mixed text can contain both. Call after escapeHtml
  // so the ellipsis is a literal character and no raw payload is interpolated
  // into HTML or attributes.
  function shortenHex(value) {
    const text = String(value ?? "");
    return text.replace(/0x[0-9a-fA-F]{12,}/g, (hex) => `${hex.slice(0, 6)}…${hex.slice(-4)}`);
  }

  function displayText(value) {
    return shortenHex(escapeHtml(value));
  }

  function asNumber(value) {
    const number = Number(value);
    return Number.isFinite(number) ? number : 0;
  }

  function money(value) {
    return moneyFormatter.format(asNumber(value));
  }

  // Price semantics only — never for USDT amounts. Values below 1 keep at
  // least four significant decimals (0.855 -> 0.8550), so rounding never turns
  // a sub-dollar liquidation price into 0.86. Values >= 1 keep money()'s 2dp.
  function formatPrice(value) {
    const number = asNumber(value);
    const abs = Math.abs(number);
    if (abs >= 1) return money(number);
    if (abs === 0) return money(0);
    const magnitude = Math.floor(Math.log10(abs));
    const digits = Math.min(12, Math.max(4, 3 - magnitude));
    return priceFormatter(digits).format(number);
  }

  function isDemoAccount() {
    const user = state.user;
    return user?.is_demo === true || user?.username === "demo";
  }

  // Page-level provenance mark. Neutral grey by design: demo/mock is a fact
  // about the data, not a warning. Empty slot collapses so real data leaves no
  // reserved gap.
  function pageDemoChipMarkup({ mock = false, mixed = false } = {}) {
    const demoAccount = isDemoAccount();
    if (!demoAccount && !mock && !mixed) return "";
    const label = mixed && !demoAccount && !mock ? "含示例数据" : "示例数据";
    return `<span class="page-demo-chip" title="本页含示例数据，仅供预览">${label}</span>`;
  }

  function pageDemoSlotMarkup() {
    return `<span class="page-demo-slot" data-page-demo-slot>${pageDemoChipMarkup()}</span>`;
  }

  function syncPageDemoSlot(options = false) {
    const flags =
      typeof options === "object" && options !== null ? options : { mock: options === true };
    document.querySelectorAll("[data-page-demo-slot]").forEach((slot) => {
      slot.innerHTML = pageDemoChipMarkup(flags);
    });
  }

  function signedMoney(value) {
    const number = asNumber(value);
    // Displayed to two decimals: anything that rounds to 0.00 is flat, never
    // a green/red "+0.00" or "-0.00".
    if (!Number.isFinite(number) || Math.abs(number) < 0.005) return money(0);
    return (number > 0 ? "+" : "-") + money(Math.abs(number));
  }

  // Wording for the excluded-flow line under the return amount. A coverage
  // step is newly counted assets, never a deposit; a mixed window keeps the
  // neutral inflow/outflow wording.
  function excludedFlowLabel(kind, amount) {
    const value = Number(amount);
    if (!Number.isFinite(value) || Math.abs(value) < 0.005) return "";
    if (kind === "coverage") return value > 0 ? "新计入资产" : "资产口径调整";
    if (kind === "cash") return value > 0 ? "净充值" : "净流出";
    return value > 0 ? "净流入" : "净流出";
  }

  function signedPercent(value, digits = 2) {
    const number = asNumber(value) * 100;
    if (!Number.isFinite(number) || Math.abs(number) < 0.005) return `${(0).toFixed(digits)}%`;
    return (number > 0 ? "+" : "") + `${number.toFixed(digits)}%`;
  }

  function percentMagnitude(value, digits = 2) {
    return `${(Math.abs(asNumber(value)) * 100).toFixed(digits)}%`;
  }

  function toneClass(value) {
    const number = asNumber(value);
    if (number > 0.0000001) return "is-positive";
    if (number < -0.0000001) return "is-negative";
    return "is-flat";
  }

  function dateObject(value) {
    if (value == null || value === "") return null;
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? null : date;
  }

  function formatDate(value) {
    const date = dateObject(value);
    if (!date) return "—";
    const month = String(date.getUTCMonth() + 1).padStart(2, "0");
    const day = String(date.getUTCDate()).padStart(2, "0");
    return `${date.getUTCFullYear()}.${month}.${day}`;
  }

  // Chart axis only. Asia/Shanghai calendar day; do not replace formatDate (UTC).
  function formatDateSH(value) {
    const date = dateObject(value);
    if (!date) return "—";
    const shifted = new Date(date.getTime() + 8 * 60 * 60 * 1000);
    const month = String(shifted.getUTCMonth() + 1).padStart(2, "0");
    const day = String(shifted.getUTCDate()).padStart(2, "0");
    return `${shifted.getUTCFullYear()}.${month}.${day}`;
  }

  function chartDateRangeLabel(start, end) {
    const a = formatDateSH(start);
    const b = formatDateSH(end);
    if (a === b) return a;
    return `${a} — ${b}`;
  }

  function formatShortDate(value) {
    const date = dateObject(value);
    if (!date) return "—";
    return `${date.getUTCMonth() + 1}月${date.getUTCDate()}日`;
  }

  // Shanghai calendar day for the returns view, matching its 00:00 baseline.
  function formatShortDateSH(value) {
    const date = dateObject(value);
    if (!date) return "—";
    const shifted = new Date(date.getTime() + 8 * 60 * 60 * 1000);
    return `${shifted.getUTCMonth() + 1}月${shifted.getUTCDate()}日`;
  }

  function formatDay(value) {
    const date = dateObject(value);
    if (!date) return "—";
    return `${date.getUTCFullYear()}年${date.getUTCMonth() + 1}月${date.getUTCDate()}日`;
  }

  function currentPath() {
    const path = window.location.pathname || "/";
    return path.length > 1 ? path.replace(/\/+$/, "") : path;
  }

  function normalizePath(path) {
    const base = String(path || "/").split("?")[0].split("#")[0] || "/";
    const trimmed = base.length > 1 ? base.replace(/\/+$/, "") : base;
    const route = ROUTE_ALIASES[trimmed] || trimmed;
    return ROUTES.has(route) ? route : "/";
  }

  function setTitle(title) {
    document.title = `${title} · 资产追踪`;
  }

  function clearSession() {
    localStorage.removeItem(TOKEN_KEY);
    state.token = "";
    state.user = null;
    state.overviewSeries = null;
    state.overviewData = null;
    state.overviewMetrics = null;
    state.wechatBannerDismissTier = null;
    state.wechatBannerStaleSeen = false;
    state.wechatBannerTier = null;
    state.holdingsMode = "asset";
    state.holdingsOpenAssets = new Set();
    state.holdingsOpenSources = new Set();
    state.holdingsOpenMisc = new Set();
    state.holdingsSourceSeeded = false;
    state.pendingChoices = new Map();
    state.pendingExpanded = new Set();
    state.pendingVisible = PENDING_PAGE_SIZE;
    state.confirmedOpen = false;
    closeCategorySheet({ restoreFocus: false });
    closeNotifyPanel({ restoreFocus: false, immediate: true });
    closeFlowToast({ animated: false });
    state.notify.pending = null;
    state.notify.events = null;
    state.notify.rules = null;
    state.notify.positions = null;
    state.notify.overview = null;
    state.notify.reconcile = null;
    state.notify.seen = null;
    state.notify.seenUser = null;
    state.notify.toastSuppressed = false;
  }

  /* ---------- API ---------- */

  async function api(path, options = {}) {
    const method = options.method || "GET";
    const headers = { Accept: "application/json", ...(options.headers || {}) };

    if (state.token) {
      headers.Authorization = `Bearer ${state.token}`;
    }

    let body;
    if (options.body !== undefined) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(options.body);
    }

    let response;
    try {
      response = await fetch(`${API_BASE}${path}`, { method, headers, body });
    } catch (cause) {
      const error = new Error("网络连接失败，请稍后重试");
      error.status = 0;
      error.cause = cause;
      throw error;
    }

    const text = await response.text();
    let data = null;
    if (text) {
      try {
        data = JSON.parse(text);
      } catch {
        data = null;
      }
    }

    if (!response.ok) {
      const detail =
        data && typeof data.detail === "string"
          ? data.detail
          : response.status === 401
            ? "登录已失效，请重新登录"
            : "请求失败，请稍后重试";
      const error = new Error(detail);
      error.status = response.status;
      error.data = data;
      throw error;
    }

    return data;
  }

  // Shared authenticated request helper for add-on modules (loop_card.js).
  if (typeof window !== "undefined") window.AssetTrackerApi = { request: api };

  function expireSession(message = "登录已过期，请重新登录") {
    clearSession();
    state.renderSeq += 1;
    if (currentPath() !== "/login") {
      window.history.replaceState(null, "", "/login");
    }
    renderLogin(message);
  }

  /* ---------- Markup: shell and shared ---------- */

  function topbarMarkup() {
    const username = state.user?.username || "未登录";
    const initial = username.slice(0, 1);
    return `
      <header class="topbar">
        <a class="brand" href="/" data-link aria-label="资产追踪，返回总览">
          ${LOGO}
          <span class="brand-name">资产追踪</span>
        </a>
        <div class="topbar-right">
          <div class="alert-slot notify-slot" data-alert-slot></div>
          <div class="user-menu">
            <button
              class="user-chip"
              type="button"
              data-action="toggle-user-menu"
              aria-haspopup="menu"
              aria-expanded="false"
              aria-label="打开账户菜单"
            >
              <span class="user-name">${escapeHtml(username)}</span>
              <span class="avatar" aria-hidden="true">${escapeHtml(initial)}</span>
            </button>
            <div class="user-popover" role="menu" hidden>
              <p class="user-popover-name">${escapeHtml(username)}</p>
              <a class="menu-item" href="/settings" role="menuitem" data-link>
                ${ICONS.settings}
                数据源设置
              </a>
              <button class="menu-logout" type="button" role="menuitem" data-action="logout">
                ${ICONS.logout}
                退出登录
              </button>
            </div>
          </div>
        </div>
      </header>`;
  }

  const RAIL_ITEMS = [
    ["overview", "/", "总览", ICONS.overview],
    ["returns", "/returns", "收益", ICONS.returns],
    ["lending", "/lending", "借贷", ICONS.lending],
    ["options", "/options", "期权", ICONS.options],
    ["settings", "/settings", "设置", ICONS.settings],
  ];

  // Bottom-bar entries live in one list so a tab can be replaced or removed
  // without rewriting the renderer. "more" opens the drawer rather than a route.
  const TAB_ITEMS = [
    { key: "overview", href: "/", label: "总览", icon: "overview" },
    { key: "returns", href: "/returns", label: "收益", icon: "returns" },
    { key: "lending", href: "/lending", label: "借贷", icon: "lending" },
    { key: "options", href: "/options", label: "期权", icon: "options" },
    { key: "more", label: "更多", icon: "more", action: "open-more" },
  ];

  function sideRailMarkup(active) {
    return `
      <aside class="side-rail" aria-label="侧边栏">
        <a class="rail-brand" href="/" data-link aria-label="资产追踪，返回总览">
          <span class="rail-brand-mark">${LOGO}</span>
          <span class="rail-brand-name">资产追踪</span>
        </a>
        <nav class="rail-nav" aria-label="主导航">
          ${RAIL_ITEMS.map(([key, href, label, icon]) => {
            return `
            <a class="rail-link${active === key ? " is-active" : ""}" href="${href}" data-link ${
              active === key ? 'aria-current="page"' : ""
            }>
              ${icon}
              <span>${label}</span>
            </a>`;
          }).join("")}
        </nav>
        <div class="rail-foot">
          <p class="rail-status-label">同步状态</p>
          <p class="rail-status-note" data-rail-sync-note>只读 · 手动同步</p>
          <button class="sync-btn sync-btn--quiet rail-sync" type="button" data-sync-run
            aria-label="${state.syncBusy ? "同步中" : "同步数据"}"
            ${state.syncBusy ? 'disabled aria-busy="true"' : ""}>
            <svg class="rail-sync-icon" viewBox="0 0 16 16" fill="none" aria-hidden="true">
              <path d="M13.2 8a5.2 5.2 0 0 1-8.7 3.8"/>
              <path d="M2.8 8a5.2 5.2 0 0 1 8.7-3.8"/>
              <path d="M10.6 3.2h1.6V1.6"/>
              <path d="M5.4 12.8H3.8v1.6"/>
            </svg>
            <span class="sync-spin" aria-hidden="true"></span>
            <span data-sync-label>${state.syncBusy ? "同步中…" : "同步"}</span>
          </button>
        </div>
      </aside>`;
  }

  function updateRailStatus() {
    const note = document.querySelector("[data-rail-sync-note]");
    if (!note) return;
    const stamp = state.overviewData?.latest_snapshot_ts || state.overviewData?.holdings_asof;
    note.textContent = stamp ? `快照 ${formatDay(stamp)}` : "只读 · 手动同步";
  }

  function tabbarMarkup(active) {
    const moreActive = active === "settings";

    return `
      <div class="tabbar-dock">
        <nav class="tabbar" aria-label="主导航">
          ${TAB_ITEMS.map((item) => {
            const isActive = item.action ? moreActive : active === item.key;
            const classes = `tab${item.action ? " tab--more" : ""}${isActive ? " is-active" : ""}`;
            const icon = ICONS[item.icon] || "";
            if (item.action) {
              return `
              <button
                class="${classes}"
                type="button"
                data-action="${item.action}"
                aria-haspopup="dialog"
                aria-expanded="${state.moreSheet ? "true" : "false"}"
              >
                ${icon}
                <span>${item.label}</span>
              </button>`;
            }
            return `
              <a class="${classes}" href="${item.href}" data-link ${
                isActive ? 'aria-current="page"' : ""
              }>
                ${icon}
                <span>${item.label}</span>
              </a>`;
          }).join("")}
        </nav>
      </div>`;
  }

  function stopSettingsLayout() {
    if (typeof settingsView !== "undefined" && settingsView.cleanup) settingsView.cleanup();
  }

  function withWechatBannerSlot(content) {
    const slot = '<div class="wechat-banner-slot" data-wechat-banner-slot></div>';
    const index = content.indexOf("</header>");
    if (index < 0) return `${slot}${content}`;
    const cut = index + "</header>".length;
    return `${content.slice(0, cut)}${slot}${content.slice(cut)}`;
  }

  function renderShell(content, active) {
    stopSettingsLayout();
    app.classList.remove("app--auth");
    app.innerHTML = `${sideRailMarkup(active)}<div class="page page--${active}">${withWechatBannerSlot(
      content,
    )}</div>${tabbarMarkup(active)}`;
    updateRailStatus();
    updatePendingBadges();
    updateWechatBanner();
    updateTopbarAlert();
    refreshNotifySnapshot();
    if (active !== "overview" && !state.overviewData) refreshWechatBanner();
  }

  function pageHead(kicker, title, note = "") {
    return `
      <header class="page-head">
        <div>
          ${kicker ? `<span class="page-kicker">${escapeHtml(kicker)}</span>` : ""}
          <div class="page-title-row">
            <h1 class="page-title">${escapeHtml(title)}</h1>
            ${pageDemoSlotMarkup()}
          </div>
        </div>
        ${note ? `<p class="page-note">${escapeHtml(note)}</p>` : ""}
      </header>`;
  }

  function segmentedMarkup() {
    return `
      <div class="returns-seg segmented" role="group" aria-label="选择收益周期">
        ${PERIODS.map(
          (period) => `
          <button
            class="segment"
            type="button"
            data-period="${period}"
            aria-pressed="${state.period === period}"
          >${PERIOD_LABELS[period]}</button>`,
        ).join("")}
      </div>`;
  }

  function retryMarkup(kind) {
    return `
      <div class="state-card">
        <span class="state-mark" aria-hidden="true">!</span>
        <h2>暂时读不到数据</h2>
        <p>连接可能短暂中断。你的数据没有丢失，稍后再试一次。</p>
        <button class="retry-btn" type="button" data-retry="${kind}">重新加载</button>
      </div>`;
  }

  function emptyPendingMarkup() {
    return `
      <div class="cleared-state">
        <span class="cleared-badge" aria-hidden="true">${ICONS.check}</span>
        <p class="cleared-kicker">待确认</p>
        <h2>这一页已经清零</h2>
        <p>没有待核对的事项。新的资金变动与对账提醒，会再安静地出现在这里。</p>
        <a class="cleared-link" href="/returns" data-link>去看收益表现</a>
      </div>`;
  }

  function toastStackDepth(index, total) {
    return Math.max(0, Math.min(2, total - 1 - index));
  }

  function toastVelocity(samples, axis = "y") {
    if (!samples || samples.length < 2) return 0;
    const first = samples[0];
    const last = samples[samples.length - 1];
    const elapsed = last.t - first.t;
    if (!(elapsed > 0)) return 0;
    return ((last[axis] - first[axis]) / elapsed) * 1000;
  }

  // Sonner-style swipe: phones dismiss upwards, desktop dismisses to the right.
  // Either gesture clears the 40px / 500px/s threshold.
  function shouldDismissToast(dx, dy, velocityX = 0, velocityY = 0) {
    return dx > 40 || dy < -40 || velocityX > 500 || velocityY < -500;
  }

  function toastIconMarkup(tone) {
    const glyph =
      tone === "error"
        ? '<path d="m8 8 8 8M16 8l-8 8"/>'
        : tone === "info"
          ? '<circle cx="12" cy="12" r="2.4" fill="currentColor" stroke="none"/>'
          : '<path d="m7.5 12.3 3 3 6-6.2"/>';
    return `<svg class="toast-icon" viewBox="0 0 24 24" fill="none" aria-hidden="true">${glyph}</svg>`;
  }

  function ensureToastStack() {
    let stack = document.querySelector(".toast-stack");
    if (!stack) {
      stack = document.createElement("div");
      stack.className = "toast-stack";
      document.body.appendChild(stack);
    }
    return stack;
  }

  function syncToastStack(stack = document.querySelector(".toast-stack")) {
    if (!stack) return;
    const toasts = [...stack.querySelectorAll(".toast:not(.is-leaving)")];
    toasts.forEach((toast, index) => {
      const depth = toastStackDepth(index, toasts.length);
      toast.dataset.depth = String(depth);
      toast.style.zIndex = String(10 - depth);
    });
  }

  function dismissToast(toast, { swipeX = 0, swipeY = 0 } = {}) {
    if (!toast || toast.dataset.closing === "1") return;
    toast.dataset.closing = "1";
    window.clearTimeout(toast._toastTimer);
    toast.classList.remove("is-visible", "is-dragging");
    toast.style.setProperty("--drag-x", `${swipeX}px`);
    toast.style.setProperty("--drag-y", `${swipeY}px`);
    toast.classList.add("is-leaving");
    window.setTimeout(() => {
      toast.remove();
      const stack = document.querySelector(".toast-stack");
      if (stack && !stack.querySelector(".toast")) stack.remove();
      else syncToastStack(stack);
    }, TOAST_EXIT);
  }

  function startToastTimer(toast) {
    window.clearTimeout(toast._toastTimer);
    if (toast.dataset.closing === "1") return;
    toast._toastTimer = window.setTimeout(() => dismissToast(toast), TOAST_LIFETIME);
  }

  function bindToastSwipe(toast) {
    let drag = null;
    const stack = () => document.querySelector(".toast-stack");
    toast.addEventListener("pointerenter", () => {
      window.clearTimeout(toast._toastTimer);
      stack()?.classList.add("is-expanded");
    });
    toast.addEventListener("pointerleave", () => {
      if (!drag) {
        stack()?.classList.remove("is-expanded");
        startToastTimer(toast);
      }
    });
    toast.addEventListener("pointerdown", (event) => {
      if (event.button !== 0 || toast.dataset.closing === "1") return;
      window.clearTimeout(toast._toastTimer);
      drag = {
        startX: event.clientX,
        startY: event.clientY,
        samples: [{ x: event.clientX, y: event.clientY, t: window.performance.now() }],
        dx: 0,
        dy: 0,
      };
      stack()?.classList.add("is-expanded");
      toast.classList.add("is-dragging");
      toast.setPointerCapture?.(event.pointerId);
    });
    toast.addEventListener("pointermove", (event) => {
      if (!drag) return;
      const now = window.performance.now();
      const dx = Math.max(0, event.clientX - drag.startX);
      const dy = Math.min(0, event.clientY - drag.startY);
      drag.dx = dx;
      drag.dy = dy;
      drag.samples.push({ x: event.clientX, y: event.clientY, t: now });
      drag.samples = drag.samples.filter((sample) => now - sample.t <= 100);
      toast.style.setProperty("--drag-x", `${dx}px`);
      toast.style.setProperty("--drag-y", `${dy}px`);
      if (dx > 4 || dy < -4) event.preventDefault();
    });
    const endDrag = () => {
      if (!drag) return;
      const { dx, dy, samples } = drag;
      const velocityX = toastVelocity(samples, "x");
      const velocityY = toastVelocity(samples, "y");
      drag = null;
      toast.classList.remove("is-dragging");
      if (shouldDismissToast(dx, dy, velocityX, velocityY)) {
        dismissToast(toast, { swipeX: dx, swipeY: dy });
        return;
      }
      toast.style.removeProperty("--drag-x");
      toast.style.removeProperty("--drag-y");
      window.setTimeout(() => stack()?.classList.remove("is-expanded"), 500);
      startToastTimer(toast);
    };
    toast.addEventListener("pointerup", endDrag);
    toast.addEventListener("pointercancel", endDrag);
    toast.addEventListener("lostpointercapture", endDrag);
  }

  function showToast(message, tone = "success") {
    const stack = ensureToastStack();
    const safeTone = tone === "error" ? "error" : tone === "info" ? "info" : "success";
    const toast = document.createElement("div");
    toast.className = `toast toast--${safeTone}`;
    toast.setAttribute("role", safeTone === "error" ? "alert" : "status");
    toast.setAttribute("aria-live", safeTone === "error" ? "assertive" : "polite");
    toast.innerHTML = `${toastIconMarkup(safeTone)}<span class="toast-text"></span>`;
    toast.querySelector(".toast-text").textContent = message;
    stack.appendChild(toast);
    const all = [...stack.querySelectorAll(".toast")];
    if (all.length > TOAST_VISIBLE_LIMIT) {
      all.slice(0, all.length - TOAST_VISIBLE_LIMIT).forEach((element) => {
        window.clearTimeout(element._toastTimer);
        element.remove();
      });
    }
    syncToastStack(stack);
    bindToastSwipe(toast);
    window.requestAnimationFrame(() => toast.classList.add("is-visible"));
    startToastTimer(toast);
  }

  /* ---------- Overview ---------- */

  function overviewSkeleton() {
    return `
      <div class="skeleton-hero" aria-hidden="true">
        <div class="skeleton sk-eyebrow"></div>
        <div class="skeleton sk-number"></div>
        <div class="skeleton sk-pill"></div>
      </div>
      <div class="skeleton sk-card" aria-hidden="true"></div>`;
  }

  function deltaMarkup(rate) {
    if (!Number.isFinite(rate)) {
      return `<span class="delta delta--flat">· 暂无前值</span>`;
    }

    // |x| < 0.005% is a flat print, not a gain or a loss.
    if (rate >= 0.00005) {
      return `<span class="delta delta--up">↑ ${percentMagnitude(rate)}</span>`;
    }
    if (rate <= -0.00005) {
      return `<span class="delta delta--down">↓ ${percentMagnitude(rate)}</span>`;
    }
    return `<span class="delta delta--flat">0.00%</span>`;
  }

  const TRACKING_EMPTY = "已从 10 月 23 日开始追踪，曲线会随每日快照逐步生成";

  function chartBadge(navSource) {
    if (navSource === "live") return "USDT · 实时估值";
    if (navSource === "sync") return "USDT · 同步估值";
    if (navSource === "demo" || navSource === "fixture") return "USDT · 示例数据";
    if (navSource === "seed") return "USDT · 模拟数据";
    if (navSource === "backfill") return "USDT · 历史回填";
    return "USDT";
  }

  // Y domain from post-sync values only. Minimum span is 4% of the last
  // value so normal noise is not blown up; centred on the data midpoint,
  // with 8% headroom above and below.
  function chartYExtent(navs) {
    const lo = Math.min(...navs);
    const hi = Math.max(...navs);
    const last = navs[navs.length - 1];
    let span = Math.max(hi - lo, Math.abs(last) * 0.04);
    if (!(span > 0)) span = 1;
    const mid = (hi + lo) / 2;
    const min = mid - span / 2 - span * 0.08;
    const max = mid + span / 2 + span * 0.08;
    return { min, max, range: max - min };
  }

  function chartMarkup(series, options) {
    const tracking = Boolean(options && options.tracking);
    if (series.length < 3) {
      const message = tracking
        ? TRACKING_EMPTY
        : series.length
          ? "再积累一个估值点，曲线就会出现"
          : "还没有净值快照";
      return `<div class="chart-empty">${message}</div>`;
    }

    const width = 440;
    const height = 176;
    const left = 7;
    const right = 433;
    const top = 15;
    const bottom = 158;
    const values = series.map((point) => asNumber(point.nav_usdt));

    // Largest adjacent NAV jump of at least 30%. Data stays raw; scale starts at the jump point.
    let jumpIndex = -1;
    let jumpRatio = 0;
    for (let i = 1; i < values.length; i += 1) {
      const prev = values[i - 1];
      const next = values[i];
      const base = Math.abs(prev);
      if (base < 1e-9) continue;
      const ratio = Math.abs(next - prev) / base;
      if (ratio >= 0.3 && ratio > jumpRatio) {
        jumpRatio = ratio;
        jumpIndex = i;
      }
    }
    const scaled = jumpIndex > 0 ? values.slice(jumpIndex) : values;
    const { min, max, range } = chartYExtent(scaled);
    // Pre-sync (dashed) values do not drive the scale; pin them to the floor.
    const plotValue = (value, index) => (jumpIndex > 0 && index < jumpIndex ? min : value);
    const xFor = (index) => left + (index / (series.length - 1)) * (right - left);
    const yRaw = (value) => top + ((max - value) / range) * (bottom - top);
    const clampY = (y) => Math.min(bottom, Math.max(top, y));

    let jumpMarkup = "";
    if (jumpIndex > 0) {
      const x = xFor(jumpIndex);
      const label = "完整同步";
      const labelWidth = 49;
      const labelPad = 2;
      const labelY = top + 14;
      const flip = x + 6 + labelWidth + labelPad > width - 4;
      const textX = flip ? x - 6 : x + 6;
      const anchor = flip ? "end" : "start";
      const bgX = flip ? textX - labelWidth - labelPad : textX - labelPad;
      const bgY = labelY - 12;
      jumpMarkup = `
        <line class="chart-jump" x1="${x.toFixed(2)}" y1="${top}" x2="${x.toFixed(2)}" y2="${bottom}"/>
        <rect class="chart-jump-label-bg" x="${bgX.toFixed(2)}" y="${bgY.toFixed(2)}" width="${(labelWidth + labelPad * 2).toFixed(2)}" height="16"/>
        <text class="chart-jump-label" x="${textX.toFixed(2)}" y="${labelY.toFixed(2)}" text-anchor="${anchor}">${label}</text>`;
    }

    const points = values.map((value, index) => [xFor(index), clampY(yRaw(plotValue(value, index)))]);
    const hoverPoints = series.map((point, index) => ({
      x: Number(points[index][0].toFixed(2)),
      y: Number(points[index][1].toFixed(2)),
      ts: point.ts,
      v: values[index],
    }));
    const pathOf = (slice) =>
      slice
        .map(([x, y], index) => `${index === 0 ? "M" : "L"}${x.toFixed(2)} ${y.toFixed(2)}`)
        .join(" ");
    const solidPoints = jumpIndex > 0 ? points.slice(jumpIndex) : points;
    const dashedPoints = jumpIndex > 0 ? points.slice(0, jumpIndex + 1) : [];
    const linePath = pathOf(solidPoints);
    const dashedPath = dashedPoints.length > 1 ? pathOf(dashedPoints) : "";
    const solidLeft = solidPoints[0][0];
    const solidRight = solidPoints[solidPoints.length - 1][0];
    const areaPath = `${linePath} L${solidRight.toFixed(2)} ${bottom + 2} L${solidLeft.toFixed(
      2,
    )} ${bottom + 2} Z`;
    const lastPoint = points[points.length - 1];
    const first = series[0];
    const last = series[series.length - 1];
    const dateSpan = chartDateRangeLabel(first.ts, last.ts);
    const ariaScale =
      jumpIndex > 0 ? "，早期同步不完整，纵轴按之后区间缩放" : "";
    const ariaLabel = `净值走势，${dateSpan}，从 ${money(values[0])} USDT 到 ${money(values[values.length - 1])} USDT${ariaScale}`;

    const gridLines = [0.25, 0.5, 0.75]
      .map((ratio) => {
        const y = top + ratio * (bottom - top);
        return `<line class="chart-grid" x1="${left}" y1="${y.toFixed(1)}" x2="${right}" y2="${y.toFixed(1)}"/>`;
      })
      .join("");

    return `
      <svg class="chart-svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="${escapeHtml(
        ariaLabel,
      )}" data-chart-points="${escapeHtml(JSON.stringify(hoverPoints))}">
        ${gridLines}
        <path class="chart-area" d="${areaPath}" fill="#D4A574" fill-opacity="0.10"/>
        ${dashedPath ? `<path class="chart-line chart-line--early" d="${dashedPath}"/>` : ""}
        <path class="chart-line chart-line--reveal" d="${linePath}"/>
        ${jumpMarkup}
        <circle class="chart-dot-halo" cx="${lastPoint[0].toFixed(2)}" cy="${lastPoint[1].toFixed(
          2,
        )}" r="8"/>
        <circle class="chart-dot" cx="${lastPoint[0].toFixed(2)}" cy="${lastPoint[1].toFixed(
          2,
        )}" r="4"/>
        <g class="chart-guide" aria-hidden="true">
          <line class="chart-guide-line" x1="${lastPoint[0].toFixed(2)}" y1="${top}" x2="${lastPoint[0].toFixed(
            2,
          )}" y2="${bottom}"/>
          <circle class="chart-guide-dot" cx="${lastPoint[0].toFixed(2)}" cy="${lastPoint[1].toFixed(
            2,
          )}" r="4"/>
        </g>
      </svg>
      <div class="chart-foot">
        <span class="chart-foot-range">${dateSpan}</span>
        <span class="chart-foot-count">${series.length} 个估值点</span>
      </div>`;
  }

  function hideChartGuides() {
    document.querySelectorAll(".chart-card.is-reading").forEach((card) => {
      const range = card.querySelector(".chart-foot-range");
      const count = card.querySelector(".chart-foot-count");
      if (range && card.dataset.chartDefaultRange) range.textContent = card.dataset.chartDefaultRange;
      if (count && card.dataset.chartDefaultCount) count.textContent = card.dataset.chartDefaultCount;
      card.classList.remove("is-reading");
    });
    document.querySelectorAll(".chart-guide.is-active").forEach((guide) => {
      guide.classList.remove("is-active");
    });
  }

  function updateChartGuide(event) {
    const svg = event.target.closest ? event.target.closest(".chart-svg") : null;
    if (!svg) {
      hideChartGuides();
      return;
    }

    let points = [];
    try {
      points = JSON.parse(svg.getAttribute("data-chart-points") || "[]");
    } catch {
      return;
    }
    if (!Array.isArray(points) || points.length === 0) return;

    const rect = svg.getBoundingClientRect();
    if (!rect.width) return;
    const chartX = ((event.clientX - rect.left) / rect.width) * 440;
    let best = points[0];
    let bestDistance = Math.abs(points[0].x - chartX);
    for (let i = 1; i < points.length; i += 1) {
      const distance = Math.abs(points[i].x - chartX);
      if (distance < bestDistance) {
        best = points[i];
        bestDistance = distance;
      }
    }

    const guide = svg.querySelector(".chart-guide");
    const line = svg.querySelector(".chart-guide-line");
    const dot = svg.querySelector(".chart-guide-dot");
    if (!guide || !line || !dot) return;
    const x = Number(best.x).toFixed(2);
    guide.classList.add("is-active");
    line.setAttribute("x1", x);
    line.setAttribute("x2", x);
    dot.setAttribute("cx", x);
    dot.setAttribute("cy", Number(best.y).toFixed(2));

    const card = svg.closest(".chart-card");
    if (!card) return;
    const range = card.querySelector(".chart-foot-range");
    const count = card.querySelector(".chart-foot-count");
    if (!range || !count) return;
    if (!card.dataset.chartDefaultRange) {
      card.dataset.chartDefaultRange = range.textContent;
      card.dataset.chartDefaultCount = count.textContent;
    }
    range.textContent = formatDateSH(best.ts);
    count.textContent = `${money(best.v)} USDT`;
    card.classList.add("is-reading");
  }

  // Front-end-only switch: fixture points (sample data) and partial points
  // (half snapshots flagged by the API at read time) are not account history.
  // Add ?show_fixtures=1 to keep them while debugging. No backend field is added.
  const INCLUDE_FIXTURE_POINTS = (() => {
    try {
      return new URLSearchParams(window.location.search).get("show_fixtures") === "1";
    } catch {
      return false;
    }
  })();

  function normalizeOverviewSeries(data) {
    return (Array.isArray(data?.series) ? data.series : [])
      .map((point) => ({
        ts: point?.ts,
        nav_usdt: asNumber(point?.nav_usdt),
        provenance: point?.provenance ?? null,
        live: point?.live ?? null,
        partial: point?.partial === true,
      }))
      .filter((point) => dateObject(point.ts))
      .filter((point) => INCLUDE_FIXTURE_POINTS || (point.provenance !== "fixture" && !point.partial));
  }


  const SOURCE_LABELS = {
    binance: "币安",
    gate: "Gate",
    bitget: "Bitget",
    bybit: "Bybit",
    kraken: "Kraken",
    coinstats: "CoinStats",
    alchemy: "Alchemy",
    zerion: "EVM",
    helius: "Solana",
    evm: "EVM",
    ethereum: "Ethereum",
    solana: "Solana",
  };

  function sourceLabel(source) {
    const key = String(source || "").toLowerCase();
    if (SOURCE_LABELS[key]) return SOURCE_LABELS[key];
    const raw = String(source || "—");
    return raw ? raw.charAt(0).toUpperCase() + raw.slice(1) : "—";
  }

  function assetLabel(asset) {
    const raw = String(asset || "—");
    if (/^[a-z0-9._\-]+$/i.test(raw) && raw.length <= 16) return raw.toUpperCase();
    return raw;
  }

  // CSS ellipsis (U+2026 via text-overflow) handles visual clipping.
  // JS middle-ellipsis is only for strings that must stay one line with both ends.
  const ELLIPSIS = "\u2026";

  function middleEllipsis(value, head = 9, tail = 7) {
    const text = String(value ?? "");
    if (text.length <= head + tail + 1) return text;
    return `${text.slice(0, head)}${ELLIPSIS}${text.slice(-tail)}`;
  }

  // Lending legs: two visual lines, or a middle ellipsis when one line would clip the date.
  function legDisplayName(symbol) {
    const text = String(symbol || "—");
    if (text.length <= 18) return text;
    return middleEllipsis(text, 8, 6);
  }

  const NAME_DATE_RE =
    /(\d{1,2}[A-Za-z]{3}\d{2,4}|\d{4}[-./]\d{1,2}[-./]\d{1,2}|\d{4}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01]))/;

  // Lending card subtitle for PT-style assets: keep the expiry date visible
  // even if the symbol has to be shortened (PT-USD3-17DEC2026 -> PT-USD3…17DEC2026).
  function collateralHeadline(value) {
    const text = String(value || "").trim();
    if (!text) return "";
    const date = nameDateHint(text);
    if (!date || text.length <= 24) return text;
    const head = text.slice(0, text.indexOf(date)).replace(/[-_/\s]+$/, "");
    return `${head.slice(0, 10)}${ELLIPSIS}${date}`;
  }

  function nameDateHint(value) {
    const text = String(value || "");
    const matched = text.match(NAME_DATE_RE);
    return matched ? matched[1] : "";
  }

  function qtyFmt(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return "—";
    const abs = Math.abs(number);
    let digits = 2;
    if (abs > 0 && abs < 1) digits = 6;
    else if (abs < 100) digits = 4;
    return number.toLocaleString("en-US", {
      maximumFractionDigits: digits,
      minimumFractionDigits: 0,
    });
  }

  function isDustHolding(row) {
    return Boolean(row?.priced) && asNumber(row?.value_usdt) < 0.01;
  }

  const MISC_THRESHOLD_FALLBACK = 50;

  function miscThreshold(data) {
    const raw = asNumber(data?.misc_threshold_usdt);
    return Number.isFinite(raw) && raw > 0 ? raw : MISC_THRESHOLD_FALLBACK;
  }

  function isMiscRow(row, threshold) {
    if (!row?.priced) return true;
    return asNumber(row?.value_usdt) < threshold;
  }

  function miscSort(a, b) {
    const aPriced = Boolean(a?.priced);
    const bPriced = Boolean(b?.priced);
    if (aPriced !== bPriced) return aPriced ? -1 : 1;
    return asNumber(b?.value_usdt) - asNumber(a?.value_usdt);
  }

  function valueCell(row) {
    if (!row?.priced) {
      return `<span class="hold-value hold-value--muted">未定价</span>`;
    }
    return `<span class="hold-value">${money(row.value_usdt)}</span>`;
  }

  const TW_CHAIN_MAP = {
    ethereum: "ethereum",
    bsc: "smartchain",
    arbitrum: "arbitrum",
    optimism: "optimism",
    polygon: "polygon",
    base: "base",
    avalanche: "avalanchec",
    solana: "solana",
  };

  // Compact keccak256 for EIP-55 only (no npm). Little-endian 32-bit lanes,
  // permutation from the public-domain js-sha3 Keccak-f[1600].
  const KECCAK_RC = [
    1, 0, 32898, 0, 32906, 2147483648, 2147516416, 2147483648, 32907, 0, 2147483649, 0,
    2147516545, 2147483648, 32777, 2147483648, 138, 0, 136, 0, 2147516425, 0, 2147483658, 0,
    2147516555, 0, 139, 2147483648, 32905, 2147483648, 32771, 2147483648, 32770, 2147483648,
    128, 2147483648, 32778, 0, 2147483658, 2147483648, 2147516545, 2147483648, 32896, 2147483648,
    2147483649, 0, 2147516424, 2147483648,
  ];

  function keccakF(s) {
    var h, l, n, c0, c1, c2, c3, c4, c5, c6, c7, c8, c9,
      b0, b1, b2, b3, b4, b5, b6, b7, b8, b9, b10, b11, b12, b13, b14, b15, b16, b17,
      b18, b19, b20, b21, b22, b23, b24, b25, b26, b27, b28, b29, b30, b31, b32, b33,
      b34, b35, b36, b37, b38, b39, b40, b41, b42, b43, b44, b45, b46, b47, b48, b49;
    for (n = 0; n < 48; n += 2) {
      c0 = s[0] ^ s[10] ^ s[20] ^ s[30] ^ s[40];
      c1 = s[1] ^ s[11] ^ s[21] ^ s[31] ^ s[41];
      c2 = s[2] ^ s[12] ^ s[22] ^ s[32] ^ s[42];
      c3 = s[3] ^ s[13] ^ s[23] ^ s[33] ^ s[43];
      c4 = s[4] ^ s[14] ^ s[24] ^ s[34] ^ s[44];
      c5 = s[5] ^ s[15] ^ s[25] ^ s[35] ^ s[45];
      c6 = s[6] ^ s[16] ^ s[26] ^ s[36] ^ s[46];
      c7 = s[7] ^ s[17] ^ s[27] ^ s[37] ^ s[47];
      c8 = s[8] ^ s[18] ^ s[28] ^ s[38] ^ s[48];
      c9 = s[9] ^ s[19] ^ s[29] ^ s[39] ^ s[49];

      h = c8 ^ ((c2 << 1) | (c3 >>> 31));
      l = c9 ^ ((c3 << 1) | (c2 >>> 31));
      s[0] ^= h;
      s[1] ^= l;
      s[10] ^= h;
      s[11] ^= l;
      s[20] ^= h;
      s[21] ^= l;
      s[30] ^= h;
      s[31] ^= l;
      s[40] ^= h;
      s[41] ^= l;
      h = c0 ^ ((c4 << 1) | (c5 >>> 31));
      l = c1 ^ ((c5 << 1) | (c4 >>> 31));
      s[2] ^= h;
      s[3] ^= l;
      s[12] ^= h;
      s[13] ^= l;
      s[22] ^= h;
      s[23] ^= l;
      s[32] ^= h;
      s[33] ^= l;
      s[42] ^= h;
      s[43] ^= l;
      h = c2 ^ ((c6 << 1) | (c7 >>> 31));
      l = c3 ^ ((c7 << 1) | (c6 >>> 31));
      s[4] ^= h;
      s[5] ^= l;
      s[14] ^= h;
      s[15] ^= l;
      s[24] ^= h;
      s[25] ^= l;
      s[34] ^= h;
      s[35] ^= l;
      s[44] ^= h;
      s[45] ^= l;
      h = c4 ^ ((c8 << 1) | (c9 >>> 31));
      l = c5 ^ ((c9 << 1) | (c8 >>> 31));
      s[6] ^= h;
      s[7] ^= l;
      s[16] ^= h;
      s[17] ^= l;
      s[26] ^= h;
      s[27] ^= l;
      s[36] ^= h;
      s[37] ^= l;
      s[46] ^= h;
      s[47] ^= l;
      h = c6 ^ ((c0 << 1) | (c1 >>> 31));
      l = c7 ^ ((c1 << 1) | (c0 >>> 31));
      s[8] ^= h;
      s[9] ^= l;
      s[18] ^= h;
      s[19] ^= l;
      s[28] ^= h;
      s[29] ^= l;
      s[38] ^= h;
      s[39] ^= l;
      s[48] ^= h;
      s[49] ^= l;

      b0 = s[0];
      b1 = s[1];
      b32 = (s[11] << 4) | (s[10] >>> 28);
      b33 = (s[10] << 4) | (s[11] >>> 28);
      b14 = (s[20] << 3) | (s[21] >>> 29);
      b15 = (s[21] << 3) | (s[20] >>> 29);
      b46 = (s[31] << 9) | (s[30] >>> 23);
      b47 = (s[30] << 9) | (s[31] >>> 23);
      b28 = (s[40] << 18) | (s[41] >>> 14);
      b29 = (s[41] << 18) | (s[40] >>> 14);
      b20 = (s[2] << 1) | (s[3] >>> 31);
      b21 = (s[3] << 1) | (s[2] >>> 31);
      b2 = (s[13] << 12) | (s[12] >>> 20);
      b3 = (s[12] << 12) | (s[13] >>> 20);
      b34 = (s[22] << 10) | (s[23] >>> 22);
      b35 = (s[23] << 10) | (s[22] >>> 22);
      b16 = (s[33] << 13) | (s[32] >>> 19);
      b17 = (s[32] << 13) | (s[33] >>> 19);
      b48 = (s[42] << 2) | (s[43] >>> 30);
      b49 = (s[43] << 2) | (s[42] >>> 30);
      b40 = (s[5] << 30) | (s[4] >>> 2);
      b41 = (s[4] << 30) | (s[5] >>> 2);
      b22 = (s[14] << 6) | (s[15] >>> 26);
      b23 = (s[15] << 6) | (s[14] >>> 26);
      b4 = (s[25] << 11) | (s[24] >>> 21);
      b5 = (s[24] << 11) | (s[25] >>> 21);
      b36 = (s[34] << 15) | (s[35] >>> 17);
      b37 = (s[35] << 15) | (s[34] >>> 17);
      b18 = (s[45] << 29) | (s[44] >>> 3);
      b19 = (s[44] << 29) | (s[45] >>> 3);
      b10 = (s[6] << 28) | (s[7] >>> 4);
      b11 = (s[7] << 28) | (s[6] >>> 4);
      b42 = (s[17] << 23) | (s[16] >>> 9);
      b43 = (s[16] << 23) | (s[17] >>> 9);
      b24 = (s[26] << 25) | (s[27] >>> 7);
      b25 = (s[27] << 25) | (s[26] >>> 7);
      b6 = (s[36] << 21) | (s[37] >>> 11);
      b7 = (s[37] << 21) | (s[36] >>> 11);
      b38 = (s[47] << 24) | (s[46] >>> 8);
      b39 = (s[46] << 24) | (s[47] >>> 8);
      b30 = (s[8] << 27) | (s[9] >>> 5);
      b31 = (s[9] << 27) | (s[8] >>> 5);
      b12 = (s[18] << 20) | (s[19] >>> 12);
      b13 = (s[19] << 20) | (s[18] >>> 12);
      b44 = (s[29] << 7) | (s[28] >>> 25);
      b45 = (s[28] << 7) | (s[29] >>> 25);
      b26 = (s[38] << 8) | (s[39] >>> 24);
      b27 = (s[39] << 8) | (s[38] >>> 24);
      b8 = (s[48] << 14) | (s[49] >>> 18);
      b9 = (s[49] << 14) | (s[48] >>> 18);

      s[0] = b0 ^ (~b2 & b4);
      s[1] = b1 ^ (~b3 & b5);
      s[10] = b10 ^ (~b12 & b14);
      s[11] = b11 ^ (~b13 & b15);
      s[20] = b20 ^ (~b22 & b24);
      s[21] = b21 ^ (~b23 & b25);
      s[30] = b30 ^ (~b32 & b34);
      s[31] = b31 ^ (~b33 & b35);
      s[40] = b40 ^ (~b42 & b44);
      s[41] = b41 ^ (~b43 & b45);
      s[2] = b2 ^ (~b4 & b6);
      s[3] = b3 ^ (~b5 & b7);
      s[12] = b12 ^ (~b14 & b16);
      s[13] = b13 ^ (~b15 & b17);
      s[22] = b22 ^ (~b24 & b26);
      s[23] = b23 ^ (~b25 & b27);
      s[32] = b32 ^ (~b34 & b36);
      s[33] = b33 ^ (~b35 & b37);
      s[42] = b42 ^ (~b44 & b46);
      s[43] = b43 ^ (~b45 & b47);
      s[4] = b4 ^ (~b6 & b8);
      s[5] = b5 ^ (~b7 & b9);
      s[14] = b14 ^ (~b16 & b18);
      s[15] = b15 ^ (~b17 & b19);
      s[24] = b24 ^ (~b26 & b28);
      s[25] = b25 ^ (~b27 & b29);
      s[34] = b34 ^ (~b36 & b38);
      s[35] = b35 ^ (~b37 & b39);
      s[44] = b44 ^ (~b46 & b48);
      s[45] = b45 ^ (~b47 & b49);
      s[6] = b6 ^ (~b8 & b0);
      s[7] = b7 ^ (~b9 & b1);
      s[16] = b16 ^ (~b18 & b10);
      s[17] = b17 ^ (~b19 & b11);
      s[26] = b26 ^ (~b28 & b20);
      s[27] = b27 ^ (~b29 & b21);
      s[36] = b36 ^ (~b38 & b30);
      s[37] = b37 ^ (~b39 & b31);
      s[46] = b46 ^ (~b48 & b40);
      s[47] = b47 ^ (~b49 & b41);
      s[8] = b8 ^ (~b0 & b2);
      s[9] = b9 ^ (~b1 & b3);
      s[18] = b18 ^ (~b10 & b12);
      s[19] = b19 ^ (~b11 & b13);
      s[28] = b28 ^ (~b20 & b22);
      s[29] = b29 ^ (~b21 & b23);
      s[38] = b38 ^ (~b30 & b32);
      s[39] = b39 ^ (~b31 & b33);
      s[48] = b48 ^ (~b40 & b42);
      s[49] = b49 ^ (~b41 & b43);

      s[0] ^= KECCAK_RC[n];
      s[1] ^= KECCAK_RC[n + 1];
    }
  };

  function keccak256Hex(message) {
    const blockCount = 34;
    const byteCount = 136;
    const shift = [0, 8, 16, 24];
    const s = new Array(50).fill(0);
    const blocks = new Array(blockCount + 1).fill(0);
    let i = 0;
    const absorb = (code) => {
      blocks[i >> 2] |= code << shift[i & 3];
      i += 1;
      if (i >= byteCount) {
        for (let j = 0; j < blockCount; j += 1) s[j] ^= blocks[j];
        keccakF(s);
        blocks.fill(0);
        i = 0;
      }
    };
    for (let n = 0; n < message.length; n += 1) absorb(message.charCodeAt(n));
    absorb(0x01);
    blocks[blockCount - 1] |= 0x80000000;
    for (let j = 0; j < blockCount; j += 1) s[j] ^= blocks[j];
    keccakF(s);
    const hexChars = "0123456789abcdef";
    let hex = "";
    for (let j = 0; j < 8; j += 1) {
      const block = s[j] >>> 0;
      hex += hexChars[(block >> 4) & 15] + hexChars[block & 15]
        + hexChars[(block >> 12) & 15] + hexChars[(block >> 8) & 15]
        + hexChars[(block >> 20) & 15] + hexChars[(block >> 16) & 15]
        + hexChars[(block >> 28) & 15] + hexChars[(block >> 24) & 15];
    }
    return hex;
  }

  function toChecksumAddress(address) {
    const raw = String(address || "").trim();
    const body = raw.startsWith("0x") || raw.startsWith("0X") ? raw.slice(2) : raw;
    if (!/^[0-9a-fA-F]{40}$/.test(body)) return raw;
    const lower = body.toLowerCase();
    const hash = keccak256Hex(lower);
    let out = "0x";
    for (let n = 0; n < 40; n += 1) {
      out += parseInt(hash[n], 16) >= 8 ? lower[n].toUpperCase() : lower[n];
    }
    return out;
  }

  const KNOWN_SYMBOL_ICONS = new Set([
    "btc", "eth", "sol", "bnb", "usdt", "usdc", "dai", "weth",
    "matic", "arb", "op", "avax", "link", "uni", "pendle",
  ]);

  const ICON_PALETTE = [
    "#d4a574", "#c4895a", "#8b6914", "#a67c52",
    "#6b4f3a", "#b8956c", "#9a7b4f", "#7a5c3e",
  ];

  // Pendle LP / PT / YT names: PLP-SUSDX-3DEC2026 → SUSDX. Non-Pendle names stay as-is.
  const PENDLE_DATE_TAIL = /-\d{1,2}[A-Za-z]{3}\d{4}$/;

  function pendleUnderlyingSymbol(asset) {
    const raw = String(asset || "").trim();
    const matched = raw.match(/^(?:PLP|PT|YT)[-_](.+)$/i);
    if (!matched) return raw;
    let body = matched[1];
    if (PENDLE_DATE_TAIL.test(body)) body = body.replace(PENDLE_DATE_TAIL, "");
    return body || raw;
  }

  function iconSymbol(asset) {
    return pendleUnderlyingSymbol(asset);
  }

  function pushChainIconCandidates(push, row) {
    const chain = String(row?.chain || "").trim().toLowerCase();
    const contract = String(row?.contract || "").trim();
    const twChain = TW_CHAIN_MAP[chain];
    if (twChain && contract) {
      const isEvm = /^0x[0-9a-fA-F]{40}$/.test(contract);
      const assetId = isEvm ? toChecksumAddress(contract) : contract;
      push(`https://assets-cdn.trustwallet.com/blockchains/${twChain}/assets/${assetId}/logo.png`);
    }
    if (contract) {
      push(`https://logo.octav.fi/api/icon/${contract.toLowerCase()}.png`);
    }
  }

  function tokenIconCandidates(row, holdingsByAsset) {
    const urls = [];
    const seen = new Set();
    const push = (url) => {
      if (!url || seen.has(url)) return;
      seen.add(url);
      urls.push(url);
    };
    const iconUrl = String(row?.icon_url || "").trim();
    if (/^https?:\/\//i.test(iconUrl)) push(iconUrl);
    pushChainIconCandidates(push, row);

    const rawAsset = String(row?.asset || "").trim();
    const underlying = pendleUnderlyingSymbol(rawAsset);
    const isPendle = underlying !== rawAsset;
    if (isPendle && holdingsByAsset) {
      const peer = holdingsByAsset.get(underlying.toUpperCase());
      const peerRow = peer?.iconRow || peer;
      if (peerRow && peerRow !== row) {
        const peerIcon = String(peerRow.icon_url || "").trim();
        if (/^https?:\/\//i.test(peerIcon)) push(peerIcon);
        pushChainIconCandidates(push, peerRow);
      }
    }

    const sym = (isPendle ? underlying : iconSymbol(rawAsset)).toLowerCase();
    if (KNOWN_SYMBOL_ICONS.has(sym)) {
      push(
        `https://raw.githubusercontent.com/spothq/cryptocurrency-icons/master/128/color/${sym}.png`,
      );
    }
    return urls;
  }

  function holdingIconLetters(asset) {
    const raw = String(asset || "").trim();
    const stripped = pendleUnderlyingSymbol(raw).replace(/[^a-z0-9]/gi, "");
    if (stripped) return stripped.slice(0, 2).toUpperCase();
    const latin = raw.replace(/[^a-z0-9]/gi, "");
    if (latin) return latin.slice(0, 2).toUpperCase();
    return "?";
  }

  function holdingIconColor(asset) {
    const text = String(asset || "");
    let hash = 0;
    for (let i = 0; i < text.length; i += 1) {
      hash = (hash * 31 + text.charCodeAt(i)) >>> 0;
    }
    return ICON_PALETTE[hash % ICON_PALETTE.length];
  }

  function tokenIconSvgDataUri(asset) {
    const raw = String(asset || "");
    const letters = raw.length <= 2 ? (raw.toUpperCase() || "?") : holdingIconLetters(asset);
    const color = holdingIconColor(pendleUnderlyingSymbol(asset) || asset);
    const svg =
      `<svg xmlns="http://www.w3.org/2000/svg" width="30" height="30" viewBox="0 0 30 30">` +
      `<circle cx="15" cy="15" r="15" fill="${color}"/>` +
      `<text x="15" y="16" text-anchor="middle" dominant-baseline="central" ` +
      `font-family="system-ui,sans-serif" font-size="11" font-weight="700" fill="#1a120c">${letters}</text>` +
      `</svg>`;
    return `data:image/svg+xml,${encodeURIComponent(svg)}`;
  }

  // Same neutral disc PT/YT use when a logo is missing: grey circle, muted $.
  const ICON_NEUTRAL_GREY = "#3a3a3e";

  function dollarFallbackIcon() {
    const svg =
      `<svg xmlns="http://www.w3.org/2000/svg" width="30" height="30" viewBox="0 0 30 30">` +
      `<circle cx="15" cy="15" r="15" fill="${ICON_NEUTRAL_GREY}"/>` +
      `<text x="15" y="16" text-anchor="middle" dominant-baseline="central" ` +
      `font-family="system-ui,sans-serif" font-size="14" font-weight="650" fill="#8f8f94">$</text>` +
      `</svg>`;
    return `data:image/svg+xml,${encodeURIComponent(svg)}`;
  }

  function isBrokenIconSrc(src) {
    const value = String(src || "").trim();
    return !value || value === "null" || value === "undefined" || value === "about:blank";
  }

  // Same 16px stroke as the lending fold arrow, but collapsed points right
  // so a 90° rotation lands pointing down when the row is open.
  const HOLD_CHEVRON_SVG =
    `<svg class="hold-chevron-svg" viewBox="0 0 16 16" fill="none" aria-hidden="true">` +
    `<path d="m6 3.5 4.5 4.5L6 12.5"/>` +
    `</svg>`;

  function holdChevronMarkup(open) {
    return `<span class="hold-chevron${open ? " is-open" : ""}" aria-hidden="true">${HOLD_CHEVRON_SVG}</span>`;
  }

  function miscStackIconMarkup() {
    return `<span class="hold-icon hold-icon--misc" aria-hidden="true">
      <svg class="hold-misc-glyph" viewBox="0 0 16 16" fill="none" aria-hidden="true">
        <path d="M2.5 5.25 8 2.75 13.5 5.25 8 7.75 2.5 5.25z"/>
        <path d="M2.5 8 8 10.5 13.5 8"/>
        <path d="M2.5 10.75 8 13.25 13.5 10.75"/>
      </svg>
    </span>`;
  }

  const SOURCE_ICON_FILES = {
    binance: "binance.svg",
    gate: "gate.svg",
    bitget: "bitget.svg",
    bybit: "bybit.svg",
    kraken: "kraken.svg",
    zerion: "zerion.svg",
    helius: "solana.svg",
    evm: "ethereum.svg",
    ethereum: "ethereum.svg",
    alchemy: "ethereum.svg",
    solana: "solana.svg",
  };

  function sourceIconLetter(source) {
    const key = String(source || "").trim().toLowerCase();
    const label = sourceLabel(key);
    const fromLabel = String(label || "").replace(/[^a-z0-9\u4e00-\u9fff]/gi, "");
    if (fromLabel) return fromLabel.slice(0, 1).toUpperCase();
    const fromKey = key.replace(/[^a-z0-9]/gi, "");
    return fromKey ? fromKey.slice(0, 1).toUpperCase() : "?";
  }

  function sourceLetterSvgDataUri(letter, color) {
    const svg =
      `<svg xmlns="http://www.w3.org/2000/svg" width="30" height="30" viewBox="0 0 30 30">` +
      `<circle cx="15" cy="15" r="15" fill="${color}"/>` +
      `<text x="15" y="16" text-anchor="middle" dominant-baseline="central" ` +
      `font-family="system-ui,sans-serif" font-size="13" font-weight="700" fill="#1a120c">${letter}</text>` +
      `</svg>`;
    return `data:image/svg+xml,${encodeURIComponent(svg)}`;
  }

  function sourceIconMarkup(source) {
    const key = String(source || "").trim().toLowerCase();
    const file = SOURCE_ICON_FILES[key];
    const letter = sourceIconLetter(key);
    // CoinStats has no official mark in Web3Icons — warm letter, never a fake brand file.
    const src = file
      ? `/static/icons/sources/${file}`
      : key === "coinstats"
        ? sourceLetterSvgDataUri(letter, "#c4895a")
        : tokenIconSvgDataUri(letter);
    const extraClass = file ? "" : " hold-icon-img--fallback";
    return `<span class="hold-icon" aria-hidden="true">
      <img class="hold-icon-img${extraClass}" src="${escapeHtml(src)}" alt="" width="30" height="30"
        loading="lazy" referrerpolicy="no-referrer"
        data-icon-asset="${escapeHtml(letter)}" />
    </span>`;
  }

  function tokenIconMarkup(row, holdingsByAsset) {
    const asset = String(row?.asset || "?");
    const candidates = tokenIconCandidates(row, holdingsByAsset).filter((url) => !isBrokenIconSrc(url));
    // No URL, or every candidate later 404s: same grey $ disc as PT/YT. Never an empty circle.
    const src = candidates[0] || dollarFallbackIcon();
    const fallbacks = candidates.slice(1);
    const fallbackAttr = fallbacks.length
      ? ` data-icon-fallbacks="${escapeHtml(fallbacks.join("|"))}"`
      : "";
    const extraClass = candidates.length ? "" : " hold-icon-img--fallback";
    const dollarAttr = candidates.length ? "" : ` data-icon-dollar="1"`;
    return `<span class="hold-icon" aria-hidden="true">
      <img class="hold-icon-img${extraClass}" src="${escapeHtml(src)}" alt="" width="30" height="30"
        loading="lazy" referrerpolicy="no-referrer"
        data-icon-asset="${escapeHtml(asset)}"${fallbackAttr}${dollarAttr} />
    </span>`;
  }

  function normalizeHoldings(raw) {
    return (Array.isArray(raw) ? raw : [])
      .filter((row) => row && row.asset && row.source)
      .map((row) => ({
        source: String(row.source),
        asset: String(row.asset),
        amount: asNumber(row.amount),
        price_usdt: row.price_usdt == null ? null : asNumber(row.price_usdt),
        value_usdt: asNumber(row.value_usdt),
        priced: row.priced !== false && row.priced !== 0,
        live: row.live !== false && row.live !== 0,
        covered_by_position: row.covered_by_position === true,
        price_source: row.price_source || null,
        chain: row.chain || null,
        contract: row.contract || null,
        icon_url: row.icon_url || null,
        asset_class: row.asset_class || null,
        risk_level: row.risk_level || null,
        risk_reason: row.risk_reason || "",
        overridden: row.overridden === true,
      }));
  }

  function aggregateByAsset(rows, threshold) {
    const map = new Map();
    for (const row of rows) {
      const key = row.asset.toUpperCase();
      let entry = map.get(key);
      if (!entry) {
        entry = {
          key,
          asset: assetLabel(row.asset),
          amount: 0,
          value: 0,
          hasUnpriced: false,
          covered: false,
          sources: [],
        };
        map.set(key, entry);
      }
      entry.amount += row.amount;
      if (row.priced) entry.value += row.value_usdt;
      else entry.hasUnpriced = true;
      if (row.covered_by_position) entry.covered = true;
      entry.sources.push(row);
      const iconScore = (item) => {
        let score = 0;
        if (item?.icon_url && /^https?:\/\//i.test(String(item.icon_url))) score += 4;
        if (item?.contract) score += 2;
        if (item?.priced) score += 1;
        return score;
      };
      const better =
        !entry.iconRow ||
        iconScore(row) > iconScore(entry.iconRow) ||
        (iconScore(row) === iconScore(entry.iconRow) && row.value_usdt > entry.iconRow.value_usdt);
      if (better) {
        entry.iconRow = {
          asset: row.asset,
          chain: row.chain,
          contract: row.contract,
          icon_url: row.icon_url,
          priced: row.priced,
          value_usdt: row.value_usdt,
        };
      }
    }

    for (const entry of map.values()) {
      entry.risk = maxRiskLevel(entry.sources);
      entry.overridden = entry.sources.some((row) => row.overridden === true);
    }

    const groups = [];
    const misc = [];
    for (const entry of map.values()) {
      // Unpriced, or priced value strictly below the API threshold.
      if (entry.hasUnpriced && entry.value <= 0) misc.push(entry);
      else if (entry.value < threshold) misc.push(entry);
      else groups.push(entry);
    }
    groups.sort((a, b) => b.value - a.value);
    misc.sort((a, b) => {
      const aUnpriced = a.hasUnpriced && a.value <= 0;
      const bUnpriced = b.hasUnpriced && b.value <= 0;
      if (aUnpriced !== bUnpriced) return aUnpriced ? 1 : -1;
      return b.value - a.value;
    });
    let miscValue = 0;
    for (const entry of misc) miscValue += entry.value;

    return { groups, misc, miscValue };
  }

  function aggregateBySource(rows, threshold) {
    const map = new Map();
    for (const row of rows) {
      const key = row.source;
      let entry = map.get(key);
      if (!entry) {
        entry = {
          source: key,
          label: sourceLabel(key),
          value: 0,
          hasUnpriced: false,
          main: [],
          misc: [],
        };
        map.set(key, entry);
      }
      // Header total always includes misc (priced value only; unpriced is 0).
      if (row.priced) entry.value += row.value_usdt;
      else entry.hasUnpriced = true;
      if (isMiscRow(row, threshold)) entry.misc.push(row);
      else entry.main.push(row);
    }

    for (const entry of map.values()) {
      entry.main.sort((a, b) => b.value_usdt - a.value_usdt);
      entry.misc.sort(miscSort);
      let miscValue = 0;
      for (const row of entry.misc) {
        if (row.priced) miscValue += row.value_usdt;
      }
      entry.miscValue = miscValue;
      entry.miscCount = entry.misc.length;
      entry.risk = maxRiskLevel([...entry.main, ...entry.misc]);
      entry.overridden = [...entry.main, ...entry.misc].some((row) => row.overridden === true);
    }

    return [...map.values()].sort((a, b) => b.value - a.value);
  }

  function holdingsIconIndex(rows) {
    const byAsset = new Map();
    for (const row of rows) {
      const key = String(row?.asset || "").trim().toUpperCase();
      if (!key) continue;
      const score = (item) => {
        let n = 0;
        if (item?.icon_url && /^https?:\/\//i.test(String(item.icon_url))) n += 4;
        if (item?.contract) n += 2;
        if (item?.priced) n += 1;
        return n;
      };
      const prev = byAsset.get(key);
      if (!prev || score(row) > score(prev) || (score(row) === score(prev) && asNumber(row.value_usdt) > asNumber(prev.value_usdt))) {
        byAsset.set(key, row);
      }
    }
    return byAsset;
  }

  function miscAssetDetail(entry, iconIndex) {
    const unpriced = entry.hasUnpriced && entry.value <= 0;
    const valueHtml = unpriced
      ? `<span class="hold-unpriced">未定价</span>`
      : `<span class="hold-value">${money(entry.value)}</span>`;
    return `
      <li class="hold-detail-row">
        ${tokenIconMarkup(entry.iconRow || { asset: entry.asset }, iconIndex)}
        <span class="hold-detail-source">${escapeHtml(entry.asset)}</span>
        <span class="hold-qty">${qtyFmt(entry.amount)}</span>
        ${valueHtml}
      </li>`;
  }

  function miscSourceDetail(row, iconIndex) {
    const valueHtml = row.priced
      ? `<span class="hold-value">${money(row.value_usdt)}</span>`
      : `<span class="hold-unpriced">未定价</span>`;
    return `
      <li class="hold-detail-row">
        ${tokenIconMarkup(row, iconIndex)}
        <span class="hold-detail-source">${escapeHtml(assetLabel(row.asset))}</span>
        <span class="hold-qty">${qtyFmt(row.amount)}</span>
        ${valueHtml}
      </li>`;
  }

  function miscFoldRow(key, count, total, open, detailHtml) {
    const countLabel = `${count} 项`;
    return `
      <li class="hold-item hold-item--misc${open ? " is-open" : ""}">
        <button class="hold-row hold-row--misc" type="button" data-holdings-toggle-misc="${escapeHtml(key)}"
          aria-expanded="${open}"
          aria-label="杂项，${countLabel}，${money(total)} USDT">
          ${holdChevronMarkup(open)}
          ${miscStackIconMarkup()}
          <span class="hold-asset">
            <span class="hold-name">杂项</span>
            <span class="hold-meta">${escapeHtml(countLabel)}</span>
          </span>
          <span class="hold-qty"></span>
          <span class="hold-value">${money(total)}</span>
        </button>
        ${open ? `<div class="hold-detail-clip"><ul class="hold-detail hold-detail--scroll hold-detail--misc">${detailHtml}</ul></div>` : ""}
      </li>`;
  }

  function holdingsByAssetMarkup(rows, threshold) {
    const { groups, misc, miscValue } = aggregateByAsset(rows, threshold);
    const iconIndex = holdingsIconIndex(rows);
    if (!groups.length && !misc.length) {
      return `<p class="hold-empty">暂无持仓，同步后显示</p>`;
    }

    const items = groups
      .map((group) => {
        const multi = group.sources.length > 1;
        const open = multi && state.holdingsOpenAssets.has(group.key);
        const metaBits = [];
        if (multi) metaBits.push(`${group.sources.length} 个来源`);
        else metaBits.push(sourceLabel(group.sources[0].source));
        const dated = nameDateHint(group.asset);
        if (dated) metaBits.push(dated);
        if (group.hasUnpriced) metaBits.push("含未定价");
        if (group.covered) metaBits.push("已计入仓位");
        const meta = `<span class="hold-meta">${escapeHtml(metaBits.join(" · "))}</span>`;
        const valueHtml =
          group.hasUnpriced && group.value <= 0
            ? `<span class="hold-value hold-value--muted">未定价</span>`
            : `<span class="hold-value">${money(group.value)}</span>`;
        const toggleAttrs = multi
          ? `type="button" data-holdings-toggle-asset="${escapeHtml(group.key)}" aria-expanded="${open}"`
          : `type="button" tabindex="-1" aria-disabled="true"`;
        const chevron = multi
          ? holdChevronMarkup(open)
          : `<span class="hold-chevron hold-chevron--ghost" aria-hidden="true"></span>`;

        let detail = "";
        if (multi && open) {
          detail = `<ul class="hold-detail">
            ${group.sources
              .slice()
              .sort((a, b) => {
                if (a.priced !== b.priced) return a.priced ? -1 : 1;
                return b.value_usdt - a.value_usdt;
              })
              .map(
                (row) => `
              <li class="hold-detail-row">
                ${tokenIconMarkup(row, iconIndex)}
                <span class="hold-detail-source">${escapeHtml(sourceLabel(row.source))}${row.covered_by_position ? `<span class="hold-covered">已计入仓位</span>` : ""}</span>
                <span class="hold-qty">${qtyFmt(row.amount)}</span>
                ${valueCell(row)}
              </li>`,
              )
              .join("")}
          </ul>`;
        }

        return `
          <li class="hold-item${open ? " is-open" : ""}">
            <button class="hold-row${multi ? "" : " hold-row--static"}" ${toggleAttrs}>
              ${chevron}
              ${tokenIconMarkup(group.iconRow || { asset: group.asset }, iconIndex)}
              <span class="hold-asset">
                <span class="hold-name" title="${escapeHtml(group.asset)}">${escapeHtml(group.asset)}${riskInlineMarkup(group.risk, group.overridden ? "含人工设定" : "")}</span>
                ${meta}
              </span>
              <span class="hold-value-stack">
                ${valueHtml}
                <span class="hold-qty">${qtyFmt(group.amount)}</span>
              </span>
            </button>
            ${detail}
          </li>`;
      })
      .join("");

    const miscKey = "asset";
    const miscOpen = misc.length > 0 && state.holdingsOpenMisc.has(miscKey);
    const miscRow = misc.length
      ? miscFoldRow(
          miscKey,
          misc.length,
          miscValue,
          miscOpen,
          misc.map((entry) => miscAssetDetail(entry, iconIndex)).join(""),
        )
      : "";

    return `<ul class="hold-list">${items}${miscRow}</ul>`;
  }

  function holdingsBySourceMarkup(rows, threshold) {
    const groups = aggregateBySource(rows, threshold);
    const iconIndex = holdingsIconIndex(rows);
    if (!groups.length) {
      return `<p class="hold-empty">暂无持仓，同步后显示</p>`;
    }

    if (!state.holdingsSourceSeeded && groups.length) {
      state.holdingsOpenSources.add(groups[0].source);
      state.holdingsSourceSeeded = true;
    }

    return `<ul class="hold-list hold-list--source">
      ${groups
        .map((group) => {
          const open = state.holdingsOpenSources.has(group.source);
          const itemCount = group.main.length + group.miscCount;
          const metaText = group.hasUnpriced
            ? `${itemCount} 项 · 含未定价`
            : `${itemCount} 项`;
          const miscKey = `source:${group.source}`;
          const miscOpen = group.miscCount > 0 && state.holdingsOpenMisc.has(miscKey);
          const miscCountLabel = `${group.miscCount} 项`;
          const miscBlock = group.miscCount
            ? `<li class="hold-detail-row hold-detail-row--misc">
                <button class="hold-misc-line" type="button"
                  data-holdings-toggle-misc="${escapeHtml(miscKey)}"
                  aria-expanded="${miscOpen}"
                  aria-label="杂项，${miscCountLabel}，${money(group.miscValue)} USDT">
                  ${holdChevronMarkup(miscOpen)}
                  ${miscStackIconMarkup()}
                  <span class="hold-misc-copy">
                    <span class="hold-detail-source">杂项</span>
                    <span class="hold-meta">${escapeHtml(miscCountLabel)}</span>
                  </span>
                  <span class="hold-value">${money(group.miscValue)}</span>
                </button>
                ${
                  miscOpen
                    ? `<ul class="hold-detail hold-detail--nested">
                        ${group.misc.map((row) => miscSourceDetail(row, iconIndex)).join("")}
                      </ul>`
                    : ""
                }
              </li>`
            : "";
          const detail = open
            ? `<ul class="hold-detail hold-detail--scroll">
                ${group.main
                  .map(
                    (row) => `
                  <li class="hold-detail-row">
                    ${tokenIconMarkup(row, iconIndex)}
                    <span class="hold-detail-source">${escapeHtml(assetLabel(row.asset))}${row.covered_by_position ? `<span class="hold-covered">已计入仓位</span>` : ""}</span>
                    <span class="hold-qty">${qtyFmt(row.amount)}</span>
                    ${valueCell(row)}
                  </li>`,
                  )
                  .join("")}
                ${miscBlock}
              </ul>`
            : "";

          return `
            <li class="hold-item hold-item--source${open ? " is-open" : ""}">
              <button class="hold-row hold-row--source" type="button"
                data-holdings-toggle-source="${escapeHtml(group.source)}"
                aria-expanded="${open}">
                ${holdChevronMarkup(open)}
                ${sourceIconMarkup(group.source)}
                <span class="hold-asset">
                  <span class="hold-name" title="${escapeHtml(group.label)}">${escapeHtml(group.label)}${riskInlineMarkup(group.risk, group.overridden ? "含人工设定" : "")}</span>
                  <span class="hold-meta">${escapeHtml(metaText)}</span>
                </span>
                <span class="hold-value-stack">
                  <span class="hold-value">${money(group.value)}</span>
                </span>
              </button>
              ${detail}
            </li>`;
        })
        .join("")}
    </ul>`;
  }

  /* ---------- Portfolio structure (phase 5) ---------- */

  const PF_CLASS_ORDER = ["cash", "crypto", "stock", "defi", "option", "other"];
  // Asset classes are a neutral grey scale. Gold and amber stay reserved for
  // interaction and risk, so the structure bar never competes with the curve.
  const PF_CLASS_META = {
    cash: { label: "现金", short: "现金", glyph: "¥", color: "rgba(245, 245, 247, 0.82)" },
    crypto: { label: "加密资产", short: "加密", glyph: "◇", color: "rgba(245, 245, 247, 0.34)" },
    stock: { label: "股票代币", short: "股票", glyph: "股", color: "rgba(245, 245, 247, 0.26)" },
    defi: { label: "DeFi", short: "DeFi", glyph: "%", color: "rgba(245, 245, 247, 0.20)" },
    option: { label: "期权", short: "期权", glyph: "⌁", color: "rgba(245, 245, 247, 0.15)" },
    other: { label: "未分类", short: "未分类", glyph: "·", color: "rgba(245, 245, 247, 0.11)" },
  };
  const PF_RISK_ORDER = ["low", "medium", "high"];
  // 风险琥珀只给高风险；低风险保持中性，中风险用低饱和古铜色。
  const PF_RISK_META = {
    low: { label: "低风险", short: "低", fill: "#4a4a4f", dot: "#909096" },
    medium: { label: "中风险", short: "中", fill: "#8a6a43", dot: "#b98f66" },
    high: { label: "高风险", short: "高", fill: "#FFD60A", dot: "#FFD60A" },
  };
  const PF_TAB_META = [
    ["all", "全部"],
    ["token", "代币"],
    ["stock", "股票"],
    ["defi", "DeFi"],
    ["option", "期权"],
  ];
  const PF_RISK_WEIGHT = { low: 1, medium: 2, high: 3 };
  const PF_BACK_SVG =
    `<svg viewBox="0 0 16 16" fill="none" aria-hidden="true">` +
    `<path d="M10 3.5 5.5 8l4.5 4.5"/></svg>`;
  const PF_RIGHT_SVG =
    `<svg viewBox="0 0 16 16" fill="none" aria-hidden="true">` +
    `<path d="m6 3.5 4.5 4.5L6 12.5"/></svg>`;
  const PF_WARN_SVG =
    `<svg viewBox="0 0 12 11" aria-hidden="true">` +
    `<path d="M6 1 11 10H1L6 1Z"/><path d="M6 4.2v2.6"/><path d="M6 8.5h.01"/></svg>`;

  function pfPct(value) {
    const number = asNumber(value);
    if (!(number > 0)) return "0%";
    const digits = number >= 10 ? 1 : 2;
    return `${number.toFixed(digits).replace(/\.0+$/, "")}%`;
  }

  function pfRiskGlyph(risk) {
    if (risk === "high") return `<span class="pf-risk-triangle" aria-hidden="true">${PF_WARN_SVG}</span>`;
    return `<span class="pf-risk-dot" aria-hidden="true"></span>`;
  }

  // Preserve both the readable prefix and the maturity suffix (17DEC2026/USDC).
  // Latin and CJK are measured differently so the ellipsis lands mid-name.
  function pfMiddleName(value) {
    const raw = String(value ?? "");
    const widthOf = (char) => (/[\u2e80-\u9fff\uff00-\uffef]/.test(char) ? 2 : 1);
    let total = 0;
    for (const char of raw) total += widthOf(char);
    if (total <= 24) return raw;
    let head = "";
    let headWidth = 0;
    for (const char of raw) {
      const width = widthOf(char);
      if (headWidth + width > 7) break;
      head += char;
      headWidth += width;
    }
    let tail = "";
    let tailWidth = 0;
    const chars = Array.from(raw);
    for (let index = chars.length - 1; index >= 0; index -= 1) {
      const char = chars[index];
      const width = widthOf(char);
      if (tailWidth + width > 14) break;
      tail = char + tail;
      tailWidth += width;
    }
    return `${head}${ELLIPSIS}${tail}`;
  }

  // Chain is the useful context for a holding; a position also names its protocol.
  function pfContextLabel(item) {
    const chain = chainLabel(item?.chain);
    const source = sourceLabel(item?.source);
    if (item?.kind === "position") return [source, chain].filter(Boolean).join(" · ");
    return chain || source;
  }

  const PF_PROTOCOL_LABELS = {
    aave: "Aave",
    morpho: "Morpho",
    pendle: "Pendle",
    kamino: "Kamino",
    jupiter: "Jupiter",
  };

  function pfDefiProtocol(item) {
    const source = String(item?.source || "").toLowerCase();
    if (PF_PROTOCOL_LABELS[source]) return PF_PROTOCOL_LABELS[source];
    // Wallet aggregators report PLP/PT/YT holdings without the protocol, so
    // the Pendle prefix is the only reliable hint left on the row.
    const name = String(item?.name || "").toUpperCase();
    if (/^(PLP|PT|YT)[-_]/.test(name)) return "Pendle";
    if (/^KJLP[-_]/.test(name)) return "Kamino";
    return "—";
  }

  // DeFi rows always keep the same three slots, even when a value is missing,
  // so Kamino positions and Pendle LP/PT/YT holdings share one row rhythm.
  function pfDefiMetaLine(item) {
    const type = item?.kind === "position" ? "仓位" : "代币";
    const chain = chainLabel(item?.chain);
    const chainText = chain ? chain.charAt(0).toUpperCase() + chain.slice(1) : "—";
    return [type, pfDefiProtocol(item), chainText].join(" · ");
  }

  function pfItemIconMarkup(item) {
    if (item?.kind === "position") return protocolIconMarkup(item.source);
    return tokenIconMarkup({
      asset: item?.name,
      chain: item?.chain,
      contract: item?.contract,
      icon_url: item?.icon_url,
    });
  }

  function pfRiskTagMarkup(item) {
    const risk = item?.risk_level;
    // Low is the default state; a row only earns a tag when it needs attention.
    if (!PF_RISK_META[risk] || risk === "low") return "";
    const label = PF_RISK_META[risk].label;
    return `<span class="pf-risk pf-risk--${risk}" title="${escapeHtml(item.risk_reason || label)}">${pfRiskGlyph(risk)}<span>${PF_RISK_META[risk].short}</span></span>`;
  }

  function pfApyLabel(item) {
    if (item?.asset_class !== "defi") return "";
    const raw = item?.apy ?? item?.net_apy ?? item?.apy_pct ?? item?.details?.net_apy ?? item?.details?.apy;
    if (raw === null || raw === undefined || raw === "") return "";
    let number = Number(raw);
    if (!Number.isFinite(number) || number <= 0) return "";
    if (number <= 1) number *= 100;
    return `${number.toFixed(number >= 10 ? 1 : 2).replace(/\.0+$/, "")}%`;
  }

  function maxRiskLevel(rows) {
    let best = null;
    for (const row of Array.isArray(rows) ? rows : []) {
      const level = row?.risk_level;
      if (!PF_RISK_META[level]) continue;
      if (!best || PF_RISK_WEIGHT[level] > PF_RISK_WEIGHT[best]) best = level;
    }
    return best;
  }

  function riskInlineMarkup(level, title, extraClass = "") {
    if (!PF_RISK_META[level] || level === "low") return "";
    const label = PF_RISK_META[level].label;
    const glyph =
      level === "high"
        ? `<span class="risk-inline-triangle" aria-hidden="true">${PF_WARN_SVG}</span>`
        : `<span class="risk-inline-dot" aria-hidden="true"></span>`;
    return `<span class="risk-inline risk-inline--${level}${extraClass ? ` ${extraClass}` : ""}" title="${escapeHtml(title || label)}">${glyph}<span>${PF_RISK_META[level].short}</span></span>`;
  }

  function pfStructureItemCount() {
    return (state.pf.structure?.by_class || []).reduce((sum, entry) => sum + asNumber(entry.items), 0);
  }

  function pfEmptyBlock(message, { retry = false } = {}) {
    return `<div class="pf-empty">
      <span class="pf-empty-mark" aria-hidden="true">◇</span>
      <p>${escapeHtml(message)}</p>
      ${retry ? `<button class="pf-retry" type="button" data-pf-retry>重新读取</button>` : ""}
    </div>`;
  }

  function pfSkeletonBody() {
    return `<div class="pf-skeleton" aria-hidden="true">
      <div class="skeleton sk-pf-total"></div>
      <div class="skeleton sk-pf-bar"></div>
      <div class="skeleton sk-pf-legend"></div>
      <div class="skeleton sk-pf-bar"></div>
      <div class="skeleton sk-pf-legend"></div>
    </div>`;
  }

  function pfTabHasData(key) {
    if (key !== "option") return true;
    const entry = (state.pf.structure?.by_class || []).find((row) => row.class === "option");
    return asNumber(entry?.items) > 0 || asNumber(entry?.value_usdt) > 0;
  }

  function pfTabsMarkup() {
    const active = state.pf.view.tab;
    return `<div class="pf-tabs segmented" role="group" aria-label="资产结构视图">
      ${PF_TAB_META.map(([key, label]) => {
        const empty = !pfTabHasData(key);
        return `<button class="segment pf-tab" type="button" data-pf-tab="${key}"
          aria-pressed="${active === key}"${empty ? ` disabled aria-disabled="true" aria-label="${label}，暂无资产"` : ""}>${label}</button>`;
      }).join("")}
    </div>`;
  }

  function pfOverviewBody() {
    const pf = state.pf;
    const structure = pf.structure || {};
    const total = asNumber(structure.total_usdt);
    const count = pfStructureItemCount();
    const byClass = Array.isArray(structure.by_class) ? structure.by_class : [];
    const byRisk = Array.isArray(structure.by_risk) ? structure.by_risk : [];
    if (!(total > 0) && count === 0) {
      return `<div class="pf-total">
          <div class="pf-total-main">
            <span class="pf-total-kicker">总资产</span>
            <div class="pf-total-value">${money(0)}<span>USDT</span></div>
          </div>
          <span class="pf-total-count">0 项</span>
        </div>
        ${pfEmptyBlock("还没有可分类的资产，同步后会自动拆解。")}`;
    }
    return `
      <div class="pf-total">
        <div class="pf-total-main">
          <span class="pf-total-kicker">总资产</span>
          <div class="pf-total-value">${money(total)}<span>USDT</span></div>
        </div>
        <span class="pf-total-count">${count} 项</span>
      </div>
      <div class="pf-charts">
        ${pfChartMarkup("class", byClass)}
        ${pfChartMarkup("risk", byRisk)}
      </div>
      <button class="pf-risk-banner" type="button" data-pf-risk="high"
        aria-label="查看高风险明细，高风险占净值 ${pfPct(structure.high_risk_pct)}">
        <span class="pf-risk-mark" aria-hidden="true">${PF_WARN_SVG}</span>
        <span class="pf-risk-copy">高风险占净值</span>
        <strong class="pf-risk-value">${pfPct(structure.high_risk_pct)}</strong>
        <span class="pf-risk-cta">明细${PF_RIGHT_SVG}</span>
      </button>
      ${pf.itemsError ? `<p class="pf-inline-error">资产明细暂时无法读取 <button class="pf-retry-link" type="button" data-pf-retry>重试</button></p>` : ""}`;
  }

  function pfChartMarkup(kind, entries) {
    const total = entries.reduce((sum, entry) => sum + Math.max(asNumber(entry.value_usdt), 0), 0);
    const title = kind === "class" ? "按大类" : "按风险档";
    const meta = kind === "class" ? "6 类" : "3 档";
    const rows = entries
      .map((entry) => {
        const key = kind === "class" ? entry.class : entry.risk;
        const itemMeta = kind === "class" ? PF_CLASS_META[key] : PF_RISK_META[key];
        if (!itemMeta) return null;
        return {
          entry,
          key,
          itemMeta,
          label: kind === "class" ? entry.label || itemMeta.label : itemMeta.label,
        };
      })
      .filter(Boolean);
    const visible = rows.filter((row) => asNumber(row.entry.value_usdt) > 0);
    const hasZero = rows.some((row) => !(asNumber(row.entry.value_usdt) > 0));
    const segments = visible
      .map(({ entry, itemMeta, label }) => {
        const color = kind === "class" ? itemMeta.color : itemMeta.fill;
        const pct = Math.max(asNumber(entry.pct), 0.35);
        return `<span class="pf-seg" aria-hidden="true"
          style="flex:${pct} 1 0;--pf-color:${color}"></span>`;
      })
      .join("");
    const legend = visible
      .map(({ entry, key, itemMeta, label }) => {
        const swatch =
          kind === "risk"
            ? `<span class="pf-legend-swatch pf-legend-swatch--risk" style="--pf-color:${itemMeta.fill}" aria-hidden="true">${key === "high" ? PF_WARN_SVG : ""}</span>`
            : `<span class="pf-legend-swatch" style="--pf-color:${itemMeta.color}" aria-hidden="true"></span>`;
        return `<button class="pf-legend-btn" type="button"
          data-pf-${kind}="${escapeHtml(key)}"
          aria-label="${escapeHtml(label)} ${pfPct(entry.pct)}，查看明细">
          ${swatch}
          <span class="pf-legend-label">${escapeHtml(label)}</span>
          <span class="pf-legend-pct">${pfPct(entry.pct)}</span>
        </button>`;
      })
      .join("");
    return `<figure class="pf-chart">
      <figcaption class="pf-chart-head">
        <span class="pf-chart-title">${title}</span>
        <span class="pf-chart-meta">${meta}${total > 0 ? "" : " · 暂无"}</span>
      </figcaption>
      <div class="pf-bar${segments ? "" : " is-empty"}" role="group" aria-label="${title}占比">${segments || `<span class="pf-bar-empty"></span>`}</div>
      <div class="pf-legend pf-legend--${kind}">${legend}${hasZero ? `<span class="pf-legend-rest" aria-hidden="true">其余为 0</span>` : ""}</div>
    </figure>`;
  }

  function pfViewTitle(view) {
    if (view.risk) return `${PF_RISK_META[view.risk]?.label || "风险"}明细`;
    if (view.class) return `${PF_CLASS_META[view.class]?.label || "资产"}明细`;
    if (view.tab === "token") return "代币明细";
    if (view.tab !== "all") return `${PF_CLASS_META[view.tab]?.label || ""}明细`;
    return "资产明细";
  }

  function pfDetailBody() {
    const pf = state.pf;
    const view = pf.view;
    const items = Array.isArray(pf.viewItems) ? pf.viewItems : [];
    const subtotal = items.reduce((sum, item) => sum + asNumber(item.value_usdt), 0);
    const overall = asNumber(pf.structure?.total_usdt);
    const share = overall > 0 ? pfPct((subtotal / overall) * 100) : "0%";
    const meta = `${items.length} 项 · ${money(subtotal)} USDT · 占净值 ${share}`;
    let body;
    if (!items.length) {
      if (pf.itemsError) body = pfEmptyBlock("资产明细暂时无法读取。", { retry: true });
      else if (pf.loading && !pf.allItems.length) body = pfSkeletonBody();
      else body = pfEmptyBlock("该分类下暂无资产。");
    } else {
      const main = items.filter((item) => !item.misc);
      const misc = items.filter((item) => item.misc);
      const miscTotal = misc.reduce((sum, item) => sum + asNumber(item.value_usdt), 0);
      const miscKey = pfCacheKey(view);
      body = `<ul class="pf-list">
        ${main.map((item) => pfItemMarkup(item)).join("")}
        ${misc.length ? pfMiscMarkup(misc, miscTotal, miscKey) : ""}
      </ul>`;
    }
    return `
      <div class="pf-detail-head">
        <button class="pf-back" type="button" data-pf-back aria-label="返回资产结构">${PF_BACK_SVG}<span>结构</span></button>
        <div class="pf-detail-copy">
          <h3 class="pf-detail-title">${escapeHtml(pfViewTitle(view))}</h3>
          <p class="pf-detail-meta">${meta}</p>
        </div>
      </div>
      ${body}`;
  }

  function pfItemMarkup(item) {
    const rawName = String(item.name || "未命名资产");
    const name = pfMiddleName(rawName);
    const key = String(item.key || "");
    const metaBits = [];
    if (item.asset_class === "defi") {
      metaBits.push(pfDefiMetaLine(item));
    } else {
      if (item.kind === "position") metaBits.push("仓位");
      const context = pfContextLabel(item);
      if (context) metaBits.push(context);
    }
    const apy = pfApyLabel(item);
    const manual = item.overridden ? `<span class="pf-manual" title="已人工设定">人工</span>` : "";
    return `<li class="pf-item">
      <div class="pf-row">
        ${pfItemIconMarkup(item)}
        <button class="pf-row-main" type="button" data-pf-edit="${escapeHtml(key)}"
          aria-label="调整 ${escapeHtml(rawName)} 的分类与风险">
          <span class="pf-name-line">
            <span class="pf-name" title="${escapeHtml(rawName)}">${escapeHtml(name)}</span>
            ${manual}
          </span>
          <span class="pf-sub-line">
            <span class="pf-sub">${escapeHtml(metaBits.filter(Boolean).join(" · "))}</span>
            <span class="pf-tag-line">${pfRiskTagMarkup(item)}${apy ? `<span class="pf-apy">年化 ${apy}</span>` : ""}</span>
          </span>
        </button>
        <span class="pf-amount">${money(item.value_usdt)}</span>
        <button class="pf-more" type="button" data-pf-edit="${escapeHtml(key)}"
          aria-label="调整 ${escapeHtml(rawName)} 的标签"><svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="3" cy="8" r="1.2"/><circle cx="8" cy="8" r="1.2"/><circle cx="13" cy="8" r="1.2"/></svg></button>
      </div>
    </li>`;
  }

  function pfMiscMarkup(items, total, key) {
    const open = state.pf.openMisc.has(key);
    return `<li class="pf-item pf-item--misc${open ? " is-open" : ""}">
      <button class="pf-misc-row" type="button" data-pf-misc="${escapeHtml(key)}"
        aria-expanded="${open}" aria-label="杂项，${items.length} 项，${money(total)} USDT">
        ${holdChevronMarkup(open)}
        ${miscStackIconMarkup()}
        <span class="pf-misc-copy">
          <span class="pf-name">杂项</span>
          <span class="pf-sub">${items.length} 项 · 低于折叠阈值</span>
        </span>
        <span class="pf-amount">${money(total)}</span>
      </button>
      ${
        open
          ? `<ul class="pf-misc-list">${items
              .map((item) => {
                const risk = PF_RISK_META[item.risk_level] ? item.risk_level : "";
                return `<li>
                  <button class="pf-misc-line" type="button" data-pf-edit="${escapeHtml(String(item.key || ""))}"
                    aria-label="调整 ${escapeHtml(item.name || "未命名资产")} 的标签">
                    ${risk ? riskInlineMarkup(risk, item.risk_reason, "risk-inline--misc") : `<span class="pf-misc-dash" aria-hidden="true"></span>`}
                    <span class="pf-misc-name">${escapeHtml(item.name || "未命名资产")}</span>
                    <span class="pf-misc-value">${money(item.value_usdt)}</span>
                  </button>
                </li>`;
              })
              .join("")}</ul>`
          : ""
      }
    </li>`;
  }

  function portfolioBodyMarkup() {
    const pf = state.pf;
    if (!pf.structure) return pfSkeletonBody();
    return pf.view.mode === "detail" ? pfDetailBody() : pfOverviewBody();
  }

  function portfolioStructureMarkup() {
    const pf = state.pf;
    if (!pf.structure) {
      const error = pf.error
        ? `<div class="pf-error">
            <p>资产结构暂时无法读取</p>
            <button class="pf-retry" type="button" data-pf-retry>重新读取</button>
          </div>`
        : pfSkeletonBody();
      return `<section class="pf-card" id="pf-section" aria-label="资产结构"${pf.loading ? ' aria-busy="true"' : ""}>
        <div class="pf-head">
          <div class="pf-head-copy">
            <h2 class="pf-title">资产结构</h2>
            <p class="pf-subtitle">按大类与风险档拆解</p>
          </div>
        </div>
        ${error}
      </section>`;
    }
    return `<section class="pf-card" id="pf-section" aria-label="资产结构">
      <div class="pf-head">
        <div class="pf-head-copy">
          <h2 class="pf-title">资产结构</h2>
          <p class="pf-subtitle">按大类与风险档拆解 · 最新净值</p>
        </div>
      </div>
      ${pfTabsMarkup()}
      <div class="pf-body" id="pf-body">${portfolioBodyMarkup()}</div>
    </section>`;
  }

  function renderPortfolioStructure() {
    const host = document.getElementById("pf-section");
    if (!host) return;
    host.outerHTML = portfolioStructureMarkup();
  }

  function syncPortfolioTabs() {
    document.querySelectorAll("[data-pf-tab]").forEach((button) => {
      button.setAttribute("aria-pressed", String(button.getAttribute("data-pf-tab") === state.pf.view.tab));
    });
  }

  function renderPortfolioBody() {
    const body = document.getElementById("pf-body");
    if (!body) return;
    body.innerHTML = portfolioBodyMarkup();
    syncPortfolioTabs();
  }

  function pfCacheKey(view) {
    return `${view.tab}|${view.class || ""}|${view.risk || ""}`;
  }

  function pfLocalItems() {
    const view = state.pf.view;
    let items = Array.isArray(state.pf.allItems) ? state.pf.allItems : [];
    if (view.class) {
      items = items.filter((item) => item.asset_class === view.class);
    } else if (view.tab === "token") {
      items = items.filter((item) => item.asset_class === "cash" || item.asset_class === "crypto");
    } else if (view.tab !== "all") {
      items = items.filter((item) => item.asset_class === view.tab);
    }
    if (view.risk) items = items.filter((item) => item.risk_level === view.risk);
    return items.slice().sort((a, b) => asNumber(b.value_usdt) - asNumber(a.value_usdt));
  }

  function pfItemsSignature(items) {
    return (Array.isArray(items) ? items : [])
      .map((item) => [item.key, item.asset_class, item.risk_level, item.overridden, item.value_usdt].join(":"))
      .join("|");
  }

  function pfRequestUrl(assetClass, risk) {
    const params = new URLSearchParams();
    if (assetClass) params.set("class", assetClass);
    if (risk) params.set("risk", risk);
    const query = params.toString();
    return `/portfolio/items${query ? `?${query}` : ""}`;
  }

  async function fetchPortfolioView(view) {
    const pf = state.pf;
    const key = pfCacheKey(view);
    if (pf.cache.has(key)) return;
    const generation = pf.gen;
    pf.cache.set(key, { pending: true });
    let classes;
    if (view.class) classes = [view.class];
    else if (view.tab === "token") classes = ["cash", "crypto"];
    else if (view.tab === "all") classes = [null];
    else classes = [view.tab];
    try {
      const responses = await Promise.all(classes.map((name) => api(pfRequestUrl(name, view.risk))));
      if (generation !== pf.gen) return;
      const seen = new Set();
      const items = responses
        .flatMap((response) => (Array.isArray(response?.items) ? response.items : []))
        .filter((item) => {
          const itemKey = String(item?.key || "");
          if (!itemKey || seen.has(itemKey)) return false;
          seen.add(itemKey);
          return true;
        })
        .sort((a, b) => asNumber(b.value_usdt) - asNumber(a.value_usdt));
      pf.cache.set(key, { items });
      if (pfViewKey(pf.view) === key && pf.view.mode === "detail") {
        const local = pfLocalItems();
        if (!local.length || pfItemsSignature(local) !== pfItemsSignature(items)) {
          pf.viewItems = items;
          renderPortfolioBody();
        }
      }
    } catch (error) {
      pf.cache.delete(key);
      if (error.status === 401) expireSession();
    }
  }

  function pfViewKey(view) {
    return `${view.tab}|${view.class || ""}|${view.risk || ""}`;
  }

  async function loadPortfolioStructure({ force = false } = {}) {
    const pf = state.pf;
    if (pf.loading && !force) return;
    const seq = ++pf.seq;
    pf.gen += 1;
    pf.loading = true;
    pf.error = null;
    pf.itemsError = null;
    if (!pf.structure) renderPortfolioStructure();
    try {
      const [structureResult, itemsResult] = await Promise.allSettled([
        api("/portfolio/structure"),
        api("/portfolio/items"),
      ]);
      if (seq !== pf.seq) return;
      if (structureResult.status === "fulfilled") {
        pf.structure = structureResult.value;
      } else if (structureResult.reason?.status === 401) {
        expireSession();
        return;
      } else {
        pf.error = structureResult.reason?.message || "资产结构暂时无法读取";
      }
      if (itemsResult.status === "fulfilled") {
        pf.allItems = Array.isArray(itemsResult.value?.items) ? itemsResult.value.items : [];
        pf.cache.clear();
        pf.cache.set("all", { items: pf.allItems });
      } else if (itemsResult.reason?.status === 401) {
        expireSession();
        return;
      } else {
        pf.itemsError = itemsResult.reason?.message || "资产明细暂时无法读取";
      }
      pf.loading = false;
      pf.viewItems = pfLocalItems();
      renderPortfolioStructure();
    } catch (error) {
      if (seq !== pf.seq) return;
      pf.loading = false;
      pf.error = error.message || "资产结构暂时无法读取";
      renderPortfolioStructure();
    }
  }

  function selectPortfolioTab(tab) {
    if (!PF_TAB_META.some(([key]) => key === tab)) return;
    if (!pfTabHasData(tab)) return;
    const pf = state.pf;
    if (pf.view.mode === "overview" && tab === "all") return;
    pf.view = { mode: tab === "all" ? "overview" : "detail", tab, class: null, risk: null };
    pf.viewItems = pfLocalItems();
    renderPortfolioBody();
    if (tab !== "all") fetchPortfolioView({ ...pf.view });
  }

  function selectPortfolioClass(assetClass) {
    if (!PF_CLASS_META[assetClass]) return;
    const tab =
      assetClass === "cash" || assetClass === "crypto"
        ? "token"
        : PF_TAB_META.some(([key]) => key === assetClass)
          ? assetClass
          : "all";
    const pf = state.pf;
    pf.view = { mode: "detail", tab, class: assetClass, risk: null };
    pf.viewItems = pfLocalItems();
    renderPortfolioBody();
    fetchPortfolioView({ ...pf.view });
  }

  function selectPortfolioRisk(risk) {
    if (!PF_RISK_META[risk]) return;
    const pf = state.pf;
    pf.view = { mode: "detail", tab: "all", class: null, risk };
    pf.viewItems = pfLocalItems();
    renderPortfolioBody();
    fetchPortfolioView({ ...pf.view });
  }

  function showPortfolioOverview() {
    const pf = state.pf;
    pf.view = { mode: "overview", tab: "all", class: null, risk: null };
    pf.viewItems = pfLocalItems();
    renderPortfolioBody();
  }

  function togglePortfolioMisc(key) {
    if (state.pf.openMisc.has(key)) state.pf.openMisc.delete(key);
    else state.pf.openMisc.add(key);
    renderPortfolioBody();
  }

  function pfFindItem(key) {
    const match = (state.pf.allItems || []).find((item) => String(item.key) === String(key));
    if (match) return match;
    return (state.pf.viewItems || []).find((item) => String(item.key) === String(key)) || null;
  }

  function pfChoiceMarkup(kind, key, label, selected, color) {
    const isClass = kind === "class";
    const attr = isClass ? "data-pf-class-option" : "data-pf-risk-option";
    return `<button class="pf-choice${isClass ? "" : " pf-choice--risk"}" type="button" ${attr}="${key}"
      style="--pf-color:${color}" aria-pressed="${selected === key}">
      ${isClass ? `<span class="pf-choice-dot" aria-hidden="true"></span>` : pfRiskGlyph(key)}
      <span>${label}</span>
    </button>`;
  }

  function portfolioSheetMarkup(item) {
    const cls = PF_CLASS_META[item.asset_class] || PF_CLASS_META.other;
    const risk = PF_RISK_META[item.risk_level] ? item.risk_level : "medium";
    const reasonLabel = item.overridden ? "当前依据（人工）" : "系统默认依据";
    const reason = String(item.risk_reason || "").trim() || "暂无分类依据。";
    return `<div class="sheet-backdrop" data-pf-sheet-close></div>
      <div class="sheet pf-sheet" role="dialog" aria-modal="true" aria-labelledby="pf-sheet-title" tabindex="-1">
        <div class="sheet-grab" aria-hidden="true"></div>
        <header class="sheet-head">
          <div>
            <p class="sheet-kicker">人工标签</p>
            <h2 id="pf-sheet-title">${escapeHtml(item.name || "未命名资产")}</h2>
            <p class="pf-sheet-sub">${escapeHtml(pfContextLabel(item))} · ${money(item.value_usdt)} USDT</p>
          </div>
          <button class="sheet-close" type="button" data-pf-sheet-close aria-label="关闭标签编辑">关闭</button>
        </header>
        <form class="pf-sheet-form" id="pf-override-form" novalidate>
          <div class="pf-sheet-body">
            <div class="pf-default">
              <span class="pf-default-kicker">${reasonLabel}</span>
              <p>${escapeHtml(reason)}</p>
              ${item.overridden ? `<span class="pf-default-note">恢复默认后按系统规则重新判定</span>` : ""}
            </div>
            <fieldset class="pf-field">
              <legend>资产大类</legend>
              <div class="pf-choice-grid">
                ${PF_CLASS_ORDER.map((key) =>
                  pfChoiceMarkup("class", key, PF_CLASS_META[key].label, item.asset_class, PF_CLASS_META[key].color),
                ).join("")}
              </div>
            </fieldset>
            <fieldset class="pf-field">
              <legend>风险档</legend>
              <div class="pf-choice-row">
                ${PF_RISK_ORDER.map((key) =>
                  pfChoiceMarkup("risk", key, PF_RISK_META[key].label, risk, PF_RISK_META[key].dot),
                ).join("")}
              </div>
            </fieldset>
            <label class="pf-field" for="pf-reason">
              <span class="pf-label">理由 <em>必填</em></span>
              <textarea class="pf-reason" id="pf-reason" name="reason" rows="2" maxlength="200"
                placeholder="例如：短期理财，按低风险处理" required></textarea>
            </label>
            <p class="pf-form-error" id="pf-form-error" role="alert" hidden></p>
          </div>
          <footer class="pf-sheet-foot">
            ${
              item.overridden
                ? `<button class="pf-reset" type="button" data-pf-reset>恢复默认</button>`
                : `<span class="pf-foot-note">仅影响分类与风险</span>`
            }
            <button class="pf-save" type="submit">保存</button>
          </footer>
        </form>
      </div>`;
  }

  function openPortfolioSheet(key, trigger) {
    const item = pfFindItem(key);
    if (!item) return;
    closePortfolioSheet({ restoreFocus: false });
    const root = document.createElement("div");
    root.className = "sheet-root pf-sheet-root";
    root.innerHTML = portfolioSheetMarkup(item);
    document.body.appendChild(root);
    document.body.classList.add("sheet-open");
    const dialog = root.querySelector(".sheet");
    state.pf.sheet = {
      key: String(item.key),
      item,
      root,
      trigger: trigger || null,
      previousFocus: document.activeElement,
      asset_class: item.asset_class || "other",
      risk_level: PF_RISK_META[item.risk_level] ? item.risk_level : "medium",
      saving: false,
    };
    window.requestAnimationFrame(() => {
      root.classList.add("is-open");
      dialog?.focus();
    });
  }

  function closePortfolioSheet({ restoreFocus = true } = {}) {
    const sheet = state.pf.sheet;
    state.pf.sheet = null;
    if (!state.categorySheet) document.body.classList.remove("sheet-open");
    if (!sheet) return;
    sheet.root.remove();
    if (restoreFocus && sheet.trigger && document.contains(sheet.trigger)) sheet.trigger.focus();
  }

  function syncPortfolioSheetChoices() {
    const sheet = state.pf.sheet;
    if (!sheet) return;
    sheet.root.querySelectorAll("[data-pf-class-option]").forEach((button) => {
      button.setAttribute("aria-pressed", String(button.getAttribute("data-pf-class-option") === sheet.asset_class));
    });
    sheet.root.querySelectorAll("[data-pf-risk-option]").forEach((button) => {
      button.setAttribute("aria-pressed", String(button.getAttribute("data-pf-risk-option") === sheet.risk_level));
    });
  }

  function choosePortfolioClass(assetClass) {
    const sheet = state.pf.sheet;
    if (!sheet || !PF_CLASS_META[assetClass]) return;
    sheet.asset_class = assetClass;
    syncPortfolioSheetChoices();
  }

  function choosePortfolioRisk(risk) {
    const sheet = state.pf.sheet;
    if (!sheet || !PF_RISK_META[risk]) return;
    sheet.risk_level = risk;
    syncPortfolioSheetChoices();
  }

  function setPortfolioSheetError(message) {
    const node = state.pf.sheet?.root.querySelector("#pf-form-error");
    if (!node) return;
    node.textContent = message || "";
    node.hidden = !message;
  }

  async function savePortfolioOverride(form) {
    const sheet = state.pf.sheet;
    if (!sheet || sheet.saving) return;
    const reasonField = form.querySelector("#pf-reason");
    const reason = String(reasonField?.value || "").trim();
    if (!reason) {
      setPortfolioSheetError("请填写人工设定的理由。");
      reasonField?.focus();
      return;
    }
    sheet.saving = true;
    setPortfolioSheetError("");
    const button = form.querySelector(".pf-save");
    if (button) {
      button.disabled = true;
      button.textContent = "保存中…";
    }
    try {
      await api("/portfolio/override", {
        method: "PUT",
        body: {
          key: sheet.key,
          asset_class: sheet.asset_class,
          risk_level: sheet.risk_level,
          reason,
        },
      });
      closePortfolioSheet({ restoreFocus: false });
      showToast("已保存人工标签");
      await refreshPortfolioAfterMutation();
    } catch (error) {
      if (error.status === 401) {
        expireSession();
        return;
      }
      sheet.saving = false;
      if (button) {
        button.disabled = false;
        button.textContent = "保存";
      }
      setPortfolioSheetError(error.message || "保存失败，请稍后重试。");
    }
  }

  async function resetPortfolioOverride(button) {
    const sheet = state.pf.sheet;
    if (!sheet || sheet.saving) return;
    sheet.saving = true;
    if (button) {
      button.disabled = true;
      button.textContent = "恢复中…";
    }
    try {
      await api(`/portfolio/override/${encodeURIComponent(sheet.key)}`, { method: "DELETE" });
      closePortfolioSheet({ restoreFocus: false });
      showToast("已恢复系统默认");
      await refreshPortfolioAfterMutation();
    } catch (error) {
      if (error.status === 401) {
        expireSession();
        return;
      }
      sheet.saving = false;
      if (button) {
        button.disabled = false;
        button.textContent = "恢复默认";
      }
      setPortfolioSheetError(error.message || "恢复失败，请稍后重试。");
    }
  }

  async function refreshPortfolioAfterMutation() {
    const pf = state.pf;
    const seq = ++pf.seq;
    pf.gen += 1;
    document.getElementById("pf-body")?.classList.add("is-updating");
    try {
      const [structureResult, itemsResult] = await Promise.allSettled([
        api("/portfolio/structure"),
        api("/portfolio/items"),
      ]);
      if (seq !== pf.seq) return;
      if (structureResult.status === "fulfilled") pf.structure = structureResult.value;
      if (itemsResult.status === "fulfilled") {
        pf.allItems = Array.isArray(itemsResult.value?.items) ? itemsResult.value.items : [];
        pf.cache.clear();
        pf.cache.set("all", { items: pf.allItems });
      }
      pf.viewItems = pfLocalItems();
      pf.loading = false;
      renderPortfolioStructure();
    } catch (error) {
      if (error.status === 401) expireSession();
    }
    refreshOverviewHoldingsCard();
  }

  async function refreshOverviewHoldingsCard() {
    try {
      const data = withWechatBannerMock(await api("/overview"));
      state.overviewData = data;
      state.overviewSeries = normalizeOverviewSeries(data);
      const card = document.querySelector("#overview-content .hold-card");
      if (card) card.outerHTML = holdingsMarkup(data);
    } catch (error) {
      if (error.status === 401) expireSession();
    }
  }

  function holdingsMarkup(data) {
    const rows = normalizeHoldings(data?.holdings);
    const threshold = miscThreshold(data);
    const mode = state.holdingsMode === "source" ? "source" : "asset";
    const body =
      rows.length === 0
        ? `<p class="hold-empty">暂无持仓，同步后显示</p>`
        : mode === "source"
          ? holdingsBySourceMarkup(rows, threshold)
          : holdingsByAssetMarkup(rows, threshold);
    const asof = data?.holdings_asof
      ? `<p class="hold-asof">快照 ${escapeHtml(formatDay(data.holdings_asof))}</p>`
      : "";

    return `
      <section class="hold-card" aria-label="持仓">
        <div class="hold-head">
          <h2 class="hold-title">持仓</h2>
          <div class="hold-seg segmented" role="group" aria-label="持仓视图">
            <button class="segment" type="button" data-holdings-mode="asset"
              aria-pressed="${mode === "asset"}">按资产</button>
            <button class="segment" type="button" data-holdings-mode="source"
              aria-pressed="${mode === "source"}">按来源</button>
          </div>
          <button class="sync-btn sync-btn--quiet hold-sync" type="button" data-sync-run
            aria-label="${state.syncBusy ? "同步中" : "同步持仓"}"
            ${state.syncBusy ? 'disabled aria-busy="true"' : ""}>
            <svg class="hold-sync-icon" viewBox="0 0 16 16" fill="none" aria-hidden="true">
              <path d="M13.2 8a5.2 5.2 0 0 1-8.7 3.8"/>
              <path d="M2.8 8a5.2 5.2 0 0 1 8.7-3.8"/>
              <path d="M10.6 3.2h1.6V1.6"/>
              <path d="M5.4 12.8H3.8v1.6"/>
            </svg>
            <span class="sync-spin" aria-hidden="true"></span>
            <span data-sync-label>${state.syncBusy ? "同步中…" : "同步"}</span>
          </button>
        </div>
        ${body}
        ${asof}
      </section>`;
  }

  function rerenderOverviewHoldings() {
    const content = document.getElementById("overview-content");
    if (!content || !state.overviewData) return;
    content.innerHTML = `<div class="reveal">${overviewMarkup(state.overviewData)}</div>`;
  }

  function withWechatBannerMock(data) {
    if (!WECHAT_BANNER_MOCK || !data || typeof data !== "object") return data;
    return { ...data, ...WECHAT_BANNER_MOCK };
  }

  function wechatAgeTier(age) {
    if (!Number.isFinite(age)) return "unknown";
    if (age < 24) return "lt24";
    if (age < 48) return "24to48";
    return "48plus";
  }

  function wechatBannerMarkup(data) {
    const tokenStale = data && data.wechat_token_stale === true;
    const sendStale = data && data.wechat_send_stale === true;
    if (!tokenStale && !sendStale) {
      state.wechatBannerDismissTier = null;
      state.wechatBannerStaleSeen = false;
      state.wechatBannerTier = null;
      return "";
    }

    let tier = "send";
    let lead = "微信告警暂时发不出去";
    let action = "告警会留在站内，不会丢。";
    if (tokenStale) {
      const rawAge = data.wechat_token_age_hours;
      const age =
        rawAge === null || rawAge === undefined || rawAge === ""
          ? Number.NaN
          : Number(rawAge);
      tier = wechatAgeTier(age);
      lead = Number.isFinite(age)
        ? `微信告警已中断 ${Math.max(0, Math.round(age))} 小时`
        : "微信告警还没连上";
      action = Number.isFinite(age)
        ? "给微信机器人发一句话即可恢复。"
        : "给微信机器人发一句话即可开通。";
    }
    state.wechatBannerTier = tier;
    state.wechatBannerStaleSeen = true;
    if (state.wechatBannerDismissTier === tier) return "";

    // Every stale tier shares the same gold way into settings, so the token
    // banner is never a dead end. data-wechat-cta pins that contract.
    const cta = '<a class="wechat-banner-cta" href="/settings" data-link data-wechat-cta>去设置</a>';
    return `<div class="wechat-banner" role="alert"
        data-wechat-banner data-wechat-tier="${tier}">
      <span class="wechat-banner-icon" aria-hidden="true">${PF_WARN_SVG}</span>
      <p class="wechat-banner-text">
        <span class="wechat-banner-lead">${escapeHtml(lead)}</span>
        <span class="wechat-banner-action">${escapeHtml(action)}</span>
      </p>
      ${cta}
      <button class="wechat-banner-close" type="button" data-wechat-dismiss
        aria-label="关闭微信告警提示">
        <svg viewBox="0 0 12 12" aria-hidden="true">
          <path d="M2 2l8 8M10 2l-8 8"/>
        </svg>
      </button>
    </div>`;
  }

  function updateWechatBanner() {
    const slot = document.querySelector("[data-wechat-banner-slot]");
    if (!slot) return;
    slot.innerHTML = wechatBannerMarkup(state.overviewData);
    updateWechatSettingsCard();
  }

  function wechatCardModel(data) {
    if (!data) return null;
    const tokenStale = data.wechat_token_stale === true;
    const sendStale = data.wechat_send_stale === true;
    const rawAge = data.wechat_token_age_hours;
    const age =
      rawAge === null || rawAge === undefined || rawAge === ""
        ? Number.NaN
        : Number(rawAge);
    let label = "正常";
    let tone = "is-on";
    let summary = "告警和提醒会通过微信机器人送达。";
    if (tokenStale) {
      label = Number.isFinite(age) ? `已失效 ${Math.max(0, Math.round(age))} 小时` : "未连接";
      tone = "is-warn";
      summary = "当前可能收不到微信告警，给微信机器人发一句话即可恢复。";
    } else if (sendStale) {
      label = "发送失败";
      tone = "is-warn";
      summary = "最近一次微信推送没有发出去，告警仍会留在站内。";
    }
    return {
      tokenStale,
      sendStale,
      age,
      label,
      tone,
      summary,
      tokenLabel: tokenStale
        ? Number.isFinite(age)
          ? `已失效 ${Math.max(0, Math.round(age))} 小时`
          : "未连接"
        : "正常",
      sendLabel: sendStale ? "最近发送失败" : "正常",
      since: sendStale && data.wechat_send_since ? data.wechat_send_since : null,
      error: data.wechat_send_error || "",
    };
  }

  function wechatSettingsMarkup() {
    const model = wechatCardModel(state.overviewData);
    if (!model) {
      return `
        <div class="cs-head">
          <span class="cs-mark" aria-hidden="true">${ICONS.bell}</span>
          <div class="cs-title">
            <h2>微信推送</h2>
            <p>正在读取微信状态…</p>
          </div>
        </div>`;
    }
    const open = Boolean(settingsState.wechatDetailsOpen);
    return `
      <div class="cs-head">
        <span class="cs-mark" aria-hidden="true">${ICONS.bell}</span>
        <div class="cs-title">
          <h2>微信推送</h2>
          <p>${escapeHtml(model.summary)}</p>
        </div>
        <span class="cs-chip ${model.tone}"><i aria-hidden="true"></i><b>${escapeHtml(
          model.label,
        )}</b></span>
      </div>
      <div class="wechat-card-actions">
        <button class="wechat-card-toggle" type="button" data-wechat-details
          aria-expanded="${open ? "true" : "false"}" aria-controls="wechat-details">
          ${open ? "收起详情" : "查看详情"}
        </button>
      </div>
      <div class="wechat-details" id="wechat-details" data-wechat-details-panel ${
        open ? "" : "hidden"
      }>
        <dl>
          <div><dt>连接状态</dt><dd>${escapeHtml(model.tokenLabel)}</dd></div>
          <div><dt>发送状态</dt><dd>${escapeHtml(model.sendLabel)}</dd></div>
          <div><dt>失败时间</dt><dd>${escapeHtml(
            model.since ? formatDateTime(model.since) : "—",
          )}</dd></div>
          <div class="wechat-details-error">
            <dt>最近错误</dt>
            <dd><code>${escapeHtml(model.error || "—")}</code></dd>
          </div>
        </dl>
      </div>`;
  }

  function updateWechatSettingsCard() {
    const area = document.getElementById("wechat-area");
    if (!area) return;
    area.innerHTML = wechatSettingsMarkup();
  }

  function updateTopbarAlert() {
    const slot = document.querySelector("[data-alert-slot]");
    if (!slot) return;
    if (!slot.querySelector("[data-notify-bell]")) slot.innerHTML = notificationBellMarkup();
    updateNotifyBadges();
  }

  function refreshWechatBanner() {
    if (wechatBannerRequest || state.overviewData || !state.token) return;
    wechatBannerRequest = api("/overview")
      .then((data) => {
        if (!state.token) return;
        state.overviewData = withWechatBannerMock(data);
        updateWechatBanner();
        updateRailStatus();
      })
      .catch((error) => {
        if (error.status === 401) expireSession();
      })
      .finally(() => {
        wechatBannerRequest = null;
      });
  }

  function metricTone(value) {
    if (!Number.isFinite(value) || value === 0) return "";
    return value > 0 ? "metric-value--up" : "metric-value--down";
  }

  function overviewMetricsMarkup() {
    const metrics = state.overviewMetrics;
    const total = asNumber(state.overviewData?.current_nav_usdt);
    const monthAmount = metrics?.monthReturn;
    const health = metrics?.minHealth;
    const monthReady = Number.isFinite(monthAmount);
    const healthReady = Number.isFinite(health);

    return `<section class="ov-metrics" id="overview-metrics" aria-label="关键指标">
      <div class="metric-tile">
        <span class="metric-label">总资产</span>
        <span class="metric-value">${money(total)}</span>
        <span class="metric-note">USDT</span>
      </div>
      <div class="metric-tile">
        <span class="metric-label">近一月收益</span>
        <span class="metric-value ${monthReady ? metricTone(monthAmount) : ""}">${
          monthReady ? signedMoney(monthAmount) : "—"
        }</span>
        <span class="metric-note">${metrics ? (monthReady ? "时间加权" : "暂无数据") : "读取中"}</span>
      </div>
      <div class="metric-tile">
        <span class="metric-label">最低健康度</span>
        <span class="metric-value">${healthReady ? health.toFixed(2) : "—"}</span>
        <span class="metric-note">${metrics ? (healthReady ? "借贷账户" : "暂无借贷仓位") : "读取中"}</span>
      </div>
    </section>`;
  }

  async function loadOverviewMetrics(seq) {
    const [returnsResult, lendingResult] = await Promise.allSettled([
      api("/returns?period=month"),
      api("/lending/summary"),
    ]);
    if (seq !== state.renderSeq) return;

    const metrics = { monthReturn: null, minHealth: null };
    const amount = returnsResult.status === "fulfilled" ? returnsResult.value?.return_amount_usdt : null;
    if (amount !== null && amount !== undefined && Number.isFinite(Number(amount))) {
      metrics.monthReturn = Number(amount);
    }
    const health =
      lendingResult.status === "fulfilled" ? lendingResult.value?.min_health_factor : null;
    if (health !== null && health !== undefined && Number.isFinite(Number(health))) {
      metrics.minHealth = Number(health);
    }
    state.overviewMetrics = metrics;
    const node = document.getElementById("overview-metrics");
    if (node) node.outerHTML = overviewMetricsMarkup();
  }

  function overviewMarkup(data) {
    const series = normalizeOverviewSeries(data);

    const current = asNumber(data?.current_nav_usdt);
    const previous = series.length > 1 ? series[series.length - 2].nav_usdt : null;
    const latestPoint = series[series.length - 1];
    const rate =
      previous !== null && previous !== 0
        ? (current - previous) / previous
        : Number.NaN;
    const asofRaw = data?.latest_snapshot_ts || latestPoint?.ts;
    const asofFull = asofRaw
      ? `数据截至 ${formatDay(asofRaw)} · 同步估值`
      : "等待第一份净值快照";
    const asofShort = asofRaw
      ? `收益已剔除资金流入 · 截至 ${formatShortDate(asofRaw)}`
      : "等待第一份净值快照";

    return `
      <div class="ov-grid">
        <div class="ov-main">
          <section class="hero">
            <div class="hero-head">
              <span class="eyebrow">当前净值</span>
              ${pageDemoSlotMarkup()}
            </div>
            <div class="hero-value">
              <span class="hero-number">${money(current)}</span>
              <span class="hero-currency">USDT</span>
            </div>
            <div class="hero-meta">
              ${deltaMarkup(rate)}
              <span class="delta-label">近一日</span>
            </div>
            <div class="hero-foot">
              <p class="hero-foot-text">${escapeHtml(asofShort)}</p>
              <details class="hero-info">
                <summary aria-label="查看净值说明">i</summary>
                <div class="hero-info-pop">
                  <p>收益已剔除资金流入，同步补全产生的跳变不计入。</p>
                  <p>${escapeHtml(asofFull)}</p>
                </div>
              </details>
            </div>
          </section>

          <section class="chart-card">
            <div class="chart-head">
              <h2 class="chart-title">净值曲线</h2>
              <span class="chart-badge">${chartBadge(data?.nav_source)}</span>
            </div>
            ${chartMarkup(series, { tracking: Boolean(data?.returns_start) })}
          </section>

          ${overviewMetricsMarkup()}
        </div>

        <div class="ov-wide">
          ${portfolioStructureMarkup()}
        </div>

        <aside class="ov-side">
          ${holdingsMarkup(data)}
        </aside>
      </div>`;
  }

  async function loadOverview(seq) {
    const content = document.getElementById("overview-content");
    if (!content) return;

    try {
      const data = withWechatBannerMock(await api("/overview"));
      if (seq !== state.renderSeq) return;
      state.overviewSeries = normalizeOverviewSeries(data);
      state.overviewData = data;
      content.innerHTML = `<div class="reveal">${overviewMarkup(data)}</div>`;
      updateWechatBanner();
      updateRailStatus();
      refreshNotifySnapshot().then(() => {
        if (seq === state.renderSeq) maybeShowFlowToast();
      });
      loadOverviewMetrics(seq);
    } catch (error) {
      if (error.status === 401) {
        expireSession();
        return;
      }
      if (seq !== state.renderSeq) return;
      content.innerHTML = retryMarkup("overview");
    }
  }

  function renderOverviewPage(seq) {
    setTitle("总览");
    renderShell(
      `${topbarMarkup()}<div id="overview-content">${overviewSkeleton()}</div>`,
      "overview",
    );
    loadOverview(seq);
    loadPortfolioStructure();
  }

  /* ---------- Returns ---------- */

  function returnsSkeleton() {
    return `
      <div aria-hidden="true">
        <div class="skeleton sk-return-number"></div>
        <div class="skeleton sk-return-rate"></div>
        <div class="sk-nav-pair">
          <div class="skeleton sk-nav"></div>
          <div class="skeleton sk-nav"></div>
        </div>
        <div class="skeleton sk-spark"></div>
      </div>`;
  }

  function sliceOverviewByPeriod(series, period) {
    if (!series?.length) return [];
    const lastTime = dateObject(series[series.length - 1].ts)?.getTime();
    if (lastTime == null) return series.slice();
    const cutoff = lastTime - PERIOD_DAYS[period] * 24 * 60 * 60 * 1000;
    let baselineIndex = 0;
    series.forEach((point, index) => {
      const time = dateObject(point.ts)?.getTime();
      if (time != null && time <= cutoff) baselineIndex = index;
    });
    return series.slice(baselineIndex);
  }

  function navBounds(overviewSeries, returnSeries) {
    const timed = (overviewSeries || [])
      .map((point) => ({
        nav: point.nav_usdt,
        time: dateObject(point.ts)?.getTime(),
      }))
      .filter((point) => point.time != null);

    if (!timed.length) return { start: null, end: null };

    if (returnSeries?.length) {
      const nearest = (time) =>
        timed.reduce((best, point) =>
          Math.abs(point.time - time) < Math.abs(best.time - time) ? point : best,
        );
      const startTime = dateObject(returnSeries[0].ts)?.getTime();
      const endTime = dateObject(returnSeries[returnSeries.length - 1].ts)?.getTime();
      return {
        start: startTime == null ? null : nearest(startTime).nav,
        end: endTime == null ? null : nearest(endTime).nav,
      };
    }

    const sliced = sliceOverviewByPeriod(overviewSeries, state.period);
    if (!sliced.length) return { start: null, end: null };
    return {
      start: sliced[0].nav_usdt,
      end: sliced[sliced.length - 1].nav_usdt,
    };
  }

  function buildTrend(returnSeries, overviewSeries) {
    if (returnSeries.length >= 2) {
      return {
        kind: "rate",
        points: returnSeries.map((point) => ({
          ts: point.ts,
          value: point.return_rate,
        })),
      };
    }

    const sliced = sliceOverviewByPeriod(overviewSeries, state.period);
    return {
      kind: "nav",
      points: sliced.map((point) => ({
        ts: point.ts,
        value: point.nav_usdt,
      })),
    };
  }

  function sparklineMarkup(trend) {
    const values = trend.points.map((point) => point.value);
    if (values.length < 2) {
      return `<p class="spark-empty">再积累一个估值点，走势就会出现</p>`;
    }

    const width = 440;
    const height = 96;
    const padX = 12;
    const padY = 16;
    let min = Math.min(...values);
    let max = Math.max(...values);

    if (max - min < 1e-9) {
      const pad = Math.max(Math.abs(max) * 0.02, trend.kind === "nav" ? 1 : 0.002);
      min -= pad;
      max += pad;
    } else {
      const pad = (max - min) * 0.18;
      min -= pad;
      max += pad;
    }

    const span = max - min || 1;
    const xFor = (index) => padX + (index / (values.length - 1)) * (width - padX * 2);
    const yFor = (value) => padY + ((max - value) / span) * (height - padY * 2);
    const coords = values.map((value, index) => [xFor(index), yFor(value)]);
    const poly = coords.map(([x, y]) => `${x.toFixed(2)},${y.toFixed(2)}`).join(" ");
    const baseline = (height - 2).toFixed(2);
    const area = `${coords[0][0].toFixed(2)},${baseline} ${poly} ${coords[coords.length - 1][0].toFixed(2)},${baseline}`;
    const last = coords[coords.length - 1];
    const yZero = yFor(0);
    const showZero = trend.kind === "rate" && yZero > padY && yZero < height - padY;
    const endPoint = trend.points[trend.points.length - 1];
    const aria =
      trend.kind === "nav"
        ? `区间净值走势，从 ${money(values[0])} USDT 到 ${money(values[values.length - 1])} USDT`
        : `累计收益率走势，从 ${signedPercent(values[0])} 到 ${signedPercent(values[values.length - 1])}`;

    return `
      <svg class="spark-svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="${escapeHtml(aria)}">
        ${
          showZero
            ? `<line class="spark-zero" x1="${padX}" y1="${yZero.toFixed(2)}" x2="${width - padX}" y2="${yZero.toFixed(2)}"/>`
            : ""
        }
        <polygon class="spark-area" points="${area}"/>
        <polyline class="spark-line" pathLength="1" points="${poly}" fill="none"/>
        <circle class="spark-dot-halo" cx="${last[0].toFixed(2)}" cy="${last[1].toFixed(2)}" r="7"/>
        <circle class="spark-dot" cx="${last[0].toFixed(2)}" cy="${last[1].toFixed(2)}" r="3.2"/>
      </svg>
      <div class="spark-foot">
        <span>${formatShortDateSH(trend.points[0].ts)}</span>
        <span>${formatShortDateSH(endPoint.ts)}</span>
      </div>`;
  }

  function navTile(label, value, ts) {
    return `
      <div class="return-nav">
        <span class="return-nav-label">${label}</span>
        <strong class="return-nav-value">${value == null ? "—" : money(value)}</strong>
        <span class="return-nav-meta">${value == null ? "暂无净值" : "USDT"} · ${formatDateSH(ts)}</span>
      </div>`;
  }

  // Relative drawdown uses the wealth index: in rate mode values are
  // cumulative returns, so the peak level is (1 + value), not the value itself.
  function returnsDrawdown(points, rateMode) {
    let peak = null;
    let drawdown = null;
    for (const point of points) {
      if (!peak || point.value > peak.value) peak = point;
      const gap = point.value - peak.value;
      if (drawdown == null || gap < drawdown.gap) {
        const denominator = rateMode ? 1 + peak.value : peak.value;
        drawdown = {
          ...point,
          gap,
          peak: peak.value,
          rate: denominator > 0 ? gap / denominator : 0,
        };
      }
    }
    return drawdown;
  }

  function returnsSideMarkup(data, series, trend) {
    const ratePoints = series
      .map((point) => ({ ts: point.ts, value: asNumber(point.return_rate) }))
      .filter((point) => Number.isFinite(point.value));
    const useRate = ratePoints.length >= 2;
    const points = useRate
      ? ratePoints
      : trend.points
          .map((point) => ({ ts: point.ts, value: asNumber(point.value) }))
          .filter((point) => Number.isFinite(point.value));
    const firstPoint = points[0];
    const lastPoint = points[points.length - 1];
    const coverage =
      firstPoint && lastPoint
        ? Math.max(1, Math.round((dateObject(lastPoint.ts) - dateObject(firstPoint.ts)) / 86400000))
        : null;

    let high = null;
    let low = null;
    for (const point of points) {
      if (!high || point.value > high.value) high = point;
      if (!low || point.value < low.value) low = point;
    }

    const rateMode = useRate && trend.kind !== "nav";
    const drawdown = returnsDrawdown(points, rateMode);
    const valueText = (value) => (rateMode ? signedPercent(value) : money(value));
    const valueTone = (value) => {
      if (!rateMode) return "";
      if (value > 0.0000001) return "is-positive";
      if (value < -0.0000001) return "is-negative";
      return "is-flat";
    };
    const source =
      data?.nav_source === "demo" || data?.nav_source === "fixture" || data?.nav_source === "seed"
        ? "示例曲线"
        : data?.nav_source === "live" || data?.nav_source === "sync"
          ? "净值快照"
          : data?.nav_source === "backfill"
            ? "历史回填"
            : "—";
    const latest = data?.latest_snapshot_ts || lastPoint?.ts;

    return `
      <aside class="returns-side" aria-label="区间剖面">
        <section class="side-card">
          <p class="side-kicker">区间剖面</p>
          <dl class="side-facts">
            <div><dt>估值点</dt><dd>${trend.points.length >= 2 ? trend.points.length : "—"}</dd></div>
            <div><dt>覆盖</dt><dd>${coverage == null ? "—" : `${coverage} 天`}</dd></div>
            ${
              high && low
                ? `<div><dt>区间高点</dt><dd class="${valueTone(high.value)}">${valueText(high.value)}<small>${formatDateSH(
                    high.ts,
                  )}</small></dd></div>
                   <div><dt>区间低点</dt><dd class="${valueTone(low.value)}">${valueText(low.value)}<small>${formatDateSH(
                     low.ts,
                   )}</small></dd></div>`
                : ""
            }
            ${
              drawdown && drawdown.rate < -0.0000001
                ? `<div><dt>最大回撤</dt><dd class="is-negative">${signedPercent(
                    drawdown.rate,
                  )}<small>${formatDateSH(drawdown.ts)}</small></dd></div>`
                : ""
            }
          </dl>
        </section>
        <section class="side-card">
          <p class="side-kicker">读数口径</p>
          <p class="side-copy">时间加权收益率，剔除区间内的充值、提现、转账，以及新增数据源、持仓口径切换带来的净值跳变，只保留资产本身的市场涨跌。</p>
          <ul class="side-list side-list--readout">
            <li>起点与终点取自同一净值序列</li>
            <li>曲线仅用真实快照，不足两点不绘制</li>
            <li>空值显示为 —，不做估算</li>
          </ul>
        </section>
        <p class="side-foot">${source} · 最新 ${formatDateSH(latest)}</p>
      </aside>`;
  }

  function returnsMarkup(data, overviewSeries, breakdown) {
    const series = (Array.isArray(data?.series) ? data.series : [])
      .map((point) => ({
        ts: point?.ts,
        return_rate: asNumber(point?.return_rate),
      }))
      .filter((point) => dateObject(point.ts));
    const rate = asNumber(data?.return_rate);
    const amount = asNumber(data?.return_amount_usdt);
    const tone = Math.abs(rate) < 0.00005 ? "is-flat" : toneClass(rate);
    const nav = navBounds(overviewSeries, series);
    const trend = buildTrend(series, overviewSeries);
    const first = series[0]?.ts || trend.points[0]?.ts;
    const last = series[series.length - 1]?.ts || trend.points[trend.points.length - 1]?.ts;

    if (!series.length && trend.points.length < 2) {
      return `
        <div class="empty-state">
          <span class="empty-glyph">${ICONS.returns}</span>
          <h2>快照不足</h2>
          <p>再积累一个估值点即可看出收益。</p>
        </div>`;
    }

    const rateMissing = data?.return_rate == null || data?.return_rate === "";
    const amountMissing = data?.return_amount_usdt == null || data?.return_amount_usdt === "";
    const excludedLabel = excludedFlowLabel(
      data?.excluded_flow_kind,
      data?.excluded_flow_usdt,
    );
    // The trend card no longer carries a "示例曲线" suffix (523e7c4 merged it
    // into the hero); the page-level demo slot below still marks fixture/demo
    // data from nav_source, not from the user id.
    return `
      <div class="returns-grid">
        <div class="returns-main">
          <section class="return-hero">
            <div class="return-hero-top">
              <div class="return-hero-copy">
                <span class="eyebrow">${escapeHtml(PERIOD_TITLES[state.period] || "区间")} · 时间加权</span>
                <p class="return-amount ${amountMissing || Math.abs(amount) < 0.005 ? "is-flat" : tone}">
                  ${amountMissing ? "—" : signedMoney(amount)}<span>USDT</span>
                </p>
                ${
                  excludedLabel
                    ? `<p class="return-excluded">区间内${excludedLabel} ${signedMoney(data?.excluded_flow_usdt)}（已剔除）</p>`
                    : ""
                }
                <div class="return-rate-row">
                  ${
                    rateMissing
                      ? `<span class="rate-large is-flat is-empty">—</span>`
                      : `<span class="rate-large ${tone}">${signedPercent(rate)}</span>`
                  }
                  <span class="rate-caption">时间加权收益率</span>
                </div>
              </div>
              <div class="return-navs">
                ${navTile("区间起净值", nav.start, first)}
                ${navTile("区间止净值", nav.end, last)}
              </div>
            </div>
            <div class="return-trend">
              ${sparklineMarkup(trend)}
              ${
                trend.kind === "nav"
                  ? `<p class="spark-note">收益序列不足，走势改用这一区间的净值。</p>`
                  : ""
              }
            </div>
          </section>
          <section class="returns-breakdown" data-returns-breakdown></section>
        </div>
        ${returnsSideMarkup(data, series, trend)}
      </div>`;
  }

  async function loadReturns(seq) {
    const content = document.getElementById("returns-content");
    if (!content) return;

    content.innerHTML = returnsSkeleton();

    const overviewTask = state.overviewSeries
      ? Promise.resolve(state.overviewSeries)
      : api("/overview")
          .then((overview) => {
            const series = normalizeOverviewSeries(overview);
            state.overviewSeries = series;
            return series;
          })
          .catch((error) => {
            if (error.status === 401) throw error;
            return [];
          });

    try {
      const [data, overviewSeries, breakdown] = await Promise.all([
        api(`/returns?period=${encodeURIComponent(state.period)}`),
        overviewTask,
        api(`/returns/breakdown?period=${encodeURIComponent(state.period)}`).catch((error) => {
          if (error.status === 401) throw error;
          return null;
        }),
      ]);
      if (seq !== state.renderSeq) return;
      content.innerHTML = `<div class="reveal">${returnsMarkup(data, overviewSeries, breakdown)}</div>`;
      const breakdownRoot = content.querySelector("[data-returns-breakdown]");
      if (breakdownRoot && window.ReturnsBreakdown) {
        window.ReturnsBreakdown.mount(breakdownRoot, breakdown, state.period);
      }
      // A fixture/demo curve can belong to a non-demo account (for example a
      // user with no wallet). Mark the page from the data, not the user id.
      syncPageDemoSlot({
        mock:
          data?.nav_source === "demo" ||
          data?.nav_source === "fixture" ||
          data?.nav_source === "seed",
      });
    } catch (error) {
      if (error.status === 401) {
        expireSession();
        return;
      }
      if (seq !== state.renderSeq) return;
      content.innerHTML = retryMarkup("returns");
    }
  }

  function renderReturnsPage(seq) {
    setTitle("收益");
    renderShell(
      `${topbarMarkup()}
      ${pageHead("收益表现", "收益", "区间随最新净值滚动")}
      ${segmentedMarkup()}
      <div class="return-content" id="returns-content">${returnsSkeleton()}</div>`,
      "returns",
    );
    loadReturns(seq);
  }

  /* ---------- Pending ---------- */

  // Read-only backend classification (GET /api/pending). Missing fields on
  // cached/historical rows simply render nothing, never a guessed value.
  // Sentences that carry no identification are hidden: the confirmed queue is
  // mostly plain inflows, and repeating the same two default lines on every
  // card is noise rather than information.
  const GENERIC_EXPLAIN = new Set(["流入记录，未发现资金外流"]);

  function isKnownCategory(value) {
    return typeof value === "string" && Object.prototype.hasOwnProperty.call(CATEGORY_LABELS, value);
  }

  function categoryLabel(value) {
    return isKnownCategory(value) ? CATEGORY_LABELS[value] : "未分类";
  }

  function categoryToneClass(value) {
    return CATEGORY_TONES[value] || "";
  }

  const ZERO_ADDRESS = "0x0000000000000000000000000000000000000000";

  // p11g sends structured counterparty fields, but the address still travels
  // inside the human-readable detail line on older rows. Prefer an explicit
  // field when one exists, then fall back to the first 20-byte EVM address.
  function counterpartyAddressOf(item) {
    const explicit = String(item?.counterparty_address || "").trim();
    if (/^0x[0-9a-fA-F]{40}$/.test(explicit)) return explicit;
    const detail = String(item?.detail || "");
    const match = detail.match(/0x[0-9a-fA-F]{40}/);
    return match ? match[0] : "";
  }

  function shortAddress(address) {
    const text = String(address || "").trim();
    if (!/^0x[0-9a-fA-F]{40}$/.test(text)) return text;
    return `${text.slice(0, 6)}…${text.slice(-4)}`;
  }

  // The label source is not a headline: it tells the reader how much to trust
  // the name, in the same quiet line as the chain.
  function counterpartySourceLabel(source) {
    const key = String(source || "").trim().toLowerCase();
    if (!key) return "";
    if (key === "manual") return "手动标签";
    if (key === "rule") return "规则标签";
    if (key === "fallback") return "未验证标签";
    if (key.endsWith("+known_owner") || key === "known_program" || key === "known_owner") {
      return "按所属程序识别";
    }
    if (key === "tx_program") return "按调用程序识别";
    if (
      key === "ens" ||
      key === "sourcify" ||
      key === "routescan" ||
      key === "zerion" ||
      key === "blockscout" ||
      key.startsWith("blockscout.") ||
      key.startsWith("rpc") ||
      key.startsWith("helius")
    ) {
      return "链上标签";
    }
    // Unknown source: hide it entirely; never surface internal ids.
    return "";
  }

  function counterpartyKindLabel(item) {
    const kind = String(item?.counterparty_kind || "").trim().toLowerCase();
    const address = counterpartyAddressOf(item);
    if (kind === "zero" || address.toLowerCase() === ZERO_ADDRESS) return "零地址(销毁/铸造)";
    if (kind === "contract") return "未识别合约";
    if (kind === "eoa") return "个人地址";
    return "未识别地址";
  }

  function counterpartyMarkup(item) {
    const label = String(item?.counterparty_label || "").trim();
    const kind = String(item?.counterparty_kind || "").trim().toLowerCase();
    const address = counterpartyAddressOf(item);
    const zero = kind === "zero" || address.toLowerCase() === ZERO_ADDRESS;
    if (!label && !address && !kind) return "";
    const unknown = !label;
    const chipLabel = label || counterpartyKindLabel(item);
    const chain = chainLabel(item?.counterparty_chain);
    const source = label ? counterpartySourceLabel(item?.counterparty_label_source) : "";
    const meta = [
      address && !zero
        ? `<code class="party-address">${escapeHtml(shortAddress(address))}</code>`
        : "",
      chain ? `<span class="party-chain">${escapeHtml(chain)}</span>` : "",
      source ? `<span class="party-source">${escapeHtml(source)}</span>` : "",
    ].filter(Boolean);
    const asset = String(item?.protocol_deposit_asset || "").trim();
    const protocolChip =
      item?.protocol_deposit === true
        ? `<span class="party-protocol">协议存入${asset ? ` · 收到 <strong>${escapeHtml(asset)}</strong>` : ""}</span>`
        : "";
    // A flat tint, not an outlined button: the chip is read-only.
    return `
      <div class="info-row info-row--party">
        <span class="info-kicker">对方</span>
        <span class="party-stack">
          <span class="party-chip${unknown ? " is-unknown" : ""}${zero ? " is-zero" : ""}">
            <span class="party-label">${escapeHtml(chipLabel)}</span>
            ${meta.length ? `<span class="party-meta">${meta.join('<span class="party-sep" aria-hidden="true">·</span>')}</span>` : ""}
          </span>
          ${protocolChip}
        </span>
      </div>`;
  }

  // `explain` is now a full sentence generated by the backend
  // ("向 3Jane 存入 2,666.32 USDC，同时收到 2,307.38 USD3 凭证"). Older meta
  // rows may still start with the counterparty label; strip only that prefix
  // so the second line never repeats the chip above it.
  function explainBodyOf(item) {
    let explain = String(item?.explain || "").trim();
    if (!explain) return "";
    const label = String(item?.counterparty_label || "").trim();
    if (label && explain.startsWith(label)) {
      explain = explain.slice(label.length).replace(/^[\s·:：,，;；-]+/, "").trim();
    }
    return explain;
  }

  function explainMarkup(item) {
    const safety = typeof item?.safety === "string" ? item.safety : "";
    // Unclassified rows with no risk statement stay quiet; only an identified
    // result or a real risk flag earns the "这是什么" line.
    if (safety !== "safe" && safety !== "review") return "";
    const body = explainBodyOf(item);
    if (!body || GENERIC_EXPLAIN.has(body)) return "";
    return `
      <div class="info-row info-row--explain">
        <span class="info-kicker">这是什么</span>
        <span class="explain-text">${displayText(body)}</span>
      </div>`;
  }

  // Risk stays text + a small line icon, never a coloured pill: red/green are
  // reserved for P&L and health, amber only marks the one row that needs a
  // human look. `none` (confirmed inflow) deliberately renders nothing.
  function safetyMarkup(item) {
    const safety = typeof item?.safety === "string" ? item.safety : "";
    if (safety === "safe") {
      return `<span class="safety-note safety-note--safe" data-safety-badge="safe">${ICONS.safety}<span>已识别 · 非外流</span></span>`;
    }
    if (safety === "review") {
      return `<span class="safety-note safety-note--review" data-safety-badge="review">${ICONS.warn}<span>请核对</span></span>`;
    }
    return "";
  }

  // The nature row is a status line, not a second category label. The title and
  // the category pill already name the nature ("协议存入"), so repeating it
  // here only crowds the card. Neutral "safe" gets a grey check, "review" gets
  // the single amber flag; `none`/missing closes the row entirely.
  function statusMarkup(item) {
    const safety = typeof item?.safety === "string" ? item.safety : "";
    if (safety !== "safe" && safety !== "review") return "";
    const nature = typeof item?.nature === "string" ? item.nature : "";
    return `
      <div class="info-row info-row--nature"${
        nature ? ` data-nature="${escapeHtml(nature)}"` : ""
      } data-safety="${escapeHtml(safety)}">
        <span class="info-kicker">状态</span>
        <span class="nature-line">${safetyMarkup(item)}</span>
      </div>`;
  }

  // Provenance mark for example rows. It only appears on a genuinely mixed
  // page (real rows + mock rows); when the whole page is example data the
  // page-level chip already says so and the per-card label would just repeat
  // it. It belongs to the card header, never to the status line, so it cannot
  // crowd the amber "请核对" flag.
  function demoTagMarkup(item) {
    if (item?.nature !== "test_data" || !state.pendingMixed) return "";
    return '<span class="nature-demo-tag">示例</span>';
  }

  function cardNatureAttrs(item) {
    const nature = typeof item?.nature === "string" ? item.nature : "";
    const safety = typeof item?.safety === "string" ? item.safety : "";
    return `${nature ? ` data-nature="${escapeHtml(nature)}"` : ""}${
      safety ? ` data-safety="${escapeHtml(safety)}"` : ""
    }`;
  }

  // The counterparty pill and the one-sentence answer share one tight block:
  // label column aligned, 6px between rows, 12px of air above and below.
  function infoMarkup(item) {
    const party = counterpartyMarkup(item);
    const status = statusMarkup(item);
    const explain = explainMarkup(item);
    if (!party && !status && !explain) return "";
    return `<div class="pending-info">${party}${status}${explain}</div>`;
  }

  function suggestedCategoryOf(item) {
    return isKnownCategory(item?.suggested_category) ? item.suggested_category : null;
  }

  function selectedCategoryOf(item) {
    const id = Number(item?.id);
    if (state.pendingChoices.has(id)) return state.pendingChoices.get(id);
    return suggestedCategoryOf(item);
  }

  function confidenceParts(value) {
    if (value === null || value === undefined || value === "") return null;
    const number = Number(value);
    if (!Number.isFinite(number)) return null;
    const clamped = Math.min(1, Math.max(0, number));
    const percent = Math.round(clamped * 100);
    const band = clamped >= 0.8 ? "high" : clamped >= 0.5 ? "mid" : "low";
    const bandLabel = band === "high" ? "高" : band === "mid" ? "建议核对" : "建议核对";
    return { percent, band, bandLabel, width: Math.max(8, percent) };
  }

  // Display-only. Titles show whole-unit amounts at 2 decimals; the full
  // figure lives in the expanded detail. Sub-unit token amounts keep their
  // precision, since "0.00" would hide the transaction.
  function shortenDecimals(value) {
    return String(value ?? "").replace(/\d+\.\d{3,}/g, (token) => {
      const [whole, fraction] = token.split(".");
      if (Math.abs(Number(whole)) < 1) return token;
      return `${whole}.${fraction.slice(0, 2)}`;
    });
  }

  function displayTitle(value) {
    return shortenDecimals(displayText(value));
  }

  function quantityLine(item) {
    const asset = String(item?.asset || "").trim();
    const raw = item?.amount_raw;
    if (raw !== null && raw !== undefined && raw !== "" && Number.isFinite(Number(raw))) {
      const figure = String(raw);
      return asset ? `数量 ${figure} ${asset}` : `数量 ${figure}`;
    }
    const title = String(item?.title || "");
    const match = title.match(/(\d+\.\d{5,})(?:\s+(\S+))?\s*$/);
    if (!match) return "";
    return match[2] ? `数量 ${match[1]} ${match[2]}` : `数量 ${match[1]}`;
  }

  function formatDateTime(value) {
    const date = dateObject(value);
    if (!date) return "—";
    const hours = String(date.getUTCHours()).padStart(2, "0");
    const minutes = String(date.getUTCMinutes()).padStart(2, "0");
    return `${formatDate(value)} ${hours}:${minutes}`;
  }

  // Compact stamp for the mobile "more" drawer: MM/DD HH:MM, UTC like the rest
  // of the static calendar helpers.
  function formatMonthDayTime(value) {
    const date = dateObject(value);
    if (!date) return "—";
    const month = String(date.getUTCMonth() + 1).padStart(2, "0");
    const day = String(date.getUTCDate()).padStart(2, "0");
    const hours = String(date.getUTCHours()).padStart(2, "0");
    const minutes = String(date.getUTCMinutes()).padStart(2, "0");
    return `${month}/${day} ${hours}:${minutes}`;
  }

  // Chain/execution time (item.ts) is shown in Beijing time, unlike the UTC
  // calendar helpers above. A zone-less ISO string is parsed as UTC so the
  // displayed instant never depends on the viewer's device timezone.
  function parseOccurredAt(value) {
    if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value;
    if (value === null || value === undefined) return null;
    let text = String(value).trim();
    if (!text) return null;
    if (/^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?$/.test(text)) {
      text = `${text.replace(" ", "T")}Z`;
    }
    const date = new Date(text);
    return Number.isNaN(date.getTime()) ? null : date;
  }

  // Card-only timestamp shape: YYYY-MM-DD [HH:MM]. 发生于/发现于/归档于 all
  // go through this helper so the date separators match site-wide. Beijing
  // mode shifts the instant +8h before reading UTC fields (no DST in
  // Asia/Shanghai). Do not replace formatDate/formatDateTime: the lending and
  // earnings pages still use their dotted UTC output.
  function formatCardDate(value, withTime = false, beijing = false) {
    const parsed = value instanceof Date ? value : parseOccurredAt(value);
    if (!parsed || Number.isNaN(parsed.getTime())) return "—";
    const date = beijing
      ? new Date(parsed.getTime() + 8 * 60 * 60 * 1000)
      : parsed;
    const month = String(date.getUTCMonth() + 1).padStart(2, "0");
    const day = String(date.getUTCDate()).padStart(2, "0");
    const dayText = `${date.getUTCFullYear()}-${month}-${day}`;
    if (!withTime) return dayText;
    const hours = String(date.getUTCHours()).padStart(2, "0");
    const minutes = String(date.getUTCMinutes()).padStart(2, "0");
    return `${dayText} ${hours}:${minutes}`;
  }

  function formatOccurredAt(value) {
    const date = parseOccurredAt(value);
    if (!date) return "";
    return formatCardDate(date, true, true);
  }

  function occurredMarkup(item) {
    const date = parseOccurredAt(item.ts);
    if (!date) return "";
    const days = Math.floor((Date.now() - date.getTime()) / 86400000);
    const ago = days > 7 ? `<span class="pending-ago">${days} 天前</span>` : "";
    return `
      <p class="pending-occurred">
        <span>发生于 <time datetime="${date.toISOString()}">${formatOccurredAt(date)}</time> · 北京时间</span>
        ${ago}
      </p>`;
  }

  function pendingSkeleton() {
    return `
      <div class="pending-grid" aria-hidden="true">
        <div class="pending-main">
          <div class="skeleton sk-pending-summary"></div>
          <div class="pending-skeleton-board">
            <div class="sk-pending-row">
              <span class="skeleton sk-line sk-line--chip"></span>
              <span class="skeleton sk-line sk-line--title"></span>
              <span class="skeleton sk-line sk-line--meta"></span>
              <span class="skeleton sk-line sk-line--body"></span>
              <span class="skeleton sk-line sk-line--body sk-line--body-short"></span>
              <span class="skeleton sk-line sk-line--action"></span>
            </div>
            <div class="sk-pending-row">
              <span class="skeleton sk-line sk-line--chip"></span>
              <span class="skeleton sk-line sk-line--title"></span>
              <span class="skeleton sk-line sk-line--meta"></span>
              <span class="skeleton sk-line sk-line--body"></span>
              <span class="skeleton sk-line sk-line--body sk-line--body-short"></span>
              <span class="skeleton sk-line sk-line--action"></span>
            </div>
          </div>
        </div>
        <div class="pending-side pending-side--skeleton">
          <div class="sk-pending-side-inner">
            <span class="skeleton sk-line sk-line--kicker"></span>
            <span class="skeleton sk-line sk-line--count"></span>
            <span class="skeleton sk-line sk-line--fact"></span>
            <span class="skeleton sk-line sk-line--fact"></span>
            <span class="skeleton sk-line sk-line--note"></span>
          </div>
        </div>
      </div>`;
  }

  function pendingSideMarkup(pending, confirmed, visibleCount) {
    const items = Array.isArray(pending) ? pending.filter(Boolean) : [];
    const pendingCount = items.length;
    const confirmedCount = Array.isArray(confirmed) ? confirmed.length : 0;
    let inflow = 0;
    let outflow = 0;
    for (const item of items) {
      const amount = asNumber(item?.amount_usdt);
      if (!Number.isFinite(amount)) continue;
      if (item?.direction === "out") outflow += Math.abs(amount);
      else inflow += Math.abs(amount);
    }
    const sources = new Map();
    const categories = new Map();
    for (const item of items) {
      const source = String(item?.source || "未知来源");
      sources.set(source, (sources.get(source) || 0) + 1);
      const category = categoryLabel(selectedCategoryOf(item) || suggestedCategoryOf(item));
      categories.set(category, (categories.get(category) || 0) + 1);
    }
    const bars = (map) => {
      const rows = [...map.entries()].sort((left, right) => right[1] - left[1]).slice(0, 3);
      const max = rows[0]?.[1] || 1;
      return rows
        .map(
          ([label, count]) => `<div class="side-bar">
            <span>${escapeHtml(label)}</span>
            <span class="side-bar-track" aria-hidden="true"><i style="width:${Math.max(
              8,
              Math.round((count / max) * 100),
            )}%"></i></span>
            <b>${count}</b>
          </div>`,
        )
        .join("");
    };
    const oldest = items.reduce((best, item) => {
      const date = dateObject(item?.created_at || item?.ts);
      if (!date) return best;
      return !best || date < best ? date : best;
    }, null);
    const waitingDays = oldest
      ? Math.max(0, Math.floor((Date.now() - oldest.getTime()) / 86400000))
      : null;
    const waitingText =
      waitingDays == null ? "—" : waitingDays === 0 ? "今天" : `${waitingDays} 天`;

    return `
      <aside class="pending-side" aria-label="核对进度">
        <div class="pending-side-inner">
          <p class="pending-side-kicker">核对进度</p>
          <p class="pending-side-count">
            <strong>${pendingCount}</strong><span>项待处理</span>
          </p>
          <dl class="pending-side-facts">
            <div><dt>已归档</dt><dd>${confirmedCount}</dd></div>
            <div><dt>本页显示</dt><dd>${visibleCount}</dd></div>
            <div><dt>流入合计</dt><dd>${money(inflow)}</dd></div>
            <div><dt>流出合计</dt><dd>${money(outflow)}</dd></div>
            <div><dt>最早等待</dt><dd>${waitingText}</dd></div>
          </dl>
          ${
            pendingCount
              ? `<div class="pending-side-block">
                   <p class="pending-side-kicker">来源</p>
                   <div class="side-bars">${bars(sources)}</div>
                 </div>
                 <div class="pending-side-block">
                   <p class="pending-side-kicker">类别</p>
                   <div class="side-bars">${bars(categories)}</div>
                 </div>`
              : ""
          }
          <p class="pending-side-note">${
            pendingCount
              ? "逐笔核对类别。确认后会写入账本；示例数据不计入。"
              : "这一轮已经全部归档。新的资金变动会重新出现在左侧。"
          }</p>
        </div>
      </aside>`;
  }

  function suggestionMarkup(item) {
    const id = Number(item.id);
    const suggested = suggestedCategoryOf(item);
    const selected = selectedCategoryOf(item);
    const overridden = state.pendingChoices.has(id) && selected !== suggested;
    const chipLabel = selected ? categoryLabel(selected) : "未分类";
    const confidence = confidenceParts(item.suggested_confidence);
    // Percent stays with the original suggestion. Once the user picks something
    // else, the bar and the reason fade together and the percent leaves the row.
    const showConfidence = Boolean(confidence) && !overridden;
    const confidenceHtml = showConfidence
      ? `<span class="confidence confidence--${confidence.band}" title="置信度 ${confidence.percent}%">
          <span class="confidence-track" aria-hidden="true"><span style="width:${confidence.width}%"></span></span>
          <span class="confidence-num">${confidence.percent}%</span>
          ${confidence.band === "low" ? `<span class="confidence-note">${confidence.bandLabel}</span>` : ""}
        </span>`
      : "";
    const originHtml =
      overridden && suggested
        ? `<div class="suggest-origin">
            <span>系统原建议：${escapeHtml(categoryLabel(suggested))}${
              confidence ? ` · ${confidence.percent}%` : ""
            }</span>
            <button class="text-toggle" type="button" data-reset-category="${id}">恢复建议</button>
          </div>`
        : "";

    return `
      <div class="suggest ${overridden ? "is-overridden" : ""}">
        <div class="suggest-row">
          <button
            class="category-row ${selected ? "" : "is-unset"} ${categoryToneClass(selected)}"
            type="button"
            data-pick-category="${id}"
            data-category="${escapeHtml(selected || "")}"
            aria-haspopup="dialog"
            aria-label="${selected ? `类别 ${chipLabel}，点击更改` : "未分类，请选择类别"}"
          >
            <span class="chip-kicker">类别</span>
            <span class="chip-value">${escapeHtml(chipLabel)}</span>
            <span class="category-chevron" aria-hidden="true">▾</span>
          </button>
          ${confidenceHtml}
        </div>
        ${originHtml}
      </div>`;
  }

  function moreParts(item, kind) {
    const id = Number(item.id);
    const key = `${kind}:${id}`;
    const open = state.pendingExpanded.has(key);
    const detail = String(item.detail || "").trim();
    const reason = kind === "pending" ? String(item.suggested_reason || "").trim() : "";
    const quantity = quantityLine(item);
    // The judgment reason is no longer printed on the card face. Keep it in
    // the expanded area, unless the detail line already contains it.
    const reasonInDetail = Boolean(reason && detail.includes(reason));
    if (!detail && !quantity && !reason) return { panel: "", toggle: "" };

    const quantityHtml = quantity ? `<p class="pending-qty">${displayText(quantity)}</p>` : "";
    const reasonHtml =
      reason && !reasonInDetail
        ? `<p class="pending-reason"><span>判定理由</span>${displayText(reason)}</p>`
        : "";
    const detailHtml = detail ? `<p class="pending-detail is-open">${displayText(detail)}</p>` : "";

    return {
      panel: `<div class="pending-more ${open ? "is-open" : ""}" id="more-${kind}-${id}" ${open ? "" : "hidden"}>
        ${quantityHtml}
        ${reasonHtml}
        ${detailHtml}
      </div>`,
      toggle: `<button
        class="text-toggle more-toggle"
        type="button"
        data-more="${kind}"
        data-id="${id}"
        data-kind="${kind}"
        aria-expanded="${open}"
        aria-controls="more-${kind}-${id}"
      ><span>${open ? "收起" : "展开"}</span><svg class="more-chevron" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="m4 6 4 4 4-4"/></svg></button>`,
    };
  }

  function moreMarkup(item, kind) {
    const { panel, toggle } = moreParts(item, kind);
    return panel + toggle;
  }

  function pendingCardMarkup(item, index) {
    const amount =
      item.amount_usdt === null || item.amount_usdt === undefined
        ? ""
        : `<span class="pending-amount">${money(item.amount_usdt)} <small>USDT</small></span>`;
    const itemId = Number(item.id);
    const selected = selectedCategoryOf(item);
    const canConfirm = Boolean(selected);
    const confirmLabel = canConfirm ? "确认并归档" : "请先选择类别";
    const more = moreParts(item, "pending");

    return `
      <article class="pending-card" style="--i:${Math.min(index, 8)}" data-pending-id="${itemId}"${cardNatureAttrs(item)}>
        <div class="pending-head">
          <div class="pending-copy">
            <div class="pending-meta">
              <span class="pending-status">待确认</span>
              ${demoTagMarkup(item)}
            </div>
            <h2 class="pending-title">${displayTitle(item.title || "未命名事项")}</h2>
          </div>
          ${amount}
        </div>
        ${occurredMarkup(item)}
        ${infoMarkup(item)}
        ${suggestionMarkup(item)}
        ${more.panel}
        <footer class="pending-foot">
          <div class="pending-foot-meta">
            <time class="pending-date pending-date--found" datetime="${escapeHtml(item.created_at || "")}">
              发现于 ${formatCardDate(item.created_at)}
            </time>
            ${more.toggle}
          </div>
          <div class="confirm-slot">
            ${canConfirm ? "" : `<p class="confirm-hint" id="confirm-hint-${itemId}">请先选择类别，再确认归档</p>`}
            <button
              class="confirm-btn"
              type="button"
              data-confirm="${itemId}"
              ${canConfirm ? "" : "disabled"}
              ${canConfirm ? "" : `aria-describedby="confirm-hint-${itemId}"`}
              aria-label="${confirmLabel}"
            >
              ${ICONS.check}
              确认
            </button>
          </div>
        </footer>
      </article>`;
  }

  function confirmedCardMarkup(item) {
    const id = Number(item.id);
    const amount =
      item.amount_usdt === null || item.amount_usdt === undefined
        ? ""
        : `<span class="pending-amount">${money(item.amount_usdt)} <small>USDT</small></span>`;
    const finalLabel = categoryLabel(item.final_category);
    return `
      <article class="pending-card pending-card--done" data-pending-id="${id}"${cardNatureAttrs(item)}>
        <div class="pending-head">
          <div class="pending-copy">
            <div class="pending-meta">
              <span class="pending-status pending-status--done">已归档</span>
              ${demoTagMarkup(item)}
            </div>
            <h2 class="pending-title">${displayTitle(item.title || "未命名事项")}</h2>
          </div>
          ${amount}
        </div>
        ${occurredMarkup(item)}
        ${infoMarkup(item)}
        ${moreMarkup(item, "confirmed")}
        <div class="done-row">
          <span class="done-chip ${isKnownCategory(item.final_category) ? "" : "is-unset"} ${categoryToneClass(item.final_category)}" aria-label="最终类别 ${escapeHtml(finalLabel)}">
            ${escapeHtml(finalLabel)}
          </span>
          <time class="pending-date" datetime="${escapeHtml(item.confirmed_at || "")}">
            归档于 ${formatCardDate(item.confirmed_at, true)}
          </time>
        </div>
      </article>`;
  }

  function confirmedSectionMarkup(confirmed) {
    if (!confirmed.length) return "";
    const open = state.confirmedOpen;
    return `
      <section class="confirmed-fold ${open ? "is-open" : ""}">
        <button
          class="confirmed-toggle"
          type="button"
          data-toggle-confirmed
          aria-expanded="${open}"
          aria-controls="confirmed-panel"
        >
          <span>已确认</span>
          <span class="confirmed-count">${confirmed.length}</span>
          <span class="confirmed-caret" aria-hidden="true"></span>
        </button>
        <div class="confirmed-panel" id="confirmed-panel" ${open ? "" : "hidden"}>
          <div class="pending-list">
            ${confirmed.map((item) => confirmedCardMarkup(item)).join("")}
          </div>
        </div>
      </section>`;
  }

  function pendingMarkup(items) {
    const list = Array.isArray(items) ? items.filter(Boolean) : [];
    const pending = list.filter((item) => item.status === "pending");
    const confirmed = list.filter((item) => item.status === "confirmed");
    const visibleCount = Math.min(state.pendingVisible, pending.length);
    const visible = pending.slice(0, visibleCount);
    const rest = pending.length - visibleCount;

    const summary = `
      <div class="pending-summary">
        <span><span class="num">${pending.length}</span> 项待处理${
          pending.length > visibleCount
            ? ` · 已显示 <span class="num">${visibleCount}</span>`
            : ""
        }</span>
        <span>${confirmed.length ? `已确认 <span class="num">${confirmed.length}</span>` : "核对后归档"}</span>
      </div>`;

    const queue = !pending.length
      ? emptyPendingMarkup()
      : `
        <div class="pending-list">
          ${visible.map((item, index) => pendingCardMarkup(item, index)).join("")}
        </div>
        ${
          rest > 0
            ? `<button class="more-btn" type="button" data-pending-more>
                再显示 ${Math.min(PENDING_PAGE_SIZE, rest)} 条
                <span>剩余 ${rest}</span>
              </button>`
            : ""
        }`;

    return `
      <div class="pending-grid">
        ${summary}
        <div class="pending-main">
          ${queue}
          ${confirmedSectionMarkup(confirmed)}
        </div>
        ${pendingSideMarkup(pending, confirmed, Math.min(visibleCount, pending.length))}
      </div>`;
  }

  function rerenderPendingList() {
    const content = document.getElementById("pending-content");
    const items = state.pendingItems;
    if (!content || !items) return;
    const shell = content.querySelector(".reveal") || content;
    shell.innerHTML = pendingMarkup(items);
  }

  function pendingProvenance(items) {
    const list = (Array.isArray(items) ? items : []).filter(Boolean);
    const mockCount = list.filter((item) => item?.nature === "test_data").length;
    return {
      hasMock: mockCount > 0,
      hasReal: list.length > mockCount,
      mixed: mockCount > 0 && list.length > mockCount,
    };
  }

  function pendingCountOf(items) {
    return (Array.isArray(items) ? items : []).filter(
      (item) => item && item.status === "pending",
    ).length;
  }

  function updatePendingBadges() {
    const count = Number.isFinite(state.pendingCount) ? state.pendingCount : 0;
    document.querySelectorAll("[data-pending-badge]").forEach((badge) => {
      badge.textContent = count > 99 ? "99+" : String(count);
      badge.hidden = count <= 0;
    });
    updateNotifyBadges();
  }

  function setPendingCount(value) {
    const count = Number(value);
    state.pendingCount = Number.isFinite(count) ? Math.max(0, Math.floor(count)) : null;
    updatePendingBadges();
  }

  async function loadPendingCount() {
    try {
      const items = await api("/pending");
      setPendingCount(pendingCountOf(items));
    } catch (error) {
      if (error.status === 401) return;
    }
  }

  async function loadPending(seq, showSkeleton = false) {
    const content = document.getElementById("pending-content");
    if (!content) return;
    if (showSkeleton) content.innerHTML = pendingSkeleton();

    try {
      const items = await api("/pending");
      if (seq !== state.renderSeq) return;
      state.pendingItems = Array.isArray(items) ? items : [];
      state.notify.pending = state.pendingItems.slice();
      setPendingCount(pendingCountOf(state.pendingItems));
      const provenance = pendingProvenance(state.pendingItems);
      const demoAccount = isDemoAccount();
      // Demo accounts are wholly sample data: the page chip carries that fact
      // and card headers stay clean. A real account only gets the "含示例数据"
      // page chip plus per-card "示例" chips when the page truly mixes both.
      state.pendingMixed = !demoAccount && provenance.mixed;
      syncPageDemoSlot({
        mock: !demoAccount && provenance.hasMock && !provenance.hasReal,
        mixed: state.pendingMixed,
      });
      const alive = new Set(
        state.pendingItems.filter((item) => item && item.status === "pending").map((item) => Number(item.id)),
      );
      for (const id of [...state.pendingChoices.keys()]) {
        if (!alive.has(id)) state.pendingChoices.delete(id);
      }
      content.innerHTML = `<div class="reveal">${pendingMarkup(state.pendingItems)}</div>`;
    } catch (error) {
      if (error.status === 401) {
        expireSession();
        return;
      }
      if (seq !== state.renderSeq) return;
      content.innerHTML = retryMarkup("pending");
    }
  }

  function renderPendingPage(seq) {
    setTitle("待确认");
    state.pendingVisible = PENDING_PAGE_SIZE;
    state.pendingMixed = false;
    closeCategorySheet({ restoreFocus: false });
    renderShell(
      `${topbarMarkup()}
      ${pageHead("待核对", "待确认", "核对每一笔，再归档")}
      <div class="pending-content" id="pending-content">${pendingSkeleton()}</div>`,
      "pending",
    );
    loadPending(seq);
  }

  function resolveConfirmCategory(id) {
    if (state.pendingChoices.has(id)) return state.pendingChoices.get(id);
    const item = [...(state.pendingItems || []), ...(state.notify.pending || [])].find(
      (row) => Number(row.id) === id,
    );
    return item ? suggestedCategoryOf(item) : null;
  }

  async function confirmPending(id, button) {
    if (!button || button.disabled) return;
    const category = resolveConfirmCategory(id);
    if (!category) {
      showToast("请先选择类别", "error");
      return;
    }
    const original = button.innerHTML;
    button.disabled = true;
    button.textContent = "确认中…";

    try {
      await api(`/pending/${id}/confirm`, {
        method: "POST",
        body: { category },
      });
      state.pendingChoices.delete(id);
      showToast("已确认，记录已归档", "success");
      await loadPending(state.renderSeq);
    } catch (error) {
      if (error.status === 401) {
        expireSession();
        return;
      }
      button.disabled = false;
      button.innerHTML = original;
      showToast(error.message || "确认失败，请稍后重试", "error");
    }
  }

  function sheetOptionMarkup(kind, suggested, selected) {
    const label = CATEGORY_LABELS[kind];
    const current = kind === selected;
    const advised = kind === suggested;
    return `
      <button
        class="sheet-option ${current ? "is-current" : ""}"
        type="button"
        role="option"
        data-sheet-option="${kind}"
        aria-selected="${current}"
      >
        <span class="sheet-option-label">${label}</span>
        ${advised ? `<span class="sheet-advice">系统建议</span>` : ""}
        <span class="sheet-check" aria-hidden="true">${current ? ICONS.check : ""}</span>
      </button>`;
  }

  function openCategorySheet(id, trigger) {
    const item = [...(state.pendingItems || []), ...(state.notify.pending || [])].find(
      (row) => Number(row.id) === id,
    );
    if (!item || item.status !== "pending") return;
    closeCategorySheet({ restoreFocus: false });
    const suggested = suggestedCategoryOf(item);
    const selected = selectedCategoryOf(item);
    const titleId = "category-sheet-title";
    const root = document.createElement("div");
    root.className = "sheet-root";
    if (state.notify.open || state.flowToast) root.classList.add("sheet-root--over-notify");
    root.innerHTML = `
      <div class="sheet-backdrop" data-sheet-close></div>
      <div
        class="sheet"
        role="dialog"
        aria-modal="true"
        aria-labelledby="${titleId}"
        tabindex="-1"
      >
        <div class="sheet-grab" data-sheet-grab aria-hidden="true"></div>
        <header class="sheet-head">
          <div>
            <p class="sheet-kicker">选择类别</p>
            <h2 id="${titleId}">${displayTitle(item.title || "未命名事项")}</h2>
          </div>
          <button class="sheet-close" type="button" data-sheet-close aria-label="关闭类别选择">关闭</button>
        </header>
        <div class="sheet-list" role="listbox" aria-label="类别">
          ${CATEGORY_ORDER.map((kind) => sheetOptionMarkup(kind, suggested, selected)).join("")}
        </div>
      </div>`;
    document.body.appendChild(root);
    document.body.classList.add("sheet-open");
    const dialog = root.querySelector(".sheet");
    state.categorySheet = {
      id,
      root,
      trigger: trigger || null,
      previousFocus: document.activeElement,
    };
    window.requestAnimationFrame(() => {
      root.classList.add("is-open");
      dialog?.focus();
    });
  }

  function closeCategorySheet({ restoreFocus = true } = {}) {
    const sheet = state.categorySheet;
    state.categorySheet = null;
    document.body.classList.remove("sheet-open");
    if (!sheet) return;
    sheet.root.remove();
    if (restoreFocus && sheet.trigger && document.contains(sheet.trigger)) {
      sheet.trigger.focus();
    }
  }

  function moreSheetSyncStamp() {
    const stamp = settingsState.sync?.last_run_at;
    return stamp ? formatMonthDayTime(stamp) : "";
  }

  function moreSheetSyncText() {
    return `只读 · 手动同步 · 最近 ${moreSheetSyncStamp()}`;
  }

  // Without a finished sync run there is no time to show, so the whole sync
  // row (and its divider) stays out of the drawer instead of printing "最近 —".
  function updateMoreSheetSync(root = document) {
    const stamp = moreSheetSyncStamp();
    root.querySelectorAll("[data-more-sync]").forEach((row) => {
      row.hidden = !stamp;
    });
    root.querySelectorAll("[data-more-sync-divider]").forEach((divider) => {
      divider.hidden = !stamp;
    });
    const node = root.querySelector("[data-more-sync-text]");
    if (node) node.textContent = moreSheetSyncText();
  }

  function moreSheetMarkup() {
    const username = state.user?.username || "未登录";
    const initial = username.slice(0, 1);
    const busy = Boolean(state.syncBusy);
    const hasSyncStamp = Boolean(moreSheetSyncStamp());
    const pending = Number.isFinite(state.pendingCount) ? Math.max(0, state.pendingCount) : 0;
    return `
      <div class="more-sheet-backdrop" data-more-close></div>
      <div
        class="more-sheet"
        role="dialog"
        aria-modal="true"
        aria-label="更多"
        tabindex="-1"
      >
        <div class="more-sheet-grab" data-more-grab aria-hidden="true"></div>
        <div class="more-sheet-list">
          <button class="more-sheet-row" type="button" data-more-nav="pending">
            <span class="more-sheet-row-icon">${ICONS.pending}</span>
            <span class="more-sheet-row-label">全部待确认</span>
            <span class="pending-badge more-sheet-badge" data-pending-badge ${
              pending > 0 ? "" : "hidden"
            }>${pending > 99 ? "99+" : pending}</span>
            <span class="more-sheet-row-chevron" aria-hidden="true">${ICONS.chevron}</span>
          </button>
        </div>
        <div class="more-sheet-divider" role="separator"></div>
        <div class="more-sheet-list">
          <button class="more-sheet-row" type="button" data-more-nav="settings">
            <span class="more-sheet-row-icon">${ICONS.settings}</span>
            <span class="more-sheet-row-label">设置</span>
            <span class="more-sheet-row-chevron" aria-hidden="true">${ICONS.chevron}</span>
          </button>
        </div>
        <div class="more-sheet-divider" data-more-sync-divider role="separator" ${
          hasSyncStamp ? "" : "hidden"
        }></div>
        <div class="more-sheet-sync" data-more-sync ${hasSyncStamp ? "" : "hidden"}>
          <p class="more-sheet-sync-text" data-more-sync-text>${escapeHtml(moreSheetSyncText())}</p>
          <button
            class="sync-btn sync-btn--quiet more-sheet-sync-btn"
            type="button"
            data-sync-run
            aria-label="${busy ? "同步中" : "同步数据"}"
            ${busy ? 'disabled aria-busy="true"' : ""}
          >
            <span class="sync-spin" aria-hidden="true"></span>
            <span data-sync-label>${busy ? "同步中…" : "同步"}</span>
          </button>
        </div>
        <div class="more-sheet-account">
          <span class="avatar more-sheet-avatar" aria-hidden="true">${escapeHtml(initial)}</span>
          <span class="more-sheet-user">${escapeHtml(username)}</span>
          <button class="more-sheet-logout" type="button" data-more-logout>退出</button>
        </div>
      </div>`;
  }

  async function refreshMoreSyncStatus(root) {
    try {
      const status = await api("/sync/status");
      settingsState.sync = status || {};
    } catch (error) {
      if (error.status === 401) return;
    }
    if (!root.isConnected) return;
    updateMoreSheetSync(root);
  }

  function moreSheetVelocity(samples) {
    if (!samples || samples.length < 2) return 0;
    const first = samples[0];
    const last = samples[samples.length - 1];
    const elapsed = last.t - first.t;
    return elapsed > 0 ? ((last.y - first.y) / elapsed) * 1000 : 0;
  }

  const MORE_SHEET_SPRING = { stiffness: 380, damping: 34 };

  function shouldCloseMoreSheet(dy, velocity, height = 0) {
    // vaul: dismiss past 30% of the sheet height, or a fast 500px/s flick.
    return (height > 0 && dy > height * 0.3) || velocity > 500;
  }

  function syncAppInert() {
    const app = document.getElementById("app");
    if (!app) return;
    const modalOpen =
      document.body.classList.contains("notify-open") ||
      document.body.classList.contains("more-sheet-open");
    if (modalOpen) {
      app.setAttribute("inert", "");
      app.setAttribute("aria-hidden", "true");
    } else {
      app.removeAttribute("inert");
      app.removeAttribute("aria-hidden");
    }
  }

  function openMoreSheet(trigger) {
    if (state.moreSheet) return;
    if (state.moreSheetTimer) {
      window.clearTimeout(state.moreSheetTimer);
      state.moreSheetTimer = 0;
    }
    document.querySelectorAll(".more-sheet-root").forEach((node) => node.remove());
    closeUserMenu();
    const root = document.createElement("div");
    root.className = "more-sheet-root";
    root.innerHTML = moreSheetMarkup();
    document.body.appendChild(root);
    document.body.classList.add("more-sheet-open");
    state.moreSheet = {
      root,
      trigger: trigger || null,
      previousFocus: document.activeElement,
    };
    trigger?.setAttribute("aria-expanded", "true");

    const dialog = root.querySelector(".more-sheet");
    const backdrop = root.querySelector(".more-sheet-backdrop");
    const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    let drag = null;
    let springFrame = 0;
    let lastDy = 0;
    const clearSpring = () => {
      if (springFrame) window.cancelAnimationFrame(springFrame);
      springFrame = 0;
    };
    if (dialog) dialog._cancelSpring = clearSpring;
    const resetDrag = () => {
      if (!dialog) return;
      clearSpring();
      dialog.classList.remove("is-dragging");
      dialog.style.transition = "";
      dialog.style.transform = "";
      if (backdrop) backdrop.style.opacity = "";
      lastDy = 0;
    };
    const springBack = () => {
      if (!dialog || reduceMotion || !lastDy) {
        resetDrag();
        return;
      }
      // vaul-like snap-back: stiffness 380 / damping 34, integrated per frame.
      const from = lastDy;
      let position = from;
      let velocity = 0;
      let last = window.performance.now();
      dialog.classList.add("is-dragging");
      dialog.style.transition = "none";
      const step = (now) => {
        const dt = Math.min(0.032, Math.max(0.001, (now - last) / 1000));
        last = now;
        const acceleration = -MORE_SHEET_SPRING.stiffness * position - MORE_SHEET_SPRING.damping * velocity;
        velocity += acceleration * dt;
        position += velocity * dt;
        if (Math.abs(position) < 0.6 && Math.abs(velocity) < 16) {
          resetDrag();
          return;
        }
        dialog.style.transform = `translateY(${position}px)`;
        springFrame = window.requestAnimationFrame(step);
      };
      springFrame = window.requestAnimationFrame(step);
    };
    dialog?.addEventListener("pointerdown", (event) => {
      if (reduceMotion || event.button !== 0) return;
      if (event.target.closest("button, a, input, textarea, select")) return;
      clearSpring();
      const now = window.performance.now();
      drag = {
        startY: event.clientY,
        dy: 0,
        id: event.pointerId,
        samples: [{ y: event.clientY, t: now }],
      };
      lastDy = 0;
      dialog.classList.add("is-dragging");
      dialog.setPointerCapture?.(event.pointerId);
    });
    dialog?.addEventListener("pointermove", (event) => {
      if (!drag || event.pointerId !== drag.id) return;
      const now = window.performance.now();
      const rawDy = event.clientY - drag.startY;
      // Upward drag has 0.5 rubber-band resistance; downward drag follows 1:1.
      const visualDy = rawDy < 0 ? rawDy * 0.5 : rawDy;
      drag.dy = rawDy;
      drag.samples.push({ y: event.clientY, t: now });
      drag.samples = drag.samples.filter((sample) => now - sample.t <= 120);
      if (Math.abs(rawDy) <= 2) return;
      event.preventDefault();
      lastDy = visualDy;
      dialog.style.transition = "none";
      dialog.style.transform = `translateY(${visualDy}px)`;
      if (backdrop) {
        const height = dialog.getBoundingClientRect().height || 1;
        const progress = Math.max(0, Math.min(1, rawDy / (height * 0.3)));
        backdrop.style.opacity = String(Math.max(0.1, 1 - progress));
      }
    });
    const endDrag = (event) => {
      if (!drag || event.pointerId !== drag.id) return;
      const dy = drag.dy;
      const now = window.performance.now();
      const velocity = moreSheetVelocity(
        drag.samples.filter((sample) => now - sample.t <= 120),
      );
      drag = null;
      dialog?.classList.remove("is-dragging");
      const height = dialog?.getBoundingClientRect().height || 0;
      if (shouldCloseMoreSheet(dy, velocity, height)) {
        closeMoreSheet({ restoreFocus: true, dragClose: true });
      } else {
        springBack();
      }
    };
    dialog?.addEventListener("pointerup", endDrag);
    dialog?.addEventListener("pointercancel", endDrag);

    window.requestAnimationFrame(() => {
      root.classList.add("is-open");
      dialog?.focus();
      syncAppInert();
    });
    updateMoreSheetSync(root);
    updatePendingBadges();
    loadPendingCount();
    refreshMoreSyncStatus(root);
  }

  function closeMoreSheet({ restoreFocus = true, onClosed = null, dragClose = false } = {}) {
    const sheet = state.moreSheet;
    if (!sheet) {
      onClosed?.();
      return;
    }
    state.moreSheet = null;
    sheet.trigger?.setAttribute("aria-expanded", "false");
    const dialog = sheet.root.querySelector(".more-sheet");
    const backdrop = sheet.root.querySelector(".more-sheet-backdrop");
    const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    dialog?._cancelSpring?.();
    sheet.root.classList.remove("is-open");
    if (dragClose && dialog && !reduceMotion) {
      dialog.style.transition = "transform 220ms ease-out, opacity 220ms ease-out";
      window.requestAnimationFrame(() => {
        dialog.style.transform = "translateY(110%)";
        if (backdrop) {
          backdrop.style.transition = "opacity 220ms ease-out";
          backdrop.style.opacity = "0";
        }
      });
    } else {
      if (dialog) {
        dialog.style.transition = "";
        dialog.style.transform = "";
      }
      if (backdrop) {
        backdrop.style.transition = "";
        backdrop.style.opacity = "";
      }
    }
    const finish = () => {
      state.moreSheetTimer = 0;
      sheet.root.remove();
      if (!state.moreSheet) document.body.classList.remove("more-sheet-open");
      syncAppInert();
      if (restoreFocus && sheet.trigger && document.contains(sheet.trigger)) {
        sheet.trigger.focus();
      }
      onClosed?.();
    };
    state.moreSheetTimer = window.setTimeout(finish, 230);
  }

  function chooseCategory(kind) {
    const sheet = state.categorySheet;
    if (!sheet || !isKnownCategory(kind)) return;
    const item = [...(state.pendingItems || []), ...(state.notify.pending || [])].find(
      (row) => Number(row.id) === sheet.id,
    );
    const suggested = suggestedCategoryOf(item);
    if (suggested && kind === suggested) state.pendingChoices.delete(sheet.id);
    else state.pendingChoices.set(sheet.id, kind);
    closeCategorySheet({ restoreFocus: false });
    rerenderPendingList();
    refreshNotifySurfaces();
    const next = document.querySelector(`[data-pick-category="${sheet.id}"]`);
    if (next) next.focus();
  }

  function resetCategory(id) {
    state.pendingChoices.delete(id);
    rerenderPendingList();
    refreshNotifySurfaces();
  }

  function toggleMore(button) {
    const id = Number(button.getAttribute("data-id"));
    const scope = button.getAttribute("data-kind") || button.getAttribute("data-more") || "pending";
    if (!Number.isInteger(id)) return;
    const key = `${scope}:${id}`;
    if (state.pendingExpanded.has(key)) state.pendingExpanded.delete(key);
    else state.pendingExpanded.add(key);
    rerenderPendingList();
    document.querySelector(`[data-more][data-id="${id}"][data-kind="${scope}"]`)?.focus();
  }

  /* ---------- Login ---------- */

  function loginMarkup(message = "") {
    return `
      <main class="login">
        <div class="login-ambient" aria-hidden="true"><span></span><span></span></div>
        <header class="login-top">
          <div class="login-brand">
            ${LOGO}
            <span>资产追踪</span>
          </div>
        </header>

        <div class="login-grid">
          <div class="login-main">
            <section class="login-intro">
              <span class="page-kicker">私人账本</span>
              <h1 class="login-title">把每一笔，<br><span>看得更清楚。</span></h1>
              <p class="login-sub">资产、收益与待办，收在一个安静的界面里。登录后查看你的私人账本。</p>
            </section>

            <form class="login-form" id="login-form" novalidate>
              <div class="field" data-field="username">
                <label for="username">用户名</label>
                <div class="field-control">
                  <input
                    id="username"
                    name="username"
                    type="text"
                    inputmode="text"
                    autocomplete="username"
                    autocapitalize="none"
                    spellcheck="false"
                    placeholder="输入用户名"
                    aria-describedby="username-error"
                    required
                  />
                </div>
                <p class="field-error" id="username-error" role="alert" hidden></p>
              </div>
              <div class="field" data-field="password">
                <label for="password">密码</label>
                <div class="field-control">
                  <input
                    id="password"
                    name="password"
                    type="password"
                    autocomplete="current-password"
                    placeholder="输入密码"
                    aria-describedby="password-error"
                    required
                  />
                  <button
                    class="pw-toggle"
                    type="button"
                    data-password-toggle
                    aria-label="显示密码"
                    aria-pressed="false"
                  >
                    ${ICONS.eye}
                  </button>
                </div>
                <p class="field-error" id="password-error" role="alert" hidden></p>
                <p class="capslock-hint" id="password-capslock" aria-live="polite" hidden>大写锁定已开启</p>
              </div>

              <p class="form-error" id="login-error" role="alert" aria-live="polite">${escapeHtml(
                message,
              )}</p>

              <button class="login-submit" id="login-submit" type="submit">
                <span data-submit-label>进入看板</span>
                ${ICONS.arrow}
                <span class="submit-progress" aria-hidden="true"></span>
              </button>
            </form>
          </div>

          <aside class="login-aside" aria-label="产品一览">
            <div class="login-aside-card">
              <div class="login-aside-head">
                <span class="login-aside-mark">${LOGO}</span>
                <span>一处安静的总览</span>
              </div>
              <p class="login-aside-quote">把分散的仓位，<br>收进同一条时间线。</p>
              <ul class="login-points">
                <li><span>总览</span><b>净值与结构</b></li>
                <li><span>收益</span><b>时间加权</b></li>
                <li><span>期权</span><b>到期与敞口</b></li>
                <li><span>待确认</span><b>逐笔归档</b></li>
              </ul>
              <p class="login-aside-foot">本地优先 · 数据不离开本机</p>
              <div class="login-orbit" aria-hidden="true"><i></i><i></i><i></i></div>
            </div>
          </aside>
        </div>

        <footer class="login-foot">
          <span>私人资产看板</span>
          <span>仅限授权用户</span>
        </footer>
      </main>`;
  }

  function renderLogin(message = "") {
    stopSettingsLayout();
    app.classList.add("app--auth");
    app.innerHTML = loginMarkup(message);
    setTitle("登录");
  }

  function setLoginError(message) {
    const errorBox = document.getElementById("login-error");
    if (errorBox) errorBox.textContent = message || "";
  }

  function loginFieldErrors(username, password) {
    const errors = {};
    if (!String(username || "").trim()) errors.username = "请输入用户名";
    if (!String(password || "")) errors.password = "请输入密码";
    return errors;
  }

  function setLoginFieldError(name, message) {
    const input = document.getElementById(name);
    const node = document.getElementById(`${name}-error`);
    if (input) {
      if (message) input.setAttribute("aria-invalid", "true");
      else input.removeAttribute("aria-invalid");
    }
    if (!node) return;
    globalThis.clearTimeout(node._hideTimer);
    if (message) {
      node.classList.remove("is-clearing");
      node.textContent = message;
      node.hidden = false;
      return;
    }
    if (node.hidden) return;
    // Keep the red field state for one 120ms fade instead of snapping off.
    node.classList.add("is-clearing");
    node._hideTimer = globalThis.setTimeout(() => {
      node.hidden = true;
      node.classList.remove("is-clearing");
      node.textContent = "";
    }, 120);
  }

  function clearLoginFieldErrors() {
    setLoginFieldError("username", "");
    setLoginFieldError("password", "");
  }

  function capsLockOn(event) {
    return Boolean(event && typeof event.getModifierState === "function" && event.getModifierState("CapsLock"));
  }

  function syncCapsLockHint(event) {
    const input = document.getElementById("password");
    const hint = document.getElementById("password-capslock");
    if (!input || !hint) return;
    const show = document.activeElement === input && capsLockOn(event);
    hint.hidden = !show;
  }

  function setPasswordVisible(visible) {
    const input = document.getElementById("password");
    const button = document.querySelector("[data-password-toggle]");
    if (!input || !button) return;
    input.type = visible ? "text" : "password";
    button.setAttribute("aria-pressed", visible ? "true" : "false");
    button.setAttribute("aria-label", visible ? "隐藏密码" : "显示密码");
    button.classList.toggle("is-visible", visible);
    const caret = input.value.length;
    input.focus({ preventScroll: true });
    try {
      input.setSelectionRange(caret, caret);
    } catch (error) {
      /* some browsers reject selection on a freshly switched input */
    }
  }

  function setLoginLoading(loading) {
    const button = document.getElementById("login-submit");
    if (!button) return;
    const label = button.querySelector("[data-submit-label]");
    button.disabled = loading;
    button.setAttribute("aria-busy", loading ? "true" : "false");
    button.classList.toggle("is-loading", loading);
    if (label) label.textContent = loading ? "正在进入…" : "进入看板";
  }

  async function handleLoginSubmit(event) {
    event.preventDefault();
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    const formData = new FormData(form);
    const username = String(formData.get("username") || "").trim();
    const password = String(formData.get("password") || "");

    const errors = loginFieldErrors(username, password);
    setLoginFieldError("username", errors.username || "");
    setLoginFieldError("password", errors.password || "");
    if (errors.username || errors.password) {
      setLoginError("");
      form.querySelector('[aria-invalid="true"]')?.focus();
      return;
    }

    setLoginError("");
    clearLoginFieldErrors();
    setLoginLoading(true);

    try {
      const data = await api("/auth/login", {
        method: "POST",
        body: { username, password },
      });

      state.token = data.access_token;
      state.user = data.user;
      localStorage.setItem(TOKEN_KEY, data.access_token);
      window.history.replaceState(null, "", "/");
      renderRoute("/");
      loadPendingCount();
    } catch (error) {
      setLoginLoading(false);
      if (error.status === 401) {
        setLoginError("");
        setLoginFieldError("password", "用户名或密码不对");
        document.getElementById("password")?.focus({ preventScroll: true });
      } else {
        setLoginError(error.status === 0 ? "网络连接失败，请稍后重试" : error.message);
      }
    }
  }


  /* ---------- Settings: CoinStats ---------- */

  const creditFormatter = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
  const syncTimeFormatter = new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });

  const settingsState = {
    key: null,
    credits: null,
    creditsError: "",
    sync: null,
    saving: false,
    deleting: false,
    confirmDelete: false,
    replacing: false,
    wallets: null,
    walletSaving: false,
    walletDeleteId: null,
    walletDeleting: false,
    walletHint: "",
    walletHintTone: "",
    walletFormOpen: false,
    users: null,
    usersLoading: false,
    usersFormOpen: false,
    usersSaving: false,
    usersHint: "",
    usersHintTone: "",
    usersResetId: null,
    usersResetting: false,
    guideOpen: false,
    alertRule: null,
    alertRules: null,
    alertEditProtocol: null,
    alertSaving: false,
    alertResetConfirm: null,
    alertResetting: false,
    alertHint: "",
    alertHintTone: "",
    alertEvents: null,
    alertEventsLoading: false,
    alertEventsError: "",
    alertEventsExpanded: false,
    alertPositions: null,
    alertSources: null,
    wechatDetailsOpen: false,
    wechatError: "",
  };

  function formatCredits(value) {
    const number = Number(value);
    return Number.isFinite(number) ? creditFormatter.format(number) : "—";
  }

  function formatSyncTime(value) {
    if (!value) return "尚未同步";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "—";
    return `${syncTimeFormatter.format(date)}（北京时间）`;
  }

  const SETTINGS_SECTIONS = [
    { id: "sync", label: "数据与同步", copy: "最近同步、触发方式与错误提示", icon: "sync" },
    { id: "source", label: "数据源", copy: "CoinStats key、额度与配置说明", icon: "source" },
    { id: "wallet", label: "钱包", copy: "EVM / Solana 地址，只读展示", icon: "wallet" },
    { id: "alert", label: "告警", copy: "健康度阈值与最近告警记录", icon: "alert" },
    { id: "wechat", label: "微信推送", copy: "推送连接、发送状态与最近错误", icon: "wechat" },
    { id: "account", label: "账号", copy: "当前登录账号与成员管理", icon: "account" },
    { id: "privacy", label: "隐私与数据", copy: "本地存储边界与配置说明", icon: "privacy" },
  ];
  const SETTINGS_SECTION_IDS = SETTINGS_SECTIONS.map((section) => section.id);
  const SETTINGS_LINE_ICONS = {
    sync: `<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M20 11a8 8 0 0 0-14.9-3M4 13a8 8 0 0 0 14.9 3"/><path d="M20 5v6h-6M4 19v-6h6"/></svg>`,
    source: `<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><circle cx="8.5" cy="8.5" r="3.5"/><path d="m11 11 8 8M16 16l2-2M14 18l2-2"/></svg>`,
    wallet: `<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M4 8.5A2.5 2.5 0 0 1 6.5 6H18a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H6.5A2.5 2.5 0 0 1 4 17.5v-9Z"/><path d="M4 8h13M15.5 13h4.5"/></svg>`,
    alert: `<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M6 9a6 6 0 0 1 12 0c0 5 2 6 2 6H4s2-1 2-6Z"/><path d="M10 19a2.5 2.5 0 0 0 4 0"/></svg>`,
    wechat: `<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M20 11.5a7.5 7.5 0 0 1-11.2 6.6L4 20l1.5-4.2A7.5 7.5 0 1 1 20 11.5Z"/><path d="M8.5 11h.01M12 11h.01M15.5 11h.01"/></svg>`,
    account: `<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><circle cx="12" cy="8" r="3.5"/><path d="M5 20a7 7 0 0 1 14 0"/></svg>`,
    privacy: `<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12 3 5 6v5c0 4.6 2.9 8 7 10 4.1-2 7-5.4 7-10V6l-7-3Z"/><path d="m9.5 12 1.8 1.8 3.4-3.6"/></svg>`,
  };
  const SETTINGS_DANGER_META = {
    wallets: {
      title: "删除全部钱包？",
      copy: "所有已保存的 EVM / Solana 地址都会移除，之后需要重新添加。",
      confirm: "删除全部钱包",
    },
    key: {
      title: "删除 CoinStats key？",
      copy: "删除后额度读取与持仓同步会停止，直到重新配置 key。",
      confirm: "删除 key",
    },
    logout: {
      title: "退出登录？",
      copy: "当前浏览器会退出登录，需要重新输入账号密码才能继续使用。",
      confirm: "退出登录",
    },
  };

  function settingsRelativeTime(value, now = Date.now()) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    const minutes = Math.max(0, Math.round((now - date.getTime()) / 60000));
    if (minutes < 1) return "刚刚";
    if (minutes < 60) return `${minutes} 分钟前`;
    const hours = Math.round(minutes / 60);
    if (hours < 24) return `${hours} 小时前`;
    return `${Math.round(hours / 24)} 天前`;
  }

  function settingsSummaryFor(id, data, now = Date.now()) {
    const key = data?.key;
    const wallets = data?.wallets;
    const rules = Array.isArray(data?.alertRules) ? data.alertRules : null;
    const sync = data?.sync;
    const wechat = data?.wechat;
    const user = data?.user;
    if (id === "sync") {
      if (!sync) return { text: "读取中", warn: false };
      if (!sync.last_run_at) return { text: "尚未同步", warn: false };
      const status = String(sync.last_status || "");
      if (status === "partial") return { text: "部分来源失败", warn: true };
      if (status && status !== "ok") return { text: "同步失败", warn: true };
      const ago = settingsRelativeTime(sync.last_run_at, now);
      return { text: `${ago}同步`, warn: false };
    }
    if (id === "source") {
      if (key == null) return { text: "读取中", warn: false };
      if (!key.configured) return { text: "未配置", warn: false };
      return { text: key.last4 ? `已配置 · ${key.last4}` : "已配置", warn: false };
    }
    if (id === "wallet") {
      if (wallets == null) return { text: "读取中", warn: false };
      return { text: wallets.length ? `${wallets.length} 个地址` : "未添加", warn: false };
    }
    if (id === "alert") {
      if (rules == null) return { text: "读取中", warn: false };
      if (!rules.length) return { text: "未设置", warn: false };
      return { text: rules.some((rule) => rule?.enabled) ? "已开启" : "已关闭", warn: false };
    }
    if (id === "wechat") {
      if (!wechat) return { text: "读取中", warn: false };
      const label = String(wechat.label || "");
      return { text: label || "未连接", warn: wechat.tone === "is-warn" || /未连接|失败/.test(label) };
    }
    if (id === "account") {
      if (!user) return { text: "未登录", warn: false };
      return { text: user.is_admin ? "管理员" : "成员", warn: false };
    }
    return { text: "只读", warn: false };
  }

  function settingsHomeItems(data) {
    const now = data?.now || Date.now();
    return SETTINGS_SECTIONS.map((section) => {
      const summary = settingsSummaryFor(section.id, data, now);
      return { ...section, summary: summary.text, warn: summary.warn };
    });
  }

  function settingsHomeRowsMarkup(items) {
    return `<div class="settings-group settings-home-list" role="list">
      ${items
        .map(
          (item) => `<a class="settings-home-row" role="listitem" href="#${item.id}"
            data-settings-open="${item.id}" aria-label="${escapeHtml(item.label)}，${escapeHtml(item.summary)}">
            <span class="settings-home-icon" aria-hidden="true">${SETTINGS_LINE_ICONS[item.id] || ""}</span>
            <span class="settings-home-name">${escapeHtml(item.label)}</span>
            <span class="settings-home-summary${item.warn ? " is-warn" : ""}" data-settings-summary="${item.id}">${
              item.warn ? '<i class="settings-warn-dot" aria-hidden="true"></i>' : ""
            }${escapeHtml(item.summary)}</span>
            <span class="settings-home-chevron" aria-hidden="true">${ICONS.chevron}</span>
          </a>`,
        )
        .join("")}
    </div>`;
  }

  function settingsDangerItems() {
    const wallets = Array.isArray(settingsState.wallets) ? settingsState.wallets : null;
    const key = settingsState.key;
    return [
      {
        kind: "wallets",
        label: "删除全部钱包",
        detail: wallets == null ? "正在读取" : wallets.length ? `${wallets.length} 个地址` : "暂无地址",
      },
      {
        kind: "key",
        label: "删除 CoinStats key",
        detail: key == null ? "正在读取" : key.configured ? "已配置" : "未配置",
      },
      { kind: "logout", label: "退出登录", detail: "" },
    ];
  }

  function settingsDangerMarkup(scope, options = {}) {
    const id = options.id ? ` id="${options.id}"` : "";
    return `<div class="settings-danger settings-danger--${scope}"${id} data-danger-scope="${scope}">
      <p class="settings-danger-title">危险操作</p>
      <div class="settings-group settings-group--danger">
        ${settingsDangerItems()
          .map(
            (item) => `<button class="settings-danger-row" type="button" data-danger="${item.kind}">
              <span>${escapeHtml(item.label)}</span>
              ${item.detail ? `<small data-danger-detail="${item.kind}">${escapeHtml(item.detail)}</small>` : ""}
            </button>`,
          )
          .join("")}
      </div>
      <p class="settings-danger-hint">以上操作都需要二次确认</p>
    </div>`;
  }

  const settingsDangerState = { kind: null, returnFocus: null, busy: false, scrollY: 0 };

  function settingsPortalRoot() {
    let portal = document.getElementById("settings-portal-root");
    if (!portal) {
      portal = document.createElement("div");
      portal.id = "settings-portal-root";
    }
    if (portal.parentElement !== document.body) document.body.append(portal);
    return portal;
  }

  function settingsLockBackgroundScroll() {
    if (document.body.classList.contains("settings-danger-open")) return;
    const y = window.scrollY || window.pageYOffset || 0;
    settingsDangerState.scrollY = y;
    document.documentElement.classList.add("settings-danger-open");
    document.body.classList.add("settings-danger-open");
    document.body.style.top = `-${y}px`;
  }

  function settingsUnlockBackgroundScroll() {
    if (!document.body.classList.contains("settings-danger-open")) return;
    const y = settingsDangerState.scrollY || 0;
    document.documentElement.classList.remove("settings-danger-open");
    document.body.classList.remove("settings-danger-open");
    document.body.style.top = "";
    window.scrollTo(0, y);
  }

  function openSettingsDanger(kind) {
    const meta = SETTINGS_DANGER_META[kind];
    if (!meta || settingsDangerState.kind) return;
    settingsDangerState.kind = kind;
    settingsDangerState.busy = false;
    settingsDangerState.returnFocus =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const root = document.createElement("div");
    root.className = "settings-confirm-root";
    root.id = "settings-danger-dialog";
    root.innerHTML = `<div class="settings-confirm-backdrop" data-settings-danger-cancel></div>
      <div class="settings-confirm" role="alertdialog" aria-modal="true" aria-labelledby="settings-danger-title" aria-describedby="settings-danger-copy">
        <span class="settings-confirm-icon" aria-hidden="true">${ICONS.warn}</span>
        <h2 id="settings-danger-title">${escapeHtml(meta.title)}</h2>
        <p id="settings-danger-copy">${escapeHtml(meta.copy)}</p>
        <div class="settings-confirm-actions">
          <button class="settings-confirm-cancel" type="button" data-settings-danger-cancel>取消</button>
          <button class="settings-confirm-submit" type="button" data-settings-danger-confirm>${escapeHtml(
            meta.confirm,
          )}</button>
        </div>
      </div>`;
    settingsPortalRoot().append(root);
    settingsLockBackgroundScroll();
    requestAnimationFrame(() => root.classList.add("is-open"));
    root.querySelector(".settings-confirm-cancel")?.focus();
  }

  function closeSettingsDanger() {
    const root = document.getElementById("settings-danger-dialog");
    if (!root) return;
    root.remove();
    settingsUnlockBackgroundScroll();
    settingsDangerState.kind = null;
    settingsDangerState.busy = false;
    if (settingsDangerState.returnFocus?.isConnected) {
      settingsDangerState.returnFocus.focus({ preventScroll: true });
    }
    settingsDangerState.returnFocus = null;
  }

  function confirmSettingsDanger() {
    const kind = settingsDangerState.kind;
    if (!kind || settingsDangerState.busy) return;
    if (kind === "wallets") {
      closeSettingsDanger();
      deleteAllWallets();
      return;
    }
    if (kind === "key") {
      closeSettingsDanger();
      deleteCoinstatsKey();
      return;
    }
    if (kind === "logout") {
      closeSettingsDanger();
      logout();
    }
  }

  function settingsHomeData() {
    return {
      key: settingsState.key,
      wallets: settingsState.wallets,
      alertRules: settingsState.alertRules,
      sync: settingsState.sync,
      wechat: wechatCardModel(state.overviewData),
      user: state.user,
    };
  }

  function updateSettingsHomeSummary() {
    const items = settingsHomeItems(settingsHomeData());
    const byId = new Map(items.map((item) => [item.id, item]));
    document.querySelectorAll("[data-settings-summary]").forEach((node) => {
      const item = byId.get(node.getAttribute("data-settings-summary"));
      if (!item) return;
      const warn = item.warn ? "1" : "0";
      if (node.dataset.summaryText === item.summary && node.dataset.summaryWarn === warn) return;
      node.dataset.summaryText = item.summary;
      node.dataset.summaryWarn = warn;
      node.classList.toggle("is-warn", item.warn);
      node.innerHTML = `${item.warn ? '<i class="settings-warn-dot" aria-hidden="true"></i>' : ""}${escapeHtml(
        item.summary,
      )}`;
    });
    const danger = settingsDangerItems();
    danger.forEach((item) => {
      const node = document.querySelector(`[data-danger-detail="${item.kind}"]`);
      if (node && item.detail && node.textContent !== item.detail) node.textContent = item.detail;
    });
  }

  function settingsIsDesktop() {
    return window.matchMedia("(min-width: 1024px)").matches;
  }

  function settingsSection(id) {
    return document.querySelector(`.settings-section[data-settings-section="${id}"]`);
  }

  function settingsHashFor(id) {
    return `${window.location.pathname}${window.location.search}#${id}`;
  }

  function settingsSetActiveNav(id) {
    document.querySelectorAll("[data-settings-anchor]").forEach((link) => {
      const active = link.getAttribute("data-settings-anchor") === id;
      link.classList.toggle("is-active", active);
      if (active) link.setAttribute("aria-current", "true");
      else link.removeAttribute("aria-current");
    });
  }

  function settingsSpyUpdate() {
    if (!settingsIsDesktop() || !document.getElementById("settings-nav")) return;
    const sections = [...document.querySelectorAll(".settings-section[data-settings-section]")];
    if (!sections.length) return;
    const threshold = window.scrollY + Math.min(180, window.innerHeight * 0.3);
    let active = sections[0].getAttribute("data-settings-section");
    sections.forEach((section) => {
      if (section.offsetTop <= threshold) active = section.getAttribute("data-settings-section");
    });
    if (window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 4) {
      active = document.getElementById("danger")
        ? "danger"
        : sections[sections.length - 1].getAttribute("data-settings-section");
    }
    settingsSetActiveNav(active);
    if (window.location.hash !== `#${active}`) {
      history.replaceState(null, "", settingsHashFor(active));
    }
  }

  let settingsSpyFrame = 0;
  function settingsScheduleSpy() {
    if (settingsSpyFrame) return;
    settingsSpyFrame = requestAnimationFrame(() => {
      settingsSpyFrame = 0;
      settingsSpyUpdate();
    });
  }

  let settingsScrollFrame = 0;
  function settingsScrollTarget(id) {
    return id === "danger" ? document.getElementById("danger") : settingsSection(id);
  }

  function settingsScrollTo(id, options = {}) {
    const section = settingsScrollTarget(id);
    if (!section) return;
    const top = Math.max(0, window.scrollY + section.getBoundingClientRect().top - 24);
    const instant =
      options.instant || window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (instant) {
      window.scrollTo(0, top);
      settingsSpyUpdate();
      return;
    }
    cancelAnimationFrame(settingsScrollFrame);
    const start = window.scrollY;
    const delta = top - start;
    const started = performance.now();
    const duration = 360;
    const step = (now) => {
      const progress = Math.min(1, (now - started) / duration);
      const eased = 1 - Math.pow(1 - progress, 3);
      window.scrollTo(0, start + delta * eased);
      if (progress < 1) settingsScrollFrame = requestAnimationFrame(step);
      else settingsSpyUpdate();
    };
    settingsScrollFrame = requestAnimationFrame(step);
  }

  const settingsView = { open: null, homeScroll: 0, pushTimer: 0, observer: null, cleanup: null };

  function settingsOpenSection(id, options = {}) {
    if (settingsIsDesktop()) {
      settingsNavigate(id, options);
      return;
    }
    if (!SETTINGS_SECTION_IDS.includes(id)) return;
    if (!settingsView.open) settingsView.homeScroll = window.scrollY;
    const page = document.querySelector(".page--settings");
    const sections = document.getElementById("settings-sections");
    const target = settingsSection(id);
    if (!page || !sections || !target) return;
    settingsView.open = id;
    page.classList.add("is-settings-subpage");
    sections.querySelectorAll(".settings-section").forEach((section) => {
      section.classList.toggle("is-active", section === target);
    });
    const title = document.getElementById("settings-subpage-title");
    const data = SETTINGS_SECTIONS.find((section) => section.id === id);
    if (title && data) title.textContent = data.label;
    window.scrollTo(0, 0);
    if (!options.instant && !window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      sections.classList.remove("is-pushing");
      void sections.offsetWidth;
      sections.classList.add("is-pushing");
      clearTimeout(settingsView.pushTimer);
      settingsView.pushTimer = setTimeout(() => sections.classList.remove("is-pushing"), 300);
    }
    if (options.updateHash !== false) history.replaceState(null, "", settingsHashFor(id));
  }

  function settingsCloseSection(options = {}) {
    const page = document.querySelector(".page--settings");
    if (!page) return;
    page.classList.remove("is-settings-subpage");
    document.getElementById("settings-sections")?.classList.remove("is-pushing");
    document.querySelectorAll(".settings-section.is-active").forEach((section) => {
      section.classList.remove("is-active");
    });
    settingsView.open = null;
    history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
    if (options.restore !== false) {
      window.scrollTo(0, settingsView.homeScroll);
      requestAnimationFrame(() => window.scrollTo(0, settingsView.homeScroll));
    }
  }

  function settingsNavigate(id, options = {}) {
    if (id !== "danger" && !SETTINGS_SECTION_IDS.includes(id)) return;
    if (settingsIsDesktop()) {
      settingsSetActiveNav(id);
      settingsScrollTo(id, options);
      if (options.updateHash !== false) history.replaceState(null, "", settingsHashFor(id));
      return;
    }
    settingsOpenSection(id, options);
  }

  function settingsModeChanged() {
    const page = document.querySelector(".page--settings");
    if (!page) return;
    if (settingsIsDesktop()) {
      page.classList.remove("is-settings-subpage");
      settingsView.open = null;
      document.querySelectorAll(".settings-section.is-active").forEach((section) => {
        section.classList.remove("is-active");
      });
      const id = window.location.hash.slice(1);
      if (SETTINGS_SECTION_IDS.includes(id) || id === "danger") {
        requestAnimationFrame(() => settingsScrollTo(id, { instant: true, updateHash: false }));
      }
      settingsSpyUpdate();
      return;
    }
    const id = window.location.hash.slice(1);
    if (SETTINGS_SECTION_IDS.includes(id)) settingsOpenSection(id, { instant: true, updateHash: false });
    else settingsCloseSection({ restore: false });
  }

  function settingsHashChanged() {
    const id = window.location.hash.slice(1);
    if (settingsIsDesktop()) {
      if (SETTINGS_SECTION_IDS.includes(id) || id === "danger") {
        settingsSetActiveNav(id);
        settingsScrollTo(id, { updateHash: false });
      }
      return;
    }
    if (SETTINGS_SECTION_IDS.includes(id)) settingsOpenSection(id, { instant: false, updateHash: false });
    else if (settingsView.open) settingsCloseSection({ restore: false });
  }

  function settingsInitLayout() {
    if (settingsView.cleanup) settingsView.cleanup();
    const content = document.getElementById("settings-content");
    if (!content) return;
    clearTimeout(settingsView.pushTimer);
    settingsView.open = null;
    settingsView.homeScroll = 0;
    const page = document.querySelector(".page--settings");
    page?.classList.remove("is-settings-subpage");
    document.querySelectorAll(".settings-section.is-active").forEach((section) => {
      section.classList.remove("is-active");
    });
    updateSettingsHomeSummary();
    const id = window.location.hash.slice(1);
    if (settingsIsDesktop()) {
      if (SETTINGS_SECTION_IDS.includes(id) || id === "danger") {
        requestAnimationFrame(() => settingsScrollTo(id, { instant: true, updateHash: false }));
      }
      settingsSpyUpdate();
    } else if (SETTINGS_SECTION_IDS.includes(id)) {
      settingsOpenSection(id, { instant: true, updateHash: false });
    }
    const observer = new MutationObserver(() => updateSettingsHomeSummary());
    observer.observe(content, { subtree: true, childList: true, characterData: true });
    settingsView.observer = observer;
    window.addEventListener("scroll", settingsScheduleSpy, { passive: true });
    window.addEventListener("hashchange", settingsHashChanged);
    window.addEventListener("resize", settingsModeChanged);
    settingsView.cleanup = () => {
      observer.disconnect();
      settingsView.observer = null;
      window.removeEventListener("scroll", settingsScheduleSpy);
      window.removeEventListener("hashchange", settingsHashChanged);
      window.removeEventListener("resize", settingsModeChanged);
      cancelAnimationFrame(settingsSpyFrame);
      settingsSpyFrame = 0;
      cancelAnimationFrame(settingsScrollFrame);
      settingsScrollFrame = 0;
      clearTimeout(settingsView.pushTimer);
      settingsView.cleanup = null;
    };
  }

  function settingsPageHead() {
    return `
      <header class="page-head page-head--settings">
        <a class="page-back" href="/" data-link aria-label="返回总览">
          ${ICONS.back}
        </a>
        <div class="page-head-copy">
          <span class="page-kicker">偏好</span>
          <h1 class="page-title">设置</h1>
          <p class="page-sub">数据源、通知与隐私</p>
        </div>
      </header>`;
  }

  function csChipMarkup() {
    const configured = Boolean(settingsState.key?.configured);
    if (configured) {
      return `<span class="cs-chip is-on"><i aria-hidden="true"></i>已配置 · 末 4 位 <b>${escapeHtml(
        settingsState.key.last4 || "····",
      )}</b></span>`;
    }
    return `<span class="cs-chip"><i aria-hidden="true"></i>未配置</span>`;
  }

  function csHeadMarkup() {
    return `
      <div class="cs-head">
        <span class="cs-mark" aria-hidden="true">${ICONS.key}</span>
        <div class="cs-title">
          <h2>CoinStats</h2>
          <p>读取币安持仓</p>
        </div>
        ${csChipMarkup()}
      </div>`;
  }

  function csReplaceFormMarkup() {
    const saving = settingsState.saving;
    return `
      <form class="cs-form" id="cs-form" autocomplete="off" novalidate>
        <label class="cs-label" for="cs-key-input">粘贴新的 CoinStats key</label>
        <input
          class="cs-input"
          id="cs-key-input"
          type="password"
          inputmode="text"
          autocomplete="off"
          autocapitalize="off"
          autocorrect="off"
          spellcheck="false"
          placeholder="粘贴新 key"
          aria-describedby="cs-hint"
        />
        <p class="cs-hint" id="cs-hint" role="status"></p>
        <div class="cs-actions" id="cs-actions">
          <button class="cs-btn cs-btn--primary ${saving ? "is-loading" : ""}" type="submit" disabled>
            <span class="cs-spin" aria-hidden="true"></span>
            <span data-cs-label>${saving ? "正在保存" : "保存新 key"}</span>
          </button>
          <button class="cs-btn cs-btn--ghost" type="button" data-cs-cancel-replace ${
            saving ? "disabled" : ""
          }>取消</button>
        </div>
      </form>`;
  }

  function csEmptyFormMarkup() {
    const saving = settingsState.saving;
    return `
      <p class="cs-empty-line">填入你的 CoinStats key，同步时自动更新币安持仓。</p>
      <form class="cs-form" id="cs-form" autocomplete="off" novalidate>
        <label class="cs-label" for="cs-key-input">API key</label>
        <input
          class="cs-input"
          id="cs-key-input"
          type="password"
          inputmode="text"
          autocomplete="off"
          autocapitalize="off"
          autocorrect="off"
          spellcheck="false"
          placeholder="粘贴你的 CoinStats API key"
          aria-describedby="cs-hint"
        />
        <p class="cs-hint" id="cs-hint" role="status"></p>
        <div class="cs-actions" id="cs-actions">
          <button class="cs-btn cs-btn--primary ${saving ? "is-loading" : ""}" type="submit" disabled>
            <span class="cs-spin" aria-hidden="true"></span>
            <span data-cs-label>${saving ? "正在保存" : "保存"}</span>
          </button>
        </div>
      </form>`;
  }

  function csConfiguredControlsMarkup() {
    if (settingsState.confirmDelete) {
      return `
        <div class="cs-confirm" role="alertdialog" aria-label="确认删除 CoinStats key">
          <p>只删除本账号的 CoinStats key，不影响已同步持仓。确定删除吗？</p>
          <div class="cs-actions">
            <button class="cs-btn cs-btn--ghost" type="button" data-cs-cancel-delete>取消</button>
            <button class="cs-btn cs-btn--danger" type="button" data-cs-delete ${
              settingsState.deleting ? "disabled" : ""
            }>
              <span class="cs-spin" aria-hidden="true"></span>
              <span data-cs-label>${settingsState.deleting ? "正在删除" : "确认删除"}</span>
            </button>
          </div>
        </div>`;
    }
    if (settingsState.replacing) {
      return csReplaceFormMarkup();
    }
    return `
      <div class="cs-actions cs-actions--row" id="cs-actions">
        <button class="cs-btn cs-btn--primary" type="button" data-cs-replace>更换 key</button>
        <button class="cs-btn cs-btn--ghost cs-btn--quiet" type="button" data-cs-ask-delete>删除</button>
      </div>`;
  }

  function csKeyAreaMarkup() {
    const configured = Boolean(settingsState.key?.configured);
    return `${csHeadMarkup()}${configured ? csConfiguredControlsMarkup() : csEmptyFormMarkup()}`;
  }

  function csUsageMarkup() {
    if (!settingsState.key?.configured) return "";
    if (settingsState.creditsError) {
      return `
        <section class="cs-section" aria-label="本月额度">
          <div class="cs-section-head"><h3>本月额度</h3></div>
          <div class="cs-note cs-note--bad">
            <p>${escapeHtml(settingsState.creditsError)}</p>
            <button class="cs-link" type="button" data-cs-reload>重新读取</button>
          </div>
        </section>`;
    }
    const credits = settingsState.credits;
    if (!credits) {
      return `
        <section class="cs-section" aria-label="本月额度">
          <div class="cs-section-head"><h3>本月额度</h3></div>
          <div class="skeleton cs-sk-line"></div>
          <div class="skeleton cs-sk-bar"></div>
        </section>`;
    }
    const total = Number(credits.total) || 0;
    const used = Number(credits.used) || 0;
    const remaining = Number.isFinite(Number(credits.remaining))
      ? Number(credits.remaining)
      : Math.max(total - used, 0);
    const ratio = total > 0 ? Math.min(Math.max(used / total, 0), 1) : 0;
    const percent = Math.max(Math.round(ratio * 1000) / 10, used > 0 ? 0.4 : 0);
    const tone = ratio >= 0.9 ? "is-hot" : ratio >= 0.7 ? "is-warm" : "";
    const last = credits.last_sync_credits;
    const plan = credits.subscription ? String(credits.subscription).toUpperCase() : "";
    return `
      <section class="cs-section" aria-label="本月额度">
        <div class="cs-section-head">
          <h3>本月额度</h3>
          ${plan ? `<span class="cs-tag">${escapeHtml(plan)}</span>` : ""}
        </div>
        <p class="cs-credit-line">
          <span class="cs-credit-main"><b>${formatCredits(used)}</b><span> / ${formatCredits(total)}</span></span>
          <span class="cs-credit-remain">剩余 ${formatCredits(remaining)}</span>
        </p>
        <div
          class="cs-meter ${tone}"
          role="progressbar"
          aria-label="已用额度"
          aria-valuemin="0"
          aria-valuemax="${total}"
          aria-valuenow="${used}"
        ><span style="width:${percent}%;min-width:${used > 0 ? "5px" : "0"}"></span></div>
        <dl class="cs-facts">
          <div>
            <dt>上次同步后</dt>
            <dd>${
              last && Number.isFinite(Number(last.used))
                ? `已用 ${formatCredits(last.used)}`
                : "暂无记录"
            }</dd>
          </div>
          <div>
            <dt>单次同步</dt>
            <dd>约 35–40 credits</dd>
          </div>
        </dl>
      </section>`;
  }

  function csSyncButtonMarkup() {
    const busy = state.syncBusy;
    return `
      <div class="cs-sync-actions">
        <button class="sync-btn sync-btn--primary" type="button" data-sync-run
          ${busy ? 'disabled aria-busy="true"' : ""}>
          <span class="sync-spin" aria-hidden="true"></span>
          <span data-sync-label>${busy ? "同步中…" : "同步现在"}</span>
        </button>
      </div>`;
  }

  function friendlySyncError(rawError) {
    const raw = typeof rawError === "string" ? rawError.trim() : "";
    if (!raw) return "同步失败，请稍后重试";
    const at = raw.indexOf("skipped:");
    const problems = (at >= 0 ? raw.slice(0, at) : raw).replace(/[;；\s]+$/, "");
    if (!problems) return "同步失败，请稍后重试";
    const technical =
      !/[\u4e00-\u9fff]/.test(problems) || /Error|Traceback|psycopg|deadlock|SQL/i.test(problems);
    if (technical || problems.length > 80) return "同步失败，请稍后重试";
    return problems;
  }

  function csSyncMarkup() {
    const sync = settingsState.sync;
    const button = csSyncButtonMarkup();
    if (!sync) {
      return `
        <section class="cs-section" aria-label="最近同步">
          <div class="cs-section-head"><h3>最近同步</h3></div>
          <div class="skeleton cs-sk-line"></div>
          ${button}
        </section>`;
    }
    const status = String(sync.last_status || "");
    const label = !sync.last_run_at
      ? "尚未同步"
      : status === "ok"
        ? "正常"
        : status === "partial"
          ? "部分来源失败"
          : status
            ? "同步失败"
            : "—";
    const tone = !sync.last_run_at ? "" : status === "ok" ? "is-ok" : "is-bad";
    const trigger = { manual: "手动", scheduled: "定时", backfill: "回填" }[sync.last_trigger] || "";
    const rawError = typeof sync.last_error === "string" ? sync.last_error.trim() : "";
    let note = "";
    if (rawError) {
      const at = rawError.indexOf("skipped:");
      const problems = (at >= 0 ? rawError.slice(0, at) : rawError).replace(/[;；\s]+$/, "");
      const skipped = at >= 0 ? rawError.slice(at + 8) : "";
      if (problems) {
        const technical =
          !/[\u4e00-\u9fff]/.test(problems) || /Error|Traceback|psycopg|deadlock|SQL/i.test(problems);
        const friendly = technical
          ? "同步时遇到临时故障，系统会在下一轮自动重试；若持续失败，请稍后再看或联系管理员。"
          : problems;
        note += `
        <div class="cs-note cs-note--bad">
          <p>${escapeHtml(friendly)}</p>
        </div>`;
      }
      if (skipped) {
        const names = [...skipped.matchAll(/([A-Za-z0-9_-]+)\s*\(no api key\)/g)].map(
          (m) => m[1].charAt(0).toUpperCase() + m[1].slice(1),
        );
        const text = names.length
          ? `${names.join("、")} 未配置 key，已自动跳过，不影响 CoinStats 同步。`
          : "部分来源未配置，已自动跳过。";
        note += `
        <div class="cs-note">
          <p>${escapeHtml(text)}</p>
        </div>`;
      }
    }
    return `
      <section class="cs-section" aria-label="最近同步">
        <div class="cs-section-head">
          <h3>最近同步</h3>
          <span class="cs-state ${tone}"><i aria-hidden="true"></i>${label}</span>
        </div>
        <p class="cs-when">${escapeHtml(formatSyncTime(sync.last_run_at))}${trigger ? ` · ${trigger}` : ""}</p>
        ${note}
        ${button}
      </section>`;
  }

  function csWechatErrorMarkup() {
    const error = String(settingsState.wechatError || "").trim();
    if (!error) return "";
    return `
      <section class="cs-section" aria-label="最近错误">
        <div class="cs-section-head"><h3>最近错误</h3></div>
        <div class="cs-note cs-note--bad">
          <p class="cs-error-raw">${escapeHtml(error)}</p>
        </div>
      </section>`;
  }

  function csGuideMarkup() {
    const open = settingsState.guideOpen;
    return `
      <section class="cs-guide ${open ? "is-open" : ""}" aria-label="配置说明">
        <button class="cs-guide-toggle" type="button" data-cs-toggle-guide aria-expanded="${open ? "true" : "false"}">
          <span>怎么配置</span>
          <span class="cs-guide-caret" aria-hidden="true">${open ? "▴" : "▾"}</span>
        </button>
        ${
          open
            ? `<div class="cs-guide-body">
          <ol>
            <li>去 <b>openapi.coinstats.app</b> 申请你自己的免费 CoinStats key。</li>
            <li>把 key 粘贴到上方输入框，点「保存」。</li>
            <li>保存后 key 加密存储，页面只显示末 4 位。</li>
          </ol>
          <p class="cs-guide-aside">若在币安侧绑定只读 API：权限只勾「读取」，IP 选「不限制」。</p>
        </div>`
            : ""
        }
      </section>`;
  }

  function settingsNavMarkup() {
    return `
      <nav class="settings-nav" id="settings-nav" aria-label="设置板块">
        <p class="settings-nav-kicker">设置</p>
        <div class="settings-nav-links">
          ${SETTINGS_SECTIONS.map(
            (section) => `<a class="settings-nav-link" href="#${section.id}" data-settings-anchor="${section.id}">${escapeHtml(
              section.label,
            )}</a>`,
          ).join("")}
        </div>
        <a class="settings-nav-danger" href="#danger" data-settings-anchor="danger">危险操作在页面底部</a>
      </nav>`;
  }

  function settingsSectionContentMarkup(id) {
    if (id === "sync") {
      return `<div class="settings-group settings-group--pad" id="cs-sync-area">${csSyncMarkup()}</div>`;
    }
    if (id === "source") {
      return `
        <div class="settings-group settings-group--pad" id="cs-key-area">${csKeyAreaMarkup()}</div>
        <div class="settings-group settings-group--pad" id="cs-usage-area">${csUsageMarkup()}</div>
        <div class="settings-group settings-group--pad" id="cs-guide-area">${csGuideMarkup()}</div>`;
    }
    if (id === "wallet") {
      return `<div class="settings-group settings-group--pad" id="wl-area">${wlMarkup()}</div>`;
    }
    if (id === "alert") {
      return `
        <div class="settings-group settings-group--pad" id="alert-area">${alertMarkup()}</div>
        <div class="settings-group settings-group--pad" id="alert-history-area" aria-live="polite">${alertHistoryMarkup()}</div>`;
    }
    if (id === "wechat") {
      return `
        <div class="settings-group settings-group--pad" id="wechat-area">${wechatSettingsMarkup()}</div>
        <div class="settings-group settings-group--pad" id="cs-wechat-error-area">${csWechatErrorMarkup()}</div>`;
    }
    if (id === "account") {
      const username = state.user?.username || "未登录";
      return `
        <div class="settings-group settings-group--pad settings-account">
          <span class="settings-account-avatar" aria-hidden="true">${ICONS.users}</span>
          <span class="settings-account-copy">
            <strong>${escapeHtml(username)}</strong>
            <small>${state.user?.is_admin ? "管理员" : "成员"} · 本地部署，数据不离开本机</small>
          </span>
        </div>
        ${state.user?.is_admin ? `<div class="settings-admin">${usersCardMarkup()}</div>` : ""}`;
    }
    return `
      <div class="settings-group settings-group--privacy">
        <div class="settings-privacy-row">
          <span class="settings-privacy-icon" aria-hidden="true">${ICONS.key}</span>
          <span><strong>API key 仅保存在本机服务端</strong><small>前端只显示末 4 位，不写入浏览器存储</small></span>
        </div>
        <div class="settings-privacy-row">
          <span class="settings-privacy-icon" aria-hidden="true">${ICONS.wallet}</span>
          <span><strong>钱包地址只读</strong><small>只用于读取持仓，不发起任何交易</small></span>
        </div>
        <div class="settings-privacy-row">
          <span class="settings-privacy-icon" aria-hidden="true">${ICONS.safety}</span>
          <span><strong>没有数据时展示空状态</strong><small>不做估算，不伪造净值或收益</small></span>
        </div>
      </div>
      <div class="settings-group settings-group--pad">
        <button class="settings-inline-link" type="button" data-settings-jump="source">
          <span>查看配置说明</span><span aria-hidden="true">${ICONS.arrow}</span>
        </button>
      </div>`;
  }

  function settingsSectionMarkup(section) {
    return `
      <section class="settings-section" id="${section.id}" data-settings-section="${section.id}"
        aria-labelledby="settings-heading-${section.id}">
        <header class="settings-section-head">
          <span class="settings-section-icon" aria-hidden="true">${SETTINGS_LINE_ICONS[section.id] || ""}</span>
          <div>
            <h2 id="settings-heading-${section.id}">${escapeHtml(section.label)}</h2>
            <p>${escapeHtml(section.copy)}</p>
          </div>
        </header>
        <div class="settings-section-body">${settingsSectionContentMarkup(section.id)}</div>
      </section>`;
  }

  function settingsMarkup() {
    return `
      <div class="settings-home" id="settings-home">
        ${settingsHomeRowsMarkup(settingsHomeItems(settingsHomeData()))}
        ${settingsDangerMarkup("home")}
      </div>
      <div class="settings-sections" id="settings-sections">
        <div class="settings-subpage-bar">
          <button class="settings-back" type="button" data-settings-back>
            ${ICONS.back}<span>设置</span>
          </button>
          <span class="settings-subpage-title" id="settings-subpage-title">设置</span>
        </div>
        ${SETTINGS_SECTIONS.map(settingsSectionMarkup).join("")}
        ${settingsDangerMarkup("desktop", { id: "danger" })}
      </div>`;
  }


  function alertNumber(value, fallback) {
    const number = Number(value);
    return Number.isFinite(number) ? String(number) : String(fallback);
  }

  const ALERT_PROTOCOLS = ["deribit", "binance", "aave", "morpho", "pendle", "kamino", "jupiter"];

  function alertProtocolKey(protocol) {
    const text = String(protocol ?? "").trim().toLowerCase();
    return text || "global";
  }

  function alertProtocolsMatch(left, right) {
    return alertProtocolKey(left) === alertProtocolKey(right);
  }

  function alertRuleList() {
    return Array.isArray(settingsState.alertRules) ? settingsState.alertRules : [];
  }

  function alertRuleFor(protocol) {
    return alertRuleList().find((row) => row && alertProtocolsMatch(row.protocol, protocol)) || null;
  }

  function alertRuleFallback(protocol) {
    const global = alertRuleList().find((row) => row && row.protocol == null);
    const base = global || { warn_hf: 1.5, danger_hf: 1.2, cooldown_min: 360, enabled: true };
    return { ...base, id: null, protocol: protocol ?? null };
  }

  function alertRuleRank(rule) {
    if (!rule || rule.protocol == null) return -1;
    const index = ALERT_PROTOCOLS.indexOf(String(rule.protocol).toLowerCase());
    return index < 0 ? ALERT_PROTOCOLS.length : index;
  }

  function alertRuleSorted(rules) {
    return [...rules].sort((left, right) => alertRuleRank(left) - alertRuleRank(right));
  }

  function alertRuleBands(rule) {
    return `警戒 ${alertNumber(rule?.warn_hf, "1.5")} · 危险 ${alertNumber(rule?.danger_hf, "1.2")}`;
  }

  function alertRuleAvatar(protocol) {
    if (protocol == null) return "全";
    const name = protocolLabel(protocol);
    return (name.match(/[A-Za-z0-9]/)?.[0] || name.slice(0, 1) || "?").toUpperCase();
  }

  // A5: keep the selected scope in the view model. A protocol without a row
  // starts as a draft and only becomes a row after a successful PUT.
  let alertHintTimer = 0;

  function selectAlertRule(protocol) {
    settingsState.alertEditProtocol = protocol ?? null;
    settingsState.alertRule = alertRuleFor(protocol) || alertRuleFallback(protocol);
    settingsState.alertResetConfirm = null;
    settingsState.alertHint = "";
    settingsState.alertHintTone = "";
    window.clearTimeout(alertHintTimer);
    const area = document.getElementById("alert-area");
    if (area) area.innerHTML = alertMarkup();
  }

  function alertMarkup() {
    const rules = alertRuleList();
    const protocol = settingsState.alertEditProtocol ?? null;
    const rule = settingsState.alertRule || alertRuleFor(protocol) || alertRuleFallback(protocol);
    const saving = settingsState.alertSaving;
    const hintTone = settingsState.alertHintTone ? `is-${settingsState.alertHintTone}` : "";
    if (!rule) {
      return `<div class="skeleton cs-sk-line"></div><div class="skeleton cs-sk-input"></div>`;
    }
    const enabled = Boolean(rule.enabled);
    const isNew = protocol != null && !alertRuleFor(protocol);
    const canReset = protocol != null && !isNew;
    const resetConfirm = canReset && settingsState.alertResetConfirm === protocol;
    const resetting = settingsState.alertResetting;
    const scopeName = protocol == null ? "全局规则" : protocolLabel(protocol);
    const configured = new Set(rules.map((row) => alertProtocolKey(row.protocol)));
    const addable = ALERT_PROTOCOLS.filter((name) => !configured.has(name));
    const rows = alertRuleSorted(rules);
    const listRows = rows
      .map((row) => {
        const rowProtocol = row.protocol ?? null;
        const active = alertProtocolsMatch(rowProtocol, protocol);
        const name = rowProtocol == null ? "全局规则" : protocolLabel(rowProtocol);
        const scope = rowProtocol == null ? "所有协议" : "仅该协议";
        return `<button class="alert-rule-row${active ? " is-active" : ""}" type="button"
          data-alert-rule="${rowProtocol == null ? "global" : escapeHtml(rowProtocol)}"
          aria-pressed="${active}">
          <span class="alert-rule-avatar${row.enabled ? " is-on" : ""}" aria-hidden="true">${escapeHtml(
            alertRuleAvatar(rowProtocol),
          )}</span>
          <span class="alert-rule-name">${escapeHtml(name)}<small>${scope}</small></span>
          <span class="alert-rule-bands">${escapeHtml(alertRuleBands(row))}</span>
        </button>`;
      })
      .join("");
    const addChips = addable
      .map(
        (name) =>
          `<button class="alert-rule-add" type="button" data-alert-rule-add="${name}">＋ ${escapeHtml(
            protocolLabel(name),
          )}</button>`,
      )
      .join("");
    return `
      <div class="cs-head">
        <span class="cs-mark" aria-hidden="true">${ICONS.bell || ICONS.key}</span>
        <div class="cs-title">
          <h2>健康度告警</h2>
          <p>低于阈值推微信 · 协议可单独设阈值，未设置的沿用全局</p>
        </div>
      </div>
      <div class="alert-rules" role="list" aria-label="告警规则范围">${listRows}</div>
      ${
        addable.length
          ? `<div class="alert-rule-adds"><span>添加协议规则</span>${addChips}</div>`
          : ""
      }
      <form class="cs-form alert-form" id="alert-form" autocomplete="off" novalidate>
        <input type="hidden" name="protocol" value="${escapeHtml(protocol ?? "")}" />
        <p class="alert-scope"><span>适用范围</span><b>${escapeHtml(scopeName)}</b>${
          isNew ? `<em>新规则</em>` : ""
        }${
          canReset
            ? `<button class="alert-scope-reset${resetConfirm ? " is-confirm" : ""}" type="button"
                data-alert-rule-reset ${resetting ? "disabled" : ""}>${
                  resetting ? "正在恢复" : resetConfirm ? "确认恢复？" : "恢复沿用全局"
                }</button>`
            : ""
        }</p>
        <div class="alert-fields">
          <label class="cs-label" for="alert-warn">警戒线
            <input class="cs-input" id="alert-warn" name="warn_hf" inputmode="decimal" value="${escapeHtml(
              alertNumber(rule.warn_hf, "1.5"),
            )}" />
          </label>
          <label class="cs-label" for="alert-danger">危险线
            <input class="cs-input" id="alert-danger" name="danger_hf" inputmode="decimal" value="${escapeHtml(
              alertNumber(rule.danger_hf, "1.2"),
            )}" />
          </label>
          <label class="cs-label" for="alert-cooldown">冷却（分钟）
            <input class="cs-input" id="alert-cooldown" name="cooldown_min" inputmode="numeric" value="${escapeHtml(
              alertNumber(rule.cooldown_min, "360"),
            )}" />
          </label>
        </div>
        <label class="alert-enable" for="alert-enabled">
          <input id="alert-enabled" name="enabled" type="checkbox" ${enabled ? "checked" : ""} />
          <span>启用告警</span>
        </label>
        <p class="cs-hint ${hintTone}" id="alert-hint" role="status">${
          settingsState.alertHintTone === "ok" ? `<span class="cs-hint-check" aria-hidden="true">✓</span>` : ""
        }${escapeHtml(settingsState.alertHint || "")}</p>
        <div class="cs-actions">
          <button class="cs-btn cs-btn--primary ${saving ? "is-loading" : ""}" type="submit" ${saving ? "disabled" : ""}>
            <span class="cs-spin" aria-hidden="true"></span>
            <span>${saving ? "正在保存" : "保存阈值"}</span>
          </button>
        </div>
      </form>`;
  }

  const ALERT_HISTORY_LIMIT = 8;
  const AH_CHECK_SVG =
    `<svg viewBox="0 0 12 12" fill="none" aria-hidden="true">` +
    `<path d="m2.4 6.2 2.3 2.2 4.9-4.8"/></svg>`;
  const AH_LEVELS = {
    danger: { label: "危险", icon: PF_WARN_SVG },
    warn: { label: "警戒", icon: PF_WARN_SVG },
    recover: { label: "恢复", icon: AH_CHECK_SVG },
    stale: { label: "过期", icon: "" },
  };

  function shanghaiParts(date) {
    const shifted = new Date(date.getTime() + 8 * 60 * 60 * 1000);
    return {
      year: String(shifted.getUTCFullYear()),
      month: String(shifted.getUTCMonth() + 1).padStart(2, "0"),
      day: String(shifted.getUTCDate()).padStart(2, "0"),
      hour: String(shifted.getUTCHours()).padStart(2, "0"),
      minute: String(shifted.getUTCMinutes()).padStart(2, "0"),
    };
  }

  function parseInstant(value) {
    if (value == null || value === "") return null;
    if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value;
    let text = String(value).trim();
    if (!text) return null;
    // SQLite / older payloads may omit the zone. API timestamps are UTC.
    if (
      /^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}/.test(text) &&
      !/(?:Z|[+-]\d{2}:?\d{2})$/i.test(text)
    ) {
      text += "Z";
    }
    const date = new Date(text);
    return Number.isNaN(date.getTime()) ? null : date;
  }

  function absoluteTimeSH(date) {
    const parts = shanghaiParts(date);
    return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`;
  }

  function relativeTimeSH(date) {
    const diff = Date.now() - date.getTime();
    if (!Number.isFinite(diff) || diff < 60 * 1000) return "刚刚";
    const minutes = Math.floor(diff / (60 * 1000));
    if (minutes < 60) return `${minutes} 分钟前`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours} 小时前`;
    const days = Math.floor(hours / 24);
    if (days < 30) return `${days} 天前`;
    return absoluteTimeSH(date).slice(0, 10);
  }

  function alertEventTime(event) {
    const date = parseInstant(event?.created_at) || parseInstant(event?.sent_at);
    if (!date) return { label: "时间未知", absolute: "", iso: "" };
    const parts = shanghaiParts(date);
    return {
      label: `${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`,
      absolute: `${absoluteTimeSH(date)}（北京时间）`,
      iso: date.toISOString(),
    };
  }

  const AH_SOURCE_SVG =
    `<svg viewBox="0 0 12 12" fill="none" aria-hidden="true">` +
    `<ellipse cx="6" cy="3.1" rx="3.5" ry="1.5"/>` +
    `<path d="M2.5 3.1v5.6c0 .83 1.57 1.5 3.5 1.5s3.5-.67 3.5-1.5V3.1"/>` +
    `<path d="M2.5 5.9c0 .83 1.57 1.5 3.5 1.5s3.5-.67 3.5-1.5"/></svg>`;

  function alertLevelMarkup(level, isSource = false) {
    if (isSource) {
      return `<span class="ah-level ah-level--source" title="数据源事件"><span class="ah-ico" aria-hidden="true">${AH_SOURCE_SVG}</span><span>数据源</span></span>`;
    }
    const key = String(level || "").toLowerCase();
    const meta = Object.prototype.hasOwnProperty.call(AH_LEVELS, key)
      ? AH_LEVELS[key]
      : { label: "未知", icon: "" };
    const tone = Object.prototype.hasOwnProperty.call(AH_LEVELS, key) ? key : "unknown";
    return `<span class="ah-level ah-level--${tone}">${
      meta.icon ? `<span class="ah-ico" aria-hidden="true">${meta.icon}</span>` : ""
    }<span>${meta.label}</span></span>`;
  }

  // p7f3 ② / p8e2 ③: source rows are about freshness/sync, not a position HF.
  // Their second line must only add new information (HF / revoked); never
  // repeat the title or the pill. Delivery status is rendered separately.
  function alertEventKind(event, place) {
    if (!place?.hasPosition) return "";
    const raw = String(event?.channel_result || "").toLowerCase();
    return raw.startsWith("invalidated") ? "告警撤销" : "";
  }

  function alertDeliveryMeta(value) {
    const raw = String(value ?? "").trim();
    const lower = raw.toLowerCase();
    if (!lower) {
      return { key: "unknown", label: "未知", title: "未记录送达状态", icon: "" };
    }
    // Older rows used queued:ret=-2 for a dead token; render them as a failed
    // delivery instead of leaving a forever-"queued" badge.
    if (
      lower.includes("failed") ||
      lower.startsWith("reconcile_fail") ||
      lower.includes("ret=-2") ||
      lower.includes("ret -2")
    ) {
      return { key: "failed", label: "发送失败", title: "微信未送达", icon: PF_WARN_SVG };
    }
    if (
      lower === "sent" ||
      lower === "mock" ||
      lower.startsWith("sent:") ||
      lower.startsWith("mock:") ||
      lower.includes(":sent") ||
      lower.includes(":mock")
    ) {
      return { key: "delivered", label: "已送达", title: "微信已送达", icon: AH_CHECK_SVG };
    }
    if (lower.startsWith("queued") || lower.includes(":queued")) {
      return {
        key: "queued",
        label: "排队中",
        title: "等待微信发送",
        icon: `<span class="ah-dot" aria-hidden="true"></span>`,
      };
    }
    return { key: "unknown", label: "未知", title: `原始状态 ${raw}`, icon: "" };
  }

  // p7f2: A4 data-source alerts have position_id=NULL and no position object.
  // Keep their own title/source when the API provides one and never let a
  // missing id leak into the row as "#null" / "undefined".
  function alertCleanText(value) {
    if (value == null) return "";
    const text = String(value).trim();
    if (!text) return "";
    return /#?(?:null|undefined|nan|none)\b/i.test(text) ? "" : text;
  }

  function alertPositionId(event) {
    const raw = event?.position_id;
    if (raw == null) return null;
    if (typeof raw === "number") {
      return Number.isFinite(raw) && raw > 0 ? String(raw) : null;
    }
    const text = alertCleanText(raw);
    return !text || text === "0" ? null : text;
  }

  function alertReconcileSourceMap(payload) {
    // A4 stores its source identity on the reconcile log (tracked_sources /
    // fired / recovered), not on alert_events. Build event_id -> source so real
    // NULL-position rows can still say "数据源 xxx" without a backend change.
    const map = new Map();
    const items = Array.isArray(payload?.items) ? payload.items : [];
    for (const row of items) {
      const add = (entry) => {
        const id = entry?.event_id;
        const raw = alertCleanText(entry?.source);
        if (id == null || !raw) return;
        map.set(String(id), raw);
      };
      // p8e3 ⑦: a reconcile-failure alert is account-wide but names the
      // failing source on the reconcile log (detail.failed_sources). Use the
      // first one so its history title can say "Aave 账户对账" instead of a
      // context-free "账户对账".
      const reconcileAlert = row?.detail?.alert;
      const failedSources = row?.detail?.failed_sources;
      if (reconcileAlert && Array.isArray(failedSources)) {
        add({ event_id: reconcileAlert.event_id, source: failedSources[0] });
      }
      const fresh = row?.detail?.data_freshness;
      if (!fresh || typeof fresh !== "object") continue;
      if (Array.isArray(fresh.fired)) fresh.fired.forEach(add);
      if (Array.isArray(fresh.recovered)) {
        fresh.recovered.forEach((entry) => {
          add(entry);
          const prior = entry?.from_event_id;
          const raw = alertCleanText(entry?.source);
          if (prior != null && raw) map.set(String(prior), raw);
        });
      }
      const tracked = fresh.tracked_sources;
      if (tracked && typeof tracked === "object") {
        for (const [source, entry] of Object.entries(tracked)) {
          add({ source, event_id: entry?.event_id });
        }
      }
    }
    return map;
  }

  function alertTrackedSource(event) {
    return settingsState.alertSources instanceof Map
      ? alertCleanText(settingsState.alertSources.get(String(event?.id)))
      : "";
  }

  function alertEventRawSource(event) {
    const trackedRaw = alertTrackedSource(event);
    return (
      alertCleanText(event?.source_name) ||
      alertCleanText(event?.source) ||
      alertCleanText(event?.source_id) ||
      trackedRaw
    ).replace(/^数据源\s*/u, "");
  }

  function alertSourceDisplay(rawSource) {
    const raw = alertCleanText(rawSource).replace(/^stale:/i, "").trim();
    if (!raw) return "";
    let [head, ...rest] = raw.split(":");
    if (head === "lending" && rest.length >= 1) {
      head = rest[0];
      rest = rest.slice(1);
    }
    const tail = rest.join(":").trim();
    if (!tail) return protocolLabel(head.trim());
    if (/^market:/i.test(tail)) return `${protocolLabel(head.trim())} market`;
    return `${protocolLabel(head.trim())} ${tail}`;
  }

  function alertEventSourceName(event) {
    const raw = alertEventRawSource(event);
    if (!raw || /^stale:/i.test(raw)) return "";
    return alertSourceDisplay(raw);
  }

  function alertEventStaleSourceName(event) {
    const raw = alertEventRawSource(event);
    if (/^stale:/i.test(raw)) return alertSourceDisplay(raw);
    if (String(event?.source_kind || "").toLowerCase() === "stale") {
      return alertSourceDisplay(raw);
    }
    return "";
  }

  function alertEventStandaloneTitle(event) {
    const own = alertCleanText(event?.title) || alertCleanText(event?.subject);
    if (own) return own;
    const level = String(event?.level || "").toLowerCase();
    const staleSource = alertEventStaleSourceName(event);
    if (staleSource) {
      return level === "recover" ? `${staleSource} 同步恢复` : `${staleSource} 数据过期`;
    }
    const source = alertEventSourceName(event);
    const raw = String(event?.channel_result || "").toLowerCase();
    if (raw.startsWith("reconcile")) {
      return source ? `${source} 账户对账` : "账户对账";
    }
    if (source) {
      if (level === "recover") return `${source} 同步恢复`;
      if (level === "stale") return `${source} 数据过期`;
      return `${source} 同步异常`;
    }
    if (
      String(event?.event_kind || "").toLowerCase() === "wechat_send_health" ||
      raw.includes("health_alert")
    ) {
      return level === "recover" ? "微信推送通道恢复" : "微信推送通道异常";
    }
    if (raw.startsWith("invalidated")) return "仓位告警已撤销";
    if (level === "recover") return "数据源同步恢复";
    if (level === "stale") return "数据源数据过期";
    if (finiteNumber(event?.hf) != null) return "全账户";
    return "数据源同步异常";
  }

  function alertPositionMeta(event) {
    const id = alertPositionId(event);
    if (id == null) {
      const raw = String(event?.channel_result || "").toLowerCase();
      return {
        id: null,
        title: alertEventStandaloneTitle(event),
        protocol: raw.startsWith("reconcile") ? "系统" : "",
        hasPosition: false,
      };
    }
    const positions = settingsState.alertPositions;
    const position = positions instanceof Map ? positions.get(id) : null;
    if (!position) return { id, title: `仓位 #${id}`, protocol: "", hasPosition: true };
    const protocol = position.protocol ? protocolLabel(position.protocol) : "";
    return {
      id,
      title: position.label || protocol || `仓位 #${id}`,
      protocol,
      hasPosition: true,
    };
  }

  // p7f3: the row title already carries the protocol for position alerts
  // ("Aave V3 ETH", "Morpho PT-wstETH"). Repeating "Aave" / "Morpho" on the
  // meta line is noise, so only keep it when it adds information.
  function alertMetaProtocol(place) {
    const protocol = alertCleanText(place?.protocol);
    if (!protocol || protocol === "系统") return "";
    const title = String(place?.title || "").toLowerCase();
    if (title && title.includes(protocol.toLowerCase())) return "";
    return protocol;
  }

  function alertHistoryItemMarkup(event, index) {
    const when = alertEventTime(event);
    const place = alertPositionMeta(event);
    const hf = finiteNumber(event?.hf);
    const delivery = alertDeliveryMeta(event?.channel_result);
    const showDelivery = delivery.key !== "unknown";
    const level = String(event?.level || "").toLowerCase();
    const protocol = alertMetaProtocol(place);
    const isSource = !place.hasPosition;
    const kind = alertEventKind(event, place);
    const hasLeftMeta = Boolean(protocol || kind || hf != null);
    const showMeta = hasLeftMeta || showDelivery;
    const positionAttr = place.hasPosition
      ? ` data-alert-position-id="${escapeHtml(place.id)}"`
      : ` data-alert-position="none"`;
    return `<li class="ah-item${level === "recover" ? " ah-item--recover" : ""}${
      isSource ? " ah-item--source" : ""
    }${showMeta ? "" : " ah-item--compact"}" data-alert-event-id="${escapeHtml(
      event?.id ?? index,
    )}"${positionAttr}>
      <div class="ah-item-top">
        ${alertLevelMarkup(event?.level, isSource)}
        <span class="ah-where" title="${escapeHtml(place.title)}">${escapeHtml(place.title)}</span>
        <time class="ah-time"${when.iso ? ` datetime="${when.iso}"` : ""}${
          when.absolute ? ` title="${escapeHtml(when.absolute)}"` : ""
        }>${escapeHtml(when.label)}</time>
      </div>
      ${
        showMeta
          ? `<div class="ah-item-meta">
        ${
          protocol
            ? `<span class="ah-proto" title="${escapeHtml(protocol)}">${escapeHtml(protocol)}</span>`
            : kind
              ? `<span class="ah-kind">${escapeHtml(kind)}</span>`
              : ""
        }
        ${hf == null ? "" : `<span class="ah-hf">HF <b>${hf.toFixed(2)}</b></span>`}
        ${
          showDelivery
            ? `<span class="ah-delivery ah-delivery--${delivery.key}" title="${escapeHtml(
                delivery.title,
              )}">${delivery.icon}<span>${escapeHtml(delivery.label)}</span></span>`
            : ""
        }
      </div>`
          : ""
      }
    </li>`;
  }

  function alertHistoryMarkup() {
    const events = Array.isArray(settingsState.alertEvents) ? settingsState.alertEvents : [];
    const loading = settingsState.alertEventsLoading;
    const error = settingsState.alertEventsError;
    const expanded = settingsState.alertEventsExpanded;
    const visible = expanded ? events : events.slice(0, ALERT_HISTORY_LIMIT);
    const more = events.length - visible.length;

    let body = "";
    if (loading) {
      body = `<div class="ah-skeleton" aria-label="正在加载告警历史">
        <div class="skeleton ah-sk-row"></div>
        <div class="skeleton ah-sk-row"></div>
        <div class="skeleton ah-sk-row"></div>
      </div>`;
    } else if (error) {
      body = `<div class="ah-state ah-state--error" role="alert">
        <p class="ah-state-title">告警历史加载失败</p>
        <p class="ah-state-note">${escapeHtml(error)}</p>
        <button class="ah-retry" type="button" data-alert-history-refresh>重试</button>
      </div>`;
    } else if (!events.length) {
      body = `<div class="ah-state"><p class="ah-state-title">暂无告警记录</p></div>`;
    } else {
      body = `<ol class="ah-list">${visible.map(alertHistoryItemMarkup).join("")}</ol>
        ${
          events.length > ALERT_HISTORY_LIMIT
            ? `<button class="ah-more" type="button" data-alert-history-more>${
                expanded ? "收起" : `查看更早 ${more} 条`
              }</button>`
            : ""
        }`;
    }

    return `<div class="ah-head">
      <div class="ah-head-copy">
        <h3>告警历史</h3>
        <p>健康度提醒 · 北京时间</p>
      </div>
      <button class="ah-refresh" type="button" data-alert-history-refresh ${
        loading ? "disabled" : ""
      }>${loading ? "加载中" : "刷新"}</button>
    </div>
    <div class="ah-body"${loading ? ' aria-busy="true"' : ""}>${body}</div>`;
  }

  async function loadAlertHistory(seq) {
    if (!document.getElementById("alert-history-area")) return;
    settingsState.alertEventsLoading = true;
    settingsState.alertEventsError = "";
    const area = document.getElementById("alert-history-area");
    if (area) area.innerHTML = alertHistoryMarkup();
    try {
      const [eventsResult, positionsResult, sourcesResult] = await Promise.allSettled([
        api("/alerts/events"),
        api("/positions?kind=lending"),
        api("/sync/reconcile?limit=20"),
      ]);
      if (seq !== state.renderSeq) return;
      if (eventsResult.status === "rejected") throw eventsResult.reason;
      const events = Array.isArray(eventsResult.value) ? eventsResult.value : [];
      const rows =
        positionsResult.status === "fulfilled" && Array.isArray(positionsResult.value)
          ? positionsResult.value
          : [];
      settingsState.alertEvents = events;
      settingsState.alertPositions = new Map(rows.map((position) => [String(position.id), position]));
      settingsState.alertSources =
        sourcesResult.status === "fulfilled"
          ? alertReconcileSourceMap(sourcesResult.value)
          : new Map();
      settingsState.alertEventsLoading = false;
      settingsState.alertEventsError = "";
      const fresh = document.getElementById("alert-history-area");
      if (fresh) fresh.innerHTML = alertHistoryMarkup();
    } catch (error) {
      if (error.status === 401) {
        expireSession();
        return;
      }
      if (seq !== state.renderSeq) return;
      settingsState.alertEventsLoading = false;
      settingsState.alertEventsError =
        error.status === 0 ? "网络连接失败，请稍后重试" : error.message || "请求失败";
      const fresh = document.getElementById("alert-history-area");
      if (fresh) fresh.innerHTML = alertHistoryMarkup();
    }
  }

  async function loadAlertRule(seq) {
    const area = document.getElementById("alert-area");
    if (!area) return;
    try {
      const rows = await api("/alerts/rules");
      if (seq !== state.renderSeq) return;
      const list = Array.isArray(rows) ? rows : [];
      settingsState.alertRules = list;
      settingsState.alertEditProtocol = null;
      settingsState.alertRule = alertRuleFor(null) || list[0] || null;
      settingsState.alertEditProtocol = settingsState.alertRule?.protocol ?? null;
      settingsState.alertHint = "";
      settingsState.alertHintTone = "";
      area.innerHTML = alertMarkup();
    } catch (error) {
      if (error.status === 401) {
        expireSession();
        return;
      }
      if (seq !== state.renderSeq) return;
      settingsState.alertHint = error.status === 0 ? "网络连接失败，请稍后重试" : error.message;
      settingsState.alertHintTone = "bad";
      const areaNow = document.getElementById("alert-area");
      if (areaNow && settingsState.alertRule) areaNow.innerHTML = alertMarkup();
    }
  }

  async function saveAlertRule(form) {
    if (settingsState.alertSaving) return;
    const protocolText = String(form.elements.protocol?.value || "").trim().toLowerCase();
    const protocol = protocolText || null;
    const warnText = String(form.elements.warn_hf?.value || "").trim();
    const dangerText = String(form.elements.danger_hf?.value || "").trim();
    const cooldownText = String(form.elements.cooldown_min?.value || "").trim();
    const warn = Number(warnText);
    const danger = Number(dangerText);
    const cooldown = Number(cooldownText);
    const enabled = Boolean(form.elements.enabled?.checked);
    if (
      !warnText ||
      !dangerText ||
      !cooldownText ||
      !Number.isFinite(warn) ||
      !Number.isFinite(danger) ||
      !Number.isFinite(cooldown) ||
      !(warn > 0) ||
      !(danger > 0) ||
      !Number.isInteger(cooldown) ||
      cooldown < 0
    ) {
      settingsState.alertHint = "警戒线、危险线须为正数，冷却为非负整数";
      settingsState.alertHintTone = "bad";
      const area = document.getElementById("alert-area");
      if (area) area.innerHTML = alertMarkup();
      return;
    }
    if (!(danger < warn)) {
      settingsState.alertHint = "危险线必须小于警戒线";
      settingsState.alertHintTone = "bad";
      const area = document.getElementById("alert-area");
      if (area) area.innerHTML = alertMarkup();
      return;
    }
    const seq = state.renderSeq;
    settingsState.alertSaving = true;
    settingsState.alertResetConfirm = null;
    settingsState.alertHint = "";
    settingsState.alertHintTone = "";
    window.clearTimeout(alertHintTimer);
    const area = document.getElementById("alert-area");
    if (area) area.innerHTML = alertMarkup();
    try {
      const saved = await api("/alerts/rules", {
        method: "PUT",
        body: { protocol, warn_hf: warn, danger_hf: danger, enabled, cooldown_min: cooldown },
      });
      settingsState.alertSaving = false;
      if (seq !== state.renderSeq) return;
      const savedProtocol = saved?.protocol ?? protocol;
      const list = [...alertRuleList()];
      const index = list.findIndex((row) => alertProtocolsMatch(row?.protocol, savedProtocol));
      if (index >= 0) list[index] = saved;
      else list.push(saved);
      settingsState.alertRules = alertRuleSorted(list);
      settingsState.alertEditProtocol = savedProtocol ?? null;
      settingsState.alertRule = saved;
      settingsState.alertHint = "已保存";
      settingsState.alertHintTone = "ok";
      const fresh = document.getElementById("alert-area");
      if (fresh) fresh.innerHTML = alertMarkup();
      showToast(protocol ? `${protocolLabel(protocol)}告警规则已保存` : "告警阈值已保存");
      alertHintTimer = window.setTimeout(() => {
        if (settingsState.alertHint !== "已保存") return;
        settingsState.alertHint = "";
        settingsState.alertHintTone = "";
        const areaNow = document.getElementById("alert-area");
        if (areaNow) areaNow.innerHTML = alertMarkup();
      }, 2400);
    } catch (error) {
      settingsState.alertSaving = false;
      if (error.status === 401) {
        expireSession();
        return;
      }
      if (seq !== state.renderSeq) return;
      // Keep the submitted values visible next to the server's 422 message.
      settingsState.alertRule = {
        ...(settingsState.alertRule && typeof settingsState.alertRule === "object"
          ? settingsState.alertRule
          : {}),
        protocol,
        warn_hf: warn,
        danger_hf: danger,
        enabled,
        cooldown_min: cooldown,
      };
      settingsState.alertHint = error.status === 0 ? "网络连接失败，请稍后重试" : error.message;
      settingsState.alertHintTone = "bad";
      const fresh = document.getElementById("alert-area");
      if (fresh) fresh.innerHTML = alertMarkup();
    }
  }

  async function resetAlertRule() {
    const protocol = settingsState.alertEditProtocol;
    if (!protocol || settingsState.alertResetting) return;
    if (settingsState.alertResetConfirm !== protocol) {
      settingsState.alertResetConfirm = protocol;
      settingsState.alertHint = "";
      settingsState.alertHintTone = "";
      const area = document.getElementById("alert-area");
      if (area) area.innerHTML = alertMarkup();
      return;
    }
    const seq = state.renderSeq;
    settingsState.alertResetting = true;
    settingsState.alertHint = "";
    settingsState.alertHintTone = "";
    window.clearTimeout(alertHintTimer);
    let area = document.getElementById("alert-area");
    if (area) area.innerHTML = alertMarkup();
    try {
      await api(`/alerts/rules?protocol=${encodeURIComponent(protocol)}`, { method: "DELETE" });
      if (seq !== state.renderSeq) return;
      settingsState.alertResetting = false;
      settingsState.alertResetConfirm = null;
      settingsState.alertRules = alertRuleList().filter(
        (row) => !alertProtocolsMatch(row?.protocol, protocol),
      );
      settingsState.alertRule = alertRuleFor(protocol) || alertRuleFallback(protocol);
      settingsState.alertHint = "已恢复沿用全局";
      settingsState.alertHintTone = "ok";
      area = document.getElementById("alert-area");
      if (area) area.innerHTML = alertMarkup();
      showToast(`${protocolLabel(protocol)}已恢复沿用全局规则`);
      alertHintTimer = window.setTimeout(() => {
        if (settingsState.alertHint !== "已恢复沿用全局") return;
        settingsState.alertHint = "";
        settingsState.alertHintTone = "";
        const fresh = document.getElementById("alert-area");
        if (fresh) fresh.innerHTML = alertMarkup();
      }, 2400);
    } catch (error) {
      settingsState.alertResetting = false;
      if (error.status === 401) {
        expireSession();
        return;
      }
      if (seq !== state.renderSeq) return;
      settingsState.alertHint =
        error.status === 0 ? "网络连接失败，请稍后重试" : error.message || "恢复失败";
      settingsState.alertHintTone = "bad";
      area = document.getElementById("alert-area");
      if (area) area.innerHTML = alertMarkup();
    }
  }


  function usersShanghaiTime(value) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "—";
    return new Intl.DateTimeFormat("zh-CN", {
      timeZone: "Asia/Shanghai",
      month: "numeric",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    }).format(date);
  }

  function usersCardMarkup() {
    return `
      <article class="cs-card users-card" id="users-card">
        <div id="users-area">${usersMarkup()}</div>
      </article>`;
  }

  function usersListMarkup() {
    const users = settingsState.users;
    if (users === null || settingsState.usersLoading) {
      return `<div class="skeleton cs-sk-line"></div><div class="skeleton cs-sk-line"></div>`;
    }
    if (!users.length) {
      return `<p class="users-empty">还没有账号。</p>`;
    }
    const resetId = settingsState.usersResetId;
    return `
      <ul class="users-list">
        ${users
          .map((user) => {
            const confirming = resetId === user.id;
            const otherLocked = resetId != null && !confirming;
            return `
          <li class="users-item ${confirming ? "is-confirming" : ""}">
            <div class="users-row ${confirming ? "is-target" : ""}">
              <div class="users-main">
                <span class="users-name">${escapeHtml(user.username)}</span>
                ${user.is_admin ? `<span class="users-badge">admin</span>` : ""}
              </div>
              <span class="users-meta">${escapeHtml(usersShanghaiTime(user.created_at))}</span>
              <button
                class="users-reset ${otherLocked ? "is-dimmed" : ""}"
                type="button"
                data-users-ask-reset="${user.id}"
                ${otherLocked || settingsState.usersResetting || settingsState.usersSaving ? "disabled" : ""}
              >重置密码</button>
            </div>
            ${
              confirming
                ? `<form class="users-reset-form" id="users-reset-form" autocomplete="off" novalidate>
              <label class="cs-label" for="users-reset-password">新密码</label>
              <input
                class="cs-input users-input"
                id="users-reset-password"
                name="password"
                type="password"
                autocomplete="new-password"
                minlength="6"
                maxlength="72"
                placeholder="至少 6 位"
                required
              />
              <div class="cs-actions">
                <button class="cs-btn cs-btn--ghost" type="button" data-users-cancel-reset ${
                  settingsState.usersResetting ? "disabled" : ""
                }>取消</button>
                <button class="cs-btn cs-btn--primary ${settingsState.usersResetting ? "is-loading" : ""}" type="submit" data-users-confirm-reset ${
                  settingsState.usersResetting ? "disabled" : ""
                }>
                  <span class="cs-spin" aria-hidden="true"></span>
                  <span>${settingsState.usersResetting ? "正在重置" : "确认"}</span>
                </button>
              </div>
            </form>`
                : ""
            }
          </li>`;
          })
          .join("")}
      </ul>`;
  }

  function usersFormMarkup() {
    const users = settingsState.users;
    const hasList = Array.isArray(users) && users.length > 0;
    const formOpen = !hasList || settingsState.usersFormOpen;
    if (hasList && !formOpen) {
      return `
      <div class="users-form-collapsed">
        <button class="users-add-toggle" type="button" data-users-open-form>
          <span aria-hidden="true">＋</span>
          <span>开账号</span>
        </button>
      </div>`;
    }
    const saving = settingsState.usersSaving;
    const hintTone = settingsState.usersHintTone ? `is-${settingsState.usersHintTone}` : "";
    const cancelBtn = hasList
      ? `<button class="cs-btn cs-btn--ghost" type="button" data-users-cancel-form ${saving ? "disabled" : ""}>取消</button>`
      : "";
    return `
      <form class="users-form ${hasList ? "users-form--listed" : "users-form--empty"}" id="users-form" autocomplete="off" novalidate>
        <div class="users-fields">
          <label class="cs-label" for="users-username">用户名</label>
          <input
            class="cs-input users-input"
            id="users-username"
            name="username"
            type="text"
            autocomplete="off"
            autocapitalize="off"
            autocorrect="off"
            spellcheck="false"
            minlength="3"
            maxlength="32"
            placeholder="字母、数字、_ . -"
          />
          <label class="cs-label" for="users-password">密码</label>
          <input
            class="cs-input users-input"
            id="users-password"
            name="password"
            type="password"
            autocomplete="new-password"
            minlength="6"
            maxlength="72"
            placeholder="至少 6 位"
          />
        </div>
        <p class="cs-hint ${hintTone}" id="users-hint" role="status">${escapeHtml(settingsState.usersHint || "")}</p>
        <div class="cs-actions">
          ${cancelBtn}
          <button class="cs-btn cs-btn--primary ${saving ? "is-loading" : ""}" type="submit" ${
            saving ? "disabled" : ""
          }>
            <span class="cs-spin" aria-hidden="true"></span>
            <span>${saving ? "正在创建" : "开账号"}</span>
          </button>
        </div>
      </form>`;
  }

  function usersMarkup() {
    return `
      <div class="cs-head">
        <span class="cs-mark" aria-hidden="true">${ICONS.users}</span>
        <div class="cs-title">
          <h2>用户</h2>
          <p>开账号 / 重置密码，仅管理员</p>
        </div>
      </div>
      <div id="users-list">${usersListMarkup()}</div>
      ${usersFormMarkup()}`;
  }

  function usersRefresh() {
    const area = document.getElementById("users-area");
    if (!area || !state.user?.is_admin) return;
    area.innerHTML = usersMarkup();
  }

  async function loadUsers(seq) {
    if (!state.user?.is_admin) return;
    settingsState.users = null;
    settingsState.usersLoading = true;
    const list = document.getElementById("users-list");
    if (list) list.innerHTML = usersListMarkup();
    try {
      const rows = await api("/admin/users");
      if (seq !== state.renderSeq) return;
      settingsState.usersLoading = false;
      settingsState.users = Array.isArray(rows) ? rows : [];
      usersRefresh();
    } catch (error) {
      if (error.status === 401) {
        expireSession();
        return;
      }
      if (seq !== state.renderSeq) return;
      settingsState.usersLoading = false;
      settingsState.users = [];
      settingsState.usersHint = error.status === 0 ? "网络连接失败，请稍后重试" : error.message;
      settingsState.usersHintTone = "bad";
      usersRefresh();
    }
  }

  async function createUser() {
    if (settingsState.usersSaving) return;
    const username = (document.getElementById("users-username")?.value || "").trim();
    const password = document.getElementById("users-password")?.value || "";
    if (!username || !password) {
      settingsState.usersHint = "请填写用户名和密码";
      settingsState.usersHintTone = "bad";
      settingsState.usersFormOpen = true;
      usersRefresh();
      document.getElementById(username ? "users-password" : "users-username")?.focus();
      return;
    }
    const seq = state.renderSeq;
    settingsState.usersSaving = true;
    settingsState.usersHint = "";
    settingsState.usersHintTone = "";
    settingsState.usersResetId = null;
    usersRefresh();
    const nameEl = document.getElementById("users-username");
    const passEl = document.getElementById("users-password");
    if (nameEl) nameEl.value = username;
    if (passEl) passEl.value = password;
    try {
      await api("/admin/users", { method: "POST", body: { username, password } });
      if (seq !== state.renderSeq) return;
      settingsState.usersSaving = false;
      settingsState.usersHint = "";
      settingsState.usersHintTone = "";
      settingsState.usersFormOpen = false;
      const rows = await api("/admin/users");
      if (seq !== state.renderSeq) return;
      settingsState.users = Array.isArray(rows) ? rows : [];
      usersRefresh();
      showToast("账号已创建");
    } catch (error) {
      settingsState.usersSaving = false;
      if (error.status === 401) {
        expireSession();
        return;
      }
      if (seq !== state.renderSeq) return;
      settingsState.usersHint = error.status === 0 ? "网络连接失败，请稍后重试" : error.message;
      settingsState.usersHintTone = "bad";
      settingsState.usersFormOpen = true;
      usersRefresh();
      const nameRestore = document.getElementById("users-username");
      const passRestore = document.getElementById("users-password");
      if (nameRestore) nameRestore.value = username;
      if (passRestore) passRestore.value = password;
      nameRestore?.focus();
    }
  }

  async function resetUserPassword(form) {
    const id = settingsState.usersResetId;
    if (id == null || settingsState.usersResetting) return;
    const password = (form?.querySelector("#users-reset-password")?.value || "").trim();
    if (password.length < 6) {
      showToast("新密码至少 6 位", "error");
      form?.querySelector("#users-reset-password")?.focus();
      return;
    }
    const seq = state.renderSeq;
    settingsState.usersResetting = true;
    usersRefresh();
    const input = document.getElementById("users-reset-password");
    if (input) input.value = password;
    try {
      await api(`/admin/users/${id}/reset-password`, { method: "POST", body: { password } });
      if (seq !== state.renderSeq) return;
      settingsState.usersResetting = false;
      settingsState.usersResetId = null;
      usersRefresh();
      showToast("密码已重置");
    } catch (error) {
      settingsState.usersResetting = false;
      if (error.status === 401) {
        expireSession();
        return;
      }
      if (seq !== state.renderSeq) return;
      settingsState.usersResetId = null;
      usersRefresh();
      showToast(error.status === 0 ? "网络连接失败，请稍后重试" : error.message, "error");
    }
  }


  function truncateAddress(address) {
    const value = String(address || "");
    if (value.length <= 16) return value;
    return `${value.slice(0, 8)}…${value.slice(-6)}`;
  }

  function wlListMarkup() {
    const wallets = settingsState.wallets;
    if (wallets === null) {
      return `<div class="skeleton cs-sk-line"></div><div class="skeleton cs-sk-line"></div>`;
    }
    if (!wallets.length) {
      return `<p class="wl-empty">还没有钱包。添加后，同步会读取这些地址的链上流水。</p>`;
    }
    const deleteId = settingsState.walletDeleteId;
    return `
      <ul class="wl-list">
        ${wallets
          .map((wallet) => {
            const confirming = deleteId === wallet.id;
            const otherLocked = deleteId != null && !confirming;
            const chainClass = wallet.chain === "solana" ? "is-solana" : "is-evm";
            const chainLabel = wallet.chain === "solana" ? "Solana" : "EVM";
            return `
          <li class="wl-item ${confirming ? "is-confirming" : ""}">
            <div class="wl-row ${confirming ? "is-target" : ""}">
              <span class="wl-chain ${chainClass}">${escapeHtml(chainLabel)}</span>
              <button
                class="wl-address"
                type="button"
                data-wl-copy="${escapeHtml(wallet.address)}"
                title="${escapeHtml(wallet.address)}"
              >
                <code>${escapeHtml(truncateAddress(wallet.address))}</code>
                <span class="wl-copy-ico" aria-hidden="true">${ICONS.copy}</span>
              </button>
              <button
                class="wl-del ${otherLocked ? "is-dimmed" : ""}"
                type="button"
                data-wl-ask-delete="${wallet.id}"
                aria-label="删除钱包"
                ${otherLocked || settingsState.walletDeleting ? "disabled" : ""}
              >删除</button>
            </div>
            ${
              confirming
                ? `<div class="cs-confirm wl-confirm" role="alertdialog" aria-label="确认删除钱包">
              <p>只删除本账号里这条地址，不影响已同步持仓与流水。确定删除吗？</p>
              <div class="cs-actions">
                <button class="cs-btn cs-btn--ghost" type="button" data-wl-cancel-delete>取消</button>
                <button class="cs-btn cs-btn--danger" type="button" data-wl-delete ${
                  settingsState.walletDeleting ? "disabled" : ""
                }>
                  <span class="cs-spin" aria-hidden="true"></span>
                  <span>${settingsState.walletDeleting ? "正在删除" : "确认删除"}</span>
                </button>
              </div>
            </div>`
                : ""
            }
          </li>`;
          })
          .join("")}
      </ul>`;
  }

  function wlFormMarkup() {
    const wallets = settingsState.wallets;
    const hasList = Array.isArray(wallets) && wallets.length > 0;
    const formOpen = !hasList || settingsState.walletFormOpen;
    if (hasList && !formOpen) {
      return `
      <div class="wl-form-collapsed">
        <button class="wl-add-toggle" type="button" data-wl-open-form>
          <span aria-hidden="true">＋</span>
          <span>添加钱包</span>
        </button>
      </div>`;
    }
    const saving = settingsState.walletSaving;
    const hintTone = settingsState.walletHintTone ? `is-${settingsState.walletHintTone}` : "";
    const cancelBtn = hasList
      ? `<button class="cs-btn cs-btn--ghost" type="button" data-wl-cancel-form>取消</button>`
      : "";
    return `
      <form class="wl-form ${hasList ? "wl-form--listed" : "wl-form--empty"}" id="wl-form" autocomplete="off" novalidate>
        <div class="wl-fields">
          <label class="cs-label" for="wl-chain">链</label>
          <select class="wl-select" id="wl-chain" name="chain">
            <option value="evm">EVM（0x…）</option>
            <option value="solana">Solana</option>
          </select>
          <label class="cs-label" for="wl-address">地址</label>
          <input
            class="cs-input wl-input"
            id="wl-address"
            type="text"
            inputmode="text"
            autocomplete="off"
            autocapitalize="off"
            autocorrect="off"
            spellcheck="false"
            placeholder="粘贴钱包地址"
          />
        </div>
        <p class="cs-hint ${hintTone}" id="wl-hint" role="status">${escapeHtml(settingsState.walletHint || "")}</p>
        <div class="cs-actions">
          ${cancelBtn}
          <button class="cs-btn cs-btn--primary ${saving ? "is-loading" : ""}" type="submit" ${
            saving ? "disabled" : ""
          }>
            <span class="cs-spin" aria-hidden="true"></span>
            <span>${saving ? "正在添加" : "添加钱包"}</span>
          </button>
        </div>
      </form>`;
  }

  function wlMarkup() {
    return `
      <div class="cs-head">
        <span class="cs-mark" aria-hidden="true">${ICONS.wallet}</span>
        <div class="cs-title">
          <h2>钱包</h2>
          <p>EVM / Solana 地址，仅本人可见</p>
        </div>
      </div>
      <div id="wl-list">${wlListMarkup()}</div>
      ${wlFormMarkup()}`;
  }

    function wlRefresh() {
    csSet("wl-area", wlMarkup());
  }

  async function loadWallets(seq) {
    settingsState.wallets = null;
    const list = document.getElementById("wl-list");
    if (list) list.innerHTML = wlListMarkup();
    try {
      const rows = await api("/wallets");
      if (seq !== state.renderSeq) return;
      settingsState.wallets = Array.isArray(rows) ? rows : [];
      wlRefresh();
    } catch (error) {
      if (error.status === 401) {
        expireSession();
        return;
      }
      if (seq !== state.renderSeq) return;
      settingsState.wallets = [];
      settingsState.walletHint = error.status === 0 ? "网络连接失败，请稍后重试" : error.message;
      settingsState.walletHintTone = "bad";
      wlRefresh();
    }
  }

  async function saveWallet() {
    if (settingsState.walletSaving) return;
    const chain = document.getElementById("wl-chain")?.value || "evm";
    const input = document.getElementById("wl-address");
    const address = (input?.value || "").trim();
    if (!address) {
      settingsState.walletHint = "请先粘贴地址";
      settingsState.walletHintTone = "bad";
      settingsState.walletFormOpen = true;
      wlRefresh();
      document.getElementById("wl-address")?.focus();
      return;
    }
    const seq = state.renderSeq;
    settingsState.walletSaving = true;
    settingsState.walletHint = "";
    settingsState.walletHintTone = "";
    settingsState.walletDeleteId = null;
    wlRefresh();
    const chainEl = document.getElementById("wl-chain");
    const addrEl = document.getElementById("wl-address");
    if (chainEl) chainEl.value = chain;
    if (addrEl) {
      addrEl.value = address;
      addrEl.readOnly = true;
    }
    try {
      await api("/wallets", { method: "POST", body: { chain, address } });
      if (seq !== state.renderSeq) return;
      settingsState.walletSaving = false;
      settingsState.walletHint = "";
      settingsState.walletHintTone = "";
      settingsState.walletFormOpen = false;
      showToast("钱包已添加");
      const rows = await api("/wallets");
      if (seq !== state.renderSeq) return;
      settingsState.wallets = Array.isArray(rows) ? rows : [];
      wlRefresh();
    } catch (error) {
      settingsState.walletSaving = false;
      if (error.status === 401) {
        expireSession();
        return;
      }
      if (seq !== state.renderSeq) return;
      settingsState.walletHint = error.status === 0 ? "网络连接失败，请稍后重试" : error.message;
      settingsState.walletHintTone = "bad";
      settingsState.walletFormOpen = true;
      wlRefresh();
      const chainRestore = document.getElementById("wl-chain");
      const addrRestore = document.getElementById("wl-address");
      if (chainRestore) chainRestore.value = chain;
      if (addrRestore) {
        addrRestore.value = address;
        addrRestore.readOnly = false;
      }
      showToast("添加失败", "error");
    }
  }

  async function deleteAllWallets() {
    if (settingsState.walletDeletingAll || settingsState.walletDeleting) return;
    const seq = state.renderSeq;
    const wallets = Array.isArray(settingsState.wallets) ? [...settingsState.wallets] : [];
    if (!wallets.length) {
      showToast("没有可删除的钱包");
      return;
    }
    settingsState.walletDeletingAll = true;
    settingsState.walletHint = "";
    settingsState.walletHintTone = "";
    wlRefresh();
    try {
      for (const wallet of wallets) {
        await api(`/wallets/${wallet.id}`, { method: "DELETE" });
      }
    } catch (error) {
      settingsState.walletDeletingAll = false;
      if (error.status === 401) {
        expireSession();
        return;
      }
      if (seq !== state.renderSeq) return;
      showToast("删除未全部完成，正在刷新列表", "error");
      loadWallets(seq);
      return;
    }
    settingsState.walletDeletingAll = false;
    settingsState.walletDeleteId = null;
    settingsState.wallets = [];
    showToast(`已删除 ${wallets.length} 个钱包`);
    wlRefresh();
  }

  async function deleteWallet() {
    const id = settingsState.walletDeleteId;
    if (id == null || settingsState.walletDeleting) return;
    const seq = state.renderSeq;
    settingsState.walletDeleting = true;
    wlRefresh();
    try {
      await api(`/wallets/${id}`, { method: "DELETE" });
      if (seq !== state.renderSeq) return;
      settingsState.walletDeleting = false;
      settingsState.walletDeleteId = null;
      settingsState.wallets = (settingsState.wallets || []).filter((row) => row.id !== id);
      showToast("已删除钱包");
      wlRefresh();
    } catch (error) {
      settingsState.walletDeleting = false;
      if (error.status === 401) {
        expireSession();
        return;
      }
      if (seq !== state.renderSeq) return;
      settingsState.walletDeleteId = null;
      settingsState.walletHint = error.status === 0 ? "网络连接失败，请稍后重试" : error.message;
      settingsState.walletHintTone = "bad";
      wlRefresh();
      showToast("删除失败", "error");
    }
  }

  async function copyWalletAddress(address, button) {
    try {
      await navigator.clipboard.writeText(address);
      showToast("地址已复制");
      if (button) {
        button.classList.add("is-copied");
        window.setTimeout(() => button.classList.remove("is-copied"), 1200);
      }
    } catch {
      showToast("复制失败，请长按地址手动复制", "error");
    }
  }


    function csSet(id, html) {
    const node = document.getElementById(id);
    if (node) node.innerHTML = html;
  }

  function csSetHint(message, tone = "") {
    const hint = document.getElementById("cs-hint");
    if (!hint) return;
    hint.textContent = message || "";
    hint.className = `cs-hint ${tone ? `is-${tone}` : ""}`;
  }

  function csRefreshKeyArea() {
    csSet("cs-key-area", csKeyAreaMarkup());
  }

  function refreshSyncButtons() {
    const busy = state.syncBusy;
    document.querySelectorAll("[data-sync-run]").forEach((button) => {
      button.disabled = busy;
      if (busy) button.setAttribute("aria-busy", "true");
      else button.removeAttribute("aria-busy");
      const label = button.querySelector("[data-sync-label]");
      if (!label) return;
      const quiet = button.classList.contains("sync-btn--quiet");
      label.textContent = busy ? "同步中…" : quiet ? "同步" : "同步现在";
    });
  }

  function sleep(ms) {
    return new Promise((resolve) => {
      window.setTimeout(resolve, ms);
    });
  }

  async function runManualSync() {
    if (state.syncBusy) return;
    state.syncBusy = true;
    refreshSyncButtons();
    let outcome = null;
    try {
      let accepted;
      try {
        accepted = await api("/sync?async=true", { method: "POST" });
      } catch (error) {
        if (error.status === 401) {
          expireSession();
          return;
        }
        const queueDown = error.status === 0 || error.status === 503;
        showToast(
          queueDown ? "同步队列不可用，请稍后重试" : error.message || "同步未能排队",
          "error",
        );
        return;
      }
      const jobId = accepted && accepted.job_id;
      if (!jobId) {
        showToast("同步未能排队", "error");
        return;
      }
      const started = Date.now();
      const terminal = new Set(["finished", "failed", "canceled", "cancelled"]);
      let job = null;
      while (Date.now() - started < 90000) {
        await sleep(1200);
        try {
          job = await api(`/sync/status?job_id=${encodeURIComponent(jobId)}`);
        } catch (error) {
          if (error.status === 401) {
            expireSession();
            return;
          }
          if (error.status === 0 || error.status === 503) {
            showToast("同步队列不可用，请稍后重试", "error");
            return;
          }
          continue;
        }
        const status = String(job?.status || "").toLowerCase();
        if (terminal.has(status)) break;
      }
      const finalStatus = String(job?.status || "").toLowerCase();
      if (!terminal.has(finalStatus)) {
        outcome = "timeout";
      } else {
        outcome = job;
      }

      let latest = null;
      try {
        latest = await api("/sync/status");
        settingsState.sync = latest || {};
        updateMoreSheetSync();
        loadPendingCount();
        if (document.getElementById("cs-sync-area")) csSet("cs-sync-area", csSyncMarkup());
      } catch (error) {
        if (error.status === 401) {
          expireSession();
          return;
        }
      }

      if (outcome === "timeout") {
        showToast("同步超时，请稍后在设置页查看状态", "error");
      } else if (outcome) {
        const lastStatus = String((latest && latest.last_status) || "").toLowerCase();
        const failed = finalStatus === "failed" || finalStatus === "canceled" || finalStatus === "cancelled";
        const runError = lastStatus === "error" || lastStatus === "failed";
        if (failed || runError) {
          showToast(friendlySyncError((latest && latest.last_error) || (outcome && outcome.last_error)), "error");
        } else {
          showToast("同步完成");
          state.syncBusy = false;
          if (currentPath() === "/" && document.getElementById("overview-content")) {
            const next = ++state.renderSeq;
            loadOverview(next);
            loadPortfolioStructure({ force: true });
          } else if (currentPath() === "/settings") {
            loadCoinstatsExtras(state.renderSeq);
          }
        }
      }
    } finally {
      state.syncBusy = false;
      refreshSyncButtons();
    }
  }

  function syncCsSubmitState() {
    const input = document.getElementById("cs-key-input");
    const actions = document.getElementById("cs-actions");
    if (!actions) return;
    const submit = actions.querySelector('button[type="submit"]');
    if (!submit) return;
    const ready = Boolean(input && input.value.trim());
    const busy = Boolean(settingsState.saving);
    submit.disabled = busy || !ready;
    submit.classList.toggle("is-ready", ready && !busy);
    submit.classList.toggle("is-loading", busy);
  }

  function csRefreshActions() {
    if (settingsState.confirmDelete || settingsState.replacing || !settingsState.key?.configured) {
      const actions = document.getElementById("cs-actions");
      if (!actions) {
        csRefreshKeyArea();
        return;
      }
      if (settingsState.confirmDelete) {
        csRefreshKeyArea();
        return;
      }
      if (settingsState.replacing) {
        const saving = settingsState.saving;
        const submit = actions.querySelector('button[type="submit"]');
        const cancel = actions.querySelector("[data-cs-cancel-replace]");
        const label = actions.querySelector("[data-cs-label]");
        if (submit) {
          syncCsSubmitState();
        }
        if (label) label.textContent = saving ? "正在保存" : "保存新 key";
        if (cancel) cancel.disabled = saving;
        return;
      }
      const label = actions.querySelector("[data-cs-label]");
      syncCsSubmitState();
      if (label) label.textContent = settingsState.saving ? "正在保存" : "保存";
      return;
    }
    csRefreshKeyArea();
  }

  async function loadCoinstatsExtras(seq) {
    if (!settingsState.key?.configured) {
      settingsState.credits = null;
      settingsState.creditsError = "";
      settingsState.sync = null;
      csSet("cs-usage-area", "");
      csSet("cs-sync-area", csSyncMarkup());
      try {
        const sync = await api("/sync/status");
        if (seq !== state.renderSeq) return;
        settingsState.sync = sync || {};
      } catch (error) {
        if (error.status === 401) {
          expireSession();
          return;
        }
        if (seq !== state.renderSeq) return;
        settingsState.sync = {};
      }
      csSet("cs-sync-area", csSyncMarkup());
      return;
    }
    settingsState.credits = null;
    settingsState.creditsError = "";
    settingsState.sync = null;
    csSet("cs-usage-area", csUsageMarkup());
    csSet("cs-sync-area", csSyncMarkup());

    const [credits, sync] = await Promise.allSettled([api("/coinstats/credits"), api("/sync/status")]);
    if (seq !== state.renderSeq) return;
    if (
      (credits.status === "rejected" && credits.reason?.status === 401) ||
      (sync.status === "rejected" && sync.reason?.status === 401)
    ) {
      expireSession();
      return;
    }

    if (credits.status === "fulfilled") {
      if (credits.value && credits.value.configured === false) {
        settingsState.key = { configured: false, last4: null };
        settingsState.replacing = false;
        settingsState.confirmDelete = false;
        csRefreshKeyArea();
        csSet("cs-usage-area", "");
        settingsState.sync = sync.status === "fulfilled" ? sync.value || {} : {};
        csSet("cs-sync-area", csSyncMarkup());
        return;
      }
      settingsState.credits = credits.value;
      settingsState.creditsError = "";
    } else {
      settingsState.credits = null;
      settingsState.creditsError =
        credits.reason?.status === 0 ? "网络连接失败，请稍后重试" : credits.reason?.message || "暂时读不到额度";
    }
    settingsState.sync = sync.status === "fulfilled" ? sync.value || {} : {};
    csSet("cs-usage-area", csUsageMarkup());
    csSet("cs-sync-area", csSyncMarkup());
  }

  async function loadSettings(seq) {
    const content = document.getElementById("settings-content");
    if (!content) return;
    try {
      const keyInfo = await api("/coinstats/key");
      if (seq !== state.renderSeq) return;
      settingsState.key = keyInfo || { configured: false, last4: null };
      settingsState.replacing = false;
      settingsState.confirmDelete = false;
      settingsState.wallets = null;
      settingsState.walletDeleteId = null;
      settingsState.walletDeletingAll = false;
      settingsState.walletHint = "";
      settingsState.walletHintTone = "";
      settingsState.walletFormOpen = false;
      settingsState.users = null;
      settingsState.usersLoading = Boolean(state.user?.is_admin);
      settingsState.usersFormOpen = false;
      settingsState.usersSaving = false;
      settingsState.usersHint = "";
      settingsState.usersHintTone = "";
      settingsState.usersResetId = null;
      settingsState.usersResetting = false;
      settingsState.alertRule = null;
      settingsState.alertSaving = false;
      settingsState.alertHint = "";
      settingsState.alertHintTone = "";
      settingsState.alertEvents = null;
      settingsState.alertEventsLoading = false;
      settingsState.alertEventsError = "";
      settingsState.alertEventsExpanded = false;
      settingsState.alertPositions = null;
      content.innerHTML = `<div class="reveal">${settingsMarkup()}</div>`;
      settingsInitLayout();
      updateWechatSettingsCard();
      loadCoinstatsExtras(seq);
      loadWechatError(seq);
      loadWallets(seq);
      loadAlertRule(seq);
      loadAlertHistory(seq);
      if (state.user?.is_admin) loadUsers(seq);
    } catch (error) {
      if (error.status === 401) {
        expireSession();
        return;
      }
      if (seq !== state.renderSeq) return;
      content.innerHTML = retryMarkup("settings");
    }
  }

  async function loadWechatError(seq) {
    try {
      const overview = await api("/overview");
      if (seq !== state.renderSeq) return;
      state.overviewData = overview;
      const raw = String(overview?.wechat_send_error || "").replace(/\s+/g, " ").trim();
      settingsState.wechatError = overview?.wechat_send_stale ? raw : "";
      updateWechatSettingsCard();
      csSet("cs-wechat-error-area", csWechatErrorMarkup());
    } catch {
      // The settings page already has its own retry UI; a missing overview
      // must not hide the sync/key controls.
    }
  }

  function renderSettingsPage(seq) {
    setTitle("设置");
    Object.assign(settingsState, {
      key: null,
      credits: null,
      creditsError: "",
      sync: null,
      saving: false,
      deleting: false,
      confirmDelete: false,
      replacing: false,
      wallets: null,
      walletSaving: false,
      walletDeleteId: null,
      walletDeleting: false,
      walletDeletingAll: false,
      walletHint: "",
      walletHintTone: "",
      walletFormOpen: false,
      users: null,
      usersLoading: false,
      usersFormOpen: false,
      usersSaving: false,
      usersHint: "",
      usersHintTone: "",
      usersResetId: null,
      usersResetting: false,
      guideOpen: false,
      alertRule: null,
      alertRules: null,
      alertEditProtocol: null,
      alertSaving: false,
      alertHint: "",
      alertHintTone: "",
      alertEvents: null,
      alertEventsLoading: false,
      alertEventsError: "",
      alertEventsExpanded: false,
      alertPositions: null,
      alertSources: null,
      wechatDetailsOpen: false,
      wechatError: "",
    });
    renderShell(
      `${topbarMarkup()}
      ${settingsPageHead()}
      <div class="settings-layout settings-layout--a">
        ${settingsNavMarkup()}
        <div class="settings-content" id="settings-content">
          <div class="settings-skeleton" aria-hidden="true">
            <div class="skeleton cs-sk-head"></div>
            <div class="skeleton cs-sk-input"></div>
          </div>
        </div>
      </div>`,
      "settings",
    );
    loadSettings(seq);
  }

  async function saveCoinstatsKey() {
    if (settingsState.saving) return;
    const input = document.getElementById("cs-key-input");
    if (!input) return;
    const value = input.value.trim();
    if (!value) {
      csSetHint("请先粘贴 key", "bad");
      input.focus();
      return;
    }
    if (value.length < 8 || value.length > 256 || /\s/.test(value)) {
      csSetHint("key 格式不对：长度 8–256，且不能含空格", "bad");
      input.focus();
      return;
    }
    const seq = state.renderSeq;
    settingsState.saving = true;
    csSetHint("");
    csRefreshActions();
    input.readOnly = true;
    try {
      const result = await api("/coinstats/key", { method: "PUT", body: { api_key: value } });
      input.value = "";
      if (seq !== state.renderSeq) return;
      settingsState.key = result || { configured: true, last4: null };
      settingsState.saving = false;
      settingsState.replacing = false;
      csRefreshKeyArea();
      showToast("CoinStats key 已保存");
      loadCoinstatsExtras(seq);
    } catch (error) {
      settingsState.saving = false;
      if (error.status === 401) {
        input.value = "";
        expireSession();
        return;
      }
      if (seq !== state.renderSeq) return;
      input.readOnly = false;
      csRefreshActions();
      const message = error.status === 0 ? "网络连接失败，请稍后重试" : error.message;
      csSetHint(message, "bad");
      showToast("保存失败", "error");
    }
  }

  async function deleteCoinstatsKey() {
    if (settingsState.deleting) return;
    const seq = state.renderSeq;
    settingsState.deleting = true;
    csRefreshKeyArea();
    try {
      await api("/coinstats/key", { method: "DELETE" });
      if (seq !== state.renderSeq) return;
      settingsState.key = { configured: false, last4: null };
      settingsState.credits = null;
      settingsState.sync = null;
      settingsState.creditsError = "";
      settingsState.deleting = false;
      settingsState.confirmDelete = false;
      settingsState.replacing = false;
      csRefreshKeyArea();
      csSet("cs-usage-area", "");
      showToast("已删除 CoinStats key");
      loadCoinstatsExtras(seq);
    } catch (error) {
      settingsState.deleting = false;
      if (error.status === 401) {
        expireSession();
        return;
      }
      if (seq !== state.renderSeq) return;
      settingsState.confirmDelete = false;
      csRefreshKeyArea();
      showToast(error.status === 0 ? "网络连接失败，请稍后重试" : error.message, "error");
    }
  }

  /* ---------- Lending + options (四期·D) ---------- */

  const LEND_PROTOCOLS = {
    aave: "Aave",
    morpho: "Morpho",
    pendle: "Pendle",
    kamino: "Kamino",
    jupiter: "Jupiter",
    deribit: "Deribit",
    binance: "币安",
  };

  const lendingState = {
    open: new Set(),
    infoOpen: false,
    lending: null,
    options: null,
    usingMock: false,
    error: "",
  };

  // Screenshot / aesthetic fixtures only. Never shown unless the live API
  // returns zero positions AND the URL carries ?mock=1.
  const LENDING_MOCK = {
    total_collateral_usdt: 28400,
    total_debt_usdt: 9200,
    net_value_usdt: 19200,
    min_health_factor: 1.12,
    positions: [
      {
        id: 3,
        kind: "lending",
        protocol: "pendle",
        chain: "arbitrum",
        account_ref: "0x3333333333333333333333333333333333333333",
        market_ref: "pendle-loop-susdx",
        label: "Pendle 循环 sUSDx",
        status: "open",
        net_value_usdt: 2700,
        collateral_usdt: 5400,
        debt_usdt: 2700,
        details: {
          collateral: [{ symbol: "PT-sUSDx", amount: "5400", usd: 5400, apy_supply: 0.092 }],
          borrow: [{ symbol: "USDC", amount: "2700", usd: 2700, apy_borrow: 0.048 }],
          health_factor: 1.12,
          net_apy: 0.136,
          interest_paid_usd: 22,
          interest_earned_usd: 61,
          pnl_usd: 39,
          pnl_apr: 0.141,
          liq_price: 0.855,
          liq_price_estimated: true,
          asof: new Date(Date.now() - 42 * 60 * 1000).toISOString(),
        },
      },
      {
        id: 1,
        kind: "lending",
        protocol: "aave",
        chain: "ethereum",
        account_ref: "0x1111111111111111111111111111111111111111",
        market_ref: "aave-v3-ethereum",
        label: "Aave V3 ETH",
        status: "open",
        net_value_usdt: 7000,
        collateral_usdt: 10000,
        debt_usdt: 3000,
        details: {
          collateral: [
            { symbol: "WETH", amount: "2.5", usd: 8000, apy_supply: 0.021 },
            { symbol: "USDC", amount: "2000", usd: 2000, apy_supply: 0.045 },
          ],
          borrow: [{ symbol: "USDT", amount: "3000", usd: 3000, apy_borrow: 0.062 }],
          health_factor: 1.85,
          net_apy: 0.012,
          interest_paid_usd: 40,
          interest_earned_usd: 55,
          pnl_usd: 15,
          pnl_apr: 0.018,
        },
      },
      {
        id: 2,
        kind: "lending",
        protocol: "morpho",
        chain: "base",
        account_ref: "0x2222222222222222222222222222222222222222",
        market_ref: "morpho-weth-usdc",
        label: "Morpho WETH/USDC",
        status: "open",
        net_value_usdt: 4500,
        collateral_usdt: 6000,
        debt_usdt: 1500,
        details: {
          collateral: [{ symbol: "WETH", amount: "1.8", usd: 6000, apy_supply: 0.03 }],
          borrow: [{ symbol: "USDC", amount: "1500", usd: 1500, apy_borrow: 0.055 }],
          health_factor: 1.4,
          net_apy: 0.008,
          interest_paid_usd: 12,
          interest_earned_usd: 18,
          pnl_usd: 6,
          pnl_apr: 0.011,
          liq_price: 2100,
        },
      },
      {
        id: 4,
        kind: "lending",
        protocol: "kamino",
        chain: "solana",
        account_ref: "Kamino111111111111111111111111111111111111",
        market_ref: "kamino-main-sol-usdc",
        label: "Kamino SOL/USDC",
        status: "open",
        net_value_usdt: 2000,
        collateral_usdt: 5000,
        debt_usdt: 3000,
        details: {
          collateral: [{ symbol: "SOL", amount: "30", usd: 5000, apy_supply: 0.052 }],
          borrow: [{ symbol: "USDC", amount: "3000", usd: 3000, apy_borrow: 0.08 }],
          health_factor: 2.1,
          net_apy: 0.004,
          interest_paid_usd: 8,
          interest_earned_usd: 10,
          pnl_usd: 2,
          pnl_apr: 0.009,
          liq_price: 90,
        },
      },
      {
        id: 5,
        kind: "lending",
        protocol: "jupiter",
        chain: "solana",
        account_ref: "Jup111111111111111111111111111111111111111",
        market_ref: "jup-lend-jup-usdc",
        label: "Jupiter Lend JUP",
        status: "open",
        net_value_usdt: 3000,
        collateral_usdt: 2000,
        debt_usdt: 0,
        details: {
          collateral: [{ symbol: "JupSOL", amount: "12.4", usd: 2000, apy_supply: 0.067 }],
          borrow: [],
          health_factor: null,
          net_apy: 0.067,
          interest_paid_usd: 0,
          interest_earned_usd: 9.4,
          pnl_usd: 9.4,
          pnl_apr: 0.067,
        },
      },
    ],
  };

  function optionExpiryIso(daysFromNow) {
    const date = new Date(Date.now() + daysFromNow * 24 * 60 * 60 * 1000);
    return date.toISOString();
  }

  function optionExpiryHours(hoursFromNow) {
    const date = new Date(Date.now() + hoursFromNow * 60 * 60 * 1000);
    return date.toISOString();
  }

  function optionsMockPayload() {
    return {
      total_mark_value_usdt: 1860,
      total_premium_usdt: 1140,
      unrealized_pnl_usdt: 720,
      positions: [
        {
          id: 14,
          kind: "option",
          protocol: "deribit",
          chain: null,
          account_ref: "deribit:demo",
          market_ref: "BTC-0DTE-98000-C",
          label: "BTC 98k 0DTE",
          status: "open",
          net_value_usdt: 180,
          collateral_usdt: 0,
          debt_usdt: 0,
          details: {
            underlying: "BTC",
            type: "call",
            strike: 98000,
            expiry: optionExpiryHours(5),
            size: 0.2,
            avg_price_usd: 420,
            premium_usd: 84,
            mark_price_usd: 900,
            mark_value_usd: 180,
            unrealized_pnl_usd: 96,
            delta: 0.41,
            iv: 0.68,
          },
        },
        {
          id: 11,
          kind: "option",
          protocol: "deribit",
          chain: null,
          account_ref: "deribit:demo",
          market_ref: "BTC-NEAR-100000-C",
          label: "BTC 100k call",
          status: "open",
          net_value_usdt: 2500,
          collateral_usdt: 0,
          debt_usdt: 0,
          details: {
            underlying: "BTC",
            type: "call",
            strike: 100000,
            expiry: optionExpiryIso(2),
            size: 0.5,
            avg_price_usd: 1800,
            premium_usd: 900,
            mark_price_usd: 5000,
            mark_value_usd: 2500,
            unrealized_pnl_usd: 1600,
            delta: 0.35,
            iv: 0.55,
          },
        },
        {
          id: 12,
          kind: "option",
          protocol: "binance",
          chain: null,
          account_ref: "binance:demo",
          market_ref: "ETH-27MAR26-2000-P",
          label: "ETH 2k put short",
          status: "open",
          net_value_usdt: -400,
          collateral_usdt: 0,
          debt_usdt: 0,
          details: {
            underlying: "ETH",
            type: "put",
            strike: 2000,
            expiry: optionExpiryIso(36),
            size: -1,
            avg_price_usd: 120,
            premium_usd: 120,
            mark_price_usd: 400,
            mark_value_usd: -400,
            unrealized_pnl_usd: -280,
            delta: -0.22,
            iv: 0.62,
          },
        },
        {
          id: 13,
          kind: "option",
          protocol: "deribit",
          chain: null,
          account_ref: "deribit:demo",
          market_ref: "ETH-FAR-4000-C",
          label: "ETH 4k call",
          status: "open",
          net_value_usdt: -240,
          collateral_usdt: 0,
          debt_usdt: 0,
          details: {
            underlying: "ETH",
            type: "call",
            strike: 4000,
            expiry: optionExpiryIso(48),
            size: -2,
            avg_price_usd: 310,
            premium_usd: 120,
            mark_price_usd: 120,
            mark_value_usd: -240,
            unrealized_pnl_usd: -600,
            delta: 0.18,
            iv: 0.71,
          },
        },
      ],
    };
  }

  function mockFlag() {
    try {
      return new URLSearchParams(window.location.search).get("mock");
    } catch {
      return null;
    }
  }

  function protocolLabel(protocol) {
    return LEND_PROTOCOLS[protocol] || String(protocol || "协议");
  }

  const PROTOCOL_ICON_FILES = {
    aave: "aave.png",
    morpho: "morpho.png",
    pendle: "pendle.png",
    kamino: "kamino.png",
    jupiter: "jupiter.png",
  };

  function protocolIconMarkup(protocol) {
    const key = String(protocol || "").trim().toLowerCase();
    const file = PROTOCOL_ICON_FILES[key];
    if (!file) {
      return `<span class="hold-icon proto-icon" aria-hidden="true"></span>`;
    }
    return `<span class="hold-icon proto-icon" aria-hidden="true">
      <img class="hold-icon-img" src="/static/icons/protocols/${file}" alt="" width="30" height="30" loading="lazy" />
    </span>`;
  }

  function underlyingIconMarkup(symbol) {
    const asset = String(symbol || "?").trim() || "?";
    return tokenIconMarkup({ asset });
  }

  function chainLabel(chain) {
    const map = {
      ethereum: "Ethereum",
      eth: "Ethereum",
      base: "Base",
      arbitrum: "Arbitrum",
      optimism: "Optimism",
      solana: "Solana",
    };
    if (!chain) return "";
    return map[String(chain).toLowerCase()] || String(chain);
  }

  const HF_FALLBACK_THRESHOLDS = { warn: 1.5, danger: 1.2 };

  // O3: thresholds come from the enabled protocol row first, then the enabled
  // global row. While rules are loading (or missing) the old 1.5/1.2 constants
  // keep the card identical to before.
  function effectiveAlertRule(protocol) {
    const list = alertRuleList();
    const key = alertProtocolKey(protocol);
    if (key !== "global") {
      const own = list.find(
        (row) => row && row.enabled !== false && alertProtocolKey(row.protocol) === key,
      );
      if (own) return own;
    }
    return list.find((row) => row && row.protocol == null && row.enabled !== false) || null;
  }

  // p8e2 ④: the bar is a fixed 1.00–1.80 health scale (1.05 ≈ 6%,
  // 1.30 ≈ 37%, 1.50 ≈ 62%). Protocol rules only move the warn/danger
  // ticks; the denominator must not shrink with the protocol thresholds.
  const HF_SCALE_MAX = 1.8;

  function hfRuleThresholds(rule) {
    const warn = finiteNumber(rule?.warn_hf);
    const danger = finiteNumber(rule?.danger_hf);
    if (warn == null || danger == null || warn <= 0 || danger <= 0 || !(danger < warn)) {
      return { ...HF_FALLBACK_THRESHOLDS };
    }
    return { warn, danger };
  }

  function hfScalePercent(value) {
    if (value == null || !Number.isFinite(Number(value))) return 0;
    const ratio = Math.max(0, Math.min(1, (Number(value) - 1) / (HF_SCALE_MAX - 1)));
    return Math.round(ratio * 1000) / 10;
  }

  function hfWarnPercent(thresholds) {
    return hfScalePercent(thresholds.warn);
  }

  function hfTone(value, rule) {
    if (value == null || !Number.isFinite(Number(value))) return "none";
    const number = Number(value);
    const { warn, danger } = hfRuleThresholds(rule);
    if (number >= warn) return "safe";
    if (number >= danger) return "warn";
    return "danger";
  }

  function hfText(value) {
    if (value == null || !Number.isFinite(Number(value))) return "—";
    return Number(value).toFixed(2);
  }

  function hfBarWidth(value) {
    return hfScalePercent(value);
  }

  function hasBorrow(position, details) {
    const legs = Array.isArray(details?.borrow) ? details.borrow : [];
    if (legs.length) return true;
    return Number.isFinite(Number(position?.debt_usdt)) && Number(position.debt_usdt) > 0;
  }

  function positionHealth(position) {
    const details = position?.details && typeof position.details === "object" ? position.details : {};
    const raw = details.health_factor;
    if (raw == null || raw === "") return Number.POSITIVE_INFINITY;
    const value = Number(raw);
    return Number.isFinite(value) ? value : Number.POSITIVE_INFINITY;
  }

  function lendingPositionsSorted(rows) {
    return rows
      .map((position, index) => ({ position, index }))
      .sort((a, b) => {
        const aDust = asNumber(a.position?.net_value_usdt) < 5;
        const bDust = asNumber(b.position?.net_value_usdt) < 5;
        if (aDust !== bDust) return aDust ? 1 : -1;
        const delta = positionHealth(a.position) - positionHealth(b.position);
        return delta || a.index - b.index;
      })
      .map((entry) => entry.position);
  }

  function finiteNumber(value) {
    if (value == null || value === "") return null;
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }

  // Missing metrics are neutral facts, never profit/loss colour. The class
  // keeps every "—" on the same grey as the rest of the muted data.
  function dashClass(value) {
    return finiteNumber(value) == null ? "is-empty-value" : "";
  }

  function rateText(value) {
    if (finiteNumber(value) == null) return "—";
    return signedPercent(value);
  }

  function dashMoney(value) {
    if (value == null || !Number.isFinite(Number(value))) return "—";
    return money(value);
  }

  function dashPrice(value) {
    if (value == null || !Number.isFinite(Number(value))) return "—";
    return formatPrice(value);
  }

  // Market value stays unsigned unless the book is net short.
  function plainMoney(value) {
    if (value == null || !Number.isFinite(Number(value))) return "—";
    const number = Number(value);
    if (number < 0) return `-${money(Math.abs(number))}`;
    return money(number);
  }

  function signedOrDash(value) {
    if (value == null || !Number.isFinite(Number(value))) return "—";
    return signedMoney(value);
  }

  // Lending legs keep a uniform two-decimal quantity, so 5,400 and 2.5 do
  // not read as different precisions in the same column. Sub-unit tokens keep
  // four decimals, otherwise 0.015 would collapse to 0.02.
  function lendingQtyText(amount) {
    const number = Number(amount);
    if (!Number.isFinite(number)) return escapeHtml(amount ?? "—");
    const digits = Math.abs(number) >= 1 ? 2 : 4;
    const formatted = Math.abs(number).toLocaleString("en-US", {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    });
    return number < 0 ? `-${formatted}` : formatted;
  }

  function qtyText(amount) {
    const number = Number(amount);
    if (!Number.isFinite(number)) return escapeHtml(amount ?? "—");
    const abs = Math.abs(number);
    const digits = abs >= 1000 ? 0 : abs >= 1 ? 2 : 4;
    const formatted = abs.toLocaleString("en-US", {
      minimumFractionDigits: 0,
      maximumFractionDigits: digits,
    });
    return number < 0 ? `-${formatted}` : formatted;
  }

  function collateralHasUnknownApy(legs) {
    return (
      Array.isArray(legs) &&
      legs.some((leg) => leg && Object.prototype.hasOwnProperty.call(leg, "apy_supply") === false)
    );
  }

  function estChip(label = "估算") {
    return `<span class="pos-est-tag">${escapeHtml(label)}</span>`;
  }

  function legRows(legs, side) {
    if (!Array.isArray(legs) || !legs.length) {
      return `<li class="pos-leg pos-leg--empty">${side === "borrow" ? "无借款" : "无抵押"}</li>`;
    }
    return legs
      .map((leg) => {
        const apy = side === "borrow" ? leg?.apy_borrow : leg?.apy_supply;
        const estimated =
          side !== "borrow" && (leg?.apy_source === "pendle_implied" || leg?.apy_estimated === true);
        const hasApy = apy != null && Number.isFinite(Number(apy));
        const apyHtml = hasApy
          ? `<span class="pos-leg-apy">${side === "borrow" ? "借" : "供"} ${escapeHtml(percentMagnitude(apy))}</span>${
              estimated ? estChip() : ""
            }`
          : side === "borrow"
            ? `<span class="pos-leg-apy">—</span>`
            : `<span class="pos-leg-apy pos-leg-apy--empty" aria-hidden="true"></span>`;
        const symbol = String(leg?.symbol || "—");
        return `
          <li class="pos-leg">
            <span class="pos-leg-name">
              <span class="pos-leg-sym" title="${escapeHtml(symbol)}">${escapeHtml(symbol)}</span>
            </span>
            <span class="pos-leg-line">
              <span class="pos-leg-qty">${lendingQtyText(leg?.amount)}</span>
              ${apyHtml}
            </span>
            <span class="pos-leg-usdline">
              <span class="pos-leg-usd">${dashMoney(leg?.usd)}</span>
            </span>
          </li>`;
      })
      .join("");
  }

  function legSumLine(legs, side, total) {
    if (!Array.isArray(legs) || legs.length < 2) return "";
    const label = side === "borrow" ? "已借" : "合计";
    return `<p class="pos-col-sum">${label} ${money(total)}</p>`;
  }

  // p7f3: a generic "缺失" chip says nothing. Name the concrete object when we
  // can (a collateral leg without a yield, or a single-market liq price);
  // otherwise render nothing at all.
  function lendingMissingLabel(details, collateralLegs) {
    const legs = Array.isArray(collateralLegs) ? collateralLegs : [];
    const missing = [];
    for (const leg of legs) {
      if (!leg || Object.prototype.hasOwnProperty.call(leg, "apy_supply")) continue;
      const symbol = String(leg.symbol || "").trim();
      if (symbol && !missing.includes(symbol)) missing.push(symbol);
    }
    if (missing.length) {
      const label = missing.length > 2 ? `${missing[0]} 等` : missing.join("、");
      return `缺失：${label} 收益`;
    }
    if (legs.length === 1 && details?.liq_price == null && details?.stale !== true) {
      // p8e2 ⑧: the liq-price gap is explained inside the expanded area only.
      return "";
    }
    return "";
  }

  function lendingFreshnessMarkup(details, key, collateralLegs) {
    const asof = parseInstant(details?.asof);
    const stale = details?.stale === true;
    const staleSince = parseInstant(details?.stale_since) || asof;
    const showAsOf = Boolean(asof || stale);
    const demoMark = lendingState.usingMock
      ? `<span class="pos-demo-note" title="本卡为示例数据">示例</span>${
          showAsOf ? `<span class="pos-demo-dot" aria-hidden="true">·</span>` : ""
        }`
      : "";
    if (!showAsOf && !demoMark) return "";
    const asofAbs = asof ? absoluteTimeSH(asof) : "";
    const ageSource = asof || staleSince;
    const ageText = ageSource ? `数据 ${relativeTimeSH(ageSource)}` : "数据时间未知";
    const missingLabel = lendingMissingLabel(details, collateralLegs);
    const titleText = stale && staleSince
      ? `最后成功数据 ${absoluteTimeSH(staleSince)}（北京时间）`
      : asofAbs
        ? `数据时间 ${asofAbs}（北京时间）`
        : "数据时间未知";
    const asofMarkup = showAsOf
      ? `<span class="pos-asof${stale ? " pos-asof--stale" : ""}" data-asof-key="${escapeHtml(
          key,
        )}" title="${escapeHtml(titleText)}">${
          stale
            ? `<span class="pos-asof-warn" aria-hidden="true">⚠</span><span>${escapeHtml(
                ageText,
              )}</span><span class="pos-asof-dot" aria-hidden="true">·</span><span>已过期</span>`
            : `<span>${escapeHtml(ageText)}</span>`
        }</span>`
      : "";
    return `<div class="pos-freshness${stale ? " pos-freshness--stale" : ""}${
      demoMark ? " pos-freshness--demo" : ""
    }">
      <span class="pos-freshness-main">${demoMark}${asofMarkup}</span>
      ${
        missingLabel
          ? `<span class="pos-quality-tag" title="${stale ? "数据已过期，部分字段缺失" : "部分字段缺失"}">${estChip(
              missingLabel,
            )}</span>`
          : ""
      }
    </div>`;
  }

  function lendingRatioText(value) {
    const number = finiteNumber(value);
    if (number == null) return "";
    const percent = number > 1 ? number : number * 100;
    return `${percent.toFixed(1)}%`;
  }

  function lendingMarketName(position, details) {
    const raw =
      details?.market_name ||
      details?.market ||
      position?.market_name ||
      "";
    return String(raw || "").trim();
  }

  function lendingMarketShort(value) {
    const text = String(value || "");
    return text.length > 30 ? `${text.slice(0, 12)}…${text.slice(-8)}` : text;
  }

  function lendingLiqMissingReason(details, collateralLegs) {
    // p8e3 ④: one short line; the fold already says "已过期" above.
    if (details?.stale === true) return "已过期，暂不计算";
    if (Array.isArray(collateralLegs) && collateralLegs.length > 1) return "多抵押，暂无法估算";
    if (details?.price_missing === true) return "缺少价格数据，暂不计算";
    return "协议暂未提供";
  }

  function lendingCard(position, options = {}) {
    const details = position?.details && typeof position.details === "object" ? position.details : {};
    const hf = details.health_factor;
    const rule = effectiveAlertRule(position?.protocol);
    const thresholds = hfRuleThresholds(rule);
    const tone = hfTone(hf, rule);
    const key = `l-${position.id ?? position.market_ref}`;
    const open = lendingState.open.has(key);
    const chain = chainLabel(position.chain);
    const label = position.label || protocolLabel(position.protocol);
    const borrowApr = Array.isArray(details.borrow)
      ? details.borrow.map((leg) => leg?.apy_borrow).find((value) => finiteNumber(value) != null)
      : null;
    const borrowed = hasBorrow(position, details);
    const hfNumber = finiteNumber(hf);
    const nearLiq = borrowed && hfNumber != null && hfNumber < thresholds.danger;
    const cardRisk = nearLiq;
    const staleCard = details.stale === true;
    const dust = asNumber(position.net_value_usdt) < 5;
    const netApy = finiteNumber(details.net_apy);
    const pnl = finiteNumber(details.pnl_usd);
    const pnlApr = finiteNumber(details.pnl_apr);
    const showAprCol = Boolean(options.showPnlApr);
    const borrowLegs = Array.isArray(details.borrow) ? details.borrow : [];
    const collateralLegs = Array.isArray(details.collateral) ? details.collateral : [];
    const ptCollateral = collateralLegs.find((leg) => /^PT[-_]/i.test(String(leg?.symbol || "")));
    // p7f3: the Pendle note belongs to cards whose collateral actually carries
    // PT with a pendle-implied yield — not to every card that happens to have
    // a generic apy_estimated flag (Kamino fee-history estimates, bad mocks).
    const ptImplied =
      Boolean(ptCollateral) &&
      collateralLegs.some(
        (leg) =>
          /^PT[-_]/i.test(String(leg?.symbol || "")) &&
          (leg?.apy_source === "pendle_implied" || leg?.apy_estimated === true),
      );
    const collateralApyUnknown =
      collateralHasUnknownApy(collateralLegs) || details.yield_data_missing === true;
    const netApyMissing = netApy == null && collateralApyUnknown;

    const showWarnLabel = borrowed && hfNumber != null && hfNumber < thresholds.warn;
    const warnPercent = hfWarnPercent(thresholds);
    const healthRow = borrowed
      ? `
        <div class="hf-row${showWarnLabel ? "" : " hf-row--notext"}">
          <div class="hf-copy">
            <span class="hf-kicker">健康度</span>
            <span class="hf-num hf-num--${tone}">${hfText(hf)}</span>
            ${nearLiq ? `<span class="pos-liq-tag"><span class="pos-liq-ico" aria-hidden="true">${PF_WARN_SVG}</span>接近清算</span>` : ""}
          </div>
          <div class="hf-track" role="img" aria-label="健康度 ${hfText(hf)}，警戒线 ${hfText(
            thresholds.warn,
          )}，满刻度 ${hfText(HF_SCALE_MAX)}">
            <span class="hf-warn" style="left:${warnPercent}%" aria-hidden="true"></span>
            ${showWarnLabel ? `<span class="hf-warn-label" style="left:${warnPercent}%">警戒线</span>` : ""}
            <span class="hf-fill hf-fill--${tone}" style="width:${hfBarWidth(hf)}%"></span>
          </div>
        </div>`
      : `<p class="hf-safe-note">无借款，无清算风险</p>`;

    const netApyEstimated = ptImplied && netApy != null;
    const netApyClass = netApyMissing
      ? "pos-apy-missing"
      : netApy == null || netApyEstimated
        ? "pos-apy-neutral"
        : toneClass(netApy);
    const netApyHtml = netApyMissing
      ? `<span class="pos-apy-missing-text">收益数据缺失</span><span class="pos-apy-info" role="img" tabindex="0" aria-label="该抵押品无公开收益数据，不计入净 APY" title="该抵押品无公开收益数据，不计入净 APY">ⓘ</span>`
      : `<span class="pos-value-est"><span class="pos-apy-value">${rateText(netApy)}</span>${
          netApyEstimated ? `<span class="pos-apy-est">${estChip()}</span>` : ""
        }</span>`;

    if (dust) {
      const supplyApy = collateralLegs
        .map((leg) => leg?.apy_supply)
        .find((value) => finiteNumber(value) != null);
      const supplyEstimated = collateralLegs.some(
        (leg) => leg?.apy_source === "pendle_implied" || leg?.apy_estimated === true,
      );
      const supplyHtml =
        finiteNumber(supplyApy) == null
          ? "—"
          : `${escapeHtml(percentMagnitude(supplyApy))}${supplyEstimated ? estChip() : ""}`;
      return `
      <article class="pos-card pos-card--dust">
        <p class="pos-dust-line">
          ${protocolIconMarkup(position.protocol)}
          <span class="pos-dust-name" title="${escapeHtml(label)}">${escapeHtml(label)}</span>
          <span class="pos-dust-net">${money(position.net_value_usdt)}</span>
          <span class="pos-dust-apy">供 ${supplyHtml}</span>
        </p>
      </article>`;
    }

    const cardTitle = ptCollateral ? protocolLabel(position.protocol) : label;
    const cardMeta = ptCollateral
      ? `${escapeHtml(collateralHeadline(ptCollateral.symbol))}${chain ? ` · ${escapeHtml(chain)}` : ""}`
      : `${escapeHtml(protocolLabel(position.protocol))}${chain ? ` · ${escapeHtml(chain)}` : ""}`;
    const cardMetaTitle = ptCollateral
      ? `${String(ptCollateral.symbol || "")}${chain ? ` · ${chain}` : ""}`
      : `${protocolLabel(position.protocol)}${chain ? ` · ${chain}` : ""}`;
    const freshnessRow = lendingFreshnessMarkup(details, key, collateralLegs);
    const marketName = lendingMarketName(position, details);
    const asofDate = parseInstant(details?.asof) || parseInstant(details?.stale_since);
    const asofAbs = asofDate ? absoluteTimeSH(asofDate) : "";

    return `
      <article class="pos-card${cardRisk ? " pos-card--risk" : ""}${staleCard ? " pos-card--stale" : ""}" data-position-id="${escapeHtml(position.id ?? "")}" data-protocol="${escapeHtml(position.protocol || "")}" data-chain="${escapeHtml(position.chain || "")}" data-label="${escapeHtml(label)}">
        <header class="pos-card-head">
          ${protocolIconMarkup(position.protocol)}
          <div class="pos-card-id">
            <h3 class="pos-card-title" title="${escapeHtml(label)}">${escapeHtml(cardTitle)}</h3>
            <p class="pos-card-meta" title="${escapeHtml(cardMetaTitle)}">${cardMeta}</p>
            ${
              marketName
                ? `<p class="pos-card-market" title="${escapeHtml(marketName)}"><span class="pos-card-market-k">市场</span>${escapeHtml(
                    lendingMarketShort(marketName),
                  )}</p>`
                : ""
            }
          </div>
          <div class="pos-card-net">
            <span class="pos-card-net-label">净值</span>
            <span class="pos-card-net-value">${money(position.net_value_usdt)}</span>
          </div>
        </header>

        ${freshnessRow}

        ${healthRow}

        <div class="pos-split">
          <section class="pos-col">
            <h4>抵押</h4>
            <ul>${legRows(details.collateral, "supply")}</ul>
            ${legSumLine(collateralLegs, "supply", position.collateral_usdt)}
          </section>
          <section class="pos-col">
            <h4>借款</h4>
            <ul>${borrowed ? legRows(details.borrow, "borrow") : `<li class="pos-leg pos-leg--empty">无借款</li>`}</ul>
            ${legSumLine(borrowLegs, "borrow", position.debt_usdt)}
          </section>
        </div>

        <dl class="pos-metrics${showAprCol ? "" : " pos-metrics--three"}">
          <div>
            <dt>借款 APR</dt>
            <dd class="pos-apr">${
              !borrowed || finiteNumber(borrowApr) == null
                ? "—"
                : escapeHtml(percentMagnitude(borrowApr))
            }</dd>
          </div>
          <div>
            <dt>净 APY</dt>
            <dd class="${netApyClass}${netApyEstimated ? " pos-est-cell" : ""}">${netApyHtml}</dd>
          </div>
          <div>
            <dt>累计收益</dt>
            <dd class="pos-pnl">${pnl == null ? "—" : signedMoney(pnl)}</dd>
          </div>
          ${
            showAprCol
              ? `<div>
            <dt>收益年化</dt>
            <dd class="${pnlApr == null ? "" : toneClass(pnlApr)}">${rateText(pnlApr)}</dd>
          </div>`
              : ""
          }
        </dl>
        ${
          ptImplied
            ? `<p class="pos-metric-note">含 Pendle 隐含收益，按当前 PT 价估算</p>`
            : ""
        }

        <button class="pos-fold" type="button" data-lend-fold="${escapeHtml(key)}" aria-expanded="${open}">
          <span>利息、风险与清算</span>
          <svg class="pos-fold-chev" viewBox="0 0 16 16" fill="none" aria-hidden="true">
            <path d="m3.5 6 4.5 4.5L12.5 6"/>
          </svg>
        </button>
        <div class="pos-fold-panel${open ? " is-open" : ""}">
          <dl class="pos-fold-body">
            <div><dt>利息成本</dt><dd>${dashMoney(details.interest_paid_usd)}</dd></div>
            <div><dt>利息收益</dt><dd class="is-positive">${dashMoney(details.interest_earned_usd)}</dd></div>
            <div><dt>清算价</dt><dd class="pos-liq-cell">${
              details.liq_price == null
                ? `<span class="pos-liq-missing">—</span><span class="pos-liq-explain">（${escapeHtml(
                    lendingLiqMissingReason(details, collateralLegs),
                  )}）</span>`
                : `<span class="pos-value-est">${formatPrice(details.liq_price)}${
                    details.liq_price_estimated === true ? estChip("估算") : ""
                  }</span>`
            }</dd></div>
            <div class="pos-fold-small"><dt>LTV</dt><dd>${escapeHtml(
              lendingRatioText(details.ltv) || "—",
            )}</dd></div>
            <div class="pos-fold-small"><dt>清算阈值</dt><dd>${escapeHtml(
              lendingRatioText(details.liq_threshold) || "—",
            )}</dd></div>
            ${
              details.liq_price_note
                ? `<div class="pos-fold-note">${escapeHtml(details.liq_price_note)}</div>`
                : details.liq_price == null && collateralLegs.length > 1
                  ? `<div class="pos-fold-note">多抵押仓位暂不计算清算价</div>`
                  : details.liq_price == null && details.price_missing === true
                    ? `<div class="pos-fold-note">缺少价格数据，暂不计算清算价</div>`
                    : ""
            }
            ${
              asofAbs
                ? `<div class="pos-fold-small pos-fold-time"><dt>数据时间</dt><dd>${escapeHtml(asofAbs)}${
                    details.stale === true ? `<span class="pos-data-stale"> · 已过期</span>` : ""
                  }</dd></div>`
                : ""
            }
          </dl>
        </div>
      </article>`;
  }

  function expiryParts(value) {
    const date = dateObject(value);
    if (!date) return { label: "—", countdown: "—", soon: false };
    const ms = date.getTime() - Date.now();
    const dayMs = 24 * 60 * 60 * 1000;
    const shanghaiDay = (instant) => {
      const shifted = new Date(instant + 8 * 60 * 60 * 1000);
      // Whole-day index (days since epoch, Shanghai calendar) so subtraction yields real day counts.
      return Math.floor(shifted.getTime() / dayMs);
    };
    const dayGap = shanghaiDay(date.getTime()) - shanghaiDay(Date.now());
    let countdown = "已到期";
    if (ms > 0 && ms <= dayMs) countdown = `${Math.max(1, Math.round(ms / (60 * 60 * 1000)))} 小时后`;
    else if (ms > 0 && dayGap <= 0) countdown = "今天";
    else if (ms > 0 && dayGap === 1) countdown = "明天";
    else if (ms > 0) countdown = `${Math.max(2, dayGap)} 天后`;
    return {
      label: formatDate(value),
      countdown,
      // Only the last 24 hours earn the amber dot; 2–3 day expiries stay quiet.
      soon: ms > 0 && ms <= dayMs,
    };
  }

  function optionCard(position) {
    const details = position?.details && typeof position.details === "object" ? position.details : {};
    const kind = details.type === "put" ? "put" : "call";
    const size = Number(details.size);
    const short = Number.isFinite(size) && size < 0;
    const expiry = expiryParts(details.expiry);
    const key = `o-${position.id ?? position.market_ref}`;
    const open = lendingState.open.has(key);
    const underlying = details.underlying || "—";
    const pnl = details.unrealized_pnl_usd;
    const noQuote = [details.avg_price_usd, details.mark_price_usd, details.mark_value_usd, pnl].every(
      (v) => finiteNumber(v) == null,
    );

    return `
      <article class="pos-card">
        <header class="pos-card-head">
          ${underlyingIconMarkup(underlying)}
          <div class="pos-card-id">
            <h3 class="pos-card-title">${escapeHtml(underlying)}
              <span class="opt-kind">${kind === "put" ? "Put" : "Call"}</span>
            </h3>
            <p class="pos-card-meta">${escapeHtml(protocolLabel(position.protocol))} · 行权 ${escapeHtml(
              qtyText(details.strike),
            )}</p>
          </div>
          <div class="pos-card-net">
            <span class="pos-card-net-label side-pill">${short ? "空头" : "多头"}</span>
            <span class="pos-card-net-value">${Number.isFinite(size) ? qtyText(size) : "—"} 张</span>
          </div>
        </header>

        <div class="opt-expiry ${expiry.soon ? "is-soon" : ""}">
          <span class="opt-expiry-line">到期 ${escapeHtml(expiry.label)} · <span class="opt-countdown">${escapeHtml(
            expiry.countdown,
          )}</span></span>
        </div>

        <dl class="pos-metrics">
          <div>
            <dt>均价</dt>
            <dd class="${dashClass(details.avg_price_usd)}">${dashPrice(details.avg_price_usd)}</dd>
          </div>
          <div>
            <dt>Mark</dt>
            <dd class="${dashClass(details.mark_price_usd)}">${dashPrice(details.mark_price_usd)}</dd>
          </div>
          <div>
            <dt>市值</dt>
            <dd class="${dashClass(details.mark_value_usd)}">${plainMoney(details.mark_value_usd)}</dd>
          </div>
          <div>
            <dt>盈亏</dt>
            <dd class="${finiteNumber(pnl) == null ? "is-empty-value" : toneClass(pnl)}">${signedOrDash(pnl)}</dd>
          </div>
        </dl>
        ${noQuote ? `<p class="opt-no-quote">暂无行情</p>` : ""}

        <button class="pos-fold" type="button" data-lend-fold="${escapeHtml(key)}" aria-expanded="${open}">
          <span>Delta / IV</span>
          <svg class="pos-fold-chev" viewBox="0 0 16 16" fill="none" aria-hidden="true">
            <path d="m3.5 6 4.5 4.5L12.5 6"/>
          </svg>
        </button>
        <div class="pos-fold-panel${open ? " is-open" : ""}">
          <dl class="pos-fold-body">
            <div><dt>Delta</dt><dd class="${dashClass(details.delta)}">${
              details.delta == null || !Number.isFinite(Number(details.delta))
                ? "—"
                : Number(details.delta).toFixed(2)
            }</dd></div>
            <div><dt>IV</dt><dd class="${dashClass(details.iv)}">${
              details.iv == null || !Number.isFinite(Number(details.iv)) ? "—" : percentMagnitude(details.iv)
            }</dd></div>
            <div><dt>权利金</dt><dd class="${dashClass(details.premium_usd)}">${dashMoney(details.premium_usd)}</dd></div>
          </dl>
        </div>
      </article>`;
  }

  function summaryCell(label, valueHtml, extraClass = "") {
    return `
      <div class="lend-sum-cell${extraClass ? ` ${extraClass}` : ""}">
        <span class="lend-sum-label">${escapeHtml(label)}</span>
        <span class="lend-sum-value">${valueHtml}</span>
      </div>`;
  }

  function summaryGrid(label, cells, variant = "") {
    const wide = cells.length === 3 ? " lend-sum--three" : "";
    const unitAfter = variant === "options";
    return `
      <section class="lend-sum${wide}${unitAfter ? " lend-sum--options" : ""}" aria-label="${escapeHtml(label)}">
        ${cells.join("")}
      </section>`;
  }

  const UNIT_AFTER = '<span class="lend-sum-unit-inline">USDT</span>';

  function lendingSummaryMarkup(summary) {
    const hf = summary?.min_health_factor;
    const tone = hfTone(hf, effectiveAlertRule(null));
    return `<div class="lend-sum-wrap">${summaryGrid("借贷汇总", [
      summaryCell("总抵押", `${money(summary?.total_collateral_usdt)}${UNIT_AFTER}`),
      summaryCell("总借款", `${money(summary?.total_debt_usdt)}${UNIT_AFTER}`),
      summaryCell("净值", `${money(summary?.net_value_usdt)}${UNIT_AFTER}`),
      summaryCell("最低健康度", `<span class="hf-num hf-num--${tone}">${hfText(hf)}</span>`),
    ])}<p class="lend-sum-note">累计收益含价格涨跌；净 APY 仅为利差<button class="lend-sum-info" type="button" data-lend-info aria-expanded="${
      lendingState.infoOpen ? "true" : "false"
    }" aria-label="口径说明" title="口径说明">ⓘ</button></p><p class="lend-sum-caliber"${
      lendingState.infoOpen ? "" : " hidden"
    }>健康度以协议口径为准（oracle 价），清算价为估算</p></div>`;
  }

  function optionsSummaryMarkup(summary) {
    const pnl = summary?.unrealized_pnl_usdt;
    const realized = summary?.realized_pnl_usdt;
    const realizedApr = summary?.realized_apr;
    const realizedReady =
      (realized != null && Number.isFinite(Number(realized))) ||
      (realizedApr != null && Number.isFinite(Number(realizedApr)));
    const cells = [
      summaryCell("总市值", `${plainMoney(summary?.total_mark_value_usdt)}${UNIT_AFTER}`),
      summaryCell("权利金成本", `${dashMoney(summary?.total_premium_usdt)}${UNIT_AFTER}`),
      summaryCell(
        "未实现盈亏",
        `<span class="${toneClass(pnl)}">${signedOrDash(pnl)}</span>${UNIT_AFTER}`,
        "lend-sum-cell--wide",
      ),
    ];
    if (realizedReady) {
      const text =
        realized != null && Number.isFinite(Number(realized))
          ? signedOrDash(realized)
          : rateText(realizedApr);
      cells.push(summaryCell("已实现 / 年化", `${text}${UNIT_AFTER}`));
    }
    return summaryGrid("期权汇总", cells, "options");
  }

  function optionRows(summary) {
    return Array.isArray(summary?.positions) ? summary.positions.filter(Boolean) : [];
  }

  function optionExpiryBucket(position) {
    const date = dateObject(position?.details?.expiry);
    if (!date) return "无到期";
    const ms = date.getTime() - Date.now();
    if (ms <= 0) return "已到期";
    const day = 24 * 60 * 60 * 1000;
    if (ms <= day) return "24 小时内";
    if (ms <= 3 * day) return "1–3 天";
    if (ms <= 7 * day) return "3–7 天";
    return "7 天以上";
  }

  function optionsAsideMarkup(summary) {
    const rows = optionRows(summary);
    if (!rows.length) {
      return `
        <aside class="options-side" aria-label="期权接入说明">
          <section class="side-card">
            <p class="side-kicker">接入期权</p>
            <ol class="side-steps">
              <li><b>01</b><span>在设置里添加 Deribit 或币安的只读 key</span></li>
              <li><b>02</b><span>手动同步一次，仓位与权利金会写入账本</span></li>
              <li><b>03</b><span>到期日、Delta 与 IV 随行情一起更新</span></li>
            </ol>
            <a class="side-link" href="/settings" data-link>去数据源设置</a>
          </section>
          <section class="side-card side-card--quiet">
            <p class="side-kicker">只读边界</p>
            <p class="side-copy">期权账户只读，不代下单、不托管资产。没有行情时，价格与希腊字母显示为 —。</p>
          </section>
        </aside>`;
    }

    const longCount = rows.filter((row) => asNumber(row?.details?.size) > 0).length;
    const shortCount = rows.filter((row) => asNumber(row?.details?.size) < 0).length;
    const underlyings = new Map();
    const buckets = new Map();
    for (const row of rows) {
      const underlying = String(row?.details?.underlying || "其他");
      underlyings.set(underlying, (underlyings.get(underlying) || 0) + 1);
      const bucket = optionExpiryBucket(row);
      buckets.set(bucket, (buckets.get(bucket) || 0) + 1);
    }
    const bucketOrder = ["24 小时内", "1–3 天", "3–7 天", "7 天以上", "已到期", "无到期"];
    const bucketRows = bucketOrder
      .filter((label) => buckets.has(label))
      .map((label) => {
        const count = buckets.get(label);
        const width = Math.max(6, Math.round((count / rows.length) * 100));
        return `<div class="side-bar">
          <span>${escapeHtml(label)}</span>
          <span class="side-bar-track" aria-hidden="true"><i style="width:${width}%"></i></span>
          <b>${count}</b>
        </div>`;
      })
      .join("");
    const underlyingChips = [...underlyings.entries()]
      .sort((left, right) => right[1] - left[1])
      .map(
        ([name, count]) =>
          `<span class="side-chip">${escapeHtml(name)}<b>${count}</b></span>`,
      )
      .join("");

    return `
      <aside class="options-side" aria-label="期权结构">
        <section class="side-card">
          <p class="side-kicker">持仓结构</p>
          <dl class="side-facts">
            <div><dt>仓位</dt><dd>${rows.length}</dd></div>
            <div><dt>多头</dt><dd>${longCount}</dd></div>
            <div><dt>空头</dt><dd>${shortCount}</dd></div>
            <div><dt>标的</dt><dd>${underlyings.size}</dd></div>
          </dl>
          <div class="side-chips">${underlyingChips}</div>
        </section>
        <section class="side-card">
          <p class="side-kicker">到期节奏</p>
          <div class="side-bars">${bucketRows}</div>
          <p class="side-copy">按到期时间由近到远分组，已到期与无到期日单独列出。</p>
        </section>
        <section class="side-card side-card--quiet">
          <p class="side-kicker">读法</p>
          <p class="side-copy">张数为负表示空头；市值与盈亏沿用协议口径，没有行情时不做估算。</p>
        </section>
      </aside>`;
  }

  function optionsBookMarkup(summary) {
    const rows = optionRows(summary);
    if (!rows.length) {
      return `
        <div class="options-grid options-grid--empty">
          <div>${emptyState(
            "options",
            "还没有期权仓位",
            "未配置期权账户。去设置添加 Deribit / 币安期权 key 后，仓位会在同步时出现。",
            "/settings",
            "去设置",
          )}</div>
          ${optionsAsideMarkup(summary)}
        </div>`;
    }
    return `
      <div class="options-book">
        ${optionsSummaryMarkup(summary)}
        <div class="options-grid">
          <div class="pos-list options-list">${rows.map(optionCard).join("")}</div>
          ${optionsAsideMarkup(summary)}
        </div>
      </div>`;
  }

  const EMPTY_ICONS = {
    lending: `<svg viewBox="0 0 32 32" fill="none" aria-hidden="true"><path d="M16 4.5 6.5 8.8v6.4c0 5.7 3.8 9.6 9.5 12.3 5.7-2.7 9.5-6.6 9.5-12.3V8.8L16 4.5z"/></svg>`,
    options: `<svg viewBox="0 0 32 32" fill="none" aria-hidden="true"><path d="M8 6.5h11.5a2 2 0 0 1 2 2V26l-3.2-2.1L15 26l-3.3-2.1L8.5 26V8.5a2 2 0 0 1 2-2z"/><path d="M12 12.5h8M12 17h8M12 21.5h5"/></svg>`,
  };

  function emptyState(kind, title, body, href, linkLabel) {
    return `
      <div class="lend-empty">
        <span class="lend-empty-icon" aria-hidden="true">${EMPTY_ICONS[kind] || EMPTY_ICONS.lending}</span>
        <p class="lend-empty-title">${escapeHtml(title)}</p>
        <p>${escapeHtml(body)}</p>
        ${
          href
            ? `<a class="lend-empty-link" href="${href}" data-link>${escapeHtml(linkLabel)}</a>`
            : ""
        }
      </div>`;
  }

  function lendingBodyMarkup() {
    if (lendingState.error) {
      return `<div class="lending-layout"><div class="lending-main">${retryMarkup("lending")}</div></div>`;
    }
    const summary = lendingState.lending;
    const rows = Array.isArray(summary?.positions) ? summary.positions : [];
    const ordered = lendingPositionsSorted(rows);
    const showPnlApr = ordered.some(
      (position) => finiteNumber(position?.details?.pnl_apr) != null,
    );
    const body = ordered.length
      ? `<div class="pos-list">${ordered.map((position) => lendingCard(position, { showPnlApr })).join("")}</div>`
      : emptyState(
          "lending",
          "还没有借贷仓位",
          "未检测到借贷仓位。绑定 EVM / Solana 钱包后，同步会自动读取。",
          "/settings",
          "绑定钱包",
        );
    return `<div class="lending-layout">
      <div class="lending-main">${rows.length ? lendingSummaryMarkup(summary) : ""}${body}</div>
    </div>`;
  }

  function rerenderLendingBody() {
    // The dedicated options page owns its own container; redraw in place there
    // so expanding Delta / IV does not depend on the lending shell existing.
    const optionsContent = document.getElementById("options-content");
    if (optionsContent) {
      optionsContent.innerHTML = `<div class="reveal">${optionsBookMarkup(lendingState.options)}</div>`;
      syncPageDemoSlot(lendingState.usingMock);
      return;
    }
    const content = document.getElementById("lending-content");
    if (!content) return;
    content.innerHTML = `<div class="reveal">${lendingBodyMarkup()}</div>`;
    syncPageDemoSlot(lendingState.usingMock);
  }

  function lendingSkeleton() {
    return `
      <div aria-hidden="true">
        <div class="skeleton lend-sk-sum"></div>
        <div class="skeleton lend-sk-card"></div>
        <div class="skeleton lend-sk-card"></div>
      </div>`;
  }

  async function loadLending(seq) {
    const content = document.getElementById("lending-content");
    if (!content) return;
    lendingState.error = "";
    lendingState.usingMock = false;
    const flag = mockFlag();
    try {
      const [lending, rules] = await Promise.all([
        api("/lending/summary"),
        api("/alerts/rules").catch((error) => {
          if (error.status === 401) throw error;
          return null;
        }),
      ]);
      if (seq !== state.renderSeq) return;
      if (Array.isArray(rules)) settingsState.alertRules = rules;
      const lendingRows = Array.isArray(lending?.positions) ? lending.positions : [];
      // Zero live positions stay an empty state. Built-in fixtures replace them
      // only when ?mock=1, and never once the book already has rows.
      const allowMock = flag === "1" && lendingRows.length === 0;
      lendingState.lending = allowMock ? LENDING_MOCK : lending;
      lendingState.usingMock = allowMock;
      content.innerHTML = `<div class="reveal">${lendingBodyMarkup()}</div>`;
      syncPageDemoSlot(allowMock);
    } catch (error) {
      if (error.status === 401) {
        expireSession();
        return;
      }
      if (seq !== state.renderSeq) return;
      // The positions API is not on this build yet (404). Treat that the same as
      // an empty book so the page can still show the empty state, or the
      // screenshot fixtures when ?mock=1 is set. Other failures stay a retry.
      if (error.status === 404 && flag !== "1") {
        lendingState.lending = {
          total_collateral_usdt: 0,
          total_debt_usdt: 0,
          net_value_usdt: 0,
          min_health_factor: null,
          positions: [],
        };
        lendingState.usingMock = false;
        content.innerHTML = `<div class="reveal">${lendingBodyMarkup()}</div>`;
        syncPageDemoSlot(false);
        return;
      }
      if (error.status === 404 && flag === "1") {
        lendingState.lending = LENDING_MOCK;
        lendingState.usingMock = true;
        content.innerHTML = `<div class="reveal">${lendingBodyMarkup()}</div>`;
        syncPageDemoSlot(true);
        return;
      }
      lendingState.error = error.message || "请求失败";
      content.innerHTML = retryMarkup("lending");
      syncPageDemoSlot(false);
    }
  }

  function optionsSkeleton() {
    return `
      <div aria-hidden="true">
        <div class="skeleton lend-sk-sum"></div>
        <div class="options-grid">
          <div>
            <div class="skeleton lend-sk-card"></div>
            <div class="skeleton lend-sk-card"></div>
          </div>
          <div class="skeleton side-sk-card"></div>
        </div>
      </div>`;
  }

  async function loadOptions(seq) {
    const content = document.getElementById("options-content");
    if (!content) return;
    const flag = mockFlag();
    try {
      const [options, rules] = await Promise.all([
        api("/options/summary"),
        api("/alerts/rules").catch((error) => {
          if (error.status === 401) throw error;
          return null;
        }),
      ]);
      if (seq !== state.renderSeq) return;
      if (Array.isArray(rules)) settingsState.alertRules = rules;
      const rows = optionRows(options);
      const allowMock = flag === "1" && rows.length === 0;
      lendingState.options = allowMock ? optionsMockPayload() : options;
      lendingState.usingMock = allowMock;
      content.innerHTML = `<div class="reveal">${optionsBookMarkup(lendingState.options)}</div>`;
      syncPageDemoSlot(allowMock);
    } catch (error) {
      if (error.status === 401) {
        expireSession();
        return;
      }
      if (seq !== state.renderSeq) return;
      if (error.status === 404) {
        const allowMock = flag === "1";
        lendingState.options = allowMock
          ? optionsMockPayload()
          : {
              total_mark_value_usdt: 0,
              total_premium_usdt: 0,
              unrealized_pnl_usdt: 0,
              positions: [],
            };
        lendingState.usingMock = allowMock;
        content.innerHTML = `<div class="reveal">${optionsBookMarkup(lendingState.options)}</div>`;
        syncPageDemoSlot(allowMock);
        return;
      }
      content.innerHTML = retryMarkup("options");
      syncPageDemoSlot(false);
    }
  }

  function renderOptionsPage(seq) {
    setTitle("期权");
    renderShell(
      `${topbarMarkup()}
      ${pageHead("衍生品持仓", "期权", "Deribit / 币安 · 只读")}
      <div id="options-content">${optionsSkeleton()}</div>`,
      "options",
    );
    loadOptions(seq);
  }

  function renderLendingPage(seq) {
    setTitle("借贷");
    renderShell(
      `${topbarMarkup()}
      ${pageHead("链上仓位", "借贷", "按钱包地址只读")}
      <div id="lending-content">${lendingSkeleton()}</div>`,
      "lending",
    );
    loadLending(seq);
    window.P25Loop?.mount(document);
  }

/* ---------- Notification centre + new-flow toast (p16) ---------- */

  // p16 owns every top-bar notification surface: the bell/badge, the centre
  // panel, and the new-flow toast. Alerts stay read-only; the only write path
  // is the existing POST /pending/{id}/confirm used by the pending page.
  const NOTIFY_BELL_SVG = `
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path d="M12 4.6a5.4 5.4 0 0 0-5.4 5.4v3.1L5 16.4h14l-1.6-3.3V10A5.4 5.4 0 0 0 12 4.6Z"/>
      <path d="M9.9 18.6a2.2 2.2 0 0 0 4.2 0"/>
    </svg>`;
  const NOTIFY_DOWN_SVG = `
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path d="M12 5v13"/>
      <path d="m6.5 12.5 5.5 5.5 5.5-5.5"/>
    </svg>`;
  const NOTIFY_CHEVRON_SVG = `
    <svg viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path d="m6 4 4 4-4 4"/>
    </svg>`;
  const NOTIFY_SEEN_KEY = "asset-tracker.notify-seen.v1";
  const NOTIFY_ALERT_KINDS = new Set(["hf", "hf_missing", "liq_distance"]);

  function reducedMotion() {
    return Boolean(window.matchMedia?.("(prefers-reduced-motion: reduce)").matches);
  }

  function notifyDelay(ms) {
    return new Promise((resolve) => window.setTimeout(resolve, ms));
  }

  function notificationBellMarkup() {
    return `
      <button class="notify-bell" type="button" data-action="toggle-notify"
        data-notify-bell aria-haspopup="dialog" aria-expanded="false"
        aria-label="通知，暂无未读">
        ${NOTIFY_BELL_SVG}
        <span class="notify-badge" data-notify-badge hidden></span>
      </button>`;
  }

  function notifySeenStore() {
    const userId = state.user?.id ?? "anon";
    if (state.notify.seen && state.notify.seenUser === userId) return state.notify.seen;
    state.notify.seenUser = userId;
    state.notify.seen = new Set();
    try {
      const raw = JSON.parse(localStorage.getItem(`${NOTIFY_SEEN_KEY}:${userId}`) || "[]");
      if (Array.isArray(raw)) {
        for (const key of raw.slice(-300)) state.notify.seen.add(String(key));
      }
    } catch (error) {
      state.notify.seen = new Set();
    }
    return state.notify.seen;
  }

  function notifyPersistSeen() {
    const seen = notifySeenStore();
    const keys = [...seen].slice(-300);
    state.notify.seen = new Set(keys);
    try {
      localStorage.setItem(`${NOTIFY_SEEN_KEY}:${state.notify.seenUser}`, JSON.stringify(keys));
    } catch (error) {
      // A full/blocked storage area must not break the badge.
    }
  }

  function notifyPendingItems() {
    const source = Array.isArray(state.notify.pending)
      ? state.notify.pending
      : Array.isArray(state.pendingItems)
        ? state.pendingItems
        : [];
    const pending = source.filter((item) => item && item.status === "pending");
    const skip = state.notify.skip;
    if (!skip.length) return pending;
    return pending.slice().sort((a, b) => {
      const ai = skip.indexOf(Number(a.id));
      const bi = skip.indexOf(Number(b.id));
      if (ai === -1 && bi === -1) return 0;
      if (ai === -1) return -1;
      if (bi === -1) return 1;
      return ai - bi;
    });
  }

  function notifyPositionsMap() {
    const map = new Map();
    const rows = Array.isArray(state.notify.positions) ? state.notify.positions : [];
    for (const row of rows) map.set(String(row.id), row);
    return map;
  }

  function notifyRuleFor(protocol, rules) {
    const list = Array.isArray(rules) ? rules : [];
    return (
      list.find((rule) => rule && rule.protocol === protocol && rule.enabled !== false) ||
      list.find((rule) => rule && rule.protocol === null) ||
      null
    );
  }

  function notifyProtocolLabel(protocol) {
    const key = String(protocol || "").trim();
    if (!key) return "借贷仓位";
    const labels = { aave_v3: "Aave V3", morpho: "Morpho", compound: "Compound" };
    return labels[key.toLowerCase()] || key;
  }

  function notifyStamp(value) {
    // Alert timestamps follow the card convention: Beijing time, MM-DD HH:mm.
    const stamp = formatCardDate(value, true, true);
    return stamp === "—" ? "" : stamp.slice(5);
  }

  function notifyDemoSuffix() {
    return isDemoAccount() ? " · 示例" : "";
  }

  // Two passes over the newest-first event list: the first row per position or
  // source is its current state. A newer recover row therefore closes older
  // warnings instead of leaving a ghost alert in the centre.
  function notifyActiveAlertRows() {
    const events = Array.isArray(state.notify.events) ? state.notify.events : [];
    const positions = notifyPositionsMap();
    const rules = state.notify.rules;
    const overview = state.notify.overview || state.overviewData || {};
    const seenPosition = new Set();
    const seenSource = new Set();
    const health = [];
    const sync = [];

    for (const event of events) {
      if (!event || typeof event !== "object") continue;
      const kind = String(event.event_kind || "");
      const level = String(event.level || "");
      if (kind === "wechat_send_health") continue;
      if (kind === "drawdown") continue;

      if (NOTIFY_ALERT_KINDS.has(kind) || (!kind && event.position_id !== null)) {
        const positionId = event.position_id === null || event.position_id === undefined
          ? `event:${event.id}`
          : String(event.position_id);
        if (seenPosition.has(positionId)) continue;
        seenPosition.add(positionId);
        if (level === "recover") continue;
        const position = positions.get(String(event.position_id)) || {};
        const protocol = position.protocol || position.protocol_key || "";
        const rule = notifyRuleFor(protocol, rules);
        const hf = Number(event.hf);
        const line = Number.isFinite(rule?.warn_hf) && Number.isFinite(rule?.danger_hf)
          ? `，${level === "danger" || hf < Number(rule.danger_hf) ? "低于" : "接近"} ${Number(rule.warn_hf).toFixed(2)} 警戒线`
          : "，请检查仓位";
        const label = position.label || position.name || notifyProtocolLabel(protocol);
        const hfText = Number.isFinite(hf) && hf >= 0 ? Number(hf).toFixed(2) : "—";
        const title =
          kind === "hf_missing" || !Number.isFinite(hf) || hf < 0
            ? `${label} 健康度暂时无法计算，请检查仓位`
            : `${label} 健康度 ${hfText}${line}`;
        health.push({
          key: `e${event.id}`,
          kind: "health",
          title,
          sub: `${notifyStamp(event.created_at) || "刚刚"}${notifyDemoSuffix()}`,
          retry: false,
        });
        continue;
      }

      const source = String(event.source_name || "").trim();
      if (!source) continue;
      if (seenSource.has(source)) continue;
      seenSource.add(source);
      if (level === "recover") continue;
      const stale = String(event.source_kind || "") === "stale";
      const latest = overview.latest_snapshot_ts ? `，已沿用 ${notifyStamp(overview.latest_snapshot_ts)} 数据` : "";
      sync.push({
        key: `e${event.id}`,
        kind: "sync",
        title: `${source} ${stale ? "数据过期" : "同步失败"}${latest}`,
        sub: `${notifyStamp(event.created_at) || "刚刚"}${notifyDemoSuffix()}`,
        retry: true,
      });
    }

    // p15b: the persisted send-health view is exposed on /overview. It has no
    // recover AlertEvent, so this read-only flag is the source of truth.
    if (overview.wechat_send_stale) {
      // Keep the row human: the raw send error belongs in Settings > 最近错误.
      const since = notifyStamp(overview.wechat_send_since);
      sync.unshift({
        key: `wechat:${overview.wechat_send_since || "stale"}`,
        kind: "sync",
        title: "微信推送通道异常",
        sub: `最近一次推送失败${since ? ` · ${since}` : ""}${notifyDemoSuffix()}`,
        retry: false,
      });
    }

    const reconcile = state.notify.reconcile || {};
    const reconcileStatus = reconcile.status || overview.reconcile_status;
    if (reconcileStatus && !["ok", "pass", "healthy"].includes(String(reconcileStatus))) {
      const latest = reconcile.latest || {};
      const failed = Array.isArray(latest.failed_sources) ? latest.failed_sources : [];
      const detail = failed.length ? ` · ${failed.slice(0, 2).join("、")}` : "";
      sync.push({
        key: `reconcile:${latest.id || reconcileStatus}`,
        kind: "sync",
        title: "账户对账未通过",
        sub: `${notifyStamp(latest.created_at || latest.ts) || "最近一次同步"}${detail}${notifyDemoSuffix()}`,
        retry: true,
      });
    }

    return { health, sync };
  }

  function notifyUnreadCount() {
    const pending = notifyPendingItems().length;
    if (!Array.isArray(state.notify.events) && !state.notify.overview && !state.overviewData) {
      return pending;
    }
    const seen = notifySeenStore();
    const rows = notifyActiveAlertRows();
    const unread = [...rows.health, ...rows.sync].filter((row) => !seen.has(row.key)).length;
    return pending + unread;
  }

  function updateNotifyBadges() {
    const count = notifyUnreadCount();
    document.querySelectorAll("[data-notify-badge]").forEach((badge) => {
      const previous = Number(badge.textContent || 0);
      badge.textContent = count > 99 ? "99+" : String(count);
      badge.hidden = count <= 0;
      if (!badge.hidden && count !== previous && !reducedMotion()) {
        badge.classList.remove("is-bump");
        void badge.offsetWidth;
        badge.classList.add("is-bump");
      }
    });
    document.querySelectorAll("[data-notify-bell]").forEach((bell) => {
      bell.setAttribute("aria-label", count > 0 ? `通知，${count} 条未读` : "通知，暂无未读");
      bell.classList.toggle("has-unread", count > 0);
    });
  }

  function markNotifyAlertsSeen() {
    const seen = notifySeenStore();
    const rows = notifyActiveAlertRows();
    let changed = false;
    for (const row of [...rows.health, ...rows.sync]) {
      if (!seen.has(row.key)) {
        seen.add(row.key);
        changed = true;
      }
    }
    if (changed) notifyPersistSeen();
    updateNotifyBadges();
  }

  async function refreshNotifySnapshot({ force = false } = {}) {
    const now = Date.now();
    if (!force && state.notify.pending && now - state.notify.lastLoadedAt < 10000) {
      return state.notify.loadPromise || Promise.resolve();
    }
    if (state.notify.loadPromise) return state.notify.loadPromise;
    state.notify.loadPromise = (async () => {
      const [pendingResult, eventResult] = await Promise.allSettled([
        api("/pending"),
        api("/alerts/events"),
      ]);
      if (pendingResult.status === "fulfilled" && Array.isArray(pendingResult.value)) {
        state.notify.pending = pendingResult.value;
        state.pendingCount = pendingCountOf(pendingResult.value);
      }
      if (eventResult.status === "fulfilled" && Array.isArray(eventResult.value)) {
        state.notify.events = eventResult.value;
      }
      state.notify.lastLoadedAt = Date.now();
      updatePendingBadges();
      updateNotifyBadges();
      if (state.notify.open) renderNotifyPanel();
      if (state.flowToast) syncFlowToast();
      return true;
    })();
    try {
      return await state.notify.loadPromise;
    } finally {
      state.notify.loadPromise = null;
    }
  }

  async function loadNotifyCenterData() {
    state.notify.loading = true;
    state.notify.error = "";
    if (state.notify.open) renderNotifyPanel();
    const results = await Promise.allSettled([
      refreshNotifySnapshot({ force: true }),
      api("/alerts/rules"),
      api("/positions?kind=lending"),
      api("/overview"),
      api("/sync/reconcile?limit=5"),
    ]);
    if (results[1].status === "fulfilled" && Array.isArray(results[1].value)) {
      state.notify.rules = results[1].value;
    }
    if (results[2].status === "fulfilled" && Array.isArray(results[2].value)) {
      state.notify.positions = results[2].value;
    }
    if (results[3].status === "fulfilled" && results[3].value) {
      state.notify.overview = results[3].value;
      if (!state.overviewData) state.overviewData = results[3].value;
    }
    if (results[4].status === "fulfilled" && results[4].value) {
      state.notify.reconcile = results[4].value;
    }
    const failed = results.filter((result) => result.status === "rejected");
    state.notify.error = failed.length ? "部分通知暂时读不到" : "";
    state.notify.loading = false;
    state.notify.lastLoadedAt = Date.now();
    markNotifyAlertsSeen();
    updateNotifyBadges();
    if (state.notify.open) renderNotifyPanel();
  }

  function notifyDirection(item) {
    const direction = String(item?.direction || "").toLowerCase();
    if (direction === "out") return "转出";
    if (direction === "in") return "转入";
    return String(item?.title || "").includes("转出") ? "转出" : "转入";
  }

  function notifyPartyLine(item) {
    const label = String(item?.counterparty_label || "").trim() || "未知对方";
    const address = shortAddress(counterpartyAddressOf(item));
    const chain = String(item?.counterparty_chain || "").trim();
    const parts = [label, address, chain].filter(Boolean).map((part) => escapeHtml(part));
    return parts.join('<span class="notify-sep" aria-hidden="true">·</span>');
  }

  function notifyAmountMarkup(item) {
    const raw = item?.amount_usdt;
    const value = raw === null || raw === undefined || raw === "" ? Number.NaN : Number(raw);
    if (!Number.isFinite(value)) return '<span class="notify-amount-missing">待估值</span>';
    const negative = value < 0 || String(item?.direction || "") === "out";
    return `<span class="notify-sign">${negative ? "−" : "+"}</span>${money(Math.abs(value))}`;
  }

  function notifyDateTime(value) {
    // Same Beijing-time card shape as the pending page (YYYY-MM-DD HH:mm).
    const stamp = formatCardDate(value, true, true);
    return stamp === "—" ? "" : stamp;
  }

  function notifyCardBodyMarkup(item, surface) {
    const id = Number(item.id);
    const selected = selectedCategoryOf(item);
    const chip = categoryLabel(selected);
    const occurred = notifyDateTime(item.ts || item.created_at);
    const demo = item.nature === "test_data" ? '<span class="notify-demo">示例</span>' : "";
    // No category (no user choice and no safe suggestion) means there is
    // nothing to archive: keep confirm inert until a category is picked.
    const canConfirm = Boolean(selected);
    const confirm = canConfirm
      ? `<button class="notify-confirm" type="button" data-notify-confirm="${id}">${ICONS.check}确认</button>`
      : `<button class="notify-confirm" type="button" data-notify-confirm="${id}" disabled aria-disabled="true" title="先选分类">先选分类</button>`;
    return `
      <div class="notify-card-head">
        <span>待确认流水 · ${notifyDirection(item)}</span>
        ${demo}
      </div>
      <div class="notify-amount">${notifyAmountMarkup(item)} <small>USDT</small></div>
      <p class="notify-party">${notifyPartyLine(item)}</p>
      ${occurred ? `<p class="notify-time">发生于 ${occurred}</p>` : ""}
      <div class="notify-cat-row">
        <span>选择分类</span>
        <button class="notify-cat" type="button" data-pick-category="${id}"
          aria-haspopup="dialog" aria-label="类别 ${escapeHtml(chip)}，点击更改">
          <span>${escapeHtml(chip)}</span><span class="notify-cat-caret" aria-hidden="true">▾</span>
        </button>
      </div>
      <div class="notify-actions">
        ${confirm}
        <button class="notify-change" type="button" data-pick-category="${id}">编辑</button>
      </div>`;
  }

  function notifyPanelCardMarkup(item, total) {
    return `
      <article class="notify-card" data-notify-card="${Number(item.id)}" tabindex="0">
        ${notifyCardBodyMarkup(item, "panel")}
      </article>`;
  }

  function notifyAlertRowMarkup(row) {
    return `
      <div class="notify-row" data-notify-alert="${escapeHtml(row.key)}">
        <span class="notify-dot ${row.kind === "sync" ? "notify-dot--sync" : "notify-dot--warn"}" aria-hidden="true"></span>
        <div class="notify-row-copy">
          <p class="notify-row-title">${escapeHtml(row.title)}</p>
          <p class="notify-row-sub">${escapeHtml(row.sub)}</p>
        </div>
        ${row.retry ? `<button class="notify-retry" type="button" data-sync-run data-notify-retry>重试</button>` : ""}
      </div>`;
  }

  function notifyPanelContentMarkup() {
    const pending = notifyPendingItems();
    const total = pending.length;
    const rows = notifyActiveAlertRows();
    const first = pending[0];
    const loading = state.notify.loading || (!state.notify.pending && !state.notify.events);
    const pendingHead = total
      ? `<div class="notify-group-head">
          <span>待确认 · <strong>${total}</strong></span>
          ${total > 1 ? '<span class="notify-hint">左滑跳过</span>' : ""}
        </div>`
      : `<div class="notify-group-head"><span>通知</span>${isDemoAccount() ? '<span class="notify-hint">示例</span>' : ""}</div>`;

    const pendingBody = first
      ? `<div class="notify-stack${total > 1 ? " has-more" : ""}">
          ${notifyPanelCardMarkup(first, total)}
          ${total > 1 ? '<span class="notify-edge notify-edge--one" aria-hidden="true"></span><span class="notify-edge notify-edge--two" aria-hidden="true"></span>' : ""}
        </div>`
      : loading
        ? `<div class="notify-loading" aria-hidden="true"><span></span><span></span><span></span></div>`
        : `<p class="notify-empty">没有待确认的流水</p>`;

    const health = rows.health.length
      ? `<section class="notify-section" aria-label="健康度告警">
          <h3>健康度告警</h3>
          ${rows.health.map(notifyAlertRowMarkup).join("")}
        </section>`
      : "";
    const sync = rows.sync.length
      ? `<section class="notify-section" aria-label="同步异常">
          <h3>同步异常</h3>
          ${rows.sync.map(notifyAlertRowMarkup).join("")}
        </section>`
      : "";
    const error = state.notify.error
      ? `<p class="notify-error">${escapeHtml(state.notify.error)}</p>`
      : "";
    return `${pendingHead}${pendingBody}${health}${sync}${error}
      <footer class="notify-foot">
        <a href="/pending" data-link data-notify-all>查看全部待确认 ${NOTIFY_CHEVRON_SVG}</a>
      </footer>`;
  }

  function renderNotifyPanel() {
    const root = state.notify.root;
    if (!root || !state.notify.open) return;
    const panel = root.querySelector(".notify-panel");
    if (!panel) return;
    const hadFocus = panel.contains(document.activeElement);
    panel.innerHTML = notifyPanelContentMarkup();
    panel.querySelector(".notify-card")?.classList.add("is-rising");
    if (hadFocus || document.activeElement === document.body) focusNotifyFirst(panel);
  }

  function focusNotifyFirst(panel) {
    const first = panel ? trapFocusables(panel)[0] || panel : null;
    first?.focus({ preventScroll: true });
  }

  function openNotifyPanel(trigger) {
    if (!state.user || state.notify.open) return;
    closeUserMenu();
    closeFlowToast({ animated: false });
    state.notify.toastSuppressed = true;
    const previousFocus = document.activeElement;
    const root = document.createElement("div");
    root.className = "notify-root";
    root.innerHTML = `
      <div class="notify-scrim" data-notify-close></div>
      <section class="notify-panel" role="dialog" aria-modal="true" aria-label="通知中心" tabindex="-1">
        ${notifyPanelContentMarkup()}
      </section>`;
    document.body.appendChild(root);
    document.body.classList.add("notify-open");
    state.notify.open = true;
    state.notify.root = root;
    state.notify.trigger = trigger || null;
    trigger?.setAttribute("aria-expanded", "true");
    const panel = root.querySelector(".notify-panel");
    window.requestAnimationFrame(() => {
      root.classList.add("is-open");
      focusNotifyFirst(panel);
      syncAppInert();
      window.setTimeout(() => {
        if (state.notify.open && panel && !panel.contains(document.activeElement)) focusNotifyFirst(panel);
      }, 220);
    });
    markNotifyAlertsSeen();
    loadNotifyCenterData();
  }

  function closeNotifyPanel({ restoreFocus = true, immediate = false } = {}) {
    const root = state.notify.root;
    state.notify.open = false;
    state.notify.root = null;
    state.notify.trigger?.setAttribute("aria-expanded", "false");
    document.body.classList.remove("notify-open");
    syncAppInert();
    if (!root) return;
    root.classList.remove("is-open");
    const remove = () => root.remove();
    if (immediate) remove();
    else window.setTimeout(remove, reducedMotion() ? 200 : 160);
    if (restoreFocus && state.notify.trigger && document.contains(state.notify.trigger)) {
      state.notify.trigger.focus({ preventScroll: true });
    }
    state.notify.trigger = null;
  }

  function toggleNotifyPanel(trigger) {
    if (state.notify.open) closeNotifyPanel();
    else openNotifyPanel(trigger);
  }

  function skipNotifyItem(id) {
    const number = Number(id);
    if (!Number.isInteger(number)) return;
    state.notify.skip = state.notify.skip.filter((value) => value !== number);
    state.notify.skip.push(number);
    renderNotifyPanel();
    announceNotify("已跳过，排到队尾", true);
  }

  function flowToastMarkup(item, total) {
    const id = Number(item.id);
    const demo = item.nature === "test_data" ? " · 示例" : "";
    const chain = String(item.counterparty_chain || "").trim();
    const label = String(item.counterparty_label || "").trim() || "未知对方";
    const sub = `${label}${chain ? ` · ${chain}` : ""}${demo}`;
    const more = total > 1 ? `还有 ${total - 1} 条 · 上滑收起` : "上滑收起";
    // The quick action must not archive an unclassified row. It mirrors the
    // expanded card: disabled until the user picks a category.
    const canConfirm = Boolean(selectedCategoryOf(item));
    return `
      <div class="flow-edge flow-edge--one" aria-hidden="true"></div>
      <div class="flow-edge flow-edge--two" aria-hidden="true"></div>
      <aside class="flow-toast" data-flow-toast role="status" aria-live="polite" aria-atomic="true">
        <button class="flow-summary" type="button" data-flow-expand aria-expanded="false" aria-controls="flow-card">
          <span class="flow-icon" aria-hidden="true">${NOTIFY_DOWN_SVG}</span>
          <span class="flow-copy">
            <span class="flow-amount">${notifyAmountMarkup(item)} <small>USDT</small></span>
            <span class="flow-sub">${escapeHtml(sub)}</span>
          </span>
        </button>
        <button class="flow-quick" type="button" data-flow-confirm="${id}"${
          canConfirm ? "" : ' disabled aria-disabled="true" title="先选分类"'
        }>${canConfirm ? "确认" : "先选分类"}</button>
        <div class="flow-card" id="flow-card" data-flow-card aria-hidden="true">
          ${notifyCardBodyMarkup(item, "toast")}
          <button class="flow-collapse" type="button" data-flow-collapse>${more}</button>
        </div>
      </aside>`;
  }

  function renderFlowToastContent() {
    const toast = state.flowToast;
    if (!toast || !toast.root) return;
    const items = notifyPendingItems();
    if (!items.length) {
      closeFlowToast();
      return;
    }
    const item = items[0];
    toast.itemId = Number(item.id);
    toast.total = items.length;
    const aside = toast.root.querySelector(".flow-toast");
    const expanded = Boolean(toast.expanded);
    toast.root.innerHTML = flowToastMarkup(item, items.length);
    const next = toast.root.querySelector(".flow-toast");
    next.classList.add("is-next");
    if (expanded) {
      next.classList.add("is-expanded");
      next.querySelector(".flow-summary")?.setAttribute("aria-expanded", "true");
      next.querySelector("[data-flow-card]")?.setAttribute("aria-hidden", "false");
    }
    if (aside) aside.remove();
  }

  function syncFlowToast() {
    if (!state.flowToast) return;
    const items = notifyPendingItems();
    if (!items.length) {
      closeFlowToast();
      return;
    }
    if (Number(items[0].id) !== state.flowToast.itemId) {
      renderFlowToastContent();
      return;
    }
    // Same head item: refresh its category label in place without replaying
    // the entrance animation or losing focus.
    const card = state.flowToast.root?.querySelector("[data-flow-card]");
    const item = items[0];
    if (card && state.flowToast.expanded) {
      const selected = selectedCategoryOf(item);
      card.querySelector(".notify-cat span")?.replaceChildren(document.createTextNode(categoryLabel(selected)));
    }
    const copy = state.flowToast.root?.querySelector(".flow-amount");
    if (copy) copy.innerHTML = `${notifyAmountMarkup(item)} <small>USDT</small>`;
  }

  function createFlowToast() {
    const items = notifyPendingItems();
    if (!items.length || currentPath() !== "/" || state.notify.toastSuppressed) return;
    const root = document.createElement("div");
    root.className = "flow-toast-root";
    root.innerHTML = flowToastMarkup(items[0], items.length);
    document.body.appendChild(root);
    state.flowToast = {
      root,
      expanded: false,
      itemId: Number(items[0].id),
      total: items.length,
      timer: 0,
      deadline: 0,
      remaining: 6000,
      paused: false,
    };
    window.requestAnimationFrame(() => root.classList.add("is-in"));
    const head = items[0];
    const rawAmount = head.amount_usdt;
    const amount = rawAmount === null || rawAmount === undefined || rawAmount === "" ? Number.NaN : Number(rawAmount);
    const party = String(head.counterparty_label || "").trim() || "未知对方";
    const direction = notifyDirection(head);
    const amountText = Number.isFinite(amount)
      ? `${direction === "转出" ? "−" : "+"}${money(Math.abs(amount))} USDT`
      : "金额待估值";
    announceNotify(`新流水 ${amountText}，来自 ${party}，待确认`);
    startFlowTimer();
  }

  function maybeShowFlowToast() {
    if (state.notify.toastSuppressed) return;
    if (state.flowToast?.root) {
      syncFlowToast();
      return;
    }
    if (!state.notify.pending) {
      refreshNotifySnapshot().then(() => maybeShowFlowToast());
      return;
    }
    createFlowToast();
  }

  function stopFlowTimer() {
    const toast = state.flowToast;
    if (!toast) return;
    if (toast.timer) window.clearTimeout(toast.timer);
    toast.timer = 0;
  }

  function startFlowTimer(duration = 6000) {
    const toast = state.flowToast;
    if (!toast || toast.expanded) return;
    stopFlowTimer();
    toast.remaining = duration;
    toast.deadline = Date.now() + duration;
    toast.timer = window.setTimeout(() => {
      if (!state.flowToast || state.flowToast.expanded) return;
      hideFlowToast();
    }, duration);
  }

  function pauseFlowTimer() {
    const toast = state.flowToast;
    if (!toast || toast.timer === 0) return;
    stopFlowTimer();
    toast.remaining = Math.max(0, toast.deadline - Date.now());
    toast.paused = true;
  }

  function resumeFlowTimer() {
    const toast = state.flowToast;
    if (!toast || !toast.paused || toast.expanded) return;
    toast.paused = false;
    startFlowTimer(Math.max(400, toast.remaining || 6000));
  }

  function expandFlowToast() {
    const toast = state.flowToast;
    if (!toast?.root) return;
    stopFlowTimer();
    toast.expanded = true;
    const aside = toast.root.querySelector(".flow-toast");
    aside?.classList.add("is-expanded");
    aside?.querySelector(".flow-summary")?.setAttribute("aria-expanded", "true");
    aside?.querySelector("[data-flow-card]")?.setAttribute("aria-hidden", "false");
    window.setTimeout(() => {
      const focusTarget =
        aside?.querySelector("[data-notify-confirm]:not([disabled])") ||
        aside?.querySelector("[data-pick-category]");
      focusTarget?.focus({ preventScroll: true });
    }, reducedMotion() ? 0 : 220);
  }

  function collapseFlowToast() {
    const toast = state.flowToast;
    if (!toast?.root) return;
    toast.expanded = false;
    const aside = toast.root.querySelector(".flow-toast");
    aside?.classList.remove("is-expanded");
    aside?.querySelector(".flow-summary")?.setAttribute("aria-expanded", "false");
    aside?.querySelector("[data-flow-card]")?.setAttribute("aria-hidden", "true");
    aside?.querySelector("[data-flow-expand]")?.focus({ preventScroll: true });
    startFlowTimer();
  }

  function closeFlowToast({ animated = true } = {}) {
    const toast = state.flowToast;
    state.flowToast = null;
    if (!toast?.root) return;
    stopFlowTimer();
    const root = toast.root;
    if (animated && !reducedMotion()) {
      root.classList.add("is-flying");
      window.setTimeout(() => root.remove(), 300);
    } else {
      root.remove();
    }
  }

  function flyFlowToastToBell() {
    if (!state.flowToast?.root) return;
    const toast = state.flowToast;
    const bell = document.querySelector("[data-notify-bell]");
    if (!bell || reducedMotion()) {
      state.flowToast = null;
      toast.root.classList.add("is-flying");
      window.setTimeout(() => toast.root.remove(), 210);
      return;
    }
    const a = toast.root.getBoundingClientRect();
    const b = bell.getBoundingClientRect();
    const x = b.left + b.width / 2 - (a.left + a.width / 2);
    const y = b.top + b.height / 2 - (a.top + a.height / 2);
    state.flowToast = null;
    stopFlowTimer();
    toast.root.style.setProperty("--fly-x", `${x}px`);
    toast.root.style.setProperty("--fly-y", `${y}px`);
    toast.root.classList.add("is-flying");
    bell.classList.add("is-ringing");
    window.setTimeout(() => bell.classList.remove("is-ringing"), 360);
    window.setTimeout(() => toast.root.remove(), 300);
  }

  function hideFlowToast() {
    const toast = state.flowToast;
    if (!toast) return;
    if (toast.expanded) return;
    if (reducedMotion()) {
      state.flowToast = null;
      toast.root.classList.add("is-flying");
      window.setTimeout(() => toast.root.remove(), 210);
      return;
    }
    flyFlowToastToBell();
  }

  function announceNotify(message, assertive = false) {
    let node = document.querySelector("[data-notify-live]");
    if (!node) {
      node = document.createElement("p");
      node.className = "notify-sr";
      node.setAttribute("data-notify-live", "");
      node.setAttribute("role", "status");
      node.setAttribute("aria-live", "polite");
      node.setAttribute("aria-atomic", "true");
      document.body.appendChild(node);
    }
    node.setAttribute("aria-live", assertive ? "assertive" : "polite");
    node.textContent = "";
    window.setTimeout(() => {
      node.textContent = message;
    }, 30);
  }

  function removePendingFromNotify(id, category) {
    const number = Number(id);
    state.notify.skip = state.notify.skip.filter((value) => value !== number);
    if (Array.isArray(state.notify.pending)) {
      state.notify.pending = state.notify.pending.filter((item) => Number(item.id) !== number);
    }
    if (Array.isArray(state.pendingItems)) {
      state.pendingItems = state.pendingItems.filter((item) => Number(item.id) !== number);
    }
    state.pendingCount = Math.max(0, (Number.isFinite(state.pendingCount) ? state.pendingCount : 1) - 1);
    updatePendingBadges();
    updateNotifyBadges();
    if (state.notify.open) renderNotifyPanel();
    if (state.flowToast) {
      if (notifyPendingItems().length) renderFlowToastContent();
      else flyFlowToastToBell();
    }
    void category;
  }

  async function confirmNotifyItem(id, button) {
    const number = Number(id);
    if (!Number.isInteger(number) || state.notify.confirming.has(number)) return;
    const category = resolveConfirmCategory(number);
    if (!category) {
      showToast("请先选择类别", "error");
      return;
    }
    state.notify.confirming.add(number);
    const original = button.innerHTML;
    button.disabled = true;
    button.classList.add("is-busy");
    button.textContent = "确认中…";
    try {
      await api(`/pending/${number}/confirm`, {
        method: "POST",
        body: { category },
      });
      state.pendingChoices.delete(number);
      button.classList.remove("is-busy");
      button.classList.add("is-success");
      button.innerHTML = ICONS.check;
      await notifyDelay(reducedMotion() ? 160 : 400);
      const card = button.closest(".notify-card");
      const toast = button.closest(".flow-toast");
      if (card) card.classList.add("is-flying");
      else if (toast) toast.classList.add("is-card-leaving");
      await notifyDelay(reducedMotion() ? 180 : 210);
      removePendingFromNotify(number, category);
      const remaining = notifyPendingItems().length;
      announceNotify(remaining ? `已确认，还有 ${remaining} 条` : "已确认，通知已清空", true);
    } catch (error) {
      if (error.status === 401) {
        expireSession();
        return;
      }
      button.disabled = false;
      button.classList.remove("is-busy");
      button.innerHTML = original;
      showToast(error.message || "确认失败，请稍后重试", "error");
    } finally {
      state.notify.confirming.delete(number);
    }
  }

  function refreshNotifySurfaces() {
    updatePendingBadges();
    if (state.notify.open) renderNotifyPanel();
    if (state.flowToast) syncFlowToast();
  }

  function installNotifyGestures() {
    let gesture = null;
    const reset = () => {
      if (!gesture) return;
      gesture.el.style.transform = "";
      gesture.el.style.transition = "";
      gesture = null;
    };
    document.addEventListener("pointerdown", (event) => {
      if (event.pointerType !== "touch" || event.button !== 0) return;
      if (event.target.closest("button, a")) return;
      const card = event.target.closest("[data-notify-card]");
      const toast = event.target.closest(".flow-toast");
      if (card) {
        gesture = {
          kind: "card",
          el: card,
          id: Number(card.getAttribute("data-notify-card")),
          x: event.clientX,
          y: event.clientY,
          dx: 0,
          dy: 0,
          vy: 0,
          lastY: event.clientY,
          lastT: performance.now(),
        };
      } else if (toast) {
        gesture = {
          kind: "toast",
          el: toast,
          x: event.clientX,
          y: event.clientY,
          dx: 0,
          dy: 0,
          vy: 0,
          lastY: event.clientY,
          lastT: performance.now(),
          expanded: Boolean(state.flowToast?.expanded),
        };
      }
    });
    document.addEventListener("pointermove", (event) => {
      if (!gesture || event.pointerType !== "touch") return;
      const now = performance.now();
      const elapsed = Math.max(1, now - gesture.lastT);
      gesture.vy = ((event.clientY - gesture.lastY) / elapsed) * 1000;
      gesture.lastY = event.clientY;
      gesture.lastT = now;
      gesture.dx = event.clientX - gesture.x;
      gesture.dy = event.clientY - gesture.y;
      if (Math.abs(gesture.dx) < 6 && Math.abs(gesture.dy) < 6) return;
      gesture.el.style.transition = "none";
      if (gesture.kind === "card") {
        const dx = Math.max(-120, Math.min(0, gesture.dx));
        gesture.el.style.transform = `translateX(${dx}px) rotate(${dx / 40}deg)`;
      } else if (!gesture.expanded && gesture.dy > 0) {
        gesture.el.style.transform = `translateY(${Math.min(36, gesture.dy * 0.5)}px)`;
      } else if (!gesture.expanded && gesture.dy < 0) {
        gesture.el.style.transform = `translateY(${Math.max(-48, gesture.dy)}px)`;
      } else if (gesture.expanded && gesture.dy < 0) {
        gesture.el.style.transform = `translateY(${Math.max(-32, gesture.dy * 0.6)}px)`;
      }
    });
    document.addEventListener("pointerup", (event) => {
      if (!gesture || event.pointerType !== "touch") return;
      const current = gesture;
      const horizontal = Math.abs(current.dx) > Math.abs(current.dy);
      const flickUp = current.vy < -500;
      if (current.kind === "card" && horizontal && current.dx < -64) {
        state.notify.swipeAt = Date.now();
        reset();
        skipNotifyItem(current.id);
        return;
      }
      if (current.kind === "toast") {
        if (!current.expanded && current.dy > 24) {
          state.notify.swipeAt = Date.now();
          reset();
          expandFlowToast();
          return;
        }
        if (!current.expanded && (current.dy < -40 || flickUp)) {
          state.notify.swipeAt = Date.now();
          reset();
          closeFlowToast();
          return;
        }
        if (current.expanded && (current.dy < -40 || flickUp)) {
          state.notify.swipeAt = Date.now();
          reset();
          collapseFlowToast();
          return;
        }
      }
      current.el.style.transition = "";
      current.el.style.transform = "";
      gesture = null;
    });
    document.addEventListener("pointercancel", reset);
  }

  /* ---------- Routing ---------- */

  function renderRoute(path) {
    if (!state.user) {
      window.history.replaceState(null, "", "/login");
      renderLogin();
      return;
    }

    if (state.moreSheet) closeMoreSheet({ restoreFocus: false });
    if (state.notify.open) closeNotifyPanel({ restoreFocus: false, immediate: true });
    if (state.flowToast) closeFlowToast({ animated: false });

    let route = normalizePath(path);
    if (route === "/login") {
      route = "/";
      window.history.replaceState(null, "", "/");
    }

    if (route !== currentPath()) {
      window.history.replaceState(null, "", route);
    }

    const seq = ++state.renderSeq;
    window.scrollTo(0, 0);

    if (route === "/returns") {
      renderReturnsPage(seq);
    } else if (route === "/pending") {
      renderPendingPage(seq);
    } else if (route === "/settings") {
      renderSettingsPage(seq);
    } else if (route === "/lending") {
      renderLendingPage(seq);
    } else if (route === "/options") {
      renderOptionsPage(seq);
    } else {
      renderOverviewPage(seq);
    }
  }

  function go(path, { replace = false } = {}) {
    const route = normalizePath(path);
    if (!state.user) {
      window.history.replaceState(null, "", "/login");
      renderLogin();
      return;
    }
    if (route === "/login") {
      window.history.replaceState(null, "", "/");
      renderRoute("/");
      return;
    }

    if (route !== currentPath()) {
      if (replace) window.history.replaceState(null, "", route);
      else window.history.pushState(null, "", route);
    }
    renderRoute(route);
  }

  function logout() {
    clearSession();
    state.renderSeq += 1;
    window.history.replaceState(null, "", "/login");
    renderLogin();
  }

  function retryCurrentPage() {
    renderRoute(currentPath());
  }

  /* ---------- Events ---------- */

  function closeUserMenu() {
    document.querySelectorAll(".user-menu").forEach((menu) => {
      menu.classList.remove("is-open");
      const trigger = menu.querySelector("[data-action='toggle-user-menu']");
      const popover = menu.querySelector(".user-popover");
      if (trigger) trigger.setAttribute("aria-expanded", "false");
      if (popover) popover.hidden = true;
    });
  }

  function toggleUserMenu(trigger) {
    const menu = trigger.closest(".user-menu");
    if (!menu) return;
    const popover = menu.querySelector(".user-popover");
    const shouldOpen = !popover || popover.hidden;
    closeUserMenu();
    if (!shouldOpen) return;
    menu.classList.add("is-open");
    trigger.setAttribute("aria-expanded", "true");
    if (popover) popover.hidden = false;
  }

  document.addEventListener(
    "error",
    (ev) => {
      const img = ev.target;
      if (!(img instanceof HTMLImageElement) || !img.classList.contains("hold-icon-img")) return;
      if (img.getAttribute("data-icon-dollar") === "1") return;
      // A data-URI that already failed is the letter disc or the $ disc — don't loop.
      if (String(img.currentSrc || img.src || "").startsWith("data:")) return;
      const raw = img.getAttribute("data-icon-fallbacks") || "";
      const queue = raw.split("|").map((url) => url.trim()).filter((url) => url && !isBrokenIconSrc(url));
      if (queue.length) {
        img.setAttribute("data-icon-fallbacks", queue.slice(1).join("|"));
        img.src = queue[0];
        return;
      }
      img.removeAttribute("data-icon-fallbacks");
      // Exhausted URLs, or the src was empty / failed to begin with: grey $ disc.
      img.src = dollarFallbackIcon();
      img.setAttribute("data-icon-dollar", "1");
      img.classList.add("hold-icon-img--fallback");
    },
    true,
  );

  document.addEventListener("pointermove", updateChartGuide, { passive: true });

  document.addEventListener("mouseover", (event) => {
    if (event.target.closest(".flow-toast")) pauseFlowTimer();
  });
  document.addEventListener("mouseout", (event) => {
    if (!event.target.closest(".flow-toast")) return;
    if (event.relatedTarget && event.relatedTarget.closest?.(".flow-toast")) return;
    resumeFlowTimer();
  });
  document.addEventListener("focusin", (event) => {
    if (event.target.closest?.(".flow-toast")) pauseFlowTimer();
  });
  document.addEventListener("focusout", (event) => {
    if (!event.target.closest?.(".flow-toast")) return;
    if (event.relatedTarget && event.relatedTarget.closest?.(".flow-toast")) return;
    resumeFlowTimer();
  });

  document.addEventListener("click", (event) => {
    const target = event.target.closest(
      "[data-link], [data-action='logout'], [data-action='open-more'], [data-more-nav], [data-more-close], [data-more-logout], [data-action='toggle-user-menu'], [data-action='toggle-alert'], [data-action='close-alert'], [data-action='toggle-notify'], [data-period], [data-confirm], [data-retry], [data-pick-category], [data-sheet-option], [data-sheet-close], [data-reset-category], [data-more], [data-pending-more], [data-toggle-confirmed], [data-cs-ask-delete], [data-cs-cancel-delete], [data-cs-delete], [data-cs-reload], [data-cs-replace], [data-cs-cancel-replace], [data-wl-copy], [data-wl-ask-delete], [data-wl-cancel-delete], [data-wl-delete], [data-wl-open-form], [data-wl-cancel-form], [data-users-open-form], [data-users-cancel-form], [data-users-ask-reset], [data-users-cancel-reset], [data-users-confirm-reset], [data-cs-toggle-guide], [data-holdings-mode], [data-holdings-toggle-asset], [data-holdings-toggle-source], [data-holdings-toggle-misc], [data-sync-run], [data-lend-fold], [data-lend-info], [data-alert-rule], [data-alert-rule-add], [data-alert-rule-reset], [data-alert-history-refresh], [data-alert-history-more], [data-pf-tab], [data-pf-class], [data-pf-risk], [data-pf-back], [data-pf-edit], [data-pf-misc], [data-pf-retry], [data-pf-sheet-close], [data-pf-class-option], [data-pf-risk-option], [data-pf-reset], [data-wechat-dismiss], [data-wechat-details], [data-notify-close], [data-flow-expand], [data-flow-collapse], [data-flow-confirm], [data-notify-confirm], [data-notify-retry], [data-password-toggle], [data-settings-open], [data-settings-jump], [data-settings-anchor], [data-settings-back], [data-danger], [data-settings-danger-cancel], [data-settings-danger-confirm]",
    );

    if (!event.target.closest(".user-menu")) {
      closeUserMenu();
    }
    if (!target) return;

    if (target.matches("[data-action='open-more']")) {
      event.preventDefault();
      openMoreSheet(target);
      return;
    }

    if (target.matches("[data-more-close]")) {
      event.preventDefault();
      closeMoreSheet();
      return;
    }

    if (target.matches("[data-more-nav]")) {
      event.preventDefault();
      const dest = target.getAttribute("data-more-nav");
      if (dest !== "settings" && dest !== "pending") return;
      closeMoreSheet({ restoreFocus: false, onClosed: () => go(`/${dest}`) });
      return;
    }

    if (target.matches("[data-more-logout]")) {
      event.preventDefault();
      closeMoreSheet({ restoreFocus: false, onClosed: () => openSettingsDanger("logout") });
      return;
    }

    if (target.matches("[data-settings-anchor]")) {
      event.preventDefault();
      settingsNavigate(target.getAttribute("data-settings-anchor"));
      return;
    }

    if (target.matches("[data-settings-open], [data-settings-jump]")) {
      event.preventDefault();
      settingsNavigate(
        target.getAttribute("data-settings-open") || target.getAttribute("data-settings-jump"),
      );
      return;
    }

    if (target.matches("[data-settings-back]")) {
      event.preventDefault();
      settingsCloseSection();
      return;
    }

    if (target.matches("[data-danger]")) {
      event.preventDefault();
      openSettingsDanger(target.getAttribute("data-danger"));
      return;
    }

    if (target.matches("[data-settings-danger-cancel]")) {
      event.preventDefault();
      closeSettingsDanger();
      return;
    }

    if (target.matches("[data-settings-danger-confirm]")) {
      event.preventDefault();
      confirmSettingsDanger();
      return;
    }

    if (target.matches("[data-pf-tab]")) {
      event.preventDefault();
      selectPortfolioTab(target.getAttribute("data-pf-tab"));
      return;
    }

    if (target.matches("[data-pf-class]")) {
      event.preventDefault();
      selectPortfolioClass(target.getAttribute("data-pf-class"));
      return;
    }

    if (target.matches("[data-pf-risk]")) {
      event.preventDefault();
      selectPortfolioRisk(target.getAttribute("data-pf-risk"));
      return;
    }

    if (target.matches("[data-pf-back]")) {
      event.preventDefault();
      showPortfolioOverview();
      return;
    }

    if (target.matches("[data-pf-misc]")) {
      event.preventDefault();
      togglePortfolioMisc(target.getAttribute("data-pf-misc"));
      return;
    }

    if (target.matches("[data-pf-edit]")) {
      event.preventDefault();
      openPortfolioSheet(target.getAttribute("data-pf-edit"), target);
      return;
    }

    if (target.matches("[data-pf-sheet-close]")) {
      event.preventDefault();
      closePortfolioSheet();
      return;
    }

    if (target.matches("[data-pf-class-option]")) {
      event.preventDefault();
      choosePortfolioClass(target.getAttribute("data-pf-class-option"));
      return;
    }

    if (target.matches("[data-pf-risk-option]")) {
      event.preventDefault();
      choosePortfolioRisk(target.getAttribute("data-pf-risk-option"));
      return;
    }

    if (target.matches("[data-pf-reset]")) {
      event.preventDefault();
      resetPortfolioOverride(target);
      return;
    }

    if (target.matches("[data-pf-retry]")) {
      event.preventDefault();
      loadPortfolioStructure({ force: true });
      return;
    }

    if (target.matches("[data-wechat-dismiss]")) {
      event.preventDefault();
      // Dismissal lives in memory only and is scoped to this stale incident
      // plus its 24h/48h age tier. A reload starts a fresh page lifecycle.
      const banner = target.closest("[data-wechat-banner]");
      state.wechatBannerDismissTier =
        banner?.getAttribute("data-wechat-tier") || state.wechatBannerTier;
      banner?.remove();
      return;
    }

    if (target.matches("[data-link]")) {
      event.preventDefault();
      closeUserMenu();
      const href = target.getAttribute("href");
      if (href) go(href);
      return;
    }

    if (target.matches("[data-action='toggle-user-menu']")) {
      event.preventDefault();
      toggleUserMenu(target);
      return;
    }

    if (target.matches("[data-wechat-details]")) {
      event.preventDefault();
      settingsState.wechatDetailsOpen = !settingsState.wechatDetailsOpen;
      updateWechatSettingsCard();
      document.querySelector("[data-wechat-details]")?.focus();
      return;
    }

    if (target.matches("[data-action='logout']")) {
      event.preventDefault();
      openSettingsDanger("logout");
      return;
    }

    if (target.matches("[data-password-toggle]")) {
      event.preventDefault();
      const input = document.getElementById("password");
      if (input) setPasswordVisible(input.type === "password");
      return;
    }

    if (target.matches("[data-action='toggle-notify']")) {
      event.preventDefault();
      toggleNotifyPanel(target);
      return;
    }

    if (target.matches("[data-notify-close]")) {
      event.preventDefault();
      closeNotifyPanel();
      return;
    }

    if (target.matches("[data-flow-expand]")) {
      event.preventDefault();
      if (Date.now() - (state.notify.swipeAt || 0) < 320) return;
      expandFlowToast();
      return;
    }

    if (target.matches("[data-flow-collapse]")) {
      event.preventDefault();
      collapseFlowToast();
      return;
    }

    if (target.matches("[data-flow-confirm]")) {
      event.preventDefault();
      confirmNotifyItem(Number(target.getAttribute("data-flow-confirm")), target);
      return;
    }

    if (target.matches("[data-notify-confirm]")) {
      event.preventDefault();
      confirmNotifyItem(Number(target.getAttribute("data-notify-confirm")), target);
      return;
    }

    if (target.matches("[data-notify-retry]")) {
      event.preventDefault();
      closeNotifyPanel({ restoreFocus: false });
      runManualSync();
      return;
    }

    if (target.matches("[data-sync-run]")) {
      event.preventDefault();
      runManualSync();
      return;
    }

    if (target.matches("[data-holdings-mode]")) {
      event.preventDefault();
      const mode = target.getAttribute("data-holdings-mode");
      if (mode !== "asset" && mode !== "source") return;
      if (state.holdingsMode === mode) return;
      state.holdingsMode = mode;
      if (mode === "source") {
        // reopen default top source if user cleared all
        // (ensure happens inside holdingsBySourceMarkup)
      }
      rerenderOverviewHoldings();
      return;
    }

    if (target.matches("[data-holdings-toggle-asset]")) {
      event.preventDefault();
      const key = target.getAttribute("data-holdings-toggle-asset");
      if (!key) return;
      if (state.holdingsOpenAssets.has(key)) state.holdingsOpenAssets.delete(key);
      else state.holdingsOpenAssets.add(key);
      rerenderOverviewHoldings();
      return;
    }

    if (target.matches("[data-holdings-toggle-source]")) {
      event.preventDefault();
      const key = target.getAttribute("data-holdings-toggle-source");
      if (!key) return;
      if (state.holdingsOpenSources.has(key)) state.holdingsOpenSources.delete(key);
      else state.holdingsOpenSources.add(key);
      rerenderOverviewHoldings();
      return;
    }

    if (target.matches("[data-holdings-toggle-misc]")) {
      event.preventDefault();
      const key = target.getAttribute("data-holdings-toggle-misc");
      if (!key) return;
      if (state.holdingsOpenMisc.has(key)) state.holdingsOpenMisc.delete(key);
      else state.holdingsOpenMisc.add(key);
      rerenderOverviewHoldings();
      return;
    }

    if (target.matches("[data-period]")) {
      const period = target.getAttribute("data-period");
      if (!PERIODS.includes(period) || state.period === period) return;
      state.period = period;
      document.querySelectorAll("[data-period]").forEach((button) => {
        button.setAttribute("aria-pressed", String(button.getAttribute("data-period") === period));
      });
      const seq = ++state.renderSeq;
      const content = document.getElementById("returns-content");
      if (content) content.innerHTML = returnsSkeleton();
      loadReturns(seq);
      return;
    }

    if (target.matches("[data-confirm]")) {
      const id = Number(target.getAttribute("data-confirm"));
      if (Number.isInteger(id) && id > 0) {
        confirmPending(id, target);
      }
      return;
    }

    if (target.matches("[data-pick-category]")) {
      event.preventDefault();
      const id = Number(target.getAttribute("data-pick-category"));
      if (Number.isInteger(id) && id > 0) openCategorySheet(id, target);
      return;
    }

    if (target.matches("[data-sheet-option]")) {
      event.preventDefault();
      chooseCategory(target.getAttribute("data-sheet-option"));
      return;
    }

    if (target.matches("[data-sheet-close]")) {
      event.preventDefault();
      closeCategorySheet();
      return;
    }

    if (target.matches("[data-reset-category]")) {
      event.preventDefault();
      const id = Number(target.getAttribute("data-reset-category"));
      if (Number.isInteger(id) && id > 0) resetCategory(id);
      return;
    }

    if (target.matches("[data-more]")) {
      event.preventDefault();
      toggleMore(target);
      return;
    }

    if (target.matches("[data-pending-more]")) {
      event.preventDefault();
      state.pendingVisible += PENDING_PAGE_SIZE;
      rerenderPendingList();
      return;
    }

    if (target.matches("[data-toggle-confirmed]")) {
      event.preventDefault();
      state.confirmedOpen = !state.confirmedOpen;
      rerenderPendingList();
      document.querySelector("[data-toggle-confirmed]")?.focus();
      return;
    }

    if (target.matches("[data-cs-replace]")) {
      event.preventDefault();
      settingsState.replacing = true;
      settingsState.confirmDelete = false;
      csRefreshKeyArea();
      document.getElementById("cs-key-input")?.focus();
      return;
    }

    if (target.matches("[data-cs-cancel-replace]")) {
      event.preventDefault();
      settingsState.replacing = false;
      settingsState.saving = false;
      csRefreshKeyArea();
      document.querySelector("[data-cs-replace]")?.focus();
      return;
    }

    if (target.matches("[data-cs-ask-delete]")) {
      event.preventDefault();
      settingsState.confirmDelete = true;
      settingsState.replacing = false;
      csRefreshKeyArea();
      document.querySelector("[data-cs-cancel-delete]")?.focus();
      return;
    }

    if (target.matches("[data-cs-cancel-delete]")) {
      event.preventDefault();
      settingsState.confirmDelete = false;
      csRefreshKeyArea();
      document.querySelector("[data-cs-ask-delete]")?.focus();
      return;
    }

    if (target.matches("[data-cs-delete]")) {
      event.preventDefault();
      deleteCoinstatsKey();
      return;
    }

    if (target.matches("[data-cs-reload]")) {
      event.preventDefault();
      loadCoinstatsExtras(state.renderSeq);
      return;
    }

    if (target.matches("[data-cs-toggle-guide]")) {
      event.preventDefault();
      settingsState.guideOpen = !settingsState.guideOpen;
      csSet("cs-guide-area", csGuideMarkup());
      return;
    }

    if (target.matches("[data-wl-open-form]")) {
      event.preventDefault();
      settingsState.walletFormOpen = true;
      settingsState.walletDeleteId = null;
      settingsState.walletDeletingAll = false;
      settingsState.walletHint = "";
      settingsState.walletHintTone = "";
      wlRefresh();
      document.getElementById("wl-address")?.focus();
      return;
    }

    if (target.matches("[data-wl-cancel-form]")) {
      event.preventDefault();
      settingsState.walletFormOpen = false;
      settingsState.walletHint = "";
      settingsState.walletHintTone = "";
      wlRefresh();
      return;
    }

    if (target.matches("[data-wl-copy]")) {
      event.preventDefault();
      const address = target.getAttribute("data-wl-copy");
      if (address) copyWalletAddress(address, target);
      return;
    }

    if (target.matches("[data-wl-ask-delete]")) {
      event.preventDefault();
      if (target.disabled) return;
      const id = Number(target.getAttribute("data-wl-ask-delete"));
      if (!Number.isInteger(id) || id <= 0) return;
      settingsState.walletDeleteId = id;
      settingsState.walletDeleting = false;
      settingsState.walletFormOpen = false;
      wlRefresh();
      document.querySelector("[data-wl-cancel-delete]")?.focus();
      return;
    }

    if (target.matches("[data-wl-cancel-delete]")) {
      event.preventDefault();
      settingsState.walletDeleteId = null;
      settingsState.walletDeleting = false;
      wlRefresh();
      return;
    }

    if (target.matches("[data-wl-delete]")) {
      event.preventDefault();
      deleteWallet();
      return;
    }

    if (target.matches("[data-users-open-form]")) {
      event.preventDefault();
      settingsState.usersFormOpen = true;
      settingsState.usersResetId = null;
      settingsState.usersHint = "";
      settingsState.usersHintTone = "";
      usersRefresh();
      document.getElementById("users-username")?.focus();
      return;
    }

    if (target.matches("[data-users-cancel-form]")) {
      event.preventDefault();
      settingsState.usersFormOpen = false;
      settingsState.usersHint = "";
      settingsState.usersHintTone = "";
      usersRefresh();
      return;
    }

    if (target.matches("[data-users-ask-reset]")) {
      event.preventDefault();
      if (target.disabled) return;
      const id = Number(target.getAttribute("data-users-ask-reset"));
      if (!Number.isInteger(id) || id <= 0) return;
      settingsState.usersResetId = id;
      settingsState.usersResetting = false;
      settingsState.usersFormOpen = false;
      usersRefresh();
      document.getElementById("users-reset-password")?.focus();
      return;
    }

    if (target.matches("[data-users-cancel-reset]")) {
      event.preventDefault();
      settingsState.usersResetId = null;
      settingsState.usersResetting = false;
      usersRefresh();
      return;
    }

    if (target.matches("[data-users-confirm-reset]")) {
      const form = target.closest("#users-reset-form");
      if (form) return;
      event.preventDefault();
      return;
    }

    if (target.matches("[data-lend-fold]")) {
      event.preventDefault();
      const key = target.getAttribute("data-lend-fold");
      if (!key) return;
      if (lendingState.open.has(key)) lendingState.open.delete(key);
      else lendingState.open.add(key);
      rerenderLendingBody();
      return;
    }

    if (target.matches("[data-lend-info]")) {
      event.preventDefault();
      lendingState.infoOpen = !lendingState.infoOpen;
      rerenderLendingBody();
      return;
    }

    if (target.matches("[data-alert-rule-reset]")) {
      event.preventDefault();
      resetAlertRule();
      return;
    }

    if (target.matches("[data-alert-rule]")) {
      event.preventDefault();
      const raw = target.getAttribute("data-alert-rule");
      selectAlertRule(!raw || raw === "global" ? null : raw);
      return;
    }

    if (target.matches("[data-alert-rule-add]")) {
      event.preventDefault();
      const protocol = target.getAttribute("data-alert-rule-add");
      if (!protocol) return;
      selectAlertRule(protocol);
      document.getElementById("alert-warn")?.focus();
      return;
    }

    if (target.matches("[data-alert-history-refresh]")) {
      event.preventDefault();
      loadAlertHistory(state.renderSeq);
      return;
    }

    if (target.matches("[data-alert-history-more]")) {
      event.preventDefault();
      settingsState.alertEventsExpanded = !settingsState.alertEventsExpanded;
      const area = document.getElementById("alert-history-area");
      if (area) area.innerHTML = alertHistoryMarkup();
      return;
    }

    if (target.matches("[data-retry]")) {
      retryCurrentPage();
    }
  });

  function trapFocusables(dialog) {
    return [
      ...dialog.querySelectorAll(
        "button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])",
      ),
    ].filter((node) => node.offsetParent !== null);
  }

  function trapDialogTab(event, dialog) {
    const focusable = trapFocusables(dialog);
    if (!focusable.length) {
      event.preventDefault();
      dialog.focus({ preventScroll: true });
      return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    const active = document.activeElement;
    if (active === dialog || !dialog.contains(active)) {
      event.preventDefault();
      (event.shiftKey ? last : first).focus({ preventScroll: true });
      return;
    }
    if (event.shiftKey && active === first) {
      event.preventDefault();
      last.focus({ preventScroll: true });
    } else if (!event.shiftKey && active === last) {
      event.preventDefault();
      first.focus({ preventScroll: true });
    }
  }

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      if (settingsDangerState.kind) {
        event.preventDefault();
        closeSettingsDanger();
        return;
      }
      if (!settingsIsDesktop() && settingsView.open) {
        event.preventDefault();
        settingsCloseSection();
        return;
      }
      if (state.moreSheet) {
        event.preventDefault();
        closeMoreSheet();
        return;
      }
      if (state.pf.sheet) {
        event.preventDefault();
        closePortfolioSheet();
        return;
      }
      if (state.categorySheet) {
        event.preventDefault();
        closeCategorySheet();
        return;
      }
      if (state.notify.open) {
        event.preventDefault();
        closeNotifyPanel();
        return;
      }
      if (state.flowToast) {
        event.preventDefault();
        if (state.flowToast.expanded) collapseFlowToast();
        else closeFlowToast();
        return;
      }
      closeUserMenu();
    }
    if (event.key === "Enter" && state.notify.open) {
      const card = event.target.closest?.("[data-notify-card]");
      if (card && !event.target.closest("button, a")) {
        const button = card.querySelector("[data-notify-confirm]");
        if (button && !button.disabled) {
          event.preventDefault();
          confirmNotifyItem(card.getAttribute("data-notify-card"), button);
          return;
        }
      }
    }
    if (
      event.key === "Tab" &&
      (state.pf.sheet || state.categorySheet || state.moreSheet || state.notify.open)
    ) {
      const root =
        state.pf.sheet?.root || state.categorySheet?.root || state.moreSheet?.root || state.notify.root;
      const dialog = root.querySelector(".more-sheet, .sheet, .notify-panel");
      if (dialog) trapDialogTab(event, dialog);
    }
  });

  ["keydown", "keyup"].forEach((type) => {
    document.addEventListener(type, (event) => {
      if (event.target instanceof HTMLInputElement && event.target.id === "password") {
        syncCapsLockHint(event);
      }
    });
  });

  document.addEventListener("focusout", (event) => {
    if (event.target instanceof HTMLInputElement && event.target.id === "password") {
      const hint = document.getElementById("password-capslock");
      if (hint) hint.hidden = true;
    }
  });

  document.addEventListener("input", (event) => {
    if (event.target instanceof HTMLInputElement && event.target.id === "cs-key-input") {
      syncCsSubmitState();
    }
    if (
      event.target instanceof HTMLInputElement &&
      (event.target.id === "username" || event.target.id === "password")
    ) {
      setLoginFieldError(event.target.id, "");
      setLoginError("");
    }
  });

  document.addEventListener("submit", (event) => {
    if (event.target instanceof HTMLFormElement && event.target.id === "login-form") {
      handleLoginSubmit(event);
    } else if (event.target instanceof HTMLFormElement && event.target.id === "cs-form") {
      event.preventDefault();
      saveCoinstatsKey();
    } else if (event.target instanceof HTMLFormElement && event.target.id === "wl-form") {
      event.preventDefault();
      saveWallet();
    } else if (event.target instanceof HTMLFormElement && event.target.id === "users-form") {
      event.preventDefault();
      createUser();
    } else if (event.target instanceof HTMLFormElement && event.target.id === "users-reset-form") {
      event.preventDefault();
      resetUserPassword(event.target);
    } else if (event.target instanceof HTMLFormElement && event.target.id === "pf-override-form") {
      event.preventDefault();
      savePortfolioOverride(event.target);
    } else if (event.target instanceof HTMLFormElement && event.target.id === "alert-form") {
      event.preventDefault();
      saveAlertRule(event.target);
    }
  });

  window.addEventListener("popstate", () => {
    let path = normalizePath(currentPath());
    if (path !== currentPath()) {
      window.history.replaceState(null, "", path);
    }

    if (!state.user) {
      if (path !== "/login") {
        window.history.replaceState(null, "", "/login");
        renderLogin();
      } else {
        renderLogin();
      }
      return;
    }

    if (path === "/login") {
      window.history.replaceState(null, "", "/");
      path = "/";
    }
    renderRoute(path);
  });

  async function boot() {
    let path = normalizePath(currentPath());
    if (path !== currentPath()) {
      const hash = window.location.hash;
      window.history.replaceState(null, "", `${path}${hash}`);
    }

    if (!state.token) {
      if (path !== "/login") {
        window.history.replaceState(null, "", "/login");
      }
      renderLogin();
      return;
    }

    try {
      state.user = await api("/auth/me");
      if (path === "/login") {
        window.history.replaceState(null, "", "/");
        path = "/";
      }
      renderRoute(path);
      loadPendingCount();
    } catch (error) {
      clearSession();
      if (path !== "/login") {
        window.history.replaceState(null, "", "/login");
      }
      renderLogin(
        error.status === 401 ? "登录已过期，请重新登录" : "无法验证登录状态，请重新登录",
      );
    }
  }

  installNotifyGestures();
  boot();
})();
