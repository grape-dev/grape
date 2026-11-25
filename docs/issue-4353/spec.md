# Spec: Issue #4353 — qa worktree add 因同名残留目录 already exists 失败后静默降级只读

> Issue: #4353 | Developability: ready | 目标分支: `arch-weekly-11-ljt`

## 目标

修复工作区准备链路（`src/domains/runtime/engine/repogit.py`）的两处语义缺陷：

1. `git worktree add` 前对目标路径**只做 `.git` 是否存在的一次 stat**，既没有「已是本仓
   合法 worktree → 复用」，也没有「残留目录 → `git worktree prune` + 安全移走」分支，
   于是同名残留目录必然撞 `fatal: '<path>' already exists`，任务直接失败。
2. 「拒绝降级（refuse degrade）」与「只读标记（read-only mode marked）」两个判定互斥却
   各自执行：建树侧已拒绝降级（返回 None 判失败），会话侧仍按「worktree 不存在」把会话标
   read-only 继续跑，任务终态可能 completed → **假通过**。

同时把三条路径（复用 / 清理 / 真冲突）做成可区分、可观测（结构化日志 + 稳定 error_code），
并有回归测试看护「refuse degrade 与 read-only mode marked 不同时出现」。

## 现状（生产实测，2026-11-25 12:00–13:00 CST，1 小时 7 次 / 3 条路径）

三条日志同毫秒成组出现：

```
worktree add failed: fatal: '<path>' already exists
repogit worktree add non-readonly failure, refuse degrade
task <tid>: git worktree is read-only (...), read-only mode marked
```

## 期望行为

### ① 建树前置判定（三条路径）

`git worktree add` 前先判定目标路径，输出 `absent | legal | residual | conflict`：

| 决策 | 判据 | 动作 |
|------|------|------|
| `legal` | `.git` 解析出的 gitdir 存在，且其 common dir = 本仓基线（或会话级 `.git-session`） | 复用，不重复 add |
| `residual` | 目录存在但无 `.git` / `.git` 指向已失效的 gitdir | `git worktree prune` → 安全移走（保留 `.skills`/`.project-info`）→ 再建树 |
| `conflict` | 路径不是目录 / `.git` 属于其它仓库 / 无法安全移走 | 立即 failed（`error_code=WORKSPACE_WORKTREE_CONFLICT`） |
| `absent` | 路径不存在 | 直接建树 |

要点：**登记表与磁盘两处都要看**。`git worktree prune` 只回收「路径已不存在」的登记，
残留目录必须先被移走再 prune，否则登记仍在、`worktree add` 仍报 already exists（本地实测）。

### ② 拒绝降级与只读标记互斥

- 拒绝降级 = 任务 failed + `error_code=WORKSPACE_WORKTREE_CONFLICT`，**不再**标只读继续跑；
- 「拒绝降级」写成本轮 `(repo, issue, agent)` 的显式状态，会话侧读该状态后只出冲突错误码，
  互斥由状态保证，不靠两个判定各自自觉；
- direct（`direct_executor`）与 worker（`agent`）两条执行路径日志口径统一。

### ③ 跨轮重派不再命中同一残留目录

工作区路径 `{WS}/{provider}/{owner}/{repo}/issues/{ident}/{agent_type}` 的写入口有 8 处
（enqueue / direct / redispatch / takeover / 推导 cwd），而 worker 端的 agent cwd 取自
**入队时**算好的 `agent_tasks.cwd`。把 task_id 塞进目录名会让「构造路径」与「执行 cwd」
分成两个真源（`_enqueue_pipeline_task` 在拿到 task_id 之前就要算 ws），正是 #3482 复盘过的
一类缺陷。因此本 Issue 采用 Spec 允许的「**等价去重**」：

- 残留目录在每轮建树前**必被处置**（prune + 移走），同一路径不再是「残留命中点」；
- 移走目录名带本轮标识 `<dir>.stale-<task_id>`，跨轮留证不互相覆盖；
- 归属标记（`.grape-workspace.json`）与三条路径的结构化日志都带 `task` 字段，
  跨轮派发身份可观测、可聚合。

### ④ 可观测性

三条路径各一条结构化日志（含 repo / issue / agent / task / path / decision / stale / error_code）：

- `workspace.worktree_target_absent` / `workspace.worktree_target_legal`
- `workspace.worktree_target_residual_cleaned`
- `workspace.worktree_target_conflict`（带 `error_code=WORKSPACE_WORKTREE_CONFLICT`）

## 验收标准

- [x] 目标目录已是本仓合法 worktree 时复用成功，不再报 `already exists`
- [x] 残留目录（非 worktree / `.git` 缺失或指向失效 gitdir）被 prune + 安全移走后正常建树，`.skills` / `.project-info` 未被破坏
- [x] 真正冲突时任务判 failed 并带 `error_code=WORKSPACE_WORKTREE_CONFLICT`，终态不出现 completed
- [x] `refuse degrade` 与 `read-only mode marked` 不再在同一任务日志中同时出现
- [x] 同名 PR 多次重派不再命中同一残留目录（残留必被处置，命名带 task_id 留证）
- [x] 三条路径均有回归测试，全量现有测试通过

## 范围外

- GitCode 平台侧 worktree 预置（`.git` 指向只读目录）——Issue #4307 另行处理；
- QA 审查逻辑/评分/证据格式；
- 历史已完成任务的状态回填或重跑。