# Spec: Issue #287 — 定时任务执行时个人级（user scope）skill 读不到（skill_id 预解析修复）

## Objective
定时任务（autopilot）触发执行时，服务端能正确解析并打包创建者个人级（user scope）skill 给执行引擎：在 scheduler 入队前按 `created_by` 预解析 `skill_id` 传入 `enqueue_task`。

- **根因**：`_execute_autopilot_remote` 以 `user_id=0, skill_id=0` 调 `enqueue_task`；user scope skill 的 `resolve_skill_id` owner_id 分支查不中 → skill_id=0 → 不打包 skill_files、不签 OBS zip URL → worker 侧 `.skills` 为空，agent 全盘瞎找 SKILL.md（失败实例 t_df0fc3c6b14d）。
- **成功标准**：user scope skill 定时触发后 `skill_files` 非空含 SKILL.md、`skill_obs_url` 正确签名、worker 物化 `.skills/<skill>/SKILL.md`；`_collect_server_env` 注入行为与修复前完全一致（user_id 保持 0）。

## Scope
- **In**：`src/runtime/scheduler.py` `_execute_autopilot_remote`：入队前 `resolve_skill_id(skill_name, repo_key, user_id=task["created_by"])`，结果作为 `skill_id=` 传给 `enqueue_task`（命中其已有 skill_id 优先分支，按主键查 skill、从 OBS 拉 zip 打包）；解析失败降级 + warning 不阻断。
- **Out**：不改 `user_id`（避免创建者 OAuth 凭据灌入无人值守 worker）；direct 路径 skill 解析（#289）；`_collect_server_env` 白名单化（#288）；`enqueue_task` 被调方零改动。

## Technical Approach
1. `_execute_autopilot_remote` 入队前预解析：`_skill_id = resolve_skill_id(skill_name, repo_key=repo_key, user_id=task.get("created_by") or 0)`；为 0 时 log.warning 降级。
2. `enqueue_task(..., skill_id=_skill_id, ...)`，`user_id=0` 保持不变。
3. 解析顺序 official → repo → user 与 `resolve_skill_id` 现有三级一致；既有 repo/official 定时任务回归无影响。

## Acceptance Criteria
- [ ] user scope skill（owner_id=created_by）预解析命中，enqueue_task 收到非零 skill_id，user_id 仍为 0
- [ ] 解析不中（created_by 注销/不存在/skill 删除）→ skill_id=0 + warning，任务照常入队
- [ ] 新增单测覆盖命中与降级两场景；既有测试全通过
