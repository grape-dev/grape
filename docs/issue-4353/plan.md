# Plan: Issue #4353 工作区建树前置判定 + 拒绝降级/只读互斥

## 改动落点

| # | 文件 | 改动 |
|---|---|---|
| 1 | `src/domains/runtime/engine/repogit.py` | 新增 `classify_worktree_target()` / `prepare_worktree_target()` / `_registered_worktree_paths()` / `_gitdir_belongs_to_repo()` / `_prune_worktree_registrations()` / `_quarantine_worktree_dir()`；`ensure_issue_worktree()` 用「三路径判定」替代 `os.path.isdir(.git)` 单判据；新增 `WORKSPACE_WORKTREE_CONFLICT` 常量与 `mark_degrade_refused()` / `degrade_refused()` 单一决策点；新增 `describe_git_write_mode()` 统一 direct/worker 日志口径；`inject_git_write_context()` 对「本轮已拒绝降级」不再置 `GRAPE_GIT_READONLY`（改置冲突标记）；`worktree_failure_error()` 附 `error_code`；`_recover_worktree_already_exists()` 重放前 prune、并支持「目标已被前置处置移走」的直重放 |
| 2 | `src/domains/runtime/engine/direct_executor.py` | 建树失败面补 `error_code=WORKSPACE_WORKTREE_CONFLICT`（`_fail_event` diag + 任务错误文案）；只读自检日志改走 `describe_git_write_mode()`，`_task_view` 带上 `worktree_dir`/`task_id` |
| 3 | `src/domains/runtime/engine/agent.py` | worker 侧同口径：建树失败面补 `error_code`；只读自检日志走 `describe_git_write_mode()`，`worktree_dir`/`task_id` 透传 |
| 4 | `tests/runtime/test_issue_4353_worktree_conflict.py` | 新增回归：复用 / 残留清理（含 registered 残留）/ 真冲突 / 拒绝降级与只读标记互斥 / error_code 透传 / 跨轮重派 |
| 5 | `docs/issue-4353/*` | Spec、方案、任务拆解 |

## 关键设计

### 1. 三路径判定（为什么不能只 stat `.git`）

- 旧判据 `os.path.isdir(issue_dir/.git) or os.path.isfile(...)` 把「`.git` 指针指向已删除
  gitdir」算成可用工作区 → 假复用 → 后续 `git status` 失败被当「只读」；
- 合法 worktree 的 admin dir 里**没有** `objects/`（对象库在 common dir），判据必须走
  `git_common_dir()`：common dir = 本仓基线 → 本仓 worktree；或 `.git-session`
  在目录内部 → 会话级 gitdir 也是合法复用对象；
- `git worktree prune` 只回收「路径已不存在」的登记：残留目录必须先整体移走再 prune，
  否则登记仍在、`worktree add` 仍报 already exists（本地 git 实测：登记 + 目录存在 → fatal；
  移走后 prune → add 成功）。

### 2. 拒绝降级的单一决策点

`refuse degrade` 与 `read-only mode marked` 原本是两处互相独立的判定，靠调用顺序「碰巧」
不能同时出现。改为状态驱动：

```
建树侧：mark_degrade_refused(repo, issue, agent, reason, code=WORKSPACE_WORKTREE_CONFLICT)
会话侧：degrade_refused(...) 命中 → mode=conflict / GRAPE_GIT_WORKSPACE_CONFLICT=true
        且 pop 掉 GRAPE_GIT_READONLY*  → 只出 error_code=WORKSPACE_WORKTREE_CONFLICT
```

状态按 `(repo, issue, agent)` 三元组隔离（并发任务互不覆盖），TTL 900s 只覆盖同轮窗口，
建树成功/入口清理时清除，跨轮不继承上一轮的拒绝判定。

### 3. 目录命名（Spec 的「等价去重」分支）

