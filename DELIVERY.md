# 资产追踪站 · 一期骨架交付

## 路径
`/workspace/asset-tracker`

## 实际 dsh 命令

环境（每次跑 dsh 前）：
```bash
set -a && source ~/.dsh/relay.env && set +a
# 已确认 DSH_RELAY_MODEL=grok-4.7-supergrok（未打印 key）
```

### 1）host 网络修复（Docker ICC 异常）
```bash
cd /workspace/asset-tracker && dsh --profile headless "你在 /workspace/asset-tracker。当前 Docker 环境里同一 compose 网络内容器之间 TCP 不通（ARP 可达、host→容器 IP 可达，但 app→postgres/redis 超时），导致 FastAPI lifespan 里 wait_for_db 卡住。

请只做最小可运行修复：

1. 改 docker-compose.yml：
   - app 服务使用 network_mode: host
   - 因此 app 不能再写 ports / depends_on 的 condition 网络依赖可以保留，但 network_mode:host 时 ports 映射无效，请删掉 app 的 ports
   - app 的 environment/DATABASE_URL 改为 postgresql+psycopg://asset:asset_pass@127.0.0.1:5432/asset_tracker
   - REDIS_URL 改为 redis://127.0.0.1:6379/0
   - 保留 postgres/redis 的 ports 发布（5432、6379），让 host 网络的 app 能连
   - 可给 app 加 restart 与 env_file；depends_on 仍可写（compose 会按顺序起，即使 host 网络下无 health condition 也尽量保留 postgres healthy 等待——若 compose 对 host network + depends_on condition 报错，则改为简单 depends_on 列表）

2. 改 backend/app/db.py：给 create_engine 增加 connect_args={\"connect_timeout\": 5}，避免再无限卡住；wait_for_db 失败要抛错。

3. 同步 .env 与 .env.example 里的 DATABASE_URL / REDIS_URL 为 127.0.0.1（演示环境说明一下：因本机 Docker ICC 异常，app 走 host 网络）。

4. 更新 README 启动说明一句：app 使用 host 网络连本机 5432/6379。

不要改业务逻辑、不要泄露任何 API key。改完后用 \`python3 -m compileall backend\` 自检。"
```

### 2）登录 FormData bug
```bash
cd /workspace/asset-tracker && dsh --profile headless "修 /workspace/asset-tracker/frontend/static/app.js 登录提交 bug。

原因：document 上委托监听 submit，handleLoginSubmit 里用了 event.currentTarget（实际是 document），导致 \`new FormData(form)\` 抛错：
Failed to construct 'FormData': parameter 1 is not of type 'HTMLFormElement'.

改法：在 handleLoginSubmit 里把 \`const form = event.currentTarget;\` 改成 \`const form = event.target;\`，并加断言/兜底：若不是 HTMLFormElement 则 return。

改完后 \`node --check frontend/static/app.js\`。不要改其他逻辑。"
```

### 3）审美官「有条件过」前端整改（P0–P2）
```bash
set -a && source ~/.dsh/relay.env && set +a
cd /workspace/asset-tracker && dsh --profile headless "你在 /workspace/asset-tracker。审美官「有条件过」，请只改前端视觉/交互（frontend/static 与相关 HTML），按严重度落地，不要动后端业务。

P0（必须）：
1. 收益页太空：选完日/周/月/年后，除主收益数字外，加迷你走势……以及区间起净值/区间止净值。
2. 待确认「确认」按钮改成半实心或实心琥珀主按钮。
3. 底栏不要退出；退出挪到头像菜单。

P1：中文小标；tabular-nums；演示账号可点一键填入。
P2：净值曲线极淡面积填充；待确认空状态。

改完 node --check；若前端进镜像则 docker compose up -d --build app。"
```

验证（整改后）：
- `node --check frontend/static/app.js` 通过
- `docker compose up -d --build app` 后 `/api/health` ok
- 新截图已覆盖 login / overview / returns / pending（390 宽）

## 启动步骤
```bash
cd /workspace/asset-tracker
docker compose up -d --build
# 打开 http://127.0.0.1:8000
docker compose ps
docker compose logs -f app
```

健康检查：
```bash
curl http://127.0.0.1:8000/api/health
# {"status":"ok"}
```

临时公网（若环境允许出站 7844）：
```bash
cloudflared tunnel --url http://127.0.0.1:8000
# URL 写入 TUNNEL_URL.txt
```

## 临时 URL
见 `TUNNEL_URL.txt`（当前写入值会随重启变化）。

**本机实测状态（Asia/Shanghai）：**
- 本地 `http://127.0.0.1:8000`：正常（health / 登录 / overview / returns / pending confirm 均通）
- trycloudflare：能申请到 hostname，但沙箱出站 **UDP/TCP 7844** 被拦，connector 无法注册，公网访问返回 Cloudflare 530/1033
- 备选 localtunnel / bore.pub / ngrok：同样因出站限制未能打通

因此一期演示请优先用本机 `http://127.0.0.1:8000`；外网能开时再重跑 cloudflared。

## 默认账号
| 角色 | 用户名 | 密码 |
| --- | --- | --- |
| 管理员 | `admin` | `admin123` |
| 演示用户 | `demo` | `demo123` |

## 截图（手机宽度 390）
| 页面 | 路径 |
| --- | --- |
| 登录 | `screenshots/login.png` |
| 总览 | `screenshots/overview.png` |
| 收益（周切换） | `screenshots/returns.png` |
| 待确认 | `screenshots/pending.png` |

## 验证结果摘要
- `docker compose up -d --build`：postgres / redis / app 均为 healthy（app 为 host 网络）
- `POST /api/auth/login` demo/admin：成功
- `GET /api/overview`：当前净值约 16969.91 USDT，含日净值序列
- `GET /api/returns?period=day|week|month|year`：成功（TWR 已剔除充提）
- `GET /api/pending` + `POST /api/pending/{id}/confirm`：成功
- 前端静态页 `/` `/login` `/returns` `/pending`：200

## Compose 状态
交付时 `docker compose ps` 为 Up（healthy）。app 使用 `network_mode: host` 连本机已发布的 5432/6379。

## 已知限制
1. **假数据**：无真实交易所/链上拉取。
2. **Docker ICC**：本机容器间 TCP 不通，故 app 改 host 网络；正常 Docker 环境可改回桥接。
3. **临时公网**：trycloudflare 在本沙箱因 7844 出站阻断未能真正连通；本地可完整演示。
4. **Redis**：仅占位，未接缓存/队列。
5. **演示密钥**：`.env` 中 JWT / MASTER_KEY / DB 密码均为公开演示值。
6. **登录 bug 已修**：`event.currentTarget` → `event.target`（FormData）。

## 请审美官审
截图已备好：`screenshots/{login,overview,returns,pending}.png`。视觉：深色克制、暖金点缀、总览大数字优先。

---

## 二期 · 交易所/链上接入 + 待确认归因 + 同步（2026-10-23，mock 跑通，key 未到）

### 实际 dsh 命令（原文）
每次先 `set -a && source ~/.dsh/relay.env && set +a`，工作目录 `/workspace/asset-tracker`，默认模型 grok-4.7-supergrok。
1. `dsh --profile headless "$(cat /workspace/p2_prompt.txt)"` —— 完整提示词原文存在 `/workspace/p2_prompt.txt`（adapter/链上/价格/归因/队列去重/TWR/RQ 同步/pytest 全量需求）。
2. `dsh --profile headless "在 /workspace/asset-tracker：Docker 镜像里没有 backend/tests/fixtures，导致容器内 mock 同步 fetched=0。修改 Dockerfile 增加 COPY backend/tests/fixtures /app/tests/fixtures（放在 COPY backend/app 之后、chown 之前），必要时调整 .dockerignore 让该目录不被排除。不要改其他文件，不要读 ~/.dsh。"`

### 改动
- `app/integrations/exchanges/`：统一 ExchangeAdapter；CCXT 只读（binance/bitget/bybit/kraken/gate），since=2026-01-01，分页 + enableRateLimit + 指数退避；Mock 读录制 fixtures，同一 normalize。
- `app/integrations/chain/`：Zerion（Alchemy 兜底）、Helius；`app/integrations/prices/`：CoinGecko Demo → DefiLlama 兜底，USDT 计价带缓存。
- `app/attribution.py`：deposit/withdraw/airdrop/kol_reward/transfer + 置信度 + 中文理由。
- 待确认表幂等迁移（`app/migrate.py`）新增来源/建议列，`(user_id, source, external_id)` 唯一去重；`POST /api/pending/{id}/confirm` 可带 `{"category": ...}` 改类别，确认写入流水，幂等。
- TWR：仅 deposit/withdraw/transfer 算外部现金流；airdrop/kol_reward 算收益。
- 同步：`POST /api/sync?async=false|true`（true 走 RQ），`GET /api/sync/status`，CLI `python -m app.sync.cli --user demo --mode mock`；compose 新增 `worker` 服务。
- 日志 redact filter；前端、演示账号、隧道文件未改。

### 测试结果
- `cd backend && python -m pytest -q` → **27 passed**（adapter 解析/分页/重试、链上+价格兜底、归因、去重与确认改类别、TWR 剔除充提、API e2e）。
- `docker compose up -d --build` → app/postgres/redis/worker 均 running，`/api/health` ok；demo 登录、overview、returns、pending 均 200。
- mock 同步：首次 `{"fetched":11,"enqueued":11,"duplicates":0}`，再次 `{"fetched":11,"enqueued":0,"duplicates":11}`；新项带建议（例：helius USDC 流入 → kol_reward 0.86）；改类别确认成功；RQ 异步任务 finished；CLI 可用；日志无 key。

### 已知限制
- 无真实 key，live 路径只在单测里用 mock 客户端验证过，真实接口字段差异需拿到 key 后联调。
- 归因是规则引擎，非模型；"自有地址"判定依赖 EVM_ADDRESSES/SOLANA_ADDRESSES 配置。
- 一期前端未改：新待确认项通过已有 title/detail 显示建议，界面上还不能选类别（API 已支持）。
- 无定时调度，需手动触发 API/CLI；快照净值仍为种子数据，没有用同步余额生成快照。
- 公网隧道不在本期范围。

### key 到位后的切换步骤
1. 在 `.env` 填：`BINANCE_API_KEY/SECRET`、`BITGET_API_KEY/SECRET/PASSPHRASE`、`BYBIT_API_KEY/SECRET`、`KRAKEN_API_KEY/SECRET`、`GATE_API_KEY/SECRET`（只读权限）、`ZERION_API_KEY`、`ALCHEMY_API_KEY`、`HELIUS_API_KEY`、`COINGECKO_DEMO_API_KEY`、`EVM_ADDRESSES`、`SOLANA_ADDRESSES`（逗号分隔）。
2. `SYNC_MODE=live`（或保持 auto：有 key 的源走 live，没 key 的走 mock）；`SYNC_SINCE=2026-01-01`。
3. `docker compose up -d --build`，再 `docker compose exec app python -m app.sync.cli --user demo --mode live` 或 `POST /api/sync`。
4. 查看 `/api/pending` 建议，逐条确认。

