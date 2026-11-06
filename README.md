# 资产追踪站

一个可本地运行的、手机优先的私人资产看板。FastAPI 提供 API，静态 HTML/CSS/JS 前端由同一容器托管；PostgreSQL 存业务数据，Redis 承担同步队列。

一期是演示骨架（假净值、假流水）。二期在不改前端的前提下接入交易所、EVM、Solana 与行情：没有密钥时自动读 `backend/tests/fixtures` 的录制 JSON，填入密钥后走只读实盘。

## 启动

前置条件：Docker 与 Docker Compose v2。

```bash
cd /workspace/asset-tracker
docker compose up -d --build
```

打开 <http://localhost:8000>。app 使用 host 网络连接本机 5432/6379（本机 Docker 容器间 TCP 不通）。首次启动会等待 Postgres、建表并自动写入种子数据。

```bash
docker compose ps
docker compose logs -f app
docker compose down          # 停止，保留数据卷
docker compose down -v       # 停止并删除演示数据
```

健康检查：

```bash
curl http://localhost:8000/api/health
# {"status":"ok"}
```

## 默认账号

| 角色 | 用户名 | 密码 | 说明 |
| --- | --- | --- | --- |
| 管理员 | `admin` | `admin123` | 可调用 `POST /api/admin/users` 开普通用户 |
| 演示用户 | `demo` | `demo123` | 已注入净值、流水、待确认与加密凭证 |

前端演示页底部也提示了 `demo / demo123`。种子只在数据库无用户时执行；重复启动不会重复写入。

## 环境变量

仓库内的 `.env` 与 `.env.example` 都是同一组演示值：

```dotenv
POSTGRES_USER=asset
POSTGRES_PASSWORD=asset_pass
POSTGRES_DB=asset_tracker
DATABASE_URL=postgresql+psycopg://asset:asset_pass@127.0.0.1:5432/asset_tracker
REDIS_URL=redis://127.0.0.1:6379/0
JWT_SECRET=demo-jwt-secret-change-me-in-prod-32b
MASTER_KEY=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
ACCESS_TOKEN_EXPIRE_MINUTES=1440
```

上面的 `JWT_SECRET` / `MASTER_KEY` **只是开发示例**。`MASTER_KEY` 是 64 位十六进制字符串（32 字节），供 AES-256-GCM 加密 `api_credentials`。`.env` 只用于本地演示，已加入 `.gitignore` 与 `.dockerignore`，不会打进镜像。

### 生产必须更换 JWT_SECRET / MASTER_KEY

仍使用上面的开发默认时，进程启动会打 WARNING（不打印密钥值）。生产轮换：

1. 生成新密钥（不要把生成结果写进仓库或本文档）：
   - `MASTER_KEY`：`openssl rand -hex 32`
   - `JWT_SECRET`：`openssl rand -base64 48` 或同样用 `openssl rand -hex 32`
2. 先重加密库里的凭证，再改 `.env`，最后重建 app / worker。在 `backend/` 下：

```bash
python -m scripts.rotate_master_key --old-key <旧 MASTER_KEY> --new-key <新 MASTER_KEY> --yes
```

也可用环境变量 `OLD_MASTER_KEY` / `NEW_MASTER_KEY`（CLI 优先）。`--dry-run` 只打印 `would_migrate` / `skipped` / `total`，不写库。确认输出 `migrated=N skipped=M total=T` 后，把新值写入 `.env`，再执行：

```bash
docker compose up -d --force-recreate app worker
```

已用新密钥加密过的行会跳过（幂等）。旧密钥和新密钥都解不开的行会整批回滚。脚本不读取 `settings.master_key`。

### 二期密钥（全部可留空）

留空时 `SYNC_MODE=auto` 不会发任何外部请求，同步读取录制 fixtures。日志经过 redact filter，不会打印 key / secret / bearer。

```dotenv
BINANCE_API_KEY=
BINANCE_API_SECRET=
BITGET_API_KEY=
BITGET_API_SECRET=
BITGET_API_PASSPHRASE=
BYBIT_API_KEY=
BYBIT_API_SECRET=
KRAKEN_API_KEY=
KRAKEN_API_SECRET=
GATE_API_KEY=
GATE_API_SECRET=
ZERION_API_KEY=
ALCHEMY_API_KEY=
HELIUS_API_KEY=
COINGECKO_DEMO_API_KEY=
EVM_ADDRESSES=
SOLANA_ADDRESSES=
SYNC_MODE=auto
SYNC_SINCE=2026-01-01
SYNC_INTERVAL_SECONDS=1800
```