工作区路径的写入口分散在 8 处（`_enqueue_pipeline_task` / `create_failed_task` /
`_create_direct_task` / `_cleanup_direct_workdir` / redispatch / takeover / `_cwd_or_derived`
/ direct executor），而 worker 的 agent cwd 取自**入队时**算好的 `agent_tasks.cwd`；
`_enqueue_pipeline_task` 在拿到 task_id 之前就要算 ws。把 task_id 塞进目录名 = 让
「构造路径」与「执行 cwd」成为两个真源（#3482 复盘过的同类缺陷）。

所以采用 Spec 允许的「等价去重」：

- 残留目录每轮必被处置（prune + 移走），同一路径不再是「残留命中点」；
- 移走目录名 `<dir>.stale-<task_id>`（缺省 `<dir>.stale-<utc>`），跨轮留证不互相覆盖；
- 归属标记与三条路径结构化日志均带 `task` 字段，派发身份可观测。

### 4. 不做的事

- 不改 `get_agent_workdir()` / `issue_dir_for()` 的布局（守 #3482 的单一真源与
  `detach/takeover` 依赖）；
- 不删除 `.stale-*` 隔离目录（#3482 的「迁移不删除」红线，留证优先）；
- 不动 `_refuse_reuse` 的 #2514 三闸门语义（脏树/错分支/无标记仍拒绝复用并隔离重建）。

## 测试

```bash
python3 -m pytest tests/runtime/test_issue_4353_worktree_conflict.py -q
python3 -m pytest tests/runtime/test_repogit.py tests/runtime/test_issue_3482_workspace_order.py \
  tests/runtime/test_issue_2245_git_writable.py tests/runtime/test_issue_2514_workspace_binding.py -q
python3 -m pytest tests/runtime -q
make lint && python3 scripts/check-arch.py
```

## 行为边界（与 Spec「保留 `.skills`/`.project-info`」的对应）

Spec 的「安全移走并保留会话资产」在实现上分两种残留处理，判据沿用 #3482 的白名单：

| 残留内容 | 处置 | 会话资产去向 |
|---|---|---|
| **含未知条目**（用户文件 / 上一轮干活的改动） | 整体 `os.rename` 为 `<dir>.stale-<task_id>` 隔离留证，**不删除** | 保留在隔离目录内（不并回新工作区，避免未知内容污染新树） |
| **只含平台产物**（`.skills`/`.project-info`/`.knowledge`/`.grape-*`…） | 清空重建（#3482 既有语义） | 由**本轮**物化重新写入（建树 → 物化顺序由 #3482 保证，Agent 启动前已就位） |

为什么纯平台产物不走隔离：这些条目是可再生物，本轮必被重写；若每轮都归档，一个
1 小时 7 次残留的仓会在 `issues/<n>/` 下持续堆积完整 checkout 副本（`.git` 已丢，
但受管文件还在），磁盘风险高于留证价值。未知条目（不可再生）一律留证，符合
#3482「迁移不删除」红线。两者都不再让 `worktree add` 撞 already exists。

「清空式」处置后目录仍可能被 git 登记（prune 不回收仍存在的路径）——此时移走空壳、
prune 后**立即删除该空壳**，不产生空的 `.stale-*` 目录垃圾。

## 风险与缓解

| 风险 | 缓解 |
|------|------|
| 清理误删会话资产 | 只「移走」不删除；`.stale-<task_id>` 保留 `.skills`/`.project-info`；隔离失败 → 判冲突不硬删 |
| 把别人的工作树当残留处置 | `.git` 指向存在但不属于本仓的 gitdir → 判 `conflict`，不触碰 |
| 上一轮拒绝降级污染下一轮只读判定 | 状态带 TTL(900s) + 建树入口/成功即清；三元组隔离 |
| 每次建树多一次 `worktree list` 子进程 | 与既有建树期 git 调用同量级（秒级），只在建树入口各一次 |

## 待决策（数值证据，B2 五源闸门）

| 数值 | 位置 | 缺哪种测量 | 现状 |
|---|---|---|---|
| `_DEGRADE_REFUSED_MAX = 200` | `src/domains/runtime/engine/repogit.py` | 单节点「同轮在途 (repo, issue, agent) 三元组」峰值 | 200 ≈ 24KB，TTL 900s 为主兜底、超限按 at 最旧淘汰；未实测，登记为待决策 |
