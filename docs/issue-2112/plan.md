# Issue #2112 方案设计

## 1. 分层

```
routes/org_dispatches.py   校验/权限/错误体/审计入口
        ↓
mgmt/org_dispatch.py       状态机 / 场景解析 / 建单 / 汇总 / 取消 / 重试 / sweeper
        ↓
issues_center.create_task_via_issue   （复用，不复制）
runtime/issue_cancel.cancel_issue_core（抽取，路由与下派共用）
```

## 2. 数据表（db_setup.register_migration，幂等）

- `org_dispatch(id, org_id, org_issue_number, org_repo_key, scene_id, status,
  plan_json, context_json, context_hash, idempotency_key, created_by, created_at, updated_at)`
- `org_dispatch_target(id, dispatch_id, repo_key, scene_id, child_issue_number,
  child_task_id, status, pr_number, error, started_at, finished_at, created_at,
  updated_at, UNIQUE(dispatch_id, repo_key))`
- 索引：`idx_org_dispatch_org(org_id, created_at)`、
  `idx_org_dispatch_target_dispatch(dispatch_id, status)`、
  `uq_org_dispatch_idem(idempotency_key)`（部分唯一索引，NULL 不参与）

状态机：dispatch `planning→dispatching→running→aggregating→completed|partial_failed|failed|cancelled`；
target `pending→creating→running→completed|failed|cancelled|skipped`。
所有推进用条件 UPDATE（`WHERE status IN (期望前任)`）保证 sweeper 与人工操作幂等。

`plan_json` 持久化 `targets/title/org_title/description/scene_id`——无子单目标重试重建时
按原 description 复原子 Issue 目标范围。

## 2.1 批次级幂等（审查 R1）

`idempotency_key = sha256(org_id : org_issue_number : sorted(targets))`，唯一索引兜底。
`create_dispatch` 的「幂等查重 → 插入」在同一 `db_lock` 临界区内完成，收口同进程
check-then-insert 竞态；跨进程由唯一索引兜底，冲突时回滚后按 key 回读复用。
同 targets 运行中重复下派返回 409，终态重复下派复用既有批次。

## 3. 建单

```python
repo_token = get_gitcode_token(repo_key) / get_github_token(repo_key)
internal_user = {"id": operator_id, "username": owner, "token": repo_token, "auth_type": "internal"}
result = await create_task_via_issue(TaskCreateBody(...), user=internal_user)
```

- 标题 `[Org #<org_issue_number>] <原需求标题>`；正文写组织需求仓库/链接、context 摘要。
- 复用 `create_task_via_issue` 内置同名 title 查重：命中 HTTP 409 且 detail 带
  `issue_number` 时视为复用（`created=False`），不重复建单。
- 建单成功回写 target 的 UPDATE 带 `WHERE status IN ('pending','creating','running')`；
  若并发取消已胜出（行仍 cancelled），best-effort 取消刚建出的子单，避免孤儿在途。

## 4. 场景解析

1. 请求 `scene_id` 且 `scene_exists_in_repo(scene_id, repo_key)`；
2. `get_repo_scene(repo_key)` 且 `scene_exists_in_repo`；
3. `len(get_scenes(repo_key=repo_key)) == 1` 时取唯一 active 场景；
4. 否则 target `failed(NO_TARGET_SCENE)`。

不直接写 SQL 读 `managed_repos`。

## 5. 汇总 sweeper

`server_tasks.start_mgmt_tasks` 注册 `org_dispatch_sweep_loop`（60s，mgmt 进程唯一），
`stop_mgmt_tasks` cancel 回收。`sweep_once()`：

- 读取非终态 dispatch；
- 对 running/creating target 读 `issue.status/stage`：cancelled→cancelled，
  completed 或 stage=done→completed；
- 应用层预计算 UTC cutoff 参数，`COALESCE(started_at, created_at) <= ?` 的
  `pending/creating/running` target → `failed(timeout)`（pending 以 created_at 起算，
  覆盖建单中途进程重启遗留的永久 pending，审查 R2/R2'；不得使用 SQLite 私有的
  `datetime('now','-7200 seconds')` 修饰符）；
- 重新汇总 dispatch 状态并写审计。

## 6. 取消

抽取 `src/domains/runtime/issue_cancel.py::cancel_issue_core(issue_id, user_id=...)`：
归属校验可选（路由传 user_id，下派传 None 由组织权限保证）。原
`cancel_issue` 路由改为薄封装，禁止双份实现。

`cancel_dispatch` 仅在**全部在途目标成功取消**后才把 dispatch 置 `cancelled`；
子单已完成（`cancel_issue_core` 抛 400）/取消失败/任务不可定位时，目标计入
`unresolved_targets`，dispatch 交 `_refresh_targets_from_issues` + `_aggregate_dispatch`
收敛（保持 running/partial_failed），接口返回 `ok=false`，避免「谎报已取消、target
与 dispatch 漂移、sweeper 不再收敛」（审查 R3）。汇总侧仅当**全部** target 均
cancelled 才判整批 `cancelled`，混合终态判 `partial_failed`。

## 7. 重试

仅 `status=failed` 的 target 可重试。已建子单 → 复用 `child_issue_number`，重置 target=running
并重新触发目标场景（`run_pipeline(issue_opened, _grape_internal_trigger=True)`）；未建子单
（NO_TARGET_SCENE/BOT_TOKEN_MISSING）→ 重走创建流程，不产生重复 dispatch。

- 重触发前先 `_cancel_inflight_agents` 终止该子单上 queued/dispatched/running 的旧
  agent（不置 Issue 取消），避免「超时但原 agent 仍在跑」时新老两次执行并发改同一
  仓库（审查 R4）；
- `_trigger_child_issue` 返回 `ok=false`（被 pipeline 跳过）时立即置
  `TRIGGER_FAILED`，不再停在 running 等 2h 超时；
- target 置 running 的 UPDATE 带 `WHERE status='failed'` 条件，防并发双重重试。

## 8. 审计

`auth/audit.py::EVENT_TYPES` 登记 6 个事件类型；detail 仅含 dispatch/org/repo/issue 等标识，
不含任何 token。

## 9. 测试 seam

HTTP seam 用 FastAPI TestClient（`src.api.main.app`）；建单 seam 为
`src.api.issues_center.create_task_via_issue`；场景 seam 为
`scene_exists_in_repo/get_repo_scene/get_scenes`；sweeper 直接调 `sweep_once()`。
