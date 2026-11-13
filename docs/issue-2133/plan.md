# Plan: Issue #2133

## 实现路径

### 1. 认领闸门（`src/domains/runtime/engine/service.py`）
`_has_verified_executor` 先显式判断 `probe_status == "spawn_failed"` 返回 False，
再走原 `worker_version`/`probe_status` 口径——避免带 worker_version 的远端引擎被短路放行。
状态驱动：ok 探活写回 `probe_status='ok'` 即自动出黑名单。

### 2. 时间窗追踪（`src/domains/runtime/engine/recovery.py` + `routes/runtime.py`）
新增 agent_runtimes 列：`spawn_fail_since` / `spawn_fail_warned_at` /
`spawn_fail_degraded_at` / `probe_ok_streak`。
心跳 probe 分支调用：
- `note_spawn_failed`：首次记 since；连败次数或时长达阈值 → 一次强告警（日志 + 审计 +
  probe_error 指引）；持续时长超第二阈值 → 一次 degraded 标记（UI 高亮，healthy 不动）。
- `note_probe_break`：fail/model_unreachable/skip/pending 断链清零。
- `note_probe_ok`：累计连续 ok；达 M 次且存在告警/降级/解绑事实 → 自动加回。

### 3. 恢复加回 + 对账（`recovery.py`）
- `_readd_scene_bindings`：按 `scene_engine_detach_log` 未 rebound 行重建场景节点
  `engine_runtime_ids`，处理后写 `rebound_at`（幂等）。
- `_reconcile_task_bindings`：cancelled 且无 completed_at → 回写终态 + errors 留痕；
  dispatched 且 started_at 空（worker 从未收到）→ 释放为 queued/runtime_id=0；
  busy_slots 按真实 active 收敛。
- 全过程写 `grape_t_engine_lifecycle_audit`（kind=recovered/manual_readd 等）。

### 4. 一键兜底
- 后端：`POST /api/settings/agent-runtimes/{rid}/readd`（可见性复用引擎列表过滤）。
- 前端：`runtimes.js` 引擎卡片 degraded 高亮 + 「一键加回」按钮；加回后刷新列表。

### 5. 数据库
`src/api/db_setup.py`：新增 `grape_t_engine_lifecycle_audit` 表 + 幂等迁移
`202611150003 issue2133-engine-recovery-schema` 补列/建表；`src/kernel/db.py` 注册表同步。

## 依赖关系
心跳侧计数 → 认领闸门读取 → 探活恢复触发加回 → 加回触发对账（单向链路，无循环）。

## 测试
`tests/runtime/test_issue_2133_recovery.py`：闸门、时间窗阈值、恢复加回 + 对账幂等、
#1982 复现全链路。