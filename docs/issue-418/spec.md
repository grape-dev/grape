# Spec: Issue #418 — A2A 派发早退裸置 failed，事件零诊断

> Coding Agent | 基于 Issue Agent 已发布 Spec 执行 | 目标分支: weekly-clean-mys | 开发分支: feat/issue-418

## Objective
统一 `src/api/routes/webhook.py` A2A 路径 4 处早退分支（pick_engine=None / auto-select 空集 /
auto-select 异常 / try 兜底）的失败回写，全部改走 `event_pipeline._fail_event(seq, reason, exit_code=1)`；
`_fail_event` 补 `exit_code` 形参与幂等说明（终态保护由 `update_event_complete` 承担）。
#369 任务侧判死路径（runtime/service.py `_fail_task_pool_unreachable` / `_fail_task_pool_unhealthy`
/ 回收超限）已在基线 commit 8c089fb95 落地 `_sync_event_failed`，本 Issue 补齐 agent_tasks.exit_code
并新增回归测试锁定行为。

## Scope

### In Scope
- `webhook.py` 4 处早退 + `_fail_scene_all_offline` 异常兜底处（同路径同模式）改走 `_fail_event`
- `event_pipeline.py` `_fail_event` 增加 `exit_code: int = 1` 形参（兼容既有调用点）
- `runtime/service.py` 两处 `_fail_task_pool_*` UPDATE 补 `exit_code=COALESCE(exit_code, 1)`
- 新增 `tests/test_issue_418_a2a_fail_event.py`（4 早退 + 幂等 + #369 判死，共 6 用例）

### Out of Scope
- #368 已修复的 `event_pipeline.py` scene-all-offline 分支（不重复）
- plugin 模式 `_on_handoff_dispatch` 事件驱动路径的裸回写（另立 Issue 跟进）
- worker 侧逻辑、status 枚举、数据迁移

## Technical Approach
- 复用 #368 已验证模式：`_fail_event` 写 `result`（人话原因，分支细化）/ `exit_code=1` /
  `completed_at`，并同步 `agent_tasks`（status=failed + COALESCE exit_code/errors）。
- 幂等：`update_event_complete` 终态保护（terminal→terminal 跳过）+ agent_tasks 更新限定
  非终态 → 双主/重复触发安全。
- reason 文案仅写早退类别与引擎上下文，不含敏感信息。

## Acceptance Criteria
- [x] 4 场景事件 result 非空、exit_code=1、completed_at 非空（test_4 早退用例）
- [x] 回归 broadcast/direct 正常派发（test_a2a_queue T1/T2 通过）
- [x] 终态幂等不被二次覆盖（test_terminal_not_overwritten）
- [x] #369 任务侧判死回写字段完整（test_pool_unreachable_syncs_event）
- [x] 现有测试通过（基线既有失败项与本次改动无关，stash 对照验证）
- [x] 新增测试 6 用例通过