---

## 三期 · 1 · 真实净值快照 + 定时同步 + 历史回填（2026-10-23）

### 实际 dsh 命令（原文）
每次先 `set -a && source ~/.dsh/relay.env && set +a`，工作目录 `/workspace/asset-tracker`，默认模型 grok-4.7-supergrok。完整提示词见 `/workspace/p3_prompt.txt`。

1. `dsh --profile headless "$(cat /workspace/p3_prompt.txt)"` —— 快照汇总、定时调度、历史回填、pytest（test_snapshots / test_scheduler）、README。
2. `dsh --profile headless "在 /workspace/asset-tracker：GET /api/sync/status 的 last_run_at/last_status/last_error/last_trigger 在手动 POST /api/sync（async=false 与 async=true）、CLI 同步、POST /api/sync/backfill 和 CLI --backfill 之后仍为 null，只有调度器记录。请让这些入口都写入同一 sync_runs 记录（trigger 分别为 manual/manual/manual/backfill/backfill，失败时记录 last_error 后再抛出），status 返回全局最近一次（任意用户或调度）的记录。补充 pytest 覆盖手动同步与回填写入状态。不改 frontend/、DELIVERY.md、演示账号、隧道文件，不读 ~/.dsh。确保 cd backend && python -m pytest -q 全绿并打印结果。"`
3. `dsh --profile headless "在 /workspace/asset-tracker/backend：tests/test_api_sync.py::test_manual_sync_and_backfill_write_global_status 在本机无限挂起……（根因：无 job_id 时扫 RQ 队列碰到 BLMOVE 阻塞；修复：无 job_id 只读 sync_runs；Redis socket_timeout=2；SQLite timeout+rollback；pytest-timeout）。"`

备份：`/workspace/asset-tracker-p2-backup.tgz`（三期开工前打）。

### 改动摘要
- `app/snapshots.py`：同步后按交易所余额 + 链上余额 × USDT 价汇总，写入 `snapshots`（`source='sync'`，带 breakdown）；同 ts upsert。
- `app/snapshots_backfill.py`：从 `SYNC_SINCE`（2026-01-01）按日倒推 `source='backfill'`；CLI `--backfill` / `POST /api/sync/backfill`；幂等。
- `app/sync/scheduler.py` + `worker_main.py`：RQ worker 主线程 + 调度守护线程；`SYNC_INTERVAL_SECONDS`（默认 1800）；Redis 锁防重入；心跳 healthcheck。
- `sync_runs` 表记录最近同步（manual / scheduled / backfill）；`GET /api/sync/status` 返回 `last_run_at/last_status/last_error/last_trigger/interval_seconds`。
- `/api/overview`、`/api/returns` 优先非 seed 快照，并返回 `nav_source`、`latest_snapshot_ts`；无非 seed 时回退 seed。
- 测试：`test_snapshots.py`、`test_scheduler.py`；挂起修复见上。
- 未改：`frontend/`（相对 p2 备份 diff 为空）、演示账号、隧道文件。

### pytest
```text
cd backend && python -m pytest -q
..........................................                                 [100%]
42 passed
```

### e2e 证据（本机，无 key，Asia/Shanghai 2026-10-23）
- `docker compose up -d --build` → app/postgres/redis/worker 均为 **healthy**；`GET /api/health` → `{"status":"ok"}`。
- 删掉 demo 用户非 seed 快照后：`GET /api/overview` → `nav_source=seed`，`current_nav_usdt=0`（无快照兜底）。
- `POST /api/sync?async=false` → `{"fetched":11,"enqueued":0,"duplicates":11}`；随后 overview → `nav_source=sync`，`current_nav_usdt=5345.5`，`latest_snapshot_ts` 为同步时刻；DB：`select source,nav_usdt … where source='sync'` 一行 `5345.50`。
- `GET /api/sync/status` 在手动同步后：`last_trigger=manual`，`last_status=ok`。
- 临时 `.env`：`SYNC_INTERVAL_SECONDS=30`，只重建 worker；约 70s 内 status 出现 `last_trigger=scheduled`、`last_status=ok`；worker 日志多次 `sync user=1/2 mode=mock`。验证后恢复 `1800` 并重建 worker。
- `POST /api/sync/backfill` → `created=295`（二次则 updated）；overview `nav_source=backfill`，series 长度 300。
- 一期/二期不回归：demo/admin 登录 200；overview / returns?period=month / pending 200；日志无 `api_key=` / `secret=`。

### 已知限制
- live 历史回填无法覆盖：链上 DeFi 仓位、无历史价代币、交易所内部划转、手续费精度（见 `snapshots_backfill.py` 注释与 README）。
- 调度间隔改 `.env` 后需重建 worker 才生效；app 进程内的 `interval_seconds` 回报值来自 app 自己的环境，与 worker 实际间隔可能短暂不一致。
- mock 净值（约 5345 USDT）与一期种子曲线（约 1.7 万）不同量级，属预期：一期种子是演示曲线，三期同步后读真实汇总。
- 前端未改：`nav_source` 等新字段仅 API 可见，UI 不展示来源标签。

## 三期 · 2 · admin 真实 EVM 接入（2026-10-23）

### 实际 dsh 命令（原文）

每次先 `set -a && source ~/.dsh/relay.env && set +a`，再 `timeout 1500 dsh --profile headless "$(cat <prompt>)"`。四轮 prompt 原文如下。

#### p32_prompt.txt

```text
在 /workspace/asset-tracker 做三期·2（live EVM 联调）。先读 README.md、DELIVERY.md、backend/app（尤其 config.py、sync/service.py、snapshots.py、snapshots_backfill.py、integrations/chain/zerion.py、alchemy.py、integrations/prices/service.py）和 backend/tests。

安全硬约束（最高优先级）：
- 绝不 cat/打印/输出 .env 或 ~/.dsh 下任何文件；绝不打印、记录或写入任何 API key；不要把 EVM 地址原文打印到终端或写进任何文件/fixtures/文档（打印时只显示前6后4，如 0x1234…abcd）。读取 .env 只能在 Python 里用 dotenv/os.environ 并直接使用变量。
- 日志与异常信息里带 key 的 URL 必须脱敏（Alchemy URL 路径里含 key：https://eth-mainnet.g.alchemy.com/v2/<KEY> → /v2/***；Zerion Basic auth 头不得出现在日志；httpx 的 INFO 请求日志要么关闭要么经 redact）。扩展 logging_redact 覆盖这些模式并加测试。
- 不改 frontend/、演示账号密码、TUNNEL_URL.txt/tunnel.sh/tunnel.log、DELIVERY.md（我来写）。

1) 地址归属：现状 settings.evm_addresses/solana_addresses 是全局的，所有用户（含 demo）共享。改为按用户绑定，改动尽量小：新增表 user_wallets(id, user_id FK, chain 'evm'|'solana', address, created_at, unique(user_id,chain,address))，启动迁移幂等创建；启动时把 env 里 EVM_ADDRESSES / SOLANA_ADDRESSES 幂等归属给 WALLET_OWNER_USERNAME（新 env，默认 admin，写进 .env.example 空值注释说明默认 admin；不要改 .env 的现有值，可以追加 WALLET_OWNER_USERNAME=admin 一行）。sync/service.py、snapshots.py、snapshots_backfill.py 改为查当前用户的 user_wallets：用户有钱包且有 key 才走 live；没有钱包的用户（demo）链上一律用 mock fixtures，绝不使用他人地址。own_addresses（归因 transfer 判定）也改为当前用户自己的地址。交易所在无 key 时照旧 mock。加测试：demo 同步不会调用 live、拿不到 admin 地址。
2) 修 Zerion/Alchemy 真实响应字段差异：用 .env 里的真实 key 和 admin 的地址实测（写一个脚本 backend/scripts/capture_fixtures.py：调 Zerion /v1/wallets/{addr}/positions/?filter[positions]=only_simple&currency=usd 与 /transactions/（分页 links.next，回溯到 2026-01-01 为止），Alchemy alchemy_getTokenBalances + alchemy_getAssetTransfers（fromBlock 对应 2026-01-01 附近，in/out 两个方向，category external/erc20，withMetadata，分页 pageKey）+ eth_getBalance；DefiLlama coins.llama.fi/prices/current 与 /prices/historical）。先实测看真实 JSON 结构，修解析：余额（Zerion positions quantity.float/numeric, fungible_info.symbol, implementations chain/address；Alchemy hex 余额按 decimals 换算，用 alchemy_getTokenMetadata 取 decimals/symbol，过滤零余额与明显垃圾/spam 代币，Zerion 有 flags.is_trash 也要过滤），转账（Zerion transactions attributes.transfers direction/quantity/fungible_info，operation_type；Alchemy transfers 的 value/asset/hash/uniqueId/metadata.blockTimestamp）。external_id 用 tx hash+log index/uniqueId 保证去重。
   价格：没有 COINGECKO_DEMO_API_KEY 时直接走 DefiLlama，不报错、不打 warning（最多 debug）。DefiLlama 用 coins 格式 ethereum:0x...（ERC20）与 coingecko:ethereum（原生 ETH），历史价用 /prices/historical/{ts}/。稳定币价也走 DefiLlama。
   真实流入流出进待确认队列，带归因建议，去重。同步后生成 admin 快照；回填 admin 历史快照到 2026-01-01（live：当前余额按真实转账倒推每日持仓×DefiLlama 历史价，注意限速，按天批量请求并缓存）。
3) fixtures：capture_fixtures.py 把真实响应脱敏后写入 backend/tests/fixtures/live/（新目录）：去除任何 key/授权头；所有出现的用户地址（大小写不敏感，含 0x 前缀与去前缀 hex 出现在 topics/data 中）替换为 0x1111111111111111111111111111111111111111 之类固定假地址，其他对手方地址也替换为确定性假地址（按出现顺序映射 0x000…0001 等）；tx hash 可保留或哈希化（推荐 sha256 截断伪造）。写完后脚本自检：fixtures 目录 grep 不含真实地址和任何 key，否则报错退出。新增 tests/test_live_fixtures.py：用这些 fixtures 测 Zerion/Alchemy 余额与转账解析、DefiLlama 计价、去重。
4) 跑 `timeout 300 ../.venv/bin/python -m pytest -q`（在 backend 下）到全绿，pytest 必须用 timeout 包裹。不要自行重建 docker；我来做 e2e。最后输出：改动文件列表、原地址归属实现与改法说明、pytest 结果、capture 脚本用法。

```

