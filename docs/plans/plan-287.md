# Plan: Issue #287 — 实现步骤

## Phase 1: 修改 src/runtime/scheduler.py
1. `_execute_autopilot_remote`：`enqueue_task` 调用前新增 skill_id 预解析（`resolve_skill_id(skill_name, repo_key, user_id=task["created_by"])`），失败 warning 降级
2. `enqueue_task(...)` 传 `skill_id=_skill_id`；`user_id=0` 不动

## Phase 2: 测试 tests/test_issue_287_skill_preresolve.py
1. user scope skill（owner_id=created_by）→ enqueue_task 收到非零 skill_id、user_id==0
2. 不可解析 skill → skill_id=0、任务仍入队（不阻断）
3. 回归：official/repo scope 解析路径不受影响（resolve_skill_id 既有测试）

## Phase 3: 收尾
1. 自审查（5 轴）→ 提交推送 → 建 PR（base=integration branch）→ Issue 评论 → A2A QA