| 变量 | 作用 |
| --- | --- |
| `BINANCE_*` / `BITGET_*` / `BYBIT_*` / `KRAKEN_*` / `GATE_*` | CCXT 只读。某所密钥不全则该所单独走 fixture |
| `BITGET_API_PASSPHRASE` | Bitget 的 password 字段，三项缺一不可 |
| `ZERION_API_KEY` | EVM 交易历史。失败或未填时改用 Alchemy |
| `ALCHEMY_API_KEY` | EVM 兜底（`alchemy_getAssetTransfers`） |
| `HELIUS_API_KEY` | Solana enhanced transactions |
| `COINGECKO_DEMO_API_KEY` | USDT 计价。失败时改 DefiLlama，再失败回 fixture |
| `EVM_ADDRESSES` / `SOLANA_ADDRESSES` | 逗号分隔的地址。链上流入若来自这些地址，归因成 `transfer` |
| `SYNC_MODE` | `auto`（默认，有 key 走 live，否则 mock）/ `mock` / `live` |
| `SYNC_SINCE` | 只拉取该日（UTC）及之后的流水，也是历史回填起点，默认 `2026-01-01` |
| `SYNC_INTERVAL_SECONDS` | worker 定时同步间隔（秒），默认 `1800`（30 分钟） |

## 目录

```text
asset-tracker/
├── docker-compose.yml
├── Dockerfile
├── .env / .env.example
├── README.md
├── backend/
│   ├── requirements.txt
│   ├── requirements-dev.txt     # pytest / httpx
│   ├── pytest.ini
│   └── app/
│       ├── main.py              # FastAPI 入口、健康检查、静态资源与 SPA fallback
│       ├── config.py            # 环境变量（含二期密钥，空则 mock）
│       ├── db.py                # SQLAlchemy engine / session / 启动等库
│       ├── models.py            # User / ApiCredential / LedgerEntry / Snapshot / SyncRun / PendingConfirmation
│       ├── migrate.py           # 启动时幂等 ALTER（Postgres 已有表补列）
│       ├── snapshots.py         # 余额 × 价格汇总，写入 sync 快照
│       ├── snapshots_backfill.py # 从 SYNC_SINCE 按日倒推 backfill 快照
│       ├── schemas.py           # Pydantic 请求与响应
│       ├── auth.py              # bcrypt、JWT Bearer、管理员依赖
│       ├── crypto.py            # AES-256-GCM encrypt_text / decrypt_text
│       ├── logging_redact.py    # 日志脱敏 filter
│       ├── attribution.py       # 充提 / 空投 / KOL / 内部划转归因
│       ├── twr.py               # 剔除充提与划转的时间加权收益
│       ├── seed.py              # 幂等演示种子
│       ├── integrations/        # exchanges (CCXT/mock) · chain · prices
│       ├── sync/                # run_sync、RQ job、定时循环、CLI
│       └── routers/
│           ├── auth.py
│           ├── overview.py
│           ├── returns.py
│           ├── pending.py
│           ├── admin.py
│           └── sync.py
└── frontend/
    ├── index.html               # SPA shell
    └── static/
        ├── app.js               # 原生 JS 路由与页面渲染
        ├── styles.css           # 深色金融风样式
        └── favicon.svg
```

## API 简述

除 `/api/health` 与登录/注册外，接口都需要 `Authorization: Bearer <token>`。所有业务查询按 JWT 中的当前用户隔离。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | `{"status":"ok"}` |
| POST | `/api/auth/register` | 注册普通用户，body：`{username,password}` |
| POST | `/api/auth/login` | 登录，返回 `{access_token,token_type,user}` |
| GET | `/api/auth/me` | 当前用户 |
| POST | `/api/admin/users` | 管理员开普通用户，body：`{username,password}` |
| GET | `/api/overview` | 当前净值与快照序列。另返回 `nav_source`、`latest_snapshot_ts` |
| GET | `/api/returns?period=day\|week\|month\|year` | 区间收益额、收益率与累计曲线。同样返回 `nav_source`、`latest_snapshot_ts` |
| GET | `/api/pending` | 当前用户的待确认/已确认记录。二期多返回 source、建议分类等字段，旧字段不变 |
| POST | `/api/pending/{id}/confirm` | 确认一条记录，幂等。body 可省略；`{"category": "deposit"}` 覆盖建议分类。有建议时写入一条流水，重复确认不重复写 |
| POST | `/api/sync?async=false&mode=mock` | 当前用户立即同步，返回 `{fetched, enqueued, duplicates}` |
| POST | `/api/sync?async=true` | 丢进 RQ 队列 `asset-tracker`，返回 `{job_id, status: "queued"}` |
| GET | `/api/sync/status?job_id=` | 查询任务；省略 job_id 时返回最近一条。始终附带 `last_run_at`、`last_status`、`last_error`、`last_trigger`、`interval_seconds` |
| POST | `/api/sync/backfill?mode=mock` | 为当前用户按日回填净值，同步执行，返回 `{created, updated, from, to}` |