#### p32_prompt2.txt

```text
在 /workspace/asset-tracker 修三期·2 的两个 live 问题。安全约束同前：绝不 cat/打印 .env 或 ~/.dsh，绝不打印/写入任何 key，地址只能以前6后4掩码出现；不改 frontend/、DELIVERY.md、演示账号、隧道文件；不要重建 docker。

问题1（严重）：admin 实际同步时日志出现 "Zerion balances failed (HTTPStatusError)"，随后快照 breakdown 用的是 mock fixture 余额（zerion ETH 0.25 / USDC 400）。真实用户绝不能静默回落 mock。改为：用户有自己的钱包时，Zerion 余额失败（含 429，先按 Retry-After/指数退避重试）→ 用 Alchemy 真实余额（eth_getBalance + alchemy_getTokenBalances + metadata，过滤垃圾/零余额）；两者都失败 → 本次不写快照，sync_runs 记录 last_status=error/partial 与 last_error（不含 key/地址原文），绝不使用 fixtures。转账历史同理（Zerion 失败 → Alchemy getAssetTransfers）。回填同理。
问题2：admin 名下有真实钱包时，交易所和 Solana 没有 key/地址的源目前用 mock fixtures，混进了 admin 的净值与待确认。改为：mock 只用于"完全没有任何 live 来源的用户"（如 demo）或显式 --mode mock；有 live 钱包的用户，缺 key/地址的源直接跳过（breakdown 里标 skipped 原因）。demo 行为不变。
另：snapshot breakdown 每条持仓增加 "live": true/false 字段，便于证明来源。
新增/修改 pytest：Zerion 429/失败→Alchemy 兜底、双失败不写快照且不读 fixtures、wallet 用户不混 mock 交易所、demo 仍全 mock。
最后在 backend 下 `timeout 300 ../.venv/bin/python -m pytest -q` 全绿，输出改动文件与结果。

```

#### p32_prompt3.txt

```text
项目 /workspace/asset-tracker（FastAPI 后端在 backend/）。三期·2 第三处修复：admin 的 live 同步已经成功（Zerion 实时持仓，fetched=227），但快照 NAV 只有 1331 USDT，而独立 Alchemy+DefiLlama 交叉核对约 9984 USDT。原因是定价只按代币符号查 DefiLlama，导致 PLP-SUSDX-3DEC2026、PT-SUSDX-3DEC2026、YT-USD3-17DEC2026、YT-SUSDX-3DEC2026、SUSDX、PENDLE、WETH、USDT0、CBETH 等都 unpriced。

要求：
1. ChainBalance（及 Alchemy 回退路径）携带 chain 与 contract 地址（Zerion: fungible_info.implementations 中与 relationships.chain 对应的 address；原生币 address 为空）。Zerion 的 chain id 映射到 DefiLlama 链名（ethereum, arbitrum, base, optimism, polygon, binance-smart-chain->bsc, avalanche, plasma, hyperevm, mantle, blast, berachain, monad, xdai 等，未知则跳过）。
2. 实时定价顺序：DefiLlama coins.llama.fi/prices/current/{chain}:{address}（批量）→ 原有 symbol/coingecko 方式 → Zerion 自带的 attributes.price（标记 price_source=zerion）。每条 holding 增加 price_source 字段（defillama_contract / defillama_symbol / zerion / none）。WETH 可映射为 ETH 价，USDT0 视为 USDT 级稳定币仅当 DefiLlama 无价时。
3. 回填（snapshots_backfill）历史价同样优先 DefiLlama historical {chain}:{address}，查不到再用现有方式；查不到就记 unpriced，不要用当前价冒充历史价。
4. 不打印、不记录任何 key 或完整钱包地址；日志里地址遮成前6后4。不改前端、不改演示账号密码、不动 tunnel 相关文件、不 cat .env。
5. 更新 tests/fixtures/live 下的脱敏 fixture（如需要，替换地址/key），并补测试：按合约地址定价、Zerion 价兜底、回填不用当前价。
6. 运行 `cd backend && timeout 300 ../.venv/bin/python -m pytest -q` 全绿。最后简要列出改动文件。

```

#### p32_prompt4.txt

```text
项目 /workspace/asset-tracker（backend/）。小修复：Pendle 收益代币 YT-*-<DDMONYYYY>（如 YT-SUSDAI-20NOV2025）到期后价值为 0，但 DefiLlama 按合约给了约 1.0 的价格，导致 admin NAV 虚高约 2835 USDT。请在实时定价和回填定价（snapshots.py / snapshots_backfill.py / prices service 中合适的一处公共函数）加入规则：资产符号匹配 ^YT-.*-(\d{1,2}[A-Z]{3}\d{4})$ 且到期日 <= 估值日期（实时用今天 UTC，回填用该历史日）时，price=0、priced=true、price_source="expired_yt"。PT 不做处理。补单测（实时与回填各一）。不打印 key/完整地址，不改前端，不 cat .env。最后运行 `cd backend && timeout 300 ../.venv/bin/python -m pytest -q` 全绿并列出改动文件。

```

### 改动摘要

- 钱包归属：新建 `user_wallets` 表。原来 `EVM_ADDRESSES` 是全局配置，所有用户共用同一个地址（demo 也会拉到）。现在启动时只绑定给 `WALLET_OWNER_USERNAME=admin`。同步、快照、回填都通过 `user_addresses(db, user_id, chain)` 只取本人的地址。admin 1 个钱包，demo 0 个。
- Zerion：遇到 429 先按 Retry-After 等，再退避重试。失败时回落到 Alchemy 的真实数据（余额和转账），不再回落 fixture。两边都失败时不写快照，并在 `sync_runs.error` 记录原因。
- 有真实钱包的用户：没有 key 的交易所和没有地址的 Solana 直接跳过，写进 `skipped`，不混入 mock。mock 只给没有任何 live 来源的用户（demo），或显式 `--mode mock`。
- 定价：DefiLlama `coins.llama.fi`，不需要 CoinGecko key。查价顺序：`chain:address` 合约价 → symbol 价 → Zerion 自带价。每条 holding 带 `live` 和 `price_source`。
- 到期的 Pendle YT 按 0 计价（`expired_yt`），否则 YT-SUSDAI-20NOV2025 会让 NAV 虚高约 2835。
- 回填的历史价先按合约查，查不到就记为未定价，不拿当前价冒充。
- 真实流水进 pending，带归因建议，按 `(user, source, external_id)` 去重。
- 日志脱敏：Basic auth、Alchemy `/v2/<KEY>` 打码，地址只保留前 6 后 4。
- 脱敏 fixture：`backend/tests/fixtures/live/`，由 `backend/scripts/capture_fixtures.py` 录制，录制时替换掉 key 和地址。

### pytest

`cd backend && timeout 300 ../.venv/bin/python -m pytest -q`：**62 passed**。

### 证据（Asia/Shanghai 2026-10-23）

- live 同步：`python -m app.sync.cli --user admin`，结果 `mode=live fetched=227 enqueued=0 duplicates=227`，说明去重生效。快照 `nav=10360.55`，58 条持仓，3 条未定价，跳过 6 个来源。状态为 `partial`，只是因为交易所和 Solana 被跳过。
- 回填：2026-01-01 → 2026-10-22，共 295 天，耗时 8 分 12 秒。末日 NAV 10374.95，与 live 一致。
- demo 不受影响：0 个钱包，仍是 mock，15 条 pending。
- 主要持仓：

| 代币 | 数量 | USDT 估值 | 价格来源 |
|---|---|---|---|
| PLP-sUSDx-3DEC2026 | 2768.46 | 6009.21 | DefiLlama 合约价 |
| PT-sUSDx-3DEC2026 | 2538.93 | 2486.68 | DefiLlama 合约价 |
| USDC | 1135.40 | 1134.79 | DefiLlama 合约价 |
| YT-USD3-17DEC2026 | 8591.78 | 182.87 | DefiLlama 合约价 |
| USDT（多链） | 179.63 | 179.45 | DefiLlama 合约价 |
| USDT0（多链） | 180.80 | 180.61 | DefiLlama 合约价 |
| YT-sUSDx-3DEC2026 | 5607.27 | 103.67 | DefiLlama 合约价 |
| sUSDx | 43.20 | 44.89 | DefiLlama 合约价 |
| ETH（多链） | ≈0.00646 | ≈15.4 | DefiLlama symbol 价 |
| PENDLE | 3.60 | 7.37 | DefiLlama 合约价 |
| YT-sUSDai-20NOV2025（已到期） | 2838.61 | 0 | expired_yt |

- 交叉核对：用独立脚本 `/workspace/xcheck.py` 直接查 Alchemy 的 ETH 主网余额（eth_getBalance + getTokenBalances）再按 DefiLlama 定价，合计 ≈ **9984.01 USDT**。各代币的数量和估值逐项一致：PLP 6009.31、PT 2486.65、USDC 1134.76。差额约 376，来自 Zerion 覆盖的其他链（USDT、USDT0 等），Alchemy 只查了主网。

### 已知限制

- Zerion 免费额度会触发 429，已做退避，并以 Alchemy 兜底（只覆盖 ETH 主网）。
- Pendle PT/YT/PLP 依赖 DefiLlama 的合约价。3 个代币未定价：JANE、OGUSDX、积分类代币。
- 历史回填 NAV 由转账倒推，再乘当日历史价，所以中间月份波动较大（例如 2 月约 430、5 月约 8172）。原因是 Pendle 仓位进出，以及部分历史价缺失，查不到的记为未定价、不补当前价。
- admin 没有交易所 key 和 Solana 地址，这两类来源被跳过，不用 mock 填充。
- `JWT_SECRET` 和 `MASTER_KEY` 的 .env 值与一期的开发默认值相同，出现在 config.py、README 和测试里。建议生产环境更换。第三方 API key 和钱包地址在仓库、日志里均 0 命中。

## 三期 · 3 · 待确认页支持选类别（2026-10-23）

### 实际 dsh 命令（原文）

每次先 `set -a && source ~/.dsh/relay.env && set +a`，再 `timeout <秒> dsh --profile headless "$(cat <prompt>)"`。备份：`cd /workspace && tar czf asset-tracker-p32-backup.tgz asset-tracker`。三轮 prompt：

#### p33_prompt.txt

