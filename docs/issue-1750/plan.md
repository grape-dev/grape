# Plan: Issue #1750 — worker 自报活跃白名单与回收时机收紧

## 测试 seam
- 服务端：直接调用 `runtime_recover_orphans` + 内存 SQLite（复用 `test_recover_orphans_*` 的真实表结构），断言 `recovered/skipped_active/skipped_young` 与任务行状态。
- worker：`_reregister` 与 `_recover_orphans_on_rid_change` 注入 `_FakeClient`，断言是否调用、调用参数（rid 与白名单）。
- client：Mock `aiohttp` session，断言请求 body。

## 切片拆解（TDD）

### 切片 1：服务端白名单判定（P1-A）
- 红：`test_a1_silent_task_in_whitelist_skipped_active`、`test_a1_whitelist_priority_over_grace_gate`。
- 绿：`RecoverOrphansBody.active_task_ids`；判定链在年龄闸门前插入白名单命中分支；`skipped_active_reason` 区分 `active_whitelist` / `recent_activity`。

### 切片 2：真孤儿与 attempts 语义
- 红/绿：`test_a2_true_orphan_recovered_with_empty_whitelist`、`test_a4_attempts_single_increment`、`test_a3_whitelisted_task_not_pooled`。
- 保持既有 SQL 参数化更新与事件回写不变。

### 切片 3：worker 回收时机（P1-B）
- 红：`test_a5_periodic_reregister_does_not_recover`、`test_a5_startup_empty_whitelist_recovers`、`test_a5_rid_change_recovers_with_whitelist`、`test_a5_rid_unchanged_does_not_recover`、`test_a5_heartbeat_failure_reregister_keeps_whitelist`。
- 绿：`_reregister(..., recover_orphans=False)`；`_recover_orphans_on_rid_change`；主循环周期路径与心跳失败重注册路径（审查修复：同进程重连不再默认空白名单无条件回收）先记录旧 rid、重注册后再按集合变化携带白名单回收；启动仍空白名单回收。

### 切片 4：client 透传与 E2E
- `test_client_recover_orphans_default_body_empty`、`test_client_recover_orphans_passes_whitelist`。
- `test_a6_silent_task_survives_periodic_cycle_then_completes`。

## 提交计划
1. `feat(#1750): add worker active whitelist to recover-orphans`
2. 回归 `tests/test_issue_1750_recover_orphans_active_whitelist.py` + 既有 recover/issue_417/task_activity 用例。

## 验收对照
| AC | 落点 |
|----|------|
| A1 | 切片 1 两用例 |
| A2 / A3 / A4 | 切片 2 |
| A5 | 切片 3 |
| A6 | 切片 4 |
| 既有回归 | `test_recover_orphans_slots.py`、`test_recover_orphans_optimization.py`、`test_issue_417_revoked_runtime_residue.py`、`test_issue_1495_task_activity_format.py` |
