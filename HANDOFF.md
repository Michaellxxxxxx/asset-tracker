# 资产追踪站 Handoff

更新时间：2026-11-09（Asia/Shanghai）

## 当前状态
- 主分支：`main`，最新提交 `76e5823`（管理端 `/admin`）。**该提交未部署**，本机运行实例仍是上一版，等叉叉批准上线。
- 本机地址：`http://127.0.0.1:8001`（同 WiFi 手机用局域网 IP）。外网为临时 Cloudflare 隧道，每次重启地址都会变。
- 本地端口：app `8001`，Postgres `5433`，Redis `6380`（本机 8000/5432/6379 有占用，故改端口）。本机走桥接网络 + 端口映射，该版 `docker-compose.yml` 只在本机工作区、不入库；仓库内保留 host 网络版。
- 服务：app / worker / postgres / redis 四容器；worker 偶发 Redis timeout 会自动重启。
- 测试基线：前端 node **38 条 → 37 通过 + 1 条既有失败**（`p25_loop_card` 找不到 `static/icons/protocols/aave.png`，该图片早已从仓库删除，测试未同步）。跑法：源码先在工作区外转成 LF，且目录链上不能有带 `"type":"module"` 的 `package.json`，否则整批假失败（CRLF 直跑只有 20 通过）。
- 后端 pytest：交接基线为 764 通过（1 skip），2026-11-09 这批前端改动未复核。

## 启动
```bash
cd /workspace/asset-tracker
cp .env.example .env   # 填入真实本地配置
docker compose up -d --build
docker compose ps
curl http://127.0.0.1:8001/api/health
```

## 运维约束
- 部署前必须备份数据库和 git，并得到叉叉明确批准。
- Git 纪律：每次改动一个提交，提交前 `git status` 只应出现本次预期的文件；本机运行适配（端口映射版 `docker-compose.yml`）永不入库；推送前先确认本地 `main` 与 `origin/main` 无分叉。
- 微信旧推送 `id <= 710` 一律不重发；`WECHAT_FLUSH_MIN_ID=710`。
- 不修改线上业务数据；缺数据标「缺失」，不编数字。
- 手机端交互改动需用 393px 视口实际点击验证。
- UI 改动须审美官复审通过后才可上线。

## 待办 / 阻塞
- 管理端 `/admin`（提交 `76e5823`）已开发并本地验完，等待叉叉批准上线。
- 微信 token 过期，需叉叉本人重新登录才能恢复告警。
- 4 个 Solana 地址按约定不加入识别表。
- 等待叉叉决定下一批需求。