交互式文档：<http://localhost:8000/api/docs>（OpenAPI JSON 在 `/api/openapi.json`）。

## TWR 计算口径

`twr.py` 把区间拆成相邻快照的子区间，剔除 `ledger_entries` 中的 `deposit` / `withdraw`：

```text
子区间收益率 = (期末净值 - 子区间净外部现金流) / 期初净值 - 1
区间收益率   = Π(1 + 子区间收益率) - 1
收益额       = 期末净值 - 期初净值 - 区间净外部现金流
```

- `deposit` 记为正流入，`withdraw` 记为负流入；`transfer` 按金额正负区分流入/流出，同样算外部现金流。
- `airdrop` / `kol_reward` 算收益，不从外部现金流里扣除。`trade` / `funding` / `other` 也不影响外部现金流。
- 一期种子只有 `deposit` / `withdraw`，因此同样的快照与流水得到的区间收益与一期一致。
- 收益窗口锚定在最新快照，而不是服务器当天日期，因此种子数据在任何时候打开都能得到有意义的区间。
- 种子净值起点 10,000 USDT，从 2026-01-01 到 2026-10-20 每日一个点；中途有 2,500 / 1,800 入金和 1,200 / 500 提现，便于验证剔除逻辑。

## 二期：同步

`docker compose up -d --build` 会同时拉起 `worker`（同一镜像、`network_mode: host`、`python -m app.sync.worker_main`）。这一个进程里 RQ worker 跑在主线程，定时同步跑在守护线程，间隔由 `SYNC_INTERVAL_SECONDS` 控制（默认 1800 秒）。Redis 锁避免上一次还没跑完时重入。每次执行写入 `sync_runs`（开始/结束时间、耗时、状态、触发方式、错误）。worker 的健康检查读 Redis 心跳 key，过期即视为不健康。不填密钥时 worker 与 API 都只读 fixtures。

接口（需登录）：

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"demo","password":"demo123"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')

# 同步执行，直接返回统计
curl -s -X POST "http://localhost:8000/api/sync?async=false&mode=mock" \
  -H "Authorization: Bearer $TOKEN"

# 丢进队列
curl -s -X POST "http://localhost:8000/api/sync?async=true" \
  -H "Authorization: Bearer $TOKEN"

curl -s "http://localhost:8000/api/sync/status" -H "Authorization: Bearer $TOKEN"
```

容器外直接跑（工作目录要能 import `app`，即 `backend/`）：

```bash
cd /workspace/asset-tracker/backend
python -m app.sync.cli --user demo --mode mock
# fetched=N enqueued=N duplicates=0

