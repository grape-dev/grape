# Spec: Issue #3482 — direct 执行路径 worktree 目标目录被占用误降级只读

> 需求权威源 = Issue #3482「📋 Spec Document」评论（comment id 202229615）。本文件为开发落地映射摘录，不改动裁定。

## 1. 目标

- direct 执行路径（local_claimer）`git worktree add` 失败处理中，对 `fatal: '<path>' already exists`（目标目录已存在且无 `.git`，典型为 `.skills` 技能物化产物占用）新增识别与恢复分支，不再落入 `non-readonly failure → refuse degrade` 分支。
- 恢复后 `inject_git_write_context()` 正确解析 gitdir，不再误置 `GRAPE_GIT_READONLY=true`，写型 coding 任务恢复 commit/push/建 PR 能力。
- worker `_execute_task` 路径同场景行为一致（对齐 #711，见 docs/new-version1/coding-worktree-skill-collision.md）。

## 2. 根因

- direct 路径 `_execute_agent_inner`（src/domains/runtime/engine/direct_executor.py）中 `_materialize_direct_workdir`（写 `.skills/`，L258）先于 `ensure_issue_worktree`（L438）执行；
- 目标目录存在且非空 → `git worktree add` 报 already exists → 不匹配 `_is_readonly_git_failure` → refuse degrade 返回 None → 无 `.git` → `inject_git_write_context` 判 `git dir not found` → 只读降级。
- worker 路径 #711 已改为「先 worktree 后技能」；但共享函数 `ensure_issue_worktree` 对「目录已被占用」仍无恢复能力，direct 路径顺序未改（物化在 prompt 构建期），故在共享函数内补恢复分支，双路径同时受益。

## 3. 验收（Spec Acceptance Criteria）

- [ ] 预置 `issues/<n>/coding/.skills`（无 `.git`）触发 coding 任务：worktree 创建成功且可 commit/push/建 PR；
- [ ] `already exists` 不再进入 refuse degrade 分支，恢复日志明确（迁移/重建/兜底）；
- [ ] 恢复后 `GRAPE_GIT_READONLY` 不被误置；
- [ ] worker `_execute_task` 路径同场景回归通过（共用 `ensure_issue_worktree`，测试覆盖）；
- [ ] 现有测试全通过；新增 already exists 恢复分支测试。

## 4. 红线（Out of Scope）

- 只读文件系统/权限类（read-only fs、permission denied）降级语义不变；
- `.skills` 物化机制写入时机不调整；物化产物保留（迁移不删除）；
- 非 git 类 agent 执行路径不动。