```text
项目 /workspace/asset-tracker。三期·3：待确认页支持选类别。只改前端 frontend/static/app.js 与 styles.css（必要时 index.html），不改后端接口/逻辑，不改演示密码，不动 tunnel 文件，不 cat .env。先读 DELIVERY.md 里审美官 P0–P2 整改（收益页走势/起止净值、待确认实心琥珀确认钮、退出挪进头像菜单、中文小标、tabular-nums、待确认空状态、净值曲线淡面积），所有这些都不能回退；沿用现有 CSS 变量（--accent 琥珀等）和视觉语言，不要模板感。

后端事实：GET /api/pending 返回 PendingOut，含 id,title,detail,amount_usdt,status,created_at,confirmed_at,source,direction,asset,suggested_category,suggested_confidence(0–1 小数或 null),suggested_reason(中文),final_category。POST /api/pending/{id}/confirm 接受可选 body {"category": "..."}，枚举 LEDGER_KINDS=deposit,withdraw,trade,funding,other,airdrop,kol_reward,transfer；无 category 时用 suggested_category（一期老数据 suggested_category 为 null，无 body 也能确认）。GET /api/pending 同时返回 confirmed 项。

需求：
1. 每张待确认卡：标题、金额（tabular-nums）、detail 之外，增加「建议」区：当前选中类别的中文标签 chip（默认=系统建议；无建议则显示「未分类」并提示"请选择类别"）+ 置信度（如 72%，高≥0.8/中≥0.5/低三档用克制的颜色或细条表示，不要花哨）+ suggested_reason 中文理由（最多 2 行，可点开展开）。若用户改过类别，chip 标明「已改为 X」，并可一键恢复建议。
2. 类别中文：deposit 充值、withdraw 提现、transfer 转账、airdrop 空投、kol_reward KOL 奖励、trade 交易、funding 资金费、other 其他收益。选择器：手机（<=640px）用底部抽屉（bottom sheet，带遮罩、拖拽条、ESC/点遮罩关闭、打开时锁 body 滚动、焦点管理、role=dialog aria-modal），列表项每行高度 ≥ 48px，当前项带琥珀勾，建议项带「系统建议」小标；桌面（>640px）用同一抽屉居中/或卡片内分段控件，但每个触控目标高度 ≥ 44px。点击卡片上的类别 chip 或「改类别」按钮打开。选择后只更新该卡片的本地选中状态（不请求后端）。
3. 确认钮保持一期实心琥珀加重样式（高度 ≥ 44px），点击时 POST /pending/{id}/confirm，body 为 {category: 当前选中类别}（无选中且无建议时禁用确认钮并提示先选类别）。成功后 toast，并刷新列表。
4. 已确认项：列表下方增加「已确认」折叠区（默认折叠，显示计数；展开后只读卡片：标题、金额、最终类别 final_category 中文 chip、确认时间 confirmed_at），不可编辑、无确认钮。待处理列表为空但有已确认项时，空状态文案仍可读并显示已确认折叠区。
5. 长内容不撕版：title/detail/reason 用 overflow-wrap:anywhere + 行数限制，长 txid/地址（detail 中 0x… 串）不得横向溢出；detail 默认 3 行，点开展开。admin 有 227 条待确认：首屏渲染前 30 条，底部「再显示 30 条」按钮；摘要行显示总数。
6. 可访问性与细节：按钮 aria-label、抽屉 aria-labelledby、prefers-reduced-motion 下关闭动画、tabular-nums 用于金额/置信度/时间。同一页事件已用事件委托（data-confirm 等），按现有风格新增 data-pick-category / data-sheet-option / data-reset-category / data-more 等，注意 pending 列表重新渲染后保持各卡片已选类别（用 state 里 Map<id, category>，确认成功后清除）。
7. 改完 `node --check frontend/static/app.js`，保证登录/总览/收益页不受影响。最后列出改动文件与新增的 data-* 钩子名。

```

#### p33_prompt2.txt

```text
项目 /workspace/asset-tracker，只改前端 frontend/static/app.js（必要时 styles.css），不改后端、不改演示密码、不动 tunnel、不 cat .env。
问题：待确认页 detail（以及任何展示用文本，含标题、建议理由、已确认卡）里后端存的是完整钱包地址（0x+40位十六进制）和完整 txid（0x+64位），截图和展示里不应出现完整地址。
请在前端渲染层加一个统一的展示函数（如 shortenHex），把文本中所有 /0x[0-9a-fA-F]{12,}/g 的串显示成「前6后4」形式（如 0x1d72…e916，用 … 连接），在 escapeHtml 之前或之后都要保证安全不引入 XSS；用于待确认卡 title/detail/suggested_reason 以及已确认卡。已缩写过的串（含 …）不受影响。保留 detail 展开/折叠逻辑与长文本不撕版。不要在 DOM 的 title/data-* 属性里保存完整串。最后 node --check frontend/static/app.js，并说明改动位置。

```

#### p33_prompt3.txt

```text
frontend/static/app.js 的 shortenHex 有 bug：文本里含「…」就整段不处理，但同一段 detail 里可能既有已缩写地址又有完整 txid。请删掉这个 includes("…") 提前返回（正则 0x+12位以上十六进制本身不会匹配已缩写串），同步修正注释。只改这一处，node --check 通过后简述。

```

### 改动摘要

只改前端 `frontend/static/app.js`、`frontend/static/styles.css`，后端和接口没动。
- 每张待确认卡显示系统建议类别 chip、置信度（细条加百分比，高 ≥0.8 / 中 ≥0.5 / 低三档）和中文理由（默认两行，可展开）。没有建议的一期老数据显示「未分类」，提示先选类别，确认钮禁用。
- 改类别：点 chip 或「改类别」，手机端底部抽屉（遮罩、拖拽条、ESC/点遮罩关闭、锁滚动、焦点管理，`role=dialog aria-modal`），桌面端居中弹层。选项行高 48px，当前项带琥珀勾，建议项标「系统建议」。选择只改本地状态，卡片显示「已改为 X」，可「恢复建议」。
- 确认钮仍是实心琥珀，高 44px，点击 `POST /api/pending/{id}/confirm`，body `{category}`。
- 已确认项放在列表下方的折叠区，只读，展示最终类别和确认时间，没有确认钮和改类别入口。
- 长内容：标题、详情、理由用 `overflow-wrap:anywhere` 加行数折叠，详情可展开。待确认首屏渲染 30 条，「再显示 30 条」继续加载。
- 展示层缩写：后端 detail 里存的是完整钱包地址和 txid，前端统一用 `shortenHex` 缩成前 6 后 4（`0x1d72…e916`）。仅展示层，后端数据不动，缩写后的完整串不进 DOM 属性。
- 新增 data-* 钩子：`data-pick-category`、`data-sheet-option`、`data-sheet-close`、`data-reset-category`、`data-more`、`data-pending-more`、`data-toggle-confirmed`。

### 测试结果

- `cd backend && timeout 300 ../.venv/bin/python -m pytest -q`：62 passed（后端没动，无回归）。
- `node --check frontend/static/app.js` 通过。
- `docker compose up -d --build` 后 app / worker / postgres / redis 均 healthy，`/static/app.js` 已含新钩子。
- Playwright（`/tmp/shot-pending-cat.mjs`，Chrome 无头，390px 触屏）真实操作 demo 账号：登录 → 待确认 → 打开抽屉（ESC 可关）→ 换成「转账」→ 出现「已改为 转账」→ 确认。请求为 `POST /api/pending/<id>/confirm body={"category":"transfer"}`；该卡从待处理列表消失，已确认折叠区里显示只读「最终类别 转账」，没有确认钮。
- 触控尺寸：确认钮 44px、类别按钮 44px、抽屉选项 48px。390px 和 1280px 下 `scrollWidth == innerWidth`，无横向溢出。
- 页面文本中检测不到完整地址：demo 和 admin（30 条，再加载后 60 条）均为 0。
- 本地和公网（当前 `TUNNEL_URL.txt` 里的地址）访问 `/`、`/pending`、`/static/app.js`、`/static/styles.css`、`/api/health` 均 200。
- 一期/二期接口回归：admin 和 demo 的 overview / returns / sync/status / pending 均 200。
- 确认测试只用了临时造的 demo 待确认项（`source=p33-test`）。测完已删掉这些行和它们生成的 ledger 记录。admin 仍是 227 条 pending，没有被确认；demo 仍是 12 条 pending、3 条 confirmed。

### 截图（screenshots/）

- `pending-category-mobile.png`（390px，列表）
- `pending-category-sheet-mobile.png`（抽屉打开）
- `pending-category-changed-mobile.png`（已改为转账）
- `pending-category-confirmed-mobile.png`（已确认只读，最终类别）
- `pending-category-desktop.png`、`pending-category-sheet-desktop.png`
- `pending-category-admin-mobile.png`（admin 真实项，只读，地址已缩写，未做任何确认）

### 已知限制

- 桌面端沿用一期的居中窄栏（约 384px），没有做宽屏多栏。
- 已确认时间沿用现有 UTC 格式，没有按本地时区转换。
- 抽屉的拖拽条目前只是视觉标识，不支持拖拽关闭，关闭靠遮罩、ESC 和「关闭」按钮。
- 一期老数据没有建议类别，必须手选才能确认，这是有意设计。
- admin 的 227 条真实待确认全部带建议类别，但 live 项的详情很长，展开前只显示 3 行。
- 地址缩写只在前端展示层做，API 返回的 detail 仍含完整地址。
- `TUNNEL_URL.txt` 在本轮期间被隧道脚本自己改写过（12:14），我没动过隧道文件，公网检查用的是新地址。

### 三期 · 3 返工（审美官「有条件过」，2026-10-23 12:3x–12:5x，UTC+8）

只改 frontend/static/app.js、styles.css，后端未动。dsh 命令：

```
set -a && source ~/.dsh/relay.env && set +a && cd /workspace/asset-tracker && timeout 1500 dsh --profile headless "$(cat /workspace/p33_prompt4.txt)" > /workspace/p33_dsh4.log 2>&1
```

p33_prompt4 原文：

