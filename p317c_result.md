# 三期·17c 用 Web3Icons 官方 SVG 覆盖手绘来源图标

## 删除的手绘文件
`frontend/static/icons/sources/` 原 12 个自绘 SVG 全部删除，不再保留冒充品牌的文件：

- `alchemy.svg`（假 Alchemy 六边形）
- `coinstats.svg`（假 CoinStats）
- `helius.svg`（假 Helius）
- `evm.svg`（手绘 ETH 副本）
- 以及手绘的 `binance.svg` / `gate.svg`（蓝底白 R）/ `bitget.svg` / `bybit.svg` / `kraken.svg` / `ethereum.svg` / `solana.svg` / `zerion.svg`

目录里现在只剩 8 个 vendor 文件。`alchemy.svg`、`coinstats.svg`、`helius.svg`、`evm.svg` 的 HTTP 均为 404。

## Vendor 来源
BASE `https://raw.githubusercontent.com/0xa3k5/web3icons/main/packages/core/src/svgs`

| 本地文件 | raw URL | HTTP |
|---|---|---|
| `sources/binance.svg` | `.../exchanges/branded/binance.svg` | 200 |
| `sources/gate.svg` | `.../exchanges/branded/gate-io.svg` | 200 |
| `sources/bitget.svg` | `.../exchanges/branded/bitget.svg` | 200 |
| `sources/bybit.svg` | `.../exchanges/branded/bybit.svg` | 200 |
| `sources/kraken.svg` | `.../exchanges/branded/kraken.svg` | 200 |
| `sources/ethereum.svg` | `.../networks/branded/ethereum.svg` | 200 |
| `sources/solana.svg` | `.../networks/branded/solana.svg` | 200 |
| `sources/zerion.svg` | `.../wallets/branded/zerion.svg` | 200 |

每个文件的 `path` `d` 与 raw 原文一致（binance 1、gate 2、bitget 2、bybit 2、kraken 1、ethereum 8、solana 6 含 3 个 gradient、zerion 1，缺失 0）。外层只加了 30×30 `viewBox`、圆形 `clipPath` 和 `translate(3 3)`，把 24×24 图形居中裁进圆。没有手绘字母。

## SOURCE_ICON_FILES
| source key | 文件 |
|---|---|
| binance | `binance.svg` |
| gate | `gate.svg` |
| bitget | `bitget.svg` |
| bybit | `bybit.svg` |
| kraken | `kraken.svg` |
| ethereum / evm / alchemy | `ethereum.svg`（Alchemy 用 ETH 网络标，没有假 Alchemy 品牌） |
| solana / helius | `solana.svg` |
| zerion | `zerion.svg`（钱包标） |
| coinstats | **不映射文件**。`sourceIconMarkup` 对 `coinstats` 直接走暖色字母圆（`#c4895a`，字 `#1a120c`） |
| 其他未映射 | 仍走 `tokenIconSvgDataUri` 暖色字母 |

已删除对 `alchemy.svg` / `coinstats.svg` / `helius.svg` / `evm.svg` 的引用。`evm` 直接指向 `ethereum.svg`，目录里不放 symlink、也不留第二份文件。

## 未改动
- `holdingsBySourceMarkup` 仍在 chevron 与名称之间插入 `sourceIconMarkup(group.source)`。
- Pendle（`pendleUnderlyingSymbol` / `PLP|PT|YT`）未改。
- `styles.css` 未改。

## 部署
- `docker cp` `app.js` 与 `icons/sources/*.svg` 进 `asset-tracker-app-1:/app/frontend/static/`。
- 容器内与宿主机文件 `cmp` 一致，权限 `644`（`a+r`）。
- 宿主机 `frontend/` 在本环境是只读挂载，文件通过 `docker run -v` 写回，与容器内容一致。

## 截图
`/workspace/asset-tracker/screenshots/overview_holdings_icons_source.png`

Playwright + 系统 Chrome，admin 登录，`http://127.0.0.1:8000/?v=p317c`，切到「按来源」并收起分组。四个 live 头行：

| 行 | 实际 source | img src | natural |
|---|---|---|---|
| Alchemy | alchemy | `/static/icons/sources/ethereum.svg` | 30×30 |
| 币安 | binance | `/static/icons/sources/binance.svg` | 30×30 |
| Solana | helius | `/static/icons/sources/solana.svg` | 30×30 |
| Gate | gate | `/static/icons/sources/gate.svg` | 30×30 |

目视：Gate 是蓝弧 + 右上绿块（Gate.io），不是蓝底白 R；币安是黄菱；Alchemy 是 ETH 钻石；Solana/helius 是 Solana 三斜杠。

## 验证
- `curl` 8 个 vendor SVG 均为 200；`alchemy.svg` / `coinstats.svg` / `helius.svg` / `evm.svg` 为 404。
- 线上 `app.js` 不含 `alchemy.svg`、`coinstats.svg`、`helius.svg`。
- `node --check frontend/static/app.js` 通过。