# 从 SYNC_SINCE 到今天按日回填净值。重复执行只更新，不新增行。
python -m app.sync.cli --user demo --backfill --mode mock
python -m app.sync.cli --all-users --backfill --mode mock
```

同步流程：每个交易所 / EVM / Solana adapter 拉 `since` 之后的充提 → 归因（`deposit` / `withdraw` / `airdrop` / `kol_reward` / `transfer`，带 0–1 置信度和中文原因）→ 按 `(user_id, source, external_id)` 去重写入待确认队列。`POST /api/pending/{id}/confirm` 把最终分类写成一条 `LedgerEntry`；不传 `category` 就用建议。一期旧的待确认项没有建议，仍然可以不带 body 确认，且不写流水。

填入真实密钥后把 `SYNC_MODE` 留 `auto`（或显式 `live`）再跑上面的命令即可。CCXT 只读、分页、`enableRateLimit`，失败按指数退避重试。行情优先 CoinGecko Demo，失败用 DefiLlama。

## 三期：净值快照

每次同步结束时按当前持仓写一条 `source='sync'` 的快照：交易所 `fetch_balances`、Zerion 持仓、Helius 余额，分别乘 USDT 价格后求和。没有报价的资产记在 `breakdown` 里、金额算 0，不猜测价格。同一时间点重复写入会命中 `uq_snapshots_user_ts`，更新原行。

`/api/overview` 和 `/api/returns` 的读取规则：

- 该用户只要有任何非 `seed` 快照，就只读这些（`sync` 与 `backfill` 一起），不再混入一期种子曲线。
- 一条非 `seed` 都没有时，才回退到 `seed`。
- 响应里的 `nav_source` 取最新一个点的来源；同一时刻 `sync` 优先于 `backfill`。`latest_snapshot_ts` 是这个点的时间。
- TWR 仍只剔除 `deposit` / `withdraw` / `transfer`，`airdrop` / `kol_reward` 继续算收益。

历史回填（`source='backfill'`）从 `SYNC_SINCE` 到今天每天一个点，打在当天 23:59:59 UTC。mock 用 fixtures 的当前余额减去此后的充提和成交倒推，所以曲线由 fixtures 唯一决定，最后一天等于当前余额。live 用同样的算法：真实当前余额，减去 `since` 之后的充提和成交，再乘当天历史价（CoinGecko `market_chart`，失败改 DefiLlama，再失败回 fixture）。

live 回填覆盖不到的部分：

- 链上 DeFi 仓位（LP、质押、借贷）不倒推，Zerion 的简单持仓在整个窗口里当作不变。
- 没有历史价格的代币。有 fixture/现货价时用它，否则该持仓当天贡献 0，并在 breakdown 里标成未计价。
- 交易所内部划转（现货 ↔ 合约）若不表现为充值、提现或成交，就不改变倒推数量。
- 手续费精度。只有适配器单独给出手续费资产和金额时才记账；省略的手续费不估算。

本地测试（无网络、无密钥，SQLite 内存库）：

```bash
cd /workspace/asset-tracker/backend
python -m pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -q
```

## 微信推送（可选）

同步写出新的待确认后，会尽力推到微信 ClawBot。不填 `WECHAT_BOT_TOKEN` 时走 mock：只打日志并把队列标成 sent，不访问外网。网站上的待确认仍是主入口。

```dotenv
WECHAT_BOT_TOKEN=
WECHAT_PUSH_MODE=auto
WECHAT_POLL_ENABLED=0
```

`auto`：有 token 为 live，否则 mock。`WECHAT_POLL_ENABLED=1` 且 live 有 token 时，worker 才长轮询入站。用户先给 Bot 发一句，服务端才能缓存 context token 并补发积压。token 不入库；context token 只在服务端缓存表。后期可加绑定表，当前入站都归到 `WECHAT_DEFAULT_USERNAME`（默认 admin）。

## 已知限制

- **默认为录制数据。** 未填密钥时交易所、链上与行情都来自 `backend/tests/fixtures`，不是账户真实余额。
- **只读。** CCXT 适配器只调用余额、充提与成交查询，没有下单或提现。
- **Redis 用于 RQ 队列。** 没有缓存层；`async=true` 需要 worker 进程在跑。
- **未包含隧道。** 如需临时公网访问，请自行启动隧道；隧道地址每次可能变化，前端全部走同源 `/api`，换地址不需要改代码。
- **演示级安全配置。** `.env` 中的 JWT secret、数据库密码和 master key 均为公开演示值，部署到任何真实环境前必须全部替换；一期没有限流、审计、CSRF/刷新令牌等生产能力。
- **收益口径是演示实现。** 外部现金流按“子区间末”处理，适合当前种子；真实场景需按现金流发生时间做更细的切分。
- **单容器单进程。** Uvicorn 直接启动，无反向代理、无多副本；数据库端口 5432 / Redis 6379 直接映射到宿主机，仅适合本地。

## 常用验证

```bash
docker compose config          # 校验 Compose 配置，不启动容器
python3 -m compileall backend  # 校验 Python 语法
node --check frontend/static/app.js
```

重置演示数据：

```bash
docker compose down -v
docker compose up -d --build
```

> 密钥只放在本机 `.env`，不要提交。日志已做脱敏，但仍不要把 `.env` 发到日志或截图里。请勿直接用于生产。