```
项目 /workspace/asset-tracker。三期·3 待确认页，审美官「有条件过」，请按下列意见全部返工。只改 frontend/static/app.js、styles.css（必要时 index.html），后端不动，不改演示密码，不动 tunnel，不 cat .env。一期审美官 P0–P2（收益页走势/起止净值、实心琥珀确认钮、退出在头像菜单、中文小标、tabular-nums、空状态、净值曲线淡面积）不得回退。保留现有 data-* 钩子与行为（改类别只改本地状态、确认 POST {category}、已确认折叠区、再显示30条、shortenHex 地址缩写）。

P0
1. 改类别后数据自相矛盾：已改为「转账」，旁边仍是 87% 置信度，理由还写「→ 提现」。改法：用户改了类别（选中 != 系统建议）后，把置信条和理由整体降为 50% 透明度，并加一行「系统原建议：提现 · 87%」，「恢复建议」放这行右侧；类别行旁不再显示百分比。无建议的项（一期老数据）改类别后不显示这行。
2. 点击对象反了：「类别 提现」做成大号描边 pill 像按钮，真正可点的「改类别」却是灰色小字、点击区远小于 44px。改法：整行（类别名 + chevron）做成一个 44px 高的可点行（button，data-pick-category），右侧加 ▾；删掉独立的「改类别」文字按钮和描边 pill。
3. 卡片层层嵌套、太长：改法：去掉内层 .suggest 卡的边框和底色，类别行改为卡内一条 hairline 分隔的普通行；「展开详情」「展开理由」合并成一个「展开 / 收起」（data-more 统一，详情+理由一起展开，状态存同一个 key）；理由默认只留一行摘要，但不要在「建议人工复核后确认类别…」这类关键句中间硬截断：默认显示理由的第一句（按 。；; 切分取首句，首句过长才用 CSS 单行省略），展开后显示全文。
P1
4. 琥珀用得太多：琥珀只留给确认钮和当前选中态（抽屉里的当前选中项、勾）；金额改 var(--text) 白色（tabular-nums），类别名用 text 色，「已改为」不用琥珀。
5. 标题里直接出 18 位小数（如「zerion 充值 3447.860021689532217300 JANE」）：标题展示时把标题中长小数（\d+\.\d{5,}）截到最多 4 位小数（去掉末尾多余 0），完整数值放进展开后的详情里（在展开区开头加一行「数量 完整值 资产」，用 item.amount_raw/asset 或从原标题提取；缩写地址规则不变）；标题最多两行（line-clamp 2）。
6. 置信条低档用红色，与「跌」的 negative 红语义冲突，且条太短、百分比字号偏小：改法：低档(<0.5) 用 muted 灰 + 文案「建议核对」；中档琥珀淡色（opacity 降低）；高档薄荷色(--positive)；条加长到约 48px 宽、4px 高；百分比字号 ≥12px 且保证对比度。
7. 已确认区「最终类别」不要大号描边 pill：改成无边框小标签（chip，小号，淡底可有可无但无描边），未分类用 muted 灰，不用琥珀。
P2
8. 类别行底边与下方 hairline 分隔线几乎贴在一起：去掉其一，间距留 12px。
9. 抽屉：.sheet 设 max-height 约 80vh（使用 80dvh 兜底 vh），内部列表可滚动，底部 padding 含 env(safe-area-inset-bottom)；选中勾必须跟着「用户当前选择」走，「系统建议」小标留在原建议项上，两者分开渲染（选了别的项后，勾在新项，小标仍在原建议项）。
10. 底栏盖住最后一张卡：确认页滚到底时，页面底部 padding 必须 ≥ 底栏高度 + safe-area + 16px，保证最后一张卡完整可见、底栏不遮挡。
11. 未分类时确认钮禁用：禁用态要有一句说明为什么不可点（卡内或确认钮上方清晰可读的 muted 文案「请先选择类别，再确认归档」，不是只变灰），确认钮 aria-disabled/disabled，保持 ≥44px，禁用态的琥珀降为低饱和但仍可识别。

其他：触控目标仍 ≥44px；prefers-reduced-motion 关动画；长地址不撕版；390px 与 1280px 都不横向溢出。改完 node --check frontend/static/app.js，列出改动位置，并逐条 1–11 说明怎么处理。
```

处理对照（1–11）：

| # | 处理 |
|---|---|
| 1 | 选中≠系统建议时置信条/理由整体 50% 透明；新增「系统原建议：提现 · 87%」行，右侧「恢复建议」；类别行旁不再显示百分比 |
| 2 | 整行（类别名+▾）= 一个 44px 的 button[data-pick-category]；删掉描边 pill 与「改类别」小字 |
| 3 | 去掉内层卡边框/底色，类别行为卡内 hairline 行；两个展开合并为一个「展开/收起」；理由默认只显示首句（按 。；; 切分），不在关键句中截断 |
| 4 | 琥珀仅留确认钮与抽屉选中态；金额 var(--text)+tabular-nums；类别名正文色 |
| 5 | 标题小数最多 4 位（3447.86），完整数量在展开详情首行「数量 …」；标题 line-clamp 2 |
| 6 | 低档=灰+「建议核对」，中档=淡琥珀，高档=薄荷；条 48×4px；百分比 12px |
| 7 | 已确认「最终类别」改无边框小 chip；未分类 muted 灰 |
| 8 | 去掉紧贴的分隔线，保留 12px 间距 |
| 9 | 抽屉 max-height min(80vh,80dvh)、列表内滚、底部含 safe-area；勾跟当前选择，「系统建议」留在原建议项（已验证：改成转账后勾在转账、标签仍在提现） |
| 10 | 之前底栏遮挡的主因是截图停留位置，同时底距不够；`.page` 底距加到 148px+safe-area（底栏约 94px）。滚到底后 viewport 实测最后一张卡底 696 < 底栏顶 768（视口 844） |
| 11 | 未分类时确认钮 disabled（≥44px，低饱和琥珀），上方文案「请先选择类别，再确认归档」+aria-describedby；新增 pending-category-disabled-mobile.png |

验收：pytest 62 passed；node --check 通过；四服务 healthy；本地与公网 200；Playwright e2e（390px/1280px，临时数据 source='p33-test'，标题不含「P33测试」）：改类别→POST /confirm {"category":"transfer"}→进已确认区只读 chip；无横向溢出；无完整 0x 地址；确认钮/类别行 44px、抽屉项 48px；admin 227 待确认未动、demo 12 待/3 已确认/ledger 6 条已恢复，临时数据与其 ledger 行已清理。

截图（screenshots/）：pending-category-mobile.png、-changed-mobile.png、-confirmed-mobile.png（滚到底后截 viewport）、-disabled-mobile.png（新）、-sheet-mobile.png、-sheet-desktop.png、-desktop.png、-admin-mobile.png。

#### 三期 · 3 返工二（审美官复审「过」，收 3 条 P2）

dsh 命令：

```
set -a && source ~/.dsh/relay.env && set +a && cd /workspace/asset-tracker && timeout 1200 dsh --profile headless "$(cat /workspace/p33_prompt5.txt)" > /workspace/p33_dsh5.log 2>&1
```

p33_prompt5 原文：

```
项目 /workspace/asset-tracker，三期·3 待确认页，审美官复审「过」，收 3 条不阻塞 P2。只改 frontend/static/styles.css、app.js（后端不动，不 cat .env，不改演示密码，不动 tunnel）。其余已通过的样式与行为一律不回退（44px 类别行、系统原建议行、置信档位、抽屉、已确认只读 chip、shortenHex 等）。

1. 左边缘不齐：「展开」缩进约 2px、已确认里「未分类」缩进约 9px，而标题左缘在 x=50（390px 视口）。统一对齐到标题左缘：待确认卡和已确认卡里的「展开/收起」、已确认 chip（含「未分类」）、日期等，视觉左缘都要与标题文字左缘一致。chip 有内边距时用负 margin-left 抵消内边距（让文字对齐而不是 chip 底色边缘），或让「未分类」也走与「转账/空投」同样的 chip 样式，保持一致。
2. 「展开」是低对比小灰字、点击区偏小：给 .text-toggle 加 padding 使可点击高度 ≥ 40px（建议 min-height:40px，display:inline-flex; align-items:center），字色提到 muted 以上一档（比现在更亮，如 var(--text) 的 ~75% 或现有 --muted 再亮一点），字号 ≥13px。注意用负 margin 抵消内边距使文字左缘仍与标题对齐（与第 1 条同时满足），不要因为增高又把卡片撑回老高度：必要时用负 margin-top/bottom 抵消多出的 padding。
3. 「展开」与确认钮之间有约 40px 空带，日期与确认钮同行靠下对齐：把 .pending-foot 的上边距压到 12–16px，去掉多余的 min-height/padding/gap；日期与确认钮垂直居中或底对齐都行但要同一行。已确认卡的 foot 同理。

改完 node --check frontend/static/app.js，并列出改动，逐条说明。
```

处理：
1. `.more-toggle` 左右 padding 10px + margin-left -10px；`.done-chip`（含「未分类」，统一淡底）padding 2px 8px + margin-left -8px。实测（390px）标题/展开/已确认 chip 文字左缘均为 x=41（CSS px），三者对齐。
2. 「展开」min-height 40px、13px、颜色 text 75%；上下 margin -10px 抵消，卡高不回弹。
3. `.pending-foot` 去掉 min-height，align-items:center，gap 12px，margin-top 14px；已确认 `.done-row` margin-top 14px。

验收：pytest 62 passed；node --check 通过；compose 四服务 healthy；本地/公网 200；确认流程 e2e 再跑通过（临时数据已清理；admin 227 待确认未动，demo 12 待/3 已确认/ledger 6 恢复）；重拍 pending-category-mobile/changed/confirmed（另顺带重拍 disabled）。

## 三期·4：交易所 live 联调（Binance + Gate）

日期：2026-10-23（UTC+8）

### dsh 命令（原文）

```bash
set -a && source ~/.dsh/relay.env && set +a
cd /workspace/asset-tracker
timeout 3000 dsh --profile headless "$(cat /workspace/p34_prompt1.txt)" > /workspace/p34_dsh1.log 2>&1
timeout 3300 dsh --profile headless "$(cat /workspace/p34_prompt2.txt)" > /workspace/p34_dsh2.log 2>&1
```

完整提示词见 `/workspace/p34_prompt1.txt`、`/workspace/p34_prompt2.txt`（归属、窗口分页、失败隔离、只读 allowlist、fixtures、测试等）。备份：`/workspace/asset-tracker-p33-backup.tgz`（13:09）。

### 归属

- 新增 `EXCHANGE_OWNER_USERNAME=admin`（`.env.example` 已写）。
- `user_exchange_credentials(db, user_id)`：仅 owner 拿到 `exchange_credentials()`；demo 恒为 `{}`，继续走 mock。
- sync / snapshot / backfill 均按用户传入 creds，不再用全局 key 污染 demo。

### 改动摘要

- `config.py`：`exchange_owner_username`；`has_live_credentials` 不再因交易所 key 把 demo 推成 live。
- `wallets.py`：`user_exchange_credentials`。
- `integrations/exchanges/ccxt_adapter.py`：分窗口充提/成交（Gate ≤30 天、until 秒；Binance ≤89 天、until 毫秒；Binance trades `fromId`；Gate trades 可无 symbol）；状态仅 `ok` 入队；`LD*` 折算；配置化 `EXCHANGE_PROFILES`。
- `errors.py` + `retry.py`：`describe_exchange_error`（http/code/msg，去 query/signature）；451/权限类不重试。
- `attribution.py` + `sync/service.py`：交易所提现到自有地址 / 同 txid 链上腿 → `transfer`（`linked_txids`）。
- `snapshots*.py` / `scheduler.py`：按用户 creds；空 key → skipped（status 仍 ok）；有 key 失败 → problems（partial）；失败交易所可沿用上一非 seed 快照（stale）。
- `logging_redact.py`：signature/timestamp 与内存中的 key 值脱敏。
- `scripts/capture_exchange_fixtures.py` + `tests/fixtures/exchanges_live/`。
- 测试：`test_exchange_live.py`、`test_exchange_readonly.py`，以及 exchanges/snapshots 等回归。

