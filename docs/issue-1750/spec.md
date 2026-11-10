# Spec: Issue #1750 — recover_orphans 增加 worker 自报活跃白名单

> 来源：Issue #1750 官方 Spec（Issue 评论 `199515616`）| 评级 ready | 目标分支 `weekly-10` @ `fb2420f1f`

## Objective
- 为 `recover_orphans` 增加「worker 自报活跃」白名单信号（`active_task_ids`），命中的任务直接 `skipped_active`，优先于年龄闸门与活跃窗口。
- 收紧回收时机：周期重注册不再无条件回收，仅在进程启动与 runtime id 集合变化时回收。
- 让静默型长任务（编译、terraform、深度思考等长时间无 `task_messages`）不再被周期回收误判孤儿。

## Scope

### In Scope
- **P1-A**：`POST /runtimes/{runtime_id}/recover-orphans` 接受可选 `active_task_ids`；命中 → `skipped_active`，优先于年龄闸门与窗口判定。
- **P1-B**：周期重注册不再无条件回收；启动（空白名单）与 rid 集合变化（携带 `active_tasks`）时调用。
- `client.recover_orphans` 与 agent 调用点传递白名单；返回体保持既有口径并补充 `skipped_active_reason`。
- 复用 #1495 `task_activity` 口径与 #1071/#369 的 attempts/诊断语义。

### Out of Scope
- 调整 `RECOVER_ORPHANS_ACTIVITY_WINDOW_SECONDS` 或新增 env 旋钮。
- #1286 的 `failure_kind`/`superseded_by`/`recover_at` 新列与 UI 文案。
- P2 任务侧 keepalive（`report_messages` 补活）。
- 跨机双跑租约/执行实例探测。

## Acceptance Criteria
- [x] A1 静默超窗口的在跑任务在白名单内 → `skipped_active`，`attempts` 不变、`status` 仍 `running`。
- [x] A2 真孤儿（启动注册、空白名单）仍被回收，首次注册即回收。
- [x] A3 回炉后同机不双跑（白名单命中不转池）。
- [x] A4 `attempts` 单次 +1（SQLite 内存库实测；MySQL 侧由同一参数化 SQL 保证，未单独执行）。
- [x] A5 周期重注册不回收；rid 集合变化带白名单回收；启动空白名单回收。
- [x] A6 loop 级模拟（非 E2E）：静默任务跨一次周期重注册判定后仍 `running`，完成路径收敛为 `completed`。
- [x] 既有 `test_recover_orphans_*`、`test_issue_417_*` 全通过，新增测试覆盖 A1–A6。

## Risks & Mitigations
| Risk | Mitigation |
|------|------------|
| 白名单误报导致任务滞留 running | `reap_stale_runtimes` + agent 超时兜底；下次启动注册回收 |
| worker 崩溃重启但 rid 未变 | 延迟到 reap/超时；后续按生产观测再启用 keepalive |
| 新旧版本混跑 | 字段可选默认空，仅保证旧 worker → 新主控方向：新 worker + 旧主控时旧端点不解析 body，白名单被忽略、rid 变化仍可能误杀静默任务（反向不保证） |
| 多主控共享 DB | 白名单随请求传递，服务端不缓存 |
