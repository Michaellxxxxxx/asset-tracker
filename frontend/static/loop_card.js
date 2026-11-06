/* p25 — looping-loan position card + protocol drawer.
 *
 * Native JS, no dependencies, no build step.  app.js only flips the page mount
 * and exposes data-position-id; everything below owns its own events.
 */
(function () {
  "use strict";

  const API_BASE = "/api/lending/positions";
  const ICON_BASE = "/static/icons/protocols";
  const DRAWER_DRAG_CLOSE_PX = 96;
  const DRAWER_DRAG_RATIO = 0.28;

  const PROTOCOLS = [
    {
      key: "aave",
      name: "Aave",
      chain: "Ethereum · 多链",
      note: "多链货币市场，存款生息、超额抵押借款。",
      icon: `${ICON_BASE}/aave.png`,
    },
    {
      key: "morpho",
      name: "Morpho",
      chain: "Ethereum",
      note: "点对点借贷市场，金库与隔离市场并存。",
      icon: `${ICON_BASE}/morpho.png`,
    },
    {
      key: "pendle",
      name: "Pendle",
      chain: "Ethereum",
      note: "本金与收益分离，PT 可作抵押循环。",
      icon: `${ICON_BASE}/pendle.png`,
    },
    {
      key: "kamino",
      name: "Kamino",
      chain: "Solana",
      note: "Solana 上的自动化流动性借贷与杠杆策略。",
      icon: `${ICON_BASE}/kamino.png`,
    },
    {
      key: "jupiter",
      name: "Jupiter",
      chain: "Solana",
      note: "Solana 聚合器与 Jupiter Lend 借贷市场。",
      icon: `${ICON_BASE}/jupiter.png`,
    },
  ];

  const PROTOCOL_BY_KEY = Object.fromEntries(PROTOCOLS.map((item) => [item.key, item]));
  const PROTOCOL_ALIASES = {
    aave_v3: "aave",
    aavev3: "aave",
    morpho_blue: "morpho",
    jupiter_lend: "jupiter",
  };

  function protocolKey(value) {
    const key = String(value || "").trim().toLowerCase();
    return PROTOCOL_ALIASES[key] || key;
  }

  function protocolIcon(protocol) {
    const item = PROTOCOL_BY_KEY[protocolKey(protocol)];
    return item ? item.icon : "";
  }

  function esc(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function num(value) {
    if (value == null || value === "") return null;
    const n = Number(value);
    return Number.isFinite(n) ? n : null;
  }

  function fmtUsd(value) {
    const n = num(value);
    if (n == null) return "—";
    return `$${n.toLocaleString("en-US", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    })}`;
  }

  function fmtSignedUsd(value) {
    const n = num(value);
    if (n == null) return "—";
    const sign = n > 0 ? "+" : n < 0 ? "−" : "";
    return `${sign}$${Math.abs(n).toLocaleString("en-US", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    })}`;
  }

  function fmtPct(value, digits = 2, signed = false) {
    const n = num(value);
    if (n == null) return "—";
    const pct = n * 100;
    const sign = signed && pct > 0 ? "+" : pct < 0 ? "−" : "";
    return `${sign}${Math.abs(pct).toFixed(digits)}%`;
  }

  function fmtSignedPct(value, digits = 2) {
    return fmtPct(value, digits, true);
  }

  function fmtPrice(value, digits = 4) {
    const n = num(value);
    if (n == null) return "—";
    const abs = Math.abs(n);
    const p =
      Number.isFinite(digits) && digits !== 4
        ? digits
        : abs > 0 && abs < 0.01
          ? 6
          : 4;
    return n.toLocaleString("en-US", {
      minimumFractionDigits: p,
      maximumFractionDigits: p,
    });
  }

  function fmtAmount(value) {
    const n = num(value);
    if (n == null) return "—";
    const abs = Math.abs(n);
    const digits = abs >= 1000 ? 2 : abs >= 1 ? 4 : 6;
    return n.toLocaleString("en-US", {
      minimumFractionDigits: 0,
      maximumFractionDigits: digits,
    });
  }

  function fmtLeverage(value) {
    const n = num(value);
    return n == null ? "—" : `${n.toFixed(2)}×`;
  }

  function fmtDistance(value) {
    const n = num(value);
    return n == null ? "—" : `${Math.max(0, n * 100).toFixed(1)}%`;
  }

  function fmtHealth(value) {
    const n = num(value);
    return n == null ? "—" : n.toFixed(3);
  }

  function healthTone(value) {
    const n = num(value);
    if (n == null) return "";
    if (n < 1.2) return "danger";
    if (n < 1.5) return "warn";
    return "safe";
  }

  function healthClass(value) {
    const tone = healthTone(value);
    return tone ? `p25-hf--${tone}` : "";
  }

  function liqBasisLabel(value) {
    const key = String(value || "").trim().toLowerCase();
    if (!key) return "—";
    if (key === "market") return "市场价";
    if (key === "oracle") return "预言机";
    if (key === "twap") return "TWAP";
    return String(value);
  }

  function fmtBeijing(value) {
    if (value == null || value === "") return "—";
    const date = new Date(value);
    if (!Number.isFinite(date.getTime())) return String(value);
    const shifted = new Date(date.getTime() + 8 * 3600 * 1000);
    const pad = (part) => String(part).padStart(2, "0");
    return `${shifted.getUTCFullYear()}-${pad(shifted.getUTCMonth() + 1)}-${pad(
      shifted.getUTCDate(),
    )} ${pad(shifted.getUTCHours())}:${pad(shifted.getUTCMinutes())}`;
  }

  function reason(missing, key, fallback) {
    const map = missing && typeof missing === "object" ? missing : {};
    return userText(map[key] || fallback || "暂不可用") || "暂不可用";
  }

  // User-visible copy must never leak backend field names such as apy_supply.
  function userText(text) {
    return String(text == null ? "" : text)
      .replace(/\s*[（(][^）)]*[A-Za-z_][A-Za-z0-9_]*[^）)]*[）)]/g, "")
      .replace(/\b[a-z]+_[a-z0-9_]+\b/g, "")
      .trim();
  }

  function toneClass(n) {
    if (n == null) return "p25-missing-text";
    if (n === 0) return "p25-zero";
    return n < 0 ? "p25-negative" : "p25-positive";
  }

  function isAccumulating(text) {
    return /数据积累中/.test(String(text || ""));
  }

  function valueBlock(value, display, missing, key, fallback) {
    if (value == null) {
      const why = reason(missing, key, fallback);
      return isAccumulating(why)
        ? `<span class="p25-missing p25-state-text" title="${esc(userText(why))}">数据积累中</span>`
        : `<span class="p25-missing p25-dash-missing" title="${esc(userText(why))}">—</span>`;
    }
    return `<span class="p25-num">${display}</span>`;
  }

  function sparkline(points, label) {
    const rows = Array.isArray(points)
      ? points
          .map((point) => ({ date: String(point.date || ""), value: num(point.value) }))
          .filter((point) => point.value != null)
      : [];
    if (!rows.length) {
      return `<div class="p25-spark p25-spark--empty" role="img" aria-label="暂无走势">数据积累中</div>`;
    }
    const values = rows.map((point) => point.value);
    const min = Math.min(...values);
    const max = Math.max(...values);
    const span = max - min || 0.0001;
    const width = 168;
    const height = 42;
    const pad = 3;
    const step = rows.length > 1 ? (width - pad * 2) / (rows.length - 1) : 0;
    const coords = rows.map((point, index) => {
      const x = pad + index * step;
      const y = pad + (1 - (point.value - min) / span) * (height - pad * 2);
      return [x, y];
    });
    const line = coords.map(([x, y]) => `${x.toFixed(2)},${y.toFixed(2)}`).join(" ");
    const area = `${pad},${height - pad} ${line} ${(pad + (rows.length - 1) * step).toFixed(2)},${height - pad}`;
    const last = coords[coords.length - 1];
    return `<svg class="p25-spark" viewBox="0 0 ${width} ${height}" role="img" aria-label="${esc(label)}最低 ${fmtPct(min)}，最高 ${fmtPct(max)}">
      <polygon class="p25-spark-area" points="${area}"></polygon>
      <polyline class="p25-spark-line" points="${line}"></polyline>
      <circle class="p25-spark-dot" cx="${last[0].toFixed(2)}" cy="${last[1].toFixed(2)}" r="2.4"></circle>
    </svg>`;
  }

  function windowBlock(windowData, days) {
    const data = windowData && typeof windowData === "object" ? windowData : {};
    const points = Array.isArray(data.points) ? data.points : [];
    const title = `${days}天`;
    // Two points cannot describe a trend: show a single quiet line until the
    // third snapshot arrives, and hide min/max/avg with the chart.
    if (points.length < 3) {
      const coverage = num(data.coverage_days);
      return `<section class="p25-window p25-window--empty">
        <h5>${title}</h5>
        <p>数据积累中 · 已覆盖 ${coverage == null ? "—" : coverage.toFixed(1)} 天，满 3 个快照后显示</p>
      </section>`;
    }
    if (data.accumulating) {
      const coverage = num(data.coverage_days);
      return `<section class="p25-window">
        <div class="p25-window-head"><h5>${title}</h5><span class="p25-accum">数据积累中${
          coverage == null ? "" : ` · 已覆盖 ${coverage.toFixed(1)} 天`
        }</span></div>
        ${sparkline(points, `${title}净年化`)}
        <p class="p25-window-range">最低 ${fmtPct(data.min)} · 最高 ${fmtPct(data.max)} · 均值 ${fmtPct(data.avg)}</p>
      </section>`;
    }
    return `<section class="p25-window">
      <div class="p25-window-head"><h5>${title}</h5><span class="p25-window-meta">${num(data.samples) || points.length} 个采样</span></div>
      ${sparkline(points, `${title}净年化`)}
      <p class="p25-window-range">最低 ${fmtPct(data.min)} · 最高 ${fmtPct(data.max)} · 均值 ${fmtPct(data.avg)}</p>
    </section>`;
  }

  function legsMarkup(side, missing, kind) {
    const block = side && typeof side === "object" ? side : {};
    const legs = Array.isArray(block.legs) ? block.legs : [];
    const borrowSide = kind === "borrow";
    const apyLabel = borrowSide ? "借款利率" : "抵押收益率";
    if (!legs.length) {
      return `<div class="p25-leg p25-leg--empty"><span>${borrowSide ? "无借款" : "暂无抵押明细"}</span></div>`;
    }
    const header = `<div class="p25-leg p25-leg--head" aria-hidden="true">
      <span class="p25-leg-symbol">资产</span>
      <span class="p25-leg-amount">数量</span>
      <span class="p25-leg-usd">价值</span>
      <span class="p25-leg-apy">年化</span>
    </div>`;
    return header + legs
      .map((leg) => {
        const rate = num(leg.apy);
        return `<div class="p25-leg">
          <span class="p25-leg-symbol" title="${esc(leg.symbol)}">${esc(leg.symbol || "—")}</span>
          <span class="p25-leg-amount">${fmtAmount(leg.amount)}</span>
          <span class="p25-leg-usd">${fmtUsd(leg.usd)}</span>
          <span class="p25-leg-apy ${rate == null ? "p25-missing-text" : rate === 0 ? "p25-zero" : ""}">${
            rate == null ? "—" : `${apyLabel} ${fmtPct(rate)}`
          }</span>
        </div>`;
      })
      .join("");
  }

  function breakdownRow(label, value, missing, key, missingLabel, negative = false, dash = false) {
    const raw = num(value);
    if (dash) {
      return `<div class="p25-break-row"><dt>${label}</dt><dd><span class="p25-dash" title="${esc(
        reason(missing, key, "历史不足，暂无法拆分"),
      )}">—</span></dd></div>`;
    }
    if (raw == null) {
      const why = reason(missing, key, "缺少历史分项利率");
      return `<div class="p25-break-row"><dt>${label}</dt><dd><span class="p25-missing p25-dash-missing" title="${esc(
        userText(why),
      )}">${missingLabel || (isAccumulating(why) ? "数据积累中" : "—")}</span></dd></div>`;
    }
    const n = negative ? -Math.abs(raw) : raw;
    return `<div class="p25-break-row"><dt>${label}</dt><dd class="${n < 0 ? "p25-negative" : n > 0 ? "p25-positive" : "p25-zero"}">${fmtSignedUsd(
      n,
    )}</dd></div>`;
  }

  function renderCard(payload, meta) {
    const data = payload && typeof payload === "object" ? payload : {};
    const info = meta && typeof meta === "object" ? meta : {};
    const missing = data.missing && typeof data.missing === "object" ? data.missing : {};
    const id = String(data.position_id == null ? info.id : data.position_id);
    const label = info.label || data.label || "循环贷仓位";
    const protocol = protocolKey(info.protocol || data.protocol);
    const protocolInfo = PROTOCOL_BY_KEY[protocol];
    const chain = info.chain || data.chain || "";
    const hf = num(data.health_factor);
    const hfWarn = hf != null && hf < 1.5;
    const netApy = num(data.net_apy);
    const netClass = toneClass(netApy);
    const spread = num(data.spread);
    const expanded = !!info.expanded;
    const moreId = `p25-loop-more-${id.replace(/[^a-zA-Z0-9_-]/g, "-")}`;
    const pnl = data.pnl && typeof data.pnl === "object" ? data.pnl : {};
    const breakdown = data.pnl_breakdown && typeof data.pnl_breakdown === "object" ? data.pnl_breakdown : {};
    const windows = data.apy_windows && typeof data.apy_windows === "object" ? data.apy_windows : {};
    const collateral = data.collateral && typeof data.collateral === "object" ? data.collateral : {};
    const borrow = data.borrow && typeof data.borrow === "object" ? data.borrow : {};

    const pnlAmount = num(pnl.amount);
    const pnlRate = num(pnl.rate);
    const pnlMissing = pnlAmount == null;
    const interestNet = num(breakdown.interest_net);
    const splitReady =
      num(breakdown.price) != null &&
      num(breakdown.collateral_interest) != null &&
      num(breakdown.borrow_interest) != null;
    const depegApplicable = data.depeg_applicable !== false;
    const depeg = num(data.depeg_liq_price);
    const borrowHigh = data.borrow_apy_high && typeof data.borrow_apy_high === "object"
      ? data.borrow_apy_high
      : null;
    const borrowHighValue = borrowHigh ? num(borrowHigh.value) : null;

    const icon = protocolInfo
      ? `<img class="p25-card-icon" src="${esc(protocolInfo.icon)}" alt="" width="34" height="34" loading="lazy" />`
      : `<span class="p25-card-icon p25-card-icon--empty" aria-hidden="true"></span>`;

    return `<article class="p25-loop-card" data-loop-card data-position-id="${esc(id)}" data-protocol="${esc(
      protocol,
    )}">
      <button class="p25-loop-toggle" type="button" data-loop-toggle aria-expanded="${expanded}" aria-controls="${moreId}">
        <span class="p25-card-head">
          ${icon}
          <span class="p25-card-id">
            <span class="p25-card-title" title="${esc(label)}">${esc(label)}</span>
            <span class="p25-card-meta">${esc(
              (protocolInfo ? protocolInfo.name : info.protocol || protocol || "协议") +
                (chain ? ` · ${chain}` : ""),
            )} · 循环贷</span>
          </span>
          <span class="p25-fold-chev" aria-hidden="true">
            <svg viewBox="0 0 16 16" fill="none"><path d="m3.5 6 4.5 4.5L12.5 6"/></svg>
          </span>
        </span>
        <span class="p25-top-grid">
          <span class="p25-metric p25-metric--principal">
            <span class="p25-metric-k">本金</span>
            <span class="p25-metric-v">${valueBlock(data.principal_usd, fmtUsd(data.principal_usd), missing, "principal", "缺少抵押或借款价值")}</span>
          </span>
          <span class="p25-metric p25-metric--apy">
            <span class="p25-metric-k">当前净年化</span>
            <span class="p25-metric-v ${netClass}">${valueBlock(netApy, fmtSignedPct(netApy), missing, "net_apy", "缺少收益率数据")}</span>
            <span class="p25-source${num(data.collateral_yield_contrib) == null || num(data.borrow_cost_contrib) == null ? " p25-source--missing" : ""}">${
              num(data.collateral_yield_contrib) == null
                ? "暂无抵押利率"
                : num(data.borrow_cost_contrib) == null
                  ? "暂无借款利率"
                  : `抵押 <b class="${toneClass(num(data.collateral_yield_contrib))}">${fmtSignedPct(data.collateral_yield_contrib)}</b> / 借款 <b class="${
                      num(data.borrow_cost_contrib) === 0 ? "p25-zero" : "p25-negative"
                    }">${fmtSignedPct(data.borrow_cost_contrib)}</b>`
            }</span>
          </span>
          <span class="p25-metric">
            <span class="p25-metric-k">健康度</span>
            <span class="p25-metric-v ${healthClass(hf)}">${valueBlock(hf, fmtHealth(hf), missing, "health_factor", "协议未提供健康度")}</span>
          </span>
          <span class="p25-metric">
            <span class="p25-metric-k">清算价</span>
            <span class="p25-metric-v">${valueBlock(data.liq_price, fmtPrice(data.liq_price), missing, "liq_price", "协议未提供清算价")}</span>
          </span>
          <span class="p25-metric">
            <span class="p25-metric-k">距清算</span>
            <span class="p25-metric-v">${valueBlock(data.distance_to_liq_pct, fmtDistance(data.distance_to_liq_pct), missing, "distance_to_liq_pct", "缺少清算价")}</span>
          </span>
        </span>
      </button>

      <div class="p25-loop-more${expanded ? " is-open" : ""}" id="${moreId}"${expanded ? "" : " hidden"}>
        <section class="p25-section" aria-labelledby="${moreId}-yield">
          <div class="p25-section-head">
            <h4 id="${moreId}-yield">收益</h4>
            <span class="p25-section-note">起点 ${esc(fmtBeijing(pnl.start))} · ${num(pnl.snapshots) || 0} 个快照</span>
          </div>
          ${
            pnlMissing
              ? `<p class="p25-empty">累计收益暂不可用：${esc(userText(reason(missing, "pnl", "快照不足或充提无法判别")))}</p>`
              : `<div class="p25-pnl-line">
                  <strong class="${toneClass(pnlAmount)}">${fmtSignedUsd(pnlAmount)}</strong>
                  <span class="${toneClass(pnlRate)}">${fmtSignedPct(pnlRate)}</span>
                  <span class="p25-pnl-caption">已扣除 ${fmtUsd(Math.abs(num(pnl.net_flows) || 0))} 充提</span>
                </div>`
          }
          <div class="p25-breakdown-head">
            <h5>收益拆解</h5>
            ${splitReady ? "" : `<span>历史不足，暂无法拆分</span>`}
          </div>
          <dl class="p25-breakdown">
            ${breakdownRow("价格涨跌（含价格与其它）", breakdown.price, missing, "price_pnl", null, false, !splitReady)}
            ${breakdownRow("抵押利息收入", breakdown.collateral_interest, missing, "collateral_interest", null, false, !splitReady)}
            ${breakdownRow("借款利息支出", breakdown.borrow_interest, missing, "borrow_interest", null, true, !splitReady)}
          </dl>
          ${
            splitReady
              ? `<p class="p25-equation">价格涨跌 ${fmtSignedUsd(breakdown.price)} + 收入 ${fmtSignedUsd(
                  breakdown.collateral_interest,
                )} − 支出 ${fmtUsd(breakdown.borrow_interest)} = 累计收益 ${fmtSignedUsd(pnlAmount)}${
                  interestNet == null ? "" : ` · 净利息 ${fmtSignedUsd(interestNet)}`
                }</p>`
              : ""
          }
          <div class="p25-windows">
            ${windowBlock(windows["7d"], 7)}
            ${windowBlock(windows["30d"], 30)}
          </div>
        </section>

        <section class="p25-section" aria-labelledby="${moreId}-structure">
          <div class="p25-section-head"><h4 id="${moreId}-structure">结构</h4></div>
          <div class="p25-structure-top">
            <div><span>杠杆</span><strong>${fmtLeverage(data.leverage)}</strong></div>
            <div><span>利差</span><strong class="${toneClass(spread)}">${
              spread == null ? "—" : `${spread >= 0 ? "+" : "−"}${Math.abs(spread * 100).toFixed(2)}pp`
            }</strong>${
              data.spread_negative
                ? `<em class="p25-flag">负利差</em>`
                : ""
            }</div>
            <div><span>抵押总值</span><strong>${fmtUsd(collateral.usd)}</strong></div>
            <div><span>借款总值</span><strong>${fmtUsd(borrow.usd)}</strong></div>
          </div>
          <div class="p25-side-block">
            <h5>抵押 <span>${fmtUsd(collateral.usd)} · ${fmtPct(data.weighted_apy_supply)}</span></h5>
            ${legsMarkup(collateral, missing, "collateral")}
          </div>
          <div class="p25-side-block">
            <h5>借款 <span>${fmtUsd(borrow.usd)} · ${fmtPct(data.weighted_apy_borrow)}</span></h5>
            ${legsMarkup(borrow, missing, "borrow")}
          </div>
        </section>

        <section class="p25-section" aria-labelledby="${moreId}-risk">
          <div class="p25-section-head"><h4 id="${moreId}-risk">风险</h4></div>
          ${
            hfWarn
              ? `<p class="pos-liq-tag p25-alert"><span class="pos-liq-ico" aria-hidden="true"><svg class="p25-alert-svg" width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M8 2.2 14.6 13.6H1.4L8 2.2Z" fill="none"/><path d="M8 6.2v3.4M8 11.7v.05" fill="none"/></svg></span>健康度 ${fmtHealth(
                  hf,
                )} < 1.5，接近清算区</p>`
              : ""
          }
          <dl class="p25-risk-grid">
            <div><dt>稳定币脱锚清算价</dt><dd>${
              !depegApplicable
                ? `<span class="p25-na">不适用 · 非稳定币循环</span>`
                : valueBlock(depeg, `${fmtPrice(depeg)} <small>${esc(data.depeg_pair || "")}</small>`, missing, "depeg_liq_price", "缺少清算价")
            }</dd></div>
            <div><dt>借款利率近期高点</dt><dd>${
              borrowHighValue == null
                ? `<span class="p25-dash" title="${esc(reason(missing, "borrow_apy_high", "历史快照未记录借款利率"))}">—</span>`
                : `${fmtPct(borrowHighValue)} <small>${esc(fmtBeijing(borrowHigh.at))}</small>`
            }</dd></div>
            <div><dt>清算价基准</dt><dd>${esc(liqBasisLabel(data.liq_price_basis))}${
              data.liq_price_estimated ? ` <span class="p25-est">估算</span>` : ""
            }</dd></div>
            <div><dt>抵押现价</dt><dd>${valueBlock(
              data.current_price,
              fmtPrice(data.current_price),
              missing,
              "current_price",
              "缺少抵押数量，无法计算现价",
            )}</dd></div>
          </dl>
        </section>
      </div>
    </article>`;
  }

  function renderSkeleton(meta) {
    const label = meta.label || "循环贷仓位";
    return `<article class="p25-loop-card p25-loop-card--loading" data-loop-card data-position-id="${esc(meta.id)}">
      <div class="p25-card-head">
        <span class="p25-card-icon p25-card-icon--empty" aria-hidden="true"></span>
        <span class="p25-card-id"><span class="p25-card-title">${esc(label)}</span><span class="p25-card-meta">读取循环贷指标…</span></span>
      </div>
      <div class="p25-loading-bar"></div>
    </article>`;
  }

  function readMeta(card) {
    return {
      id: card.getAttribute("data-position-id"),
      protocol: card.getAttribute("data-protocol") || "",
      chain: card.getAttribute("data-chain") || "",
      label: card.getAttribute("data-label") || "",
    };
  }

  const expandedIds = new Set();
  const inflight = new Set();

  async function enhanceCard(card) {
    const meta = readMeta(card);
    if (!meta.id || card.classList.contains("p25-loop-card")) return;
    expandedIds.forEach((id) => {
      if (id === meta.id) meta.expanded = true;
    });
    card.outerHTML = renderSkeleton(meta);
    if (inflight.has(meta.id)) return;
    inflight.add(meta.id);
    try {
      const shared = typeof window !== "undefined" && window.AssetTrackerApi;
      const path = `/lending/positions/${encodeURIComponent(meta.id)}/loop`;
      let payload;
      if (shared && typeof shared.request === "function") {
        payload = await shared.request(path);
      } else {
        const response = await fetch(`${API_BASE}/${encodeURIComponent(meta.id)}/loop`, {
          credentials: "same-origin",
          headers: { Accept: "application/json" },
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        payload = await response.json();
      }
      const live = document.querySelector(
        `[data-loop-card][data-position-id="${String(meta.id).replace(/"/g, '\\"')}"]`,
      );
      if (!live) return;
      const expanded = expandedIds.has(meta.id);
      live.outerHTML = renderCard(payload, { ...meta, expanded });
    } catch (error) {
      const live = document.querySelector(
        `[data-loop-card][data-position-id="${String(meta.id).replace(/"/g, '\\"')}"]`,
      );
      if (live) {
        live.outerHTML = `<article class="p25-loop-card p25-loop-card--error" data-position-id="${esc(meta.id)}">
          <div class="p25-card-head">
            <span class="p25-card-icon p25-card-icon--empty" aria-hidden="true"></span>
            <span class="p25-card-id"><span class="p25-card-title">${esc(meta.label || "循环贷仓位")}</span>
            <span class="p25-card-meta">循环贷指标暂不可用 · ${esc(error.message || "请求失败")}</span></span>
          </div>
        </article>`;
      }
    } finally {
      inflight.delete(meta.id);
    }
  }

  function enhanceAll(root) {
    const scope = root && root.querySelectorAll ? root : document;
    scope.querySelectorAll("article.pos-card[data-position-id]").forEach((card) => {
      enhanceCard(card);
    });
  }

  function bindCardEvents(root) {
    if (root.__p25CardEvents) return;
    root.__p25CardEvents = true;
    root.addEventListener("click", (event) => {
      const toggle = event.target.closest("[data-loop-toggle]");
      if (!toggle) return;
      const card = toggle.closest("[data-loop-card]");
      if (!card) return;
      const id = card.getAttribute("data-position-id");
      const more = document.getElementById(toggle.getAttribute("aria-controls"));
      const open = toggle.getAttribute("aria-expanded") !== "true";
      toggle.setAttribute("aria-expanded", String(open));
      if (more) {
        more.hidden = !open;
        more.classList.toggle("is-open", open);
      }
      card.classList.toggle("is-expanded", open);
      if (open) expandedIds.add(id);
      else expandedIds.delete(id);
    });
  }

  function protocolStackMarkup() {
    return PROTOCOLS.map(
      (item, index) =>
        `<img src="${esc(item.icon)}" alt="" width="18" height="18" loading="lazy" style="z-index:${
          PROTOCOLS.length - index
        }" />`,
    ).join("");
  }

  function titleRowMarkup() {
    return `<button class="p25-title-row" type="button" data-p25-protocols aria-haspopup="dialog" aria-controls="p25-protocol-drawer">
      <span class="p25-title-copy">已接入 ${PROTOCOLS.length} 个协议</span>
      <span class="p25-stack" aria-hidden="true">${protocolStackMarkup()}</span>
      <span class="p25-title-chev" aria-hidden="true">
        <svg viewBox="0 0 16 16" fill="none"><path d="m6 3.5 4.5 4.5L6 12.5"/></svg>
      </span>
    </button>`;
  }

  function injectTitleRow() {
    const page = document.querySelector(".page--lending");
    if (!page || page.querySelector("[data-p25-protocols]")) return;
    const row = page.querySelector(".page-title-row");
    if (!row) return;
    row.insertAdjacentHTML("beforeend", titleRowMarkup());
  }

  function drawerMarkup() {
    const items = PROTOCOLS.map(
      (item) => `<li class="p25-protocol">
        <img src="${esc(item.icon)}" alt="" width="38" height="38" loading="lazy" />
        <span class="p25-protocol-copy">
          <span class="p25-protocol-head"><strong>${esc(item.name)}</strong><em>${esc(item.chain)}</em></span>
          <span class="p25-protocol-note">${esc(item.note)}</span>
        </span>
      </li>`,
    ).join("");
    return `<div class="p25-drawer" id="p25-protocol-drawer" data-p25-drawer hidden>
      <div class="p25-drawer-backdrop" data-p25-close aria-hidden="true"></div>
      <section class="p25-drawer-panel" role="dialog" aria-modal="true" aria-labelledby="p25-drawer-title" tabindex="-1">
        <div class="p25-drawer-grip" data-p25-grip aria-hidden="true"><span></span></div>
        <button class="p25-drawer-close" type="button" data-p25-close aria-label="关闭协议说明">×</button>
        <p class="p25-drawer-kicker">借贷接入</p>
        <h2 class="p25-drawer-title" id="p25-drawer-title">已接入 ${PROTOCOLS.length} 个协议</h2>
        <ul class="p25-protocol-list">${items}</ul>
        <p class="p25-drawer-foot">只读，不发起交易</p>
      </section>
    </div>`;
  }

  const drawerState = {
    bound: false,
    open: false,
    lastFocus: null,
    dragStart: null,
    panel: null,
    root: null,
  };

  function drawerShouldClose(dy, panelHeight) {
    const n = Number(dy);
    if (!Number.isFinite(n) || n <= 0) return false;
    const limit = Math.min(DRAWER_DRAG_CLOSE_PX, Number(panelHeight || 0) * DRAWER_DRAG_RATIO);
    return n >= (limit > 0 ? limit : DRAWER_DRAG_CLOSE_PX);
  }

  function trapFocusIndex(current, length, shift) {
    if (!length) return -1;
    if (current < 0 || current >= length) return shift ? length - 1 : 0;
    return (current + (shift ? -1 : 1) + length) % length;
  }

  function focusables(panel) {
    return Array.from(
      panel.querySelectorAll(
        'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      ),
    ).filter((el) => el.getClientRects().length > 0);
  }

  function setDrawerOpen(open) {
    const root = drawerState.root;
    if (!root) return;
    root.hidden = !open;
    requestAnimationFrame(() => root.classList.toggle("is-open", open));
    document.documentElement.classList.toggle("p25-drawer-lock", open);
    drawerState.open = open;
    if (open) {
      const close = root.querySelector(".p25-drawer-close");
      if (close) close.focus({ preventScroll: true });
    } else if (drawerState.lastFocus && document.contains(drawerState.lastFocus)) {
      drawerState.lastFocus.focus({ preventScroll: true });
    }
  }

  function closeDrawer() {
    if (!drawerState.open) return;
    const root = drawerState.root;
    setDrawerOpen(false);
    window.setTimeout(() => {
      if (root && !drawerState.open) root.hidden = true;
    }, 190);
  }

  function openDrawer(trigger) {
    if (!drawerState.root) {
      document.body.insertAdjacentHTML("beforeend", drawerMarkup());
      drawerState.root = document.querySelector("[data-p25-drawer]");
      drawerState.panel = drawerState.root.querySelector(".p25-drawer-panel");
    }
    drawerState.lastFocus = trigger || document.activeElement;
    setDrawerOpen(true);
  }

  function bindDrawer() {
    if (drawerState.bound) return;
    drawerState.bound = true;
    document.addEventListener("click", (event) => {
      if (event.target.closest("[data-p25-protocols]")) {
        event.preventDefault();
        openDrawer(event.target.closest("[data-p25-protocols]"));
        return;
      }
      if (event.target.closest("[data-p25-close]")) {
        event.preventDefault();
        closeDrawer();
      }
    });
    document.addEventListener("keydown", (event) => {
      if (!drawerState.open) return;
      if (event.key === "Escape") {
        event.preventDefault();
        closeDrawer();
        return;
      }
      if (event.key !== "Tab") return;
      const panel = drawerState.panel;
      if (!panel) return;
      const items = focusables(panel);
      if (!items.length) {
        event.preventDefault();
        panel.focus();
        return;
      }
      const current = items.indexOf(document.activeElement);
      const next = trapFocusIndex(current, items.length, event.shiftKey);
      event.preventDefault();
      items[next].focus();
    });
    const grip = () => drawerState.root && drawerState.root.querySelector("[data-p25-grip]");
    document.addEventListener("pointerdown", (event) => {
      if (!drawerState.open || !event.target.closest("[data-p25-grip]")) return;
      const panel = drawerState.panel;
      if (!panel) return;
      drawerState.dragStart = { y: event.clientY, height: panel.getBoundingClientRect().height };
      panel.classList.add("is-dragging");
    });
    document.addEventListener("pointermove", (event) => {
      if (!drawerState.dragStart || !drawerState.panel) return;
      const dy = Math.max(0, event.clientY - drawerState.dragStart.y);
      drawerState.panel.style.transform = `translateY(${dy}px)`;
    });
    document.addEventListener("pointerup", (event) => {
      if (!drawerState.dragStart || !drawerState.panel) return;
      const dy = Math.max(0, event.clientY - drawerState.dragStart.y);
      const start = drawerState.dragStart;
      drawerState.dragStart = null;
      drawerState.panel.classList.remove("is-dragging");
      drawerState.panel.style.transform = "";
      if (drawerShouldClose(dy, start.height)) closeDrawer();
      const el = grip();
      if (el) el.setAttribute("aria-hidden", "true");
    });
    document.addEventListener("pointercancel", () => {
      if (!drawerState.panel) return;
      drawerState.dragStart = null;
      drawerState.panel.classList.remove("is-dragging");
      drawerState.panel.style.transform = "";
    });
  }

  let observer = null;

  function mount(root) {
    const scope = root && root.querySelector ? root : document;
    injectTitleRow();
    bindDrawer();
    const content = document.getElementById("lending-content");
    bindCardEvents(document);
    if (content) {
      if (observer) observer.disconnect();
      observer = new MutationObserver(() => enhanceAll(content));
      observer.observe(content, { childList: true, subtree: true });
    }
    enhanceAll(scope);
  }

  const api = {
    mount,
    renderCard,
    renderSkeleton,
    enhanceAll,
    protocolKey,
    protocolIcon,
    PROTOCOLS,
    DRAWER_DRAG_CLOSE_PX,
    DRAWER_DRAG_RATIO,
    drawerShouldClose,
    trapFocusIndex,
    fmtUsd,
    fmtSignedUsd,
    fmtPct,
    fmtSignedPct,
    fmtPrice,
    fmtAmount,
    fmtLeverage,
    fmtDistance,
    fmtHealth,
    healthTone,
    healthClass,
    liqBasisLabel,
    fmtBeijing,
    userText,
    toneClass,
    reason,
  };

  if (typeof window !== "undefined") {
    window.P25Loop = api;
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", () => mount(document));
    } else {
      mount(document);
    }
  }
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})();