### 验收结果

| 项 | 结果 |
|---|---|
| pytest | **92 passed**（`cd backend && .venv/bin/python -m pytest -q`） |
| 四服务 | app / worker / postgres / redis 均 healthy（`docker compose up -d --build`） |
| 归属 | admin_creds=`binance,gate`；demo_creds=`[]` |
| live 同步（admin） | Gate 成功；Binance 失败（见下）；EVM 不受影响 |
| 二次同步 | fetched=245, enqueued=0, **duplicates=245** |
| demo | sync status=ok，mock 余额（gate USDT=10），pending 12/confirmed 3 未染 live |
| 泄漏扫描 | **0 matches**（脚本搜 `.env` 内 API_KEY/SECRET 值于仓库/日志/DELIVERY/容器日志/pg_dump/fixtures；不打印值） |

### Gate 主要余额与 USDT 估值（admin live，无 key）

| 资产 | 数量 | ≈USDT |
|---|---|---|
| USDT | 14.18603252313 | 14.19 |
| USDC | 1.33880248 | 1.34 |
| SOL | 0.05173458 | 5.41 |
| GT / GT2 / MON / FF 等 | 少量 | 未定价或≈0 |

（NAV 快照合并 Gate + Zerion EVM；admin sync NAV ≈ 10379 USDT。）

### Binance 确切错误（本机出口，不绕过）

- HTTP **451**
- body 原文：`{"code": 0, "msg": "Service unavailable from a restricted location according to 'b. Eligibility' in https://www.binance.com/en/terms. Please contact customer service if you believe you received this message in error."}`
- 记入 sync_runs（partial）：`binance: ExchangeNotAvailable http=451 code=0 msg="Service unavailable from a restricted location according to 'b. Eligibility' in https://www.binance.com/en/terms. Please contact customer service if you believe you received this message in error."`
- 同 run 的 skipped：`bitget (no api key); bybit (no api key); kraken (no api key); solana (no wallet address)`（空 key 不拉高为 error）
- 未使用代理 / 未换域名 / 未重试绕过。

### 交易所↔自有地址

- Gate 充提中已有条目归因 `transfer`（与自有钱包地址/同 txid 匹配）；孤立充提仍为 deposit/withdraw。
- TWR 仍把 deposit/withdraw/transfer 当外部流；双腿 transfer 净额为 0（单测覆盖）。

### 已知限制

- Binance 本环境 HTTP 451，无法取 live 余额/充提/成交；逻辑与 `_synthetic` fixtures 单测覆盖。
- Binance 成交候选集有上限；卖光且无充提记录的币、Convert/Dust/Earn 可能漏。
- 回填未在本轮长时间重跑（交付优先；窗口逻辑已测）；需要时再对 admin `--backfill`。
- 只读 allowlist 扫调用形态；注释已排除。

## 三期·5：JWT_SECRET / MASTER_KEY 轮换

日期：2026-10-23

### 做了什么

- 生成新的 `JWT_SECRET` 与 `MASTER_KEY` 写入部署用 `.env`（本仓库不记录值）。**密钥已轮换，值不入库。**
- `api_credentials` 用旧 MASTER_KEY 解密、新 MASTER_KEY 重加密（AES-256-GCM，nonce 12 字节前缀，AD=`asset-tracker/api-credential/v1`）。已是新密钥的行跳过；旧/新都解不开则整批回滚。
- 启动告警：`warn_if_default_secrets` 在 lifespan 里、`wait_for_db()` 之前调用。当前 settings 仍等于开发默认时，对 `JWT_SECRET` / `MASTER_KEY` 各打一条 WARNING，日志不含密钥明文。
- 迁移脚本：`backend/scripts/rotate_master_key.py`（`python -m scripts.rotate_master_key`）。密钥只来自 `--old-key` / `--new-key`，或环境变量 `OLD_MASTER_KEY` / `NEW_MASTER_KEY`（CLI 优先）。可选 `--database-url`（默认 `DATABASE_URL`）、`--dry-run`、`--yes`。成功打印 `migrated=N skipped=M total=T`。
- 加解密纯函数 `encrypt_with_key` / `decrypt_with_key` 在 `crypto.py`；`encrypt_text` / `decrypt_text` 继续走 `settings.master_key`。

### 用法（值用占位，不要把真实密钥写进仓库）

```bash
cd /workspace/asset-tracker/backend
python -m scripts.rotate_master_key --old-key <旧 64 hex> --new-key <新 64 hex> --dry-run
python -m scripts.rotate_master_key --old-key <旧 64 hex> --new-key <新 64 hex> --yes
# 然后把新 JWT_SECRET / MASTER_KEY 写入 .env，再：
docker compose up -d --force-recreate app worker
```

生成：`openssl rand -hex 32`（MASTER_KEY）；JWT 可用 `openssl rand -base64 48` 或 hex。

### 验收

| 命令 | 预期 |
|---|---|
| `curl -fsS http://127.0.0.1:8000/api/health` | `{"status":"ok"}` |
| `POST /api/auth/login` admin / demo | 200，返回 access_token（演示密码不变） |
| `cd backend && ../.venv/bin/python -m pytest -q` | **98 passed**（含默认密钥 WARNING 与轮换幂等/回滚；较三期·4 的 92 新增 6） |
| `stat -c %a .env` | `600` |

### 回滚

1. 用 `/workspace/db-before-p35.sql` 恢复数据库（凭证仍是轮换前的密文）。
2. 从备份恢复轮换前的 `.env`（含旧 `JWT_SECRET` / `MASTER_KEY`）。不要把密钥贴进本文档或工单。
3. 重启 app / worker，使进程重新读旧 `.env`。

## 三期·6：微信 ClawBot 推送骨架

日期：2026-10-23

### 做了什么

- 新的待确认在同步事务里入队（`wechat_push_queue`），提交后再冲刷一次。失败只记日志，不回滚 pending。
- `backend/app/integrations/wechat/`：`client`（live / mock）、`queue`、`commands`、`service`（从 `routers/pending.py` 抽出的确认逻辑）、`poll`、`worker`。
- mock：不发 HTTP，日志 `wechat mock send pending_id=…`，队列 status=`sent`，`last_error=mock`。
- live：`POST https://ilinkai.weixin.qq.com/ilink/bot/sendmessage`。没有 context token、`ret=-2` 或 HTTP 错误时留在队列（`attempts` 增加，满 5 次标 `failed`），error 里不含 token。
- 入站（默认关）：刷新 `wechat_context_tokens`，冲刷该用户队列，解析「确认 #id 类别」。解析失败只回用法，不改库。
- worker：`sync/worker_main.py` 在 scheduler 之后调用 `start_wechat_background()`。总是起 `wechat-flush`（默认 30 秒）；仅 `WECHAT_POLL_ENABLED=1` 且 mode=live 且有 token 才起 `wechat-poll`。
- **token 不入库。** `WECHAT_BOT_TOKEN` 只在 `.env`。`context_token` 只在服务端缓存表 `wechat_context_tokens`，不进日志、不进聊天回显。

### 配置

| 变量 | 含义 |
| --- | --- |
| `WECHAT_BOT_TOKEN` | 可空。空则不能 live |
| `WECHAT_PUSH_MODE` | `mock` / `live` / `auto`（默认）。`auto`：非空 token → live，否则 mock。显式 `mock` 覆盖 token |
| `WECHAT_POLL_ENABLED` | `0`/`1`，默认 `0` |
| `WECHAT_ILINK_BASEURL` | iLink 地址，空则用默认 `https://ilinkai.weixin.qq.com` |
| `WECHAT_ILINK_USER_ID` / `WECHAT_ILINK_BOT_ID` | 扫码后得到的用户 / Bot id，发送时作 `to_user_id`。只在 `.env`，日志脱敏 |
| `WECHAT_DEFAULT_USERNAME` | 入站骨架绑定的用户名，默认 `admin`。后期可做绑定表 |
| `WECHAT_DEFAULT_USER_ID` | 可选，优先于用户名 |
| `WECHAT_FLUSH_INTERVAL_SECONDS` | flush 线程间隔，默认 30 |

### 上线步骤

1. `.env` 填写 `WECHAT_BOT_TOKEN`（不要提交、不要写进本文档）。`chmod 600 .env`。
2. `WECHAT_PUSH_MODE=auto`（或 `live`）。先保持 `WECHAT_POLL_ENABLED=0`，确认进程能起来。
3. 用户先给 Bot 发一句任意文本，让服务端拿到 context token。
4. 再把 `WECHAT_POLL_ENABLED=1`，重启 worker。之后新 pending 会推送；积压在用户开口后补发。回复「确认 #123 空投」或「123 充值」即可归档。

### 验收

```bash
cd /workspace/asset-tracker/backend && ../.venv/bin/python -m pytest -q
```

### 实机接入（已启用 live + poll）

- 第一次主动推送可能没有 context_token（用户必须先给 Bot 发一条消息）。此时条目留在队列，属预期；用户发消息后 poller 刷新 context_token、冲刷队列，并回「已连通，之后有待确认会推给你。」。
- context_token 过期（ret=-2）同样留队列，等用户再发一句。
- bot_token / ilink id 不入库、不进仓库、不进日志，只在 `.env`（600）。
- 重建：`docker compose up -d --build --force-recreate app worker`。

测试固定 `WECHAT_PUSH_MODE=mock`、空 token、poll 关闭，不打 ilink。health 与同步路径不变。

## 三期·7：收益统计起点（returns baseline）

日期：2026-10-23

### 做了什么

- `users.returns_start_date`（DATE，可空）。`migrate.py` 幂等：`ADD COLUMN IF NOT EXISTS`，并只在 admin 为空时写入 `2026-10-23`，不覆盖已有值。demo 保持 NULL。全新库由 `seed.py` 给 admin 写同一日期。
- `/api/returns`：设了起点就只用 `>= 起点当天 00:00（Asia/Shanghai）` 的快照。起点当天第一条快照是基准，收益 0%。TWR、收益额、收益率都只看起点之后。起点之后已确认的充提仍然剔除。
- 日/周/月/年的区间若伸到起点之前，会截到起点后的第一条快照，并返回 `window_clamped=true` 与 `effective_start_ts`（前端可提示「从 2026-10-23 起算」）。起点晚于所有快照时返回空 series、收益 0。
- 响应只加字段：`/api/returns` 新增 `returns_start`、`window_clamped`、`effective_start_ts`；`/api/overview` 新增 `returns_start`。`/api/overview` 的净值曲线仍返回完整历史。
- 没有界面改动。没有动微信、交易所、JWT/MASTER_KEY、演示密码。

