# 三期·18 路 B — admin 未定价持仓核查并补价

范围：只动后端 pricing / sync。未改 frontend、overview 图表、演示密码、隧道。未打印任何 key。

## 基线

admin（user_id=1）最新 `source=sync` 快照 **647**（2026-10-23 19:42:43 UTC）：

- 净值 **16118.34**
- 持仓 79，已定价 40，未定价 **39**
- 未定价来源：alchemy 23、gate 9、binance 4、helius 3

## 39 条清单与分类

数量与合约取自快照 647。A 类在补价后的快照 661 里标了 `price_source=exchange_ticker`（当时的 last 价写在最后一列，仅作记录，不是写死的价格）。

### A 可定价真币（7）

交易所行优先走该所公开 ticker；Binance 公开 REST 在本机 HTTP 451，同一符号改问 Gate 公开盘口（不是假价）。

| 来源 | 符号 | 数量 | chain | 合约 | 661 的 last / 入账 |
| --- | --- | --- | --- | --- | --- |
| binance | WLFI | 1.31 | — | — | 0.05465 → 0.07 |
| binance | ASTER | 0.00022207 | — | — | 0.7002 → 0.00（数量小，四舍五入后为 0） |
| binance | AT | 0.08225394 | — | — | 0.13936 → 0.01 |
| binance | BREV | 0.04107377 | — | — | 0.07809 → 0.00 |
| gate | GT | 0.05397868591 | — | — | 10.10 → 0.55 |
| gate | MON | 1.0 | — | — | 0.02174 → 0.02 |
| gate | FF | 0.01220228 | — | — | 0.1002 → 0.00 |

这 7 条都已标为已定价。入净值的只有 WLFI、AT、GT、MON（合计 +0.65）。ASTER / BREV / FF 有真价，但 `amount × price` 量化到分后是 0.00。

### B 粉尘（8）

数量绝对值 `< 1e-6` 的交易所行，ticker 路径直接跳过，保持未定价。不入净值。

| 来源 | 符号 | 数量 | 说明 |
| --- | --- | --- | --- |
| gate | ANLOG | 2.6e-5 | Gate 已下架，无 ANLOG/USDT |
| gate | FRAG | 6.8e-9 | 无盘口 |
| gate | LION | 5.4e-9 | 数量低于门槛 |
| gate | LN | 3e-10 | 数量低于门槛 |
| gate | SKATE | 6.8e-9 | 数量低于门槛 |
| alchemy | OGUSDX | 5.681e-7 | 合约无 DefiLlama 报价 |
| alchemy | YRISE | 9e-5 | 合约无报价（数量略高于 1e-6，但仍无价） |
| alchemy | YU | 1.488e-7 | 合约无报价 |

YRISE 数量是 9e-5，高于粉尘门槛，但合约查无价，效果与粉尘一样：不入净值。

### C 疑似垃圾 / 空投（20）+ 无合约定价的 Solana（3）

**保持 `priced=false`，不入净值，不在前端隐藏，也不写死假价格。**

alchemy（ethereum），乱名或无流动性合约：

| 符号 | 数量 | 合约 |
| --- | --- | --- |
| CAT | 1 | 0x53fdca91fd33b9131b5ceade42a3edbd9b38edff |
| DOG | 1 | 0x16613999219f9b56bededbf3722e44c4c2045300 |
| DOG | 1 | 0x379b526f2ff7b3f50c0b4f48ef6ddd224adfbb90 |
| DOG | 1 | 0x37dabad8ac496148596196fe9adeb54ee3111c78 |
| ERC20 | ~4.81e57 | 0x4dba528d88695e36d3816705a30095b3a80fa5b9 |
| ERC20 | ~4.81e57 | 0xa0c5f9c82e7a67eb995da394bea78b056561eb11 |
| ERC20 | ~4.81e57 | 0xd21bccd9d762ed68929d55c2ce4e74bef74f0eba |
| ERC20 | ~4.81e57 | 0xdb9106401760b2d6db2f051f3a5fcfe2ecd45076 |
| ERC20 | ~4.81e57 | 0xde08f07969e94d3ea34acc42ef1d44f2f379786f |
| JANE | 18031.03 | 0x333333330522f64ee8d0b3039c460b41670e3404 |
| KEKIUS | 1 | 0xcdb9f907bd8828be9643b39cd4638d362fd6e9c4 |
| NEMOCLAW | 5 | 0xbbcd44263236dfbf8e4705a4eb58226671d74c5e |
| OPENCLAW | 5 | 0xb5885b4bb63928f77e1f669785dae2f3cc7562d7 |
| OZAK | 5 | 0x6a7b57bade63ceec4904203907fc5dbbd3197b91 |
| PORT | 1 | 0x83819bf7e906bcf57e9f5b20453a2eff43f3845c |
| SEND | 15 | 0x91d6cf4af9697ec95d9e1754e84d2b31c67a5ab7 |
| SUPERDOG | 5 | 0x695e449525d42055d368f84e97912032c7410a46 |
| WATTOIN | 5 | 0x5eef5946ad78e614bb3ee7b9ed1097c517ae97f7 |
| XCHAT | 5 | 0x782ba12feccbd018bbfcd5aad2f786f9e0f40bbd |
| YES | 3 | 0x7f3ba3f18f1378fbd8efa0a20bfe7016e2efd266 |

