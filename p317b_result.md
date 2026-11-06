# 三期·17b 按来源分组行图标

## 改动文件
- `frontend/static/app.js`
  - 新增 `SOURCE_ICON_FILES`、`sourceIconLetter`、`sourceIconMarkup(source)`
  - `holdingsBySourceMarkup` 的 `hold-row--source` 在 chevron 与 `hold-asset` 之间插入 30px 圆标（`hold-icon` / `hold-icon-img`）
  - 图标失败走现有 document `error` listener：`data-icon-asset` 为来源首字母，落到 `tokenIconSvgDataUri` 暖色字母
  - `SOURCE_LABELS` 补 alchemy→Alchemy、bitget/bybit/kraken/coinstats/evm/ethereum/solana；zerion 仍为 EVM、helius 仍为 Solana
  - 展开明细仍用 `tokenIconMarkup`；`pendleUnderlyingSymbol` 未改
- `frontend/static/styles.css`
  - `.hold-row--source` 列改为 `14px 30px minmax(0,1.2fr) …`，与按资产行对齐；`.hold-row` 本身未改
- `frontend/static/icons/sources/*.svg`（自绘、暗底友好）
  - alchemy, binance, bitget, bybit, coinstats, ethereum, evm, gate, helius, kraken, solana, zerion

## 截图
- `/workspace/asset-tracker/screenshots/overview_holdings_icons_source.png`（按来源：Alchemy / 币安 / Solana / Gate 头行图标）
- `/workspace/asset-tracker/screenshots/overview_holdings_icons_asset.png`（按资产，Pendle 底层图标仍在）

## 来源 → 图标
- binance → 黄底币安菱形 `binance.svg`
- gate → 蓝底白字标 `gate.svg`
- bitget → 青色几何 `bitget.svg`
- bybit → 橙字标 `bybit.svg`
- kraken → 紫底 `kraken.svg`
- coinstats → 蓝/绿 `coinstats.svg`
- alchemy → 蓝底六边形 `alchemy.svg`（live）
- zerion → 蓝色尖标 `zerion.svg`（EVM 风格，不是空白）
- helius → `solana.svg`（三斜杠）
- solana → `solana.svg`
- evm / ethereum → `ethereum.svg`（ETH 钻石）
- 未映射 source → 暖色字母圆标（sourceLabel 首字，否则 source key）

## Pendle
`PLP-` / `PT-` / `YT-` 去日期尾后用底层符号取图标（如 PLP-SUSDX-3DEC2026 → SUSDX）；非 Pendle 名称不变。本次未改该逻辑。

## 验证
- `node --check frontend/static/app.js` 通过
- `.venv/bin/pytest backend/tests/test_overview_holdings.py backend/tests/test_snapshots.py` 19 passed
- 已 `docker cp` `app.js` / `styles.css` 进 `asset-tracker-app-1:/app/frontend/static/`，SVG 写入同路径 `icons/sources/`，均为 `a+r`（644）
- Playwright + 系统 Chrome，admin/admin123，`http://127.0.0.1:8000/?v=p317b2`：四个 live 来源头行图标 naturalWidth/Height = 30