### 验证

```bash
cd /workspace/asset-tracker/backend && ../.venv/bin/python -m pytest -q   # 112 passed（含 tests/test_returns_start.py）
docker compose up -d --build --force-recreate app worker
# 登录后：GET /api/returns?period=day|week|month|year，GET /api/overview
```

- demo：改动前后 `/api/returns`（四个 period）与 `/api/overview` 逐字段一致。
- admin：`returns_start` 为 `2026-10-23`，收益只从起点后的快照算。

### 备份与 dsh

- 改前备份：`/workspace/db-before-p37.sql`（600）。
- 本轮改动量小，代码手写，没有调用 dsh。

## 三期·8：币安经 CoinStats Open API 中转

日期：2026-10-23

### 为什么

共享电脑出口在美国，币安 API 直连返回 451。改为：币安只读 key 提交给 CoinStats，由 CoinStats 的服务器去拉币安，我们只和 `https://api.coinstats.app` 通信（请求头 `X-API-KEY`）。**每个用户用自己的 CoinStats key**（去 https://openapi.coinstats.app 自己申请，免费档 20000 credits/月、2 请求/秒），不共用。

### 做了什么

- 币安适配器 `backend/app/integrations/exchanges/coinstats_adapter.py`：`CoinStatsBinanceAdapter` 输出现有 `NormalizedBalance / Transfer / Trade`，走原有 `sync/service` 的去重、待确认和净值快照。
  - 余额 `POST /v1/exchange/balance`（10 credits）；交易 `PATCH /v1/exchange/sync`（20）→ 轮询 `GET /v1/exchange/status` → `GET /v1/exchange/transactions`（4/页）；额度 `GET /v1/usage/credits`（免费）。
  - 限流：请求间隔 ≥0.55 秒；429 按 0.5/1/2/4 秒退避（尊重 `Retry-After`），耗尽后给出人话错误。
  - 同步间隔：CoinStats 的结果在进程内缓存 `COINSTATS_SYNC_INTERVAL_SECONDS`（默认 21600 = 6 小时，可配）。调度仍是每 30 分钟，但 6 小时内不会重复花 credits。粗算每次约 35–40 credits，每天 4 次约 4000 credits/月。
  - 选择逻辑 `integrations/exchanges/factory.py`：该用户配了 CoinStats key 且有币安 key 时，币安走 CoinStats，服务器不再直连币安；其他交易所不变。
- 存储：CoinStats key 作为 provider `coinstats` 加密存 `api_credentials`（AES-GCM，同现有 MASTER_KEY）。`backend/app/coinstats_keys.py`。没有新表，没有迁移。币安 key 沿用现有（admin 已配），不动。
- 后端 API（登录后只管本人）：
  - `GET /api/coinstats/key` → `{configured, last4}`
  - `PUT /api/coinstats/key` body `{"api_key": "..."}` → 同上（保存时不联网校验）
  - `DELETE /api/coinstats/key`
  - `GET /api/coinstats/credits` → `{configured, total, used, remaining, subscription, last_sync_credits}`
- 错误人话化（同步失败原因里会看到）：CoinStats key 无效（401/403）、429 太频繁、币安 key 被拒绝、币安 IP 白名单拒绝、超时、CoinStats 同步中、CoinStats 服务不可用。文案不含任何 key。
- 新增环境变量（均可选）：`COINSTATS_BASE_URL`、`COINSTATS_TIMEOUT_SECONDS`（20）、`COINSTATS_SYNC_INTERVAL_SECONDS`（21600）。
- 没有界面改动，没有动微信、收益起点、JWT/MASTER_KEY、演示密码、隧道。

### 币安 IP 白名单怎么处理（请先读）

CoinStats 官方的说法：它的服务器**出口 IP 不固定、没有公开的固定 IP 列表**，所以官方要求币安只读 key 选「**不限制 IP**」（币安的系统生成只读 key 本来就可以不限 IP）。排查办法：

1. 优先：在币安「API 管理」里把这个只读 key 的 IP 访问限制改为「不限制」，权限只开「读取」。
2. 如果必须保留白名单：先触发一次同步，失败信息里会带币安/CoinStats 返回的请求 IP（形如 `request ip: x.x.x.x` 或 `IP[x.x.x.x] not in whitelist`，我们会解析出来写进人话错误）。把这个 IP 加入白名单。注意它可能会变，会反复失败。
3. 确认 CoinStats 当前出口：向 CoinStats 在线客服 / support 询问，或以上面失败信息里的 IP 为准。

### 叉叉 / 朋友的操作步骤（没有密钥）

1. 去 https://openapi.coinstats.app 注册，创建自己的 API key（免费档即可）。
2. 币安只读 key：权限只开读取，IP 选不限制（见上）。admin 的币安 key 已经在服务器上，不用再动。
3. 登录资产追踪，用接口保存自己的 CoinStats key（界面尚未提供，先用接口，示例里 `$TOKEN` 是登录得到的 token）：
   ```bash
   curl -s -X PUT http://127.0.0.1:8000/api/coinstats/key \
     -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d '{"api_key":"<你的 CoinStats key>"}'
   ```
4. 查看剩余额度：`curl -s http://127.0.0.1:8000/api/coinstats/credits -H "Authorization: Bearer $TOKEN"`。
5. 删除：`curl -s -X DELETE http://127.0.0.1:8000/api/coinstats/key -H "Authorization: Bearer $TOKEN"`。
6. 目前币安 key 仍只有 `EXCHANGE_OWNER_USERNAME`（admin）那一份；朋友自己的币安 key 保存入口不在本期范围。

### 联调命令（等拿到真 CoinStats key 再跑；key 只通过 stdin/环境变量，不进命令历史和日志）

```bash
cd /workspace/asset-tracker
TOKEN=$(curl -s -X POST http://127.0.0.1:8000/api/auth/login -H 'Content-Type: application/json' -d '{"username":"admin","password":"admin123"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
# 保存（key 放在环境变量 CS_KEY 里）
python3 -c 'import json,os;print(json.dumps({"api_key":os.environ["CS_KEY"]}))' | curl -s -X PUT http://127.0.0.1:8000/api/coinstats/key -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d @-
curl -s http://127.0.0.1:8000/api/coinstats/credits -H "Authorization: Bearer $TOKEN"
curl -s -X POST "http://127.0.0.1:8000/api/sync?mode=live" -H "Authorization: Bearer $TOKEN"   # 一次同步，约 35–40 credits
curl -s http://127.0.0.1:8000/api/sync/status -H "Authorization: Bearer $TOKEN"                  # last_error 里看人话错误
curl -s http://127.0.0.1:8000/api/coinstats/credits -H "Authorization: Bearer $TOKEN"            # 对比 used 变化
```

### 验证与开发方式

```bash
cd /workspace/asset-tracker/backend && ../.venv/bin/python -m pytest -q   # 129 passed（原 112，+17：适配器 10、接口 7）
docker compose up -d --build --force-recreate app worker
```

- 测试全 mock（`httpx.MockTransport` 与 monkeypatch），不打外网，没有用真 key。
- 备份：`/workspace/db-before-p38.sql`（600）。本轮没有数据库结构变更。
- 本轮用 dsh 并行（各 900 秒上限，后台）：路线 1 适配器与测试、路线 2 key 管理接口与测试。公共契约 `/workspace/p38_contract.md` 和 `coinstats_keys.py` 由我手写。命令：
  `set -a && source ~/.dsh/relay.env && set +a && cd /workspace/asset-tracker && (timeout 900 dsh --profile headless "$(cat /workspace/p38_prompt_r1.txt)" > /workspace/p38_dsh_r1.log 2>&1; echo exit:$? >> /workspace/p38_dsh_r1.log) &`（路线 2 同理，prompt 为 `p38_prompt_r2.txt`，日志 `p38_dsh_r2.log`）。
- 先前一版方案（本地同步脚本 / ingest / 同步令牌）已作废并整体回滚，代码里没有任何残留。

### 净值显示修正（联调后）

`select_nav_snapshots`：只要存在 `source=sync` 的快照，曲线止于最新 sync，其后的 backfill 不再盖住 overview / returns 的当前净值；无 sync 时仍用最新 backfill。`_previous_holdings` 同样优先取最新 sync。

## 三期·9：数据源设置页（CoinStats 配置）

纯前端改动（`frontend/static/app.js`、`frontend/static/styles.css`），后端业务代码与接口未改；`/settings` 由已有的 SPA 兜底返回 `index.html`（200）。

- **入口**：头像菜单「退出登录」上方新增「数据源设置」，底栏仍是 3 个 tab，没有第 5 个。路由 `/settings` 已加入 `ROUTES`。
- **CoinStats 卡片**：
  - 状态胶囊：未配置 / 已配置 · 末 4 位。
  - `type=password` 输入框 + 保存（已配置时为「更换 key」）+ 删除；删除需二次确认（取消 / 确认删除）。
  - 本月额度：已用 / 总量 / 剩余，进度条（>70% 转暖、>90% 转红），数字 tabular-nums。
  - 「上次同步后」的已用 / 剩余（取自 `last_sync_credits`），以及单次同步约 35–40 credits 的提示。
  - 最近同步状态、时间（北京时间）、触发方式；错误以中文人话呈现，技术性报错（含 `Error` / `deadlock` 等）统一转成「同步时遇到临时故障…」，未配置来源的 `skipped` 提示改写成「Bitget、Kraken 未配置 key，已自动跳过」。
- **空状态**：未配置时显示虚线引导块，额度与同步区块不出现。
- **说明文案**：去 openapi.coinstats.app 申请自己的免费 key；币安只读 key 权限只勾「读取」、IP 选「不限制」；key 加密存储、不回显；每人自备、不共用。
- **key 卫生**：保存请求发出后立即清空输入框；key 不进 state、不进 console、不回显到 DOM（页面只显示末 4 位）；前端先校验 8–256 位且无空格，后端报错原文（中文）行内提示 + toast。保存 / 删除按钮均有 loading。
- **验证**：`node --check frontend/static/app.js` 通过；`docker compose up -d --build app worker` 后 `GET /settings` 返回 200；pytest 131 passed；用 playwright + 系统 Chrome（headless）、mock 路由（假 key，不碰真 key）走通 保存 / 校验失败 / 额度 429 / 删除确认 / 取消 / 删除，断言 key 不出现在 DOM 与 console。
- **截图**（390 宽，@2x）：`screenshots/settings_demo.png`（无 key 空态）、`screenshots/settings_admin.png`（已配置，仅显示末 4 位）。