helius（solana）。`quote_contracts` 已把 mint 小写后以 `solana:` 前缀问 DefiLlama，这三枚都没有返回：

| 符号 | 数量 | mint |
| --- | --- | --- |
| KJLP-JSOL | 3.356864 | J5o36oSuGS4nQ7cfZiAuvmaN2bggcoMoqp7BB7zATtwE |
| KJLP-USDC | 0.032705 | FVk7hoHvqAkdpNY5Zxr19WzVxd35jgHcaG4FNr1UUVn9 |
| YE | 0.000969 | 4Z6AbaXT9nk5J3uihNSjBVUoxUTSosRbDTkTfAFMpump |

另外 gate **GT2**（0.27099935）不在 A 类：Gate 上是 GT2-Staked，`trade_disabled`，没有 GT2/USDT。保持未定价，不编价格。

## 代码改动

- `backend/app/integrations/prices/service.py`
  - `PriceService.quote_exchange_ticker(source, asset)`：ccxt 无 key 的 `fetch_ticker('SYMBOL/USDT')`，没有再试 `SYMBOL/USD`，取 `last`（否则 `close`）。
  - 来源映射 `_CCXT_TICKER_IDS`：`gate` → `gateio`。
  - Binance 451 时，只对同一符号回退到 gateio 公开盘口（`_TICKER_FALLBACK_VENUES`）。
  - 同一 `ccxt_id:SYMBOL` 在 TTL 内只打一次，失败也缓存为 None。单个符号失败不中断整次定价。
  - mock 且没有注入 client 时不联网。
  - 修了一个客户端缓存 bug：Binance 先失败并把 client 放进 dict 后，Gate 的 client 不会再被创建。
- `backend/app/snapshots.py`
  - `_price_balance`：无合约的交易所行（binance / bitget / bybit / kraken / gate）在 DefiLlama 符号之前问 ticker，`price_source=exchange_ticker`。
  - `|amount| < 1e-6` 不走 ticker（B 类粉尘）。
  - 过期 Pendle YT、合约价、Zerion 兜底的原顺序不变。
  - stale carry（`_price_carried`）用同样的报价，但 `price_source` 仍是 `stale`。
- `backend/tests/test_chain_prices.py`
  - ticker 命中则交易所行 priced，同符号只打一次。
  - ticker 失败则仍 unpriced，并继续试 USD。
  - Binance 失败回退 Gate。
  - 粉尘数量不入价，便宜但数量正常的币（FF）仍入价。

## 前后对比

| | 快照 | 时间 (UTC) | 持仓 | 已定价 | 未定价 | 净值 |
| --- | --- | --- | --- | --- | --- | --- |
| 前 | 647 | 2026-10-23 19:42:43 | 79 | 40 | 39 | 16118.34 |
| 后 | 661 | 2026-10-23 20:51:01 | 79 | **47** | **32** | **16118.90** |

净值差 **+0.56**。逐行对账：ticker 新入账 +0.65（WLFI 0.07、AT 0.01、GT 0.55、MON 0.02），其余已定价行的市价波动合计 −0.09（Pendle / PT / YT 略降，CRCLX、SOL 略升）。没有跳变。

已定价 +7 = A 类 7 条。Gate SOL / Binance BNB 本来就有价（DefiLlama），现在改记为 `exchange_ticker`，金额只随市价移动（SOL 5.36 → 5.38）。

仍未定价 32 条，原因：

- B 粉尘：gate ANLOG / FRAG / LION / LN / SKATE，alchemy OGUSDX / YRISE / YU。
- gate GT2：无现货盘口。
- C 类 alchemy 20 条（不含上面 3 条粉尘）：无流动性 / 乱名，故意不定价。
- helius 3 条：DefiLlama `solana:<mint>` 无报价。

中间有一次 CLI 没带上 `.env`（`python -m app.sync.cli` 在 import settings 之后才 `load_dotenv`，而 shell 里没有这些变量），写出快照 660：nav 5880.43、只有 20 条交易所 stale carry。已用 `set -a && . ./.env` 重跑，661 恢复 79 条。660 不是补价结果，不要拿它做对比。

## pytest

`cd /workspace/asset-tracker/backend && ../.venv/bin/pytest -q`

全绿：174 passed（新增 4 个 ticker 用例：命中、失败、Binance 回退 Gate、粉尘跳过）。
