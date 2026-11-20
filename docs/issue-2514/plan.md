# Plan: Issue #2514 — 工作区与 Issue/基线绑定

> 实现蓝本：PR #1176（#1939，closed 未合入）。本方案按其 diff 在 `weekly-mys` 基线重建，
> 并按当前代码形态适配（本基线 `cleanup_issue_worktree` **已有** #2041 归档守卫：
> 命中风险才 bundle+patch 保全、失败即拒绝删除；新语义在其之上再补一层按 task_id
> 命名的终态证据存档，两条链并存——守卫负责“删除前风险判定与拒绝”，新存档负责
> “终态可核对的 `workspace-<task_id>.patch`”；形参仅做兼容扩展）。

## 方案概述

在既有 `ensure_issue_worktree` 内加「标记 → 分支 → 脏树」三道复用闸门 + 基线硬校验，
补齐终态清理与派发前巡检；不新造模块，不改变 `grape git` 凭据路径。

## 关键实现

### 1. 归属标记（`repogit.WORKSPACE_MARKER`）

`.grape-workspace.json` 写 `issue_number/agent/baseline/task_id/created_at`，并写入
per-worktree `git rev-parse --git-path info/exclude`——防 Agent `add -A` 误提交、防
`git status` 误报脏。标记缺失或与当前 Issue/agent 不符 = 历史残留（`stale_marker`）。

### 2. 复用闸门（`ensure_issue_worktree`）

命中任一即 `_refuse_reuse`：

| 闸门 | 判据 | reason |
|------|------|--------|
| 标记 | 缺失 / `issue_number` / `agent` 不符 | `stale_marker` |
| 分支 | `HEAD != feat/issue-{N}`（inspector 的 detached HEAD 放行） | `wrong_branch` |
| 脏树 | `git status --porcelain -z` 非空（轻文件与 askpass 白名单除外） | `dirty` |

拒绝处置：`git diff HEAD --binary` + untracked **文件内容**（逐文件
`git diff --no-index --binary`，可 `git apply` 还原）存档 →
`git worktree remove --force` → `worktree prune` → 重建。存档失败或移除失败（只读 gitdir）→
记 warning 保留原目录，不无限重试；保留的外来脏树**不重写归属标记**。日志统一
`workspace.reuse_refused reason=... dirty_files=N`。建树/重建/终态清理/巡检统一使用
`_repo_lock(realpath(repo_base))` 同一把仓库锁。

### 3. 基线闸门（`_validate_issue_baseline`）——**修正 #1176 的 Critical**

- 基线**只解析一次**：`_resolve_base_branch` 返回 `(resolved, requested, fallback_reason)`。
  `requested`（场景注入 `GRAPE_WORK_BRANCH` / 场景 DB）在 bare mirror 中核对
  `refs/remotes/origin/<requested>`；不存在 → 回退默认分支，并记
  `workspace.baseline_fallback requested=... resolved=... reason=...`。
- 闸门的 `expected` **只用 resolved**，另带 `requested_baseline` 作溯源字段；
  不再让注入值直接覆盖 expected（#1176 覆写 → 闸门恒失败的根因）。
- `fetch origin <resolved>` 失败仅告警（只读卷 #2245/#1891），判据用 mirror 已有 refs；
  `rev-list --left-right --count HEAD...origin/<resolved>`：
  - `0/0` 直通；`0/b>0` → `merge --ff-only` 并记 `workspace.baseline_fast_forward`；
  - `a>0 && b>0`（分叉）或 HEAD 不可达基线 → `WorkspaceBaselineError`
    （`issue/path/expected_baseline/actual_head/ahead/behind/reason` + `requested_baseline`）。
- inspector（qa）工作区按交付 sha 对齐（detached HEAD），不走基线闸门。

### 4. 终态清理

`remove_issue_worktree_if_pushed(..., task_id=)` 语义 = 任务终态必清：脏树先落
`workspace-<task_id>.patch`（含 untracked 内容）、未推提交追加 `log -p remote..HEAD`
存档，再走 `cleanup_issue_worktree` 删除目录。基线 `cleanup_issue_worktree` 已有
#2041 归档守卫（`archive_worktree_before_reset`：bundle + patch + untracked 快照，
命中风险才触发、失败即拒绝删除），新存档在其之前完成，形成“终态固定命名证据 +
守卫风险保全”两条链；**新存档失败则保留现场不删**，宁可残留不丢码。
清理失败只告警，不阻断终态上报。

### 5. 派发前巡检（`patrol_workspaces`）

worker 启动 + `_run_loop` 每 `GRAPE_WORKSPACE_PATROL_INTERVAL`（默认 3600s，下限 60s）：

- 逐 repo `worktree list --porcelain` 对账 + `worktree prune`；
- 回收「超 `GRAPE_WORKSPACE_TTL_HOURS`（默认 24）+ 无活跃任务」的**带标记**目录（脏树先存档；存档失败或状态不可读 → 保留不删）；
- 无 `.git` 的空目录按 orphan 回收；无标记的非空目录只计数不删（人工 worktree 防误删）；
- 活跃任务 cwd（`active_task_objs`）走白名单保护。

## 改动清单

| 文件 | 改动 |
|------|------|
| `src/domains/runtime/engine/repogit.py` | 标记/闸门/基线校验/终态清理/巡检 |
| `src/domains/runtime/engine/agent.py` | 派发前闸门阻断、终态清理传 `task_id`、派发前+启动巡检 |
| `src/domains/runtime/engine/direct_executor.py` | direct 入口同语义（同 task_id/strict，基线错误上抛） |
| `src/domains/runtime/pipeline/event_pipeline.py` | `_cleanup_direct_workdir(task_id=)` 透传 |
| `tests/runtime/test_issue_2514_workspace_binding.py` | 新增回归（真实本地 git，零网络） |
| `tests/runtime/test_issue_2514_wiring.py` | 复审补测（direct_executor 基线阻断 / agent 巡检接线） |

## 开关

| 变量 | 默认 | 语义 |
|------|------|------|
| `GRAPE_WORKSPACE_STRICT` | `1`（开） | `0` = 基线分叉降级 warn 继续复用 |
| `GRAPE_WORKSPACE_TTL_HOURS` | `24` | 巡检回收阈值 |
| `GRAPE_WORKSPACE_PATROL_INTERVAL` | `3600` | 派发前巡检节流（秒） |