## 三期·10：Solana 余额拉取修复（Helius RPC / DAS）

### 背景
`https://api.helius.xyz/v0/addresses/{addr}/balances` 已下线（HTTP 404），live sync 日志出现 `solana balance fetch failed (HTTPStatusError)`，Solana 持仓未进净值。

### 改动
- `backend/app/integrations/chain/helius.py`
  - **原生 SOL**：Helius RPC `getBalance`；`contract` = WSOL mint，走 DefiLlama `solana:<mint>`。
  - **SPL**：优先 DAS `getAssetsByOwner`；失败回退 `getTokenAccountsByOwner`。
  - 零余额与 NFT 忽略；DAS `price_info` → `quote_usdt` 兜底；无价不中断（`unpriced`）。
  - 人话错误 `HeliusError`，日志/异常不含 URL / `api-key`。
- `backend/tests/test_helius_rpc.py`：MockTransport（SOL / SPL / 回退 / 无价 / RPC 报错 / key 脱敏 / since 过滤）。

### since 配置
- 同步 API **无** `since` 查询参数，只读环境变量 **`SYNC_SINCE`**（`app.config.settings.sync_since`）。
- 联调前已将 `.env` 的 `SYNC_SINCE` 从 `2026-01-01` 改为 **`2026-10-23`**（与 admin `returns_start` 对齐），recreate 后容器内已确认。

### transactions
- legacy balances：**404**；legacy transactions：仍 **200**。
- transfers 按 `SYNC_SINCE` 过滤。本次 live sync **未新增** helius pending（`helius_ts ≥ 2026-10-23` = 0）。库内仍有历史 helius pending 636 条（均 `< 2026-10-23`，来自改 since 前的旧同步），本任务未清理。

### 联调结果（2026-10-24 上海时间附近）
- rebuild：`docker compose up -d --build --force-recreate app worker`，healthy。
- sync 前 overview：`current_nav_usdt=16246.32`，`nav_source=sync`。
- 首次 live sync 写入快照 nav=**16499.77**（helius 合计约 **253.47**）；再次 `POST /api/sync?mode=live` → `last_status=ok`，nav=**16499.84**。
- pending：882 → 882（Δ0）；来源 helius 636 / zerion 227 / gate 18 / binance 1。

#### 两地址估值摘要（地址仅前 6 位；breakdown 无 address 字段，按 SOL 数量与探针对齐）
| 前缀 | SOL | 主要代币估值（USDT） | 小计约 |
|------|-----|----------------------|--------|
| `85Rtk5` | 0.4798（≈50.13） | USDC+≈99.20，CRCLx≈72.91，ONe≈1.72，JitoSOL≈0.16，USDC/hyUSD≈0 | ≈224 |
| `E2yWHe` | 0.0318（≈3.32） | USDT≈23.76，V2EX≈2.27；kJLP-* / YE 无价记 0 | ≈29 |
| **helius 合计** | | | **≈253.47** |

无价（进 unpriced）：`helius:KJLP-JSOL`、`helius:KJLP-USDC`、`helius:YE`。

### 测试
```bash
cd backend && ../.venv/bin/python -m pytest -q
# 143 passed
```

### 未改
- 前端；未清历史 helius pending。

### 审美官有条件过 · 修订

按意见改完：标题左侧 ← 返回总览；已配置默认收起（点「更换 key」再展开输入）；额度一行合并；空态短提示；「怎么配置」三步只讲 CoinStats；同步失败错误块左侧 2px `#f07178`；删除确认文案写明只删本账号 key。重截 `settings_{demo,admin,admin_replace,admin_delete,admin_syncfail}.png`。

## 三期·11：起点前 pending 归档 + 钱包管理

### A. 归档起点前旧 pending
- 规则：有 `returns_start_date` 的用户，将 `pending_confirmations` 中 `status=pending` 且发生时间 `COALESCE(ts, created_at)` **严格早于**该日 00:00 Asia/Shanghai 的条目改为 `rejected`，`suggested_reason=起点前自动归档`。
- 不动已 confirmed / rejected；demo（`returns_start_date` 空）不动；可重复跑（幂等）。
- 改库前备份：`/workspace/db-before-p311.sql`（chmod 600）。
- 实现：`backend/app/archive_prestart.py`（`archive_prestart_pending`）+ CLI `backend/scripts/archive_prestart_pending.py`。
- **admin 归档条数：636**（跑后起点前 pending=0；库内仍有起点后 pending 1 条）。demo archived=0、pending=13 不变。二次跑 archived=0。

### B. 钱包管理
- API（本人）：`GET/POST /api/wallets`、`DELETE /api/wallets/{id}`；`chain=evm|solana`；EVM `0x`+40hex（入库小写）；Solana Base58 32–44；去重 409「该地址已添加」；格式错 422 中文。
- 表：已有 `user_wallets`（`uq_user_wallet`）；同步已读，无需改 sync。
- 前端 `/settings`：CoinStats 卡片下方「钱包」卡片（列表截断+复制、添加、删除二次确认）；深色+暖金，390 优先。
- 截图：`screenshots/settings_wallets_{demo,admin,admin_add,admin_delete}.png`。

### 测试与验证
```bash
cd /workspace/asset-tracker/backend && ../.venv/bin/python -m pytest -q   # 147 passed
node --check frontend/static/app.js
docker compose up -d --build app worker
# admin 起点前 pending≈0；wallets CRUD 200/201/204/409/422
```

### 改动文件
- 新增：`backend/app/archive_prestart.py`、`backend/scripts/archive_prestart_pending.py`、`backend/app/routers/wallets.py`、`backend/tests/test_archive_prestart.py`、`backend/tests/test_wallets_api.py`
- 修改：`backend/app/schemas.py`、`backend/app/main.py`、`frontend/static/app.js`、`frontend/static/styles.css`、`DELIVERY.md`

## 三期·12：总览持仓明细（按资产 / 按来源）

### API
- 扩展 `GET /api/overview`：`holdings`（扁平列表）+ `holdings_asof`。
- 仅取该用户最新 `source=sync` 快照的 `breakdown.holdings[]`；无 sync / breakdown 坏 → `holdings=[]`，`holdings_asof=null`（不回落 seed/backfill）。
- 后端不做按资产/按来源聚合；金额透传，可选 `chain`/`contract`/`price_source`/`stale`。
- 辅助：`latest_sync_holdings`（`snapshots.py`）；schema：`HoldingOut` + `OverviewOut` 字段。

### 前端
- 总览净值曲线下「持仓」卡；分段切换「按资产」（默认，同资产合并）/「按来源」。
- 尘埃：`priced && value_usdt < 0.01` → 「其他」；未定价显示「未定价」，组旁「含未定价」。
- 来源展示：币安 / Gate / EVM / Solana；390 深色暖金。
- 截图：`screenshots/overview_holdings_by_asset.png`、`overview_holdings_by_source.png`、`overview_holdings_demo.png`。

### 测试与验证
```bash
cd /workspace/asset-tracker/backend && ../.venv/bin/python -m pytest -q   # 151 passed
node --check frontend/static/app.js
docker compose up -d --build app
# admin overview holdings=94，nav 不变；切换两种模式正常
```

### 改动文件
- 新增：`backend/tests/test_overview_holdings.py`
- 修改：`backend/app/schemas.py`、`backend/app/snapshots.py`、`backend/app/routers/overview.py`、`frontend/static/app.js`、`frontend/static/styles.css`、`DELIVERY.md`

### 未改
- 同步逻辑 / 净值计算 / 微信 / 密码 / 隧道；未接 Bybit/Bitget/Kraken。


## 三期·13：持仓代币图标 + Holding chain/contract（2026-10-24）

- Holding / price_holdings / stale 携带 `chain`+`contract`；overview 透传
- 前端：TrustWallet → spothq → SVG 字母占位；`tokenIconMarkup`
- 截图：`screenshots/overview_holdings_icons_asset.png`、`overview_holdings_icons_source.png`
- pytest：overview holdings / snapshots 相关绿；全量 155；`node --check` 通过
- live sync：admin 最新 sync 快照含 chain/contract（见 wip DELIVERY_P313）
- 审美：过（来源父行无图标，仅 chevron+展示名）

## 三期·14：手动同步按钮（2026-10-24）

- 设置页「同步现在」+ 总览 quiet「同步」；`POST /api/sync?async=true` + 轮询 status
- 截图：`screenshots/settings_manual_sync.png`、`overview_manual_sync.png`
- 审美：过

## 三期·15：收益页打磨（2026-10-24）

- period 选中态、金额/收益率层级、spark 暖金、空态短句
- 截图：`screenshots/returns_day.png`、`returns_month.png`
- 审美：过

## 三期·16：多用户开账号 / 重置密码（2026-10-24）

- `GET /api/admin/users`；`POST .../reset-password`；设置页「用户」卡（仅 admin）
- 演示密码未改；demo 无入口
- 截图：`screenshots/settings_users_admin.png`、`settings_users_demo_hidden.png`
- 审美：过

## 热修：pending 死锁（2026-10-24）

- `sync/service.py`：`pg_advisory_xact_lock(872314, user_id)` 串行化同用户 sync；prefetch `(source,external_id)`；insert 用 savepoint，不再整段 rollback
- 验证：admin live sync → snapshot id=643，holdings=94，chain=70，contract=59（PLP/USDC 合约回）
- pytest：attribution/api_sync/exchange_live 41 passed

## 热修：持仓图标 EIP-55 / Octav / Zerion icon_url（2026-10-24）

- Trust CDN + EIP-55 checksum；Octav；Zerion `icon_url` 持久化；无 TW 映射链跳过 TW
- 截图：`screenshots/overview_holdings_icons_asset.png`
- 注：Zerion 日限额耗尽时 icon_url 可能为 0，常见币仍走 Trust/符号 CDN

## 三期·17 Pendle 底层图标（2026-10-24）

- 解析：`^(PLP|PT|YT)[-_]` 去前缀 + 去尾部日期段 → 底层符号；复用同 overview 底层仓 icon；再符号 CDN；再暖色字母 SVG
- 改动：`frontend/static/app.js`（已 docker cp 进 app）
- 截图：`screenshots/overview_holdings_icons_asset.png`

## 三期·17b 按来源行图标（与 17 同批，2026-10-24）

- `sourceIconMarkup` + `frontend/static/icons/sources/*.svg`
- 截图：`screenshots/overview_holdings_icons_source.png`、`overview_holdings_icons_asset.png`

## 三期·17c 来源图标 Web3Icons vendor（2026-10-24）

- 覆盖错误手绘；`icons/sources/` 仅 Web3Icons exchanges/networks/wallets branded SVG
- alchemy→ethereum、helius→solana；coinstats 字母占位
- 截图：`screenshots/overview_holdings_icons_source.png`
