# 资产追踪站 Handoff

更新时间：2026-11-06（Asia/Shanghai）

## 当前状态
- 主分支：`master`，已部署版本 `6d394fe`（p44），另有本地运行配置提交。
- 线上地址：`https://schilling-enforced-rename.ngrok-free.dev`
- 本地端口：app `8001`，Postgres `5433`，Redis `6380`（本机 8000/5432/6379 有占用，故改端口；容器均走 host 网络）。
- 服务：app / worker / postgres / redis 四容器；worker 偶发 Redis timeout 会自动重启。
- 测试基线：pytest 764 通过（1 skip），前端 node 测试 37 通过。

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
- 微信旧推送 `id <= 710` 一律不重发；`WECHAT_FLUSH_MIN_ID=710`。
- 不修改线上业务数据；缺数据标「缺失」，不编数字。
- 手机端交互改动需用 393px 视口实际点击验证。
- UI 改动须审美官复审通过后才可上线。

## 待办 / 阻塞
- 微信 token 过期，需叉叉本人重新登录才能恢复告警。
- 4 个 Solana 地址按约定不加入识别表。
- 等待叉叉决定下一批需求。
