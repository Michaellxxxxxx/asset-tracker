/* p26: 收益页分段表 + 资产涨跌明细. Native JS, no dependencies/build.
   Pure markup helpers are exported on window.ReturnsBreakdown for node tests;
   mount() is the only DOM entry point used by app.js. */
(function (root) {
  "use strict";

  var moneyFormatter = new Intl.NumberFormat("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });

  // r1: the asset list only opens with the ten material rows. Everything else
  // folds into one summary row that can be expanded in place.
  var ASSET_DISPLAY_LIMIT = 10;
  var MIN_ASSET_AMOUNT = 0.01;
  var AMOUNT_FLAT_EPSILON = 0.004999999;
  var RATE_FLAT_EPSILON = 0.000049999;
  var WEEKDAYS = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];
  var DAY_MS = 86400000;
  var SHANGHAI_OFFSET_MS = 8 * 3600000;

  function asNumber(value, fallback) {
    var number = Number(value);
    return Number.isFinite(number) ? number : fallback;
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (character) {
      return {
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      }[character];
    });
  }

  function money(value) {
    return moneyFormatter.format(asNumber(value, 0));
  }

  // ±0.00 is not a direction: collapse it to a plain 0.00 so the flat tone
  // (white 48%) is the only signal left.
  function formatAmount(value) {
    if (value === null || value === undefined || value === "") return "—";
    var number = asNumber(value, null);
    if (number === null) return "—";
    var rounded = Math.round(number * 100) / 100;
    if (rounded === 0) return "0.00";
    return (rounded > 0 ? "+" : "-") + money(Math.abs(rounded));
  }

  function formatRate(value) {
    if (value === null || value === undefined || value === "") return "—";
    var number = asNumber(value, null);
    if (number === null) return "—";
    var percent = (number * 100).toFixed(2);
    if (percent === "0.00" || percent === "-0.00") return "0.00%";
    if (number > 0) return "+" + percent + "%";
    return percent + "%";
  }

  function toneClass(value, epsilon) {
    var number = asNumber(value, 0);
    var threshold = epsilon == null ? 1e-7 : epsilon;
    if (number > threshold) return "is-positive";
    if (number < -threshold) return "is-negative";
    return "is-flat";
  }

  function pad2(value) {
    return (value < 10 ? "0" : "") + value;
  }

  function shanghaiParts(value) {
    var stamp = value instanceof Date ? value.getTime() : new Date(value).getTime();
    if (!Number.isFinite(stamp)) return null;
    var shifted = new Date(stamp + SHANGHAI_OFFSET_MS);
    return {
      year: shifted.getUTCFullYear(),
      month: shifted.getUTCMonth() + 1,
      day: shifted.getUTCDate(),
      stamp: Date.UTC(shifted.getUTCFullYear(), shifted.getUTCMonth(), shifted.getUTCDate()),
    };
  }

  function partsFromKey(key) {
    var match = String(key || "").match(/^(\d{4})-(\d{2})-(\d{2})$/);
    if (!match) return null;
    return {
      year: Number(match[1]),
      month: Number(match[2]),
      day: Number(match[3]),
      stamp: Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3])),
    };
  }

  function isoWeekStart(year, week) {
    var jan4 = new Date(Date.UTC(year, 0, 4));
    var jan4Weekday = jan4.getUTCDay() || 7;
    return new Date(jan4.getTime() - (jan4Weekday - 1) * DAY_MS + (week - 1) * 7 * DAY_MS);
  }

  // r1 label format: ISO week "10.20–10.26", day "10.28 周二", month
  // "2026年10月". The backend keeps raw labels; the range is formatted here.
  function segmentLabel(row, unit) {
    var key = row && row.key;
    if (unit === "month") {
      var monthMatch = String(key || "").match(/^(\d{4})-(\d{2})$/);
      if (monthMatch) return monthMatch[1] + "年" + Number(monthMatch[2]) + "月";
      return (row && row.label) || "";
    }
    if (unit === "week") {
      var start = shanghaiParts(row && row.start);
      var end = shanghaiParts(row && row.end);
      if (!start || !end) {
        var weekMatch = String(key || "").match(/^(\d{4})-W(\d{2})$/);
        if (!weekMatch) return (row && row.label) || "";
        var monday = isoWeekStart(Number(weekMatch[1]), Number(weekMatch[2]));
        start = { stamp: Date.UTC(monday.getUTCFullYear(), monday.getUTCMonth(), monday.getUTCDate()) };
        end = { stamp: start.stamp + 7 * DAY_MS };
      }
      var last = new Date(end.stamp - DAY_MS);
      var startDate = new Date(start.stamp);
      return (
        pad2(startDate.getUTCMonth() + 1) + "." + pad2(startDate.getUTCDate()) +
        "–" +
        pad2(last.getUTCMonth() + 1) + "." + pad2(last.getUTCDate())
      );
    }
    var day = shanghaiParts(row && row.start) || partsFromKey(key);
    if (!day) return (row && row.label) || "";
    return pad2(day.month) + "." + pad2(day.day) + " " + WEEKDAYS[new Date(day.stamp).getUTCDay()];
  }

  function weekNumberLabel(row) {
    var match = String((row && row.key) || "").match(/^(\d{4})-W(\d{2})$/);
    return match ? "第" + Number(match[2]) + "周" : "";
  }

  function sortAssets(assets) {
    return (Array.isArray(assets) ? assets.slice() : []).sort(function (left, right) {
      var width = Math.abs(asNumber(right && right.amount_usdt, 0)) -
        Math.abs(asNumber(left && left.amount_usdt, 0));
      if (width !== 0) return width;
      return String((left && left.asset) || "").localeCompare(String((right && right.asset) || ""));
    });
  }

  // Default: |amount| >= 0.01, first N rows. The remainder keeps its order and
  // its exact sum so the collapsed line matches the visible rows after opening.
  function splitAssets(assets, limit) {
    var cap = limit == null ? ASSET_DISPLAY_LIMIT : limit;
    var sorted = sortAssets(assets);
    var visible = [];
    var rest = [];
    var total = 0;
    sorted.forEach(function (row) {
      var amount = Math.abs(asNumber(row && row.amount_usdt, 0));
      if (visible.length < cap && amount >= MIN_ASSET_AMOUNT) {
        visible.push(row);
      } else {
        rest.push(row);
        total += asNumber(row && row.amount_usdt, 0);
      }
    });
    return { visible: visible, rest: rest, total: total, count: rest.length };
  }

  function moreSummaryText(count, total) {
    return "其余 " + count + " 项 · 合计 " + formatAmount(total);
  }

  function segmentsFor(data, period, view) {
    var segments = (data && data.segments) || {};
    if (period === "year") return Array.isArray(segments.month) ? segments.month : [];
    if (period === "week") return Array.isArray(segments.day) ? segments.day : [];
    if (period === "month") {
      return view.unit === "day"
        ? (Array.isArray(segments.day) ? segments.day : [])
        : (Array.isArray(segments.week) ? segments.week : []);
    }
    return [];
  }

  function modeToggleMarkup(view) {
    return (
      '<div class="rb-mode segmented" role="group" aria-label="选择显示口径">' +
      '<button class="segment" type="button" data-rb-mode="amount" aria-pressed="' +
      (view.mode === "amount") +
      '">额</button>' +
      '<button class="segment" type="button" data-rb-mode="rate" aria-pressed="' +
      (view.mode === "rate") +
      '">率</button>' +
      "</div>"
    );
  }

  function unitToggleMarkup(view) {
    return (
      '<div class="rb-units segmented" role="group" aria-label="选择分段粒度">' +
      '<button class="segment" type="button" data-rb-unit="week" aria-pressed="' +
      (view.unit === "week") +
      '">按周</button>' +
      '<button class="segment" type="button" data-rb-unit="day" aria-pressed="' +
      (view.unit === "day") +
      '">按日</button>' +
      "</div>"
    );
  }

  function valueCell(value, mode, missingText) {
    if (value === null || value === undefined) {
      return '<span class="rb-missing-text">' + escapeHtml(missingText || "缺失") + "</span>";
    }
    var epsilon = mode === "rate" ? RATE_FLAT_EPSILON : AMOUNT_FLAT_EPSILON;
    return (
      '<span class="' + toneClass(value, epsilon) + '">' +
      (mode === "rate" ? formatRate(value) : formatAmount(value)) +
      "</span>"
    );
  }

  function segmentsMarkup(data, period, view) {
    if (period === "day") return "";
    var rows = segmentsFor(data, period, view);
    if (!rows.length) return "";
    var unit = period === "year" ? "month" : period === "week" ? "day" : view.unit;
    var title = period === "year"
      ? "按月"
      : period === "week"
        ? "按日"
        : view.unit === "week"
          ? "按周"
          : "按日";
    var unitToggle = period === "month" ? unitToggleMarkup(view) : "";
    var body = rows
      .map(function (row) {
        var label = escapeHtml(segmentLabel(row, unit));
        var key = escapeHtml((row && row.key) || "");
        var weekNo = unit === "week" ? weekNumberLabel(row) : "";
        var caption = weekNo
          ? '<span class="rb-week-no">' + escapeHtml(weekNo) + "</span>"
          : "";
        if (row && row.missing) {
          return (
            '<tr class="rb-row is-missing" data-rb-key="' + key + '">' +
            '<th scope="row"><span class="rb-range">' + label + "</span>" + caption + "</th>" +
            '<td><span class="rb-missing-text">无快照</span></td>' +
            "</tr>"
          );
        }
        var value = view.mode === "rate" ? row.rate : row.amount_usdt;
        return (
          '<tr class="rb-row" data-rb-key="' + key + '">' +
          '<th scope="row"><span class="rb-range">' + label + "</span>" + caption + "</th>" +
          "<td>" + valueCell(value, view.mode) + "</td>" +
          "</tr>"
        );
      })
      .join("");

    return (
      '<section class="rb-block rb-segments" aria-labelledby="rb-segments-title">' +
      '<div class="rb-block-head">' +
      '<div class="rb-block-copy">' +
      '<p class="rb-kicker">' + title + " · 时间加权</p>" +
      '<h2 class="rb-title" id="rb-segments-title">分段收益</h2>' +
      "</div>" +
      unitToggle +
      "</div>" +
      '<p class="rb-note">额按区间起点净值折算</p>' +
      '<div class="rb-table-wrap">' +
      '<table class="rb-table rb-table--segments">' +
      '<thead><tr><th scope="col">区间</th><th scope="col">' +
      (view.mode === "rate" ? "收益率" : "收益额 USDT") +
      "</th></tr></thead><tbody>" + body + "</tbody></table>" +
      "</div></section>"
    );
  }

  function assetRowMarkup(row, mode) {
    var asset = escapeHtml((row && row.asset) || "—");
    var label = escapeHtml((row && row.asset_class_label) || "未分类");
    var value = mode === "rate" ? row && row.rate : row && row.amount_usdt;
    var missing = mode === "rate" ? "缺失" : "—";
    return (
      '<tr class="rb-row rb-asset-row">' +
      '<th scope="row"><span class="rb-asset-cell">' +
      '<span class="rb-asset" title="' + asset + '">' + asset + "</span>" +
      '<span class="rb-tag">' + label + "</span></span></th>" +
      "<td>" + valueCell(value, mode, missing) + "</td>" +
      "</tr>"
    );
  }

  function assetRowsMarkup(assets, mode) {
    return (Array.isArray(assets) ? assets : [])
      .map(function (row) {
        return assetRowMarkup(row, mode);
      })
      .join("");
  }

  function assetsMarkup(data, view) {
    var missing = Boolean(data && data.assets_missing);
    var assets = Array.isArray(data && data.assets) ? data.assets : [];
    var split = splitAssets(assets);
    var body;
    if (missing) {
      body =
        '<tr class="rb-row is-missing"><th scope="row">资产明细</th>' +
        '<td><span class="rb-missing-text">缺失</span></td></tr>';
    } else {
      body = assetRowsMarkup(split.visible, view.mode);
      if (!body) {
        body =
          '<tr class="rb-row is-missing"><th scope="row">资产明细</th>' +
          '<td><span class="rb-missing-text">暂无</span></td></tr>';
      }
    }

    var collapsed = view.assetsExpanded ? "" : " is-collapsed";
    var moreMarkup = "";
    if (!missing && split.count > 0) {
      moreMarkup =
        '<tr class="rb-more-row"><td colspan="2">' +
        '<button class="rb-more-toggle" type="button" data-rb-expand aria-expanded="' +
        (view.assetsExpanded ? "true" : "false") +
        '" aria-controls="rb-more-panel">' +
        '<span class="rb-more-text">其余 ' + split.count + " 项 · 合计 " +
        valueCell(split.total, "amount") + "</span>" +
        '<span class="rb-more-chevron" aria-hidden="true">⌄</span>' +
        "</button>" +
        '<div class="rb-more-panel' + (view.assetsExpanded ? " is-expanded" : "") +
        '" id="rb-more-panel" data-rb-more aria-hidden="' +
        (view.assetsExpanded ? "false" : "true") +
        '"><table class="rb-table rb-table--assets rb-table--more"><tbody>' +
        assetRowsMarkup(split.rest, view.mode) +
        "</tbody></table></div></td></tr>";
    }

    var unattributed = data && data.unattributed_usdt;
    var unattrCell;
    if (missing || unattributed === null || unattributed === undefined) {
      unattrCell = '<span class="rb-missing-text">缺失</span>';
    } else if (view.mode === "rate") {
      unattrCell = '<span class="rb-missing-text">—</span>';
    } else {
      unattrCell = valueCell(unattributed, "amount");
    }
    var reason = escapeHtml((data && data.unattributed_reason) || "差额来源见口径说明。");
    var missingReason = escapeHtml((data && data.assets_missing_reason) || "窗口内没有可用的持仓快照。");

    return (
      '<section class="rb-block rb-assets' + collapsed + '" aria-labelledby="rb-assets-title">' +
      '<div class="rb-block-head">' +
      '<div class="rb-block-copy">' +
      '<h2 class="rb-title" id="rb-assets-title">各资产涨跌</h2>' +
      "</div></div>" +
      '<div class="rb-table-wrap">' +
      '<table class="rb-table rb-table--assets">' +
      '<thead><tr><th scope="col">资产</th><th scope="col" title="按 |收益额| 降序">' +
      (view.mode === "rate" ? "收益率" : "收益额 USDT") +
      '<span class="rb-sort-arrow" aria-label="按收益额绝对值降序">↓</span>' +
      "</th></tr></thead><tbody>" +
      body + moreMarkup +
      '<tr class="rb-row rb-unattributed" title="' + reason + '">' +
      '<th scope="row"><span class="rb-asset-cell">' +
      '<span class="rb-asset">未归因</span>' +
      '<span class="rb-tag rb-tag--muted">差额</span></span></th>' +
      "<td>" + unattrCell + "</td></tr>" +
      "</tbody></table>" +
      "</div>" +
      (missing
        ? '<p class="rb-explain">' + missingReason + "</p>"
        : '<p class="rb-explain">未归因 = 区间总收益 − Σ资产价格效应。' + reason + "</p>") +
      "</section>"
    );
  }

  function widgetMarkup(data, period, view) {
    var state = view || { mode: "amount", unit: period === "month" ? "week" : "day" };
    if (period === "month" && state.unit !== "week" && state.unit !== "day") {
      state = { mode: state.mode, unit: "week", assetsExpanded: state.assetsExpanded };
    }
    var segments = segmentsMarkup(data, period, state);
    return (
      '<div class="rb-shell" data-rb-period="' + escapeHtml(period) + '">' +
      '<header class="rb-top">' +
      '<div class="rb-top-copy"><h2 class="rb-headline">收益拆解</h2></div>' +
      modeToggleMarkup(state) +
      "</header>" +
      segments +
      assetsMarkup(data, state) +
      "</div>"
    );
  }

  function render(rootNode) {
    var bundle = rootNode && rootNode.__rb;
    if (!bundle) return;
    rootNode.innerHTML = widgetMarkup(bundle.data, bundle.period, bundle.state);
  }

  function toggleAssets(rootNode, expanded) {
    var bundle = rootNode && rootNode.__rb;
    if (!bundle) return;
    bundle.state.assetsExpanded = expanded;
    var panel = rootNode.querySelector("[data-rb-more]");
    var button = rootNode.querySelector("[data-rb-expand]");
    var row = rootNode.querySelector(".rb-assets");
    if (panel) {
      panel.classList.toggle("is-expanded", expanded);
      panel.setAttribute("aria-hidden", expanded ? "false" : "true");
    }
    if (button) button.setAttribute("aria-expanded", expanded ? "true" : "false");
    if (row) row.classList.toggle("is-collapsed", !expanded);
  }

  function mount(rootNode, data, period) {
    if (!rootNode) return;
    if (!data) {
      rootNode.innerHTML = "";
      return;
    }
    rootNode.__rb = {
      data: data,
      period: period || (data && data.period) || "month",
      state: {
        mode: "amount",
        unit: period === "month" ? "week" : "day",
        assetsExpanded: false,
      },
    };
    if (!rootNode.__rbBound) {
      rootNode.__rbBound = true;
      rootNode.addEventListener("click", function (event) {
        var target = event.target && event.target.closest
          ? event.target.closest("[data-rb-mode], [data-rb-unit], [data-rb-expand]")
          : null;
        if (!target || !rootNode.contains(target)) return;
        var bundle = rootNode.__rb;
        if (!bundle) return;
        var mode = target.getAttribute("data-rb-mode");
        var unit = target.getAttribute("data-rb-unit");
        if (target.hasAttribute("data-rb-expand")) {
          event.preventDefault();
          toggleAssets(rootNode, target.getAttribute("aria-expanded") !== "true");
        } else if (mode === "amount" || mode === "rate") {
          bundle.state.mode = mode;
          event.preventDefault();
          render(rootNode);
        } else if (unit === "week" || unit === "day") {
          bundle.state.unit = unit;
          event.preventDefault();
          render(rootNode);
        }
      });
    }
    render(rootNode);
  }

  root.ReturnsBreakdown = {
    ASSET_DISPLAY_LIMIT: ASSET_DISPLAY_LIMIT,
    MIN_ASSET_AMOUNT: MIN_ASSET_AMOUNT,
    escapeHtml: escapeHtml,
    formatAmount: formatAmount,
    formatRate: formatRate,
    toneClass: toneClass,
    segmentLabel: segmentLabel,
    weekNumberLabel: weekNumberLabel,
    sortAssets: sortAssets,
    splitAssets: splitAssets,
    moreSummaryText: moreSummaryText,
    segmentsMarkup: segmentsMarkup,
    assetsMarkup: assetsMarkup,
    widgetMarkup: widgetMarkup,
    mount: mount,
  };
})(typeof window !== "undefined" ? window : globalThis);
