# P4 曲线假坑修复 — select_nav_snapshots

## 问题

admin 曲线在 sync 点 16121.07（id 669，2026-10-23 23:23:48 UTC）与 16498.04（id 673，2026-10-24 00:10:11 UTC）之间夹着 backfill 行 **#590**（2026-10-23 23:59:59 UTC，nav **10381.64**，旧估值）。旧规则在存在 sync 时保留 `ts <= latest_sync_ts` 的 backfill，所以这条日终回填落在两个真实 sync 点中间，曲线出现假坑。

## 改动

只改 `backend/app/snapshots.py` 的 `select_nav_snapshots`（及 docstring），并补测试。

- 有 sync：sync 行全留；backfill 只留 `ts < earliest_sync_ts`；`ts >= earliest_sync_ts` 的 backfill 丢弃（含夹在两个 sync 之间、与 sync 同一时刻、以及晚于最新 sync 的）。
- 无 sync：全部 backfill 保留，`nav_source="backfill"`。
- 无 sync 且无 backfill：仍走 seed，`nav_source="seed"`。
- 返回的 `NavSource` 不变（有 sync → `"sync"`，仅 backfill → `"backfill"`，仅 seed → `"seed"`）。

调用方 `overview` / `twr.calculate_returns` 仍只消费该函数的返回，未改。`_previous_holdings` 未改（仍取最新 sync）。

## 测试

`cd backend && ../.venv/bin/python -m pytest -q`：**254 passed**。

新增：

- `test_backfill_between_syncs_is_dropped_pre_sync_kept`：16121 / 10381.64 / 16498 夹心 backfill 丢弃；早于首个 sync 的 backfill 保留；seed 不进曲线；`nav_source=="sync"`。
- `test_no_sync_keeps_every_backfill`：无 sync 时全部 backfill 保留，`nav_source=="backfill"`。

既有 `test_overview_prefers_sync_when_later_backfill_exists`、`test_overview_falls_back_to_backfill_when_no_sync`、`test_overview_falls_back_to_seed_when_no_real_snapshot` 仍绿（晚于 sync 的 backfill 继续丢弃；无 sync / 仅 seed 行为不变）。

## 部署

- `docker cp backend/app/snapshots.py` → `asset-tracker-app-1:/app/app/snapshots.py` 与 `asset-tracker-worker-1:/app/app/snapshots.py`。
- 容器内非 root `chmod` 被拒；`docker exec -u root … chmod a+r` 后权限 `-rw-r--r--`。
- `docker restart asset-tracker-app-1 asset-tracker-worker-1`，两容器 healthy。
- 容器内源码含 `earliest_sync_ts`，不再含 `latest_sync_ts`。

## 验收

- `GET /api/health` → **200** `{"status":"ok"}`。
- admin 登录后 `GET /api/overview` → **200**，`nav_source=sync`，`series` 57 点，`returns_start=2026-10-23`，`current_nav_usdt=29799.36`（2026-10-24T03:40:43Z）。
- series 中 **不再有** nav≈10381.64 夹在 16121 与 16498 之间：16121.07（23:23:48）→ 16120.82（23:51:30）→ **16498.04**（10-24 00:10:11），中间无 23:59:59 点。库内 #590 仍在，只是不再入选。
- 10-23 早期 10378.95~10381.26 均为 `source=sync`（id 294、591…），在 `returns_start` 之后，属真实早期 sync，保留。10-22 23:59:59 backfill 10395.95 早于首个 sync，仍保留（overview 因 `returns_start=2026-10-23` 未画进 series，属既有起点过滤，不是本次丢弃）。

## git

工作区在开跑 `git add -A && git commit` 时已把本函数与测试一并写入 `7ee04db`（`pre-run snapshot`）。本结果文件另提交。并行的 morpho/前端未暂存改动未纳入本次提交。
