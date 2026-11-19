# Plan: Issue #3482 direct/worker 建树时序对齐 + 前置处置 + fail-closed

## 改动落点

| # | 文件 | 改动 |
|---|---|---|
| 1 | `src/domains/runtime/engine/direct_executor.py` | 新增 `_materialize_workdir_and_skill_gate()`（A）：物化 `.skills`/`.project-info`/`.knowledge` + `_skill_consistency` 校验 + prompt 回写整体下移；`_execute_agent_inner` 在 `ensure_issue_worktree` 之后调用它，建树失败走 C 的 fail-closed 分支（`fail_task` + `_fail_event`，code=`workspace_unavailable`） |
| 2 | `src/domains/runtime/engine/repogit.py` | (B) 新增 `dispose_occupied_workdir_target()` + 白名单常量 `_PLATFORM_ASSET_NAMES/_PREFIXES`，`ensure_issue_worktree` 在建树前按 stat 分流处置；(C) `ensure_worktree_git_writable` 增 `mode=missing`，`inject_git_write_context` 对 missing 不再置 `GRAPE_GIT_READONLY`（改置 `GRAPE_GIT_WORKSPACE_MISSING`）；(D) 新增 `readonly_errno_reason()`，worktree add 失败改按 errno 判只读，stderr 文案降为日志线索 |
| 3 | `src/domains/runtime/engine/agent.py` | (C) worker `_execute_task_impl` 捕获 `ensure_issue_worktree` 返回值，None/异常 → `{"_error": "[workspace_unavailable] ...", "_started": False}`（与 `WorkspaceBaselineError` 同口径），不再 `log.warning` 后继续 |
| 4 | `tests/runtime/test_repogit.py` | 重写 #3482 用例：B 前置处置（平台产物清空 / 未知内容 `.stale-<ts>` 隔离 / 隔离失败→None）、最后防线单测、D 的 errno 判据（EROFS/EACCES/EPERM vs ENOSPC/EIO）、C 的 missing-gitdir 不置只读 |
| 5 | `tests/runtime/test_issue_3482_workspace_order.py` | 源码级顺序看护：direct 端 `ensure_issue_worktree` 先于物化；物化与 `_skill_consistency` 同函数（不能只移一半）；direct/worker 双路径 fail-closed 与 worker 既有顺序 |

## 关键设计

- **顺序（A）**：`get_agent_workdir()` 与 `issue_dir_for()` 指向同一目录（`{ws}/issues/<n>/<agent>`），
  因此物化写的就是 worktree 根目录——顺序必须「先建树、后物化」。
- **判据（B/D）不用文案**：B 用 `os.listdir` + 白名单分流；D 用实写探测捕获 errno。
  `_is_already_exists_collision` / `_is_readonly_git_failure` 仍保留，但只用于日志与最后防线触发。
- **C 的失败语义**：direct 端 `fail_task()`（写 task failed + 事件级联）优先于裸 SQL；
  worker 端沿用 `_error` 返回由 `_wrap` 上报 failed，两条路径同为「显式失败」。
- **最后防线**：`_recover_worktree_already_exists()`（迁移 → 重放 worktree add → 合并产物 →
  失败回滚 → 会话 gitdir 兜底）保留，覆盖前置处置之后仍撞 `already exists` 的竞态。
- **行为变化**：预置「仅平台产物」的目录不再保留 `.skills`——清空后由本轮物化重新生成
  （A 保证物化在建树之后必然执行），因此旧用例「产物保留」断言按新语义调整。
- **`.stale-<ts>` 生命周期**：隔离目录不会被误删——`cleanup_issue_worktree` / 巡检只处理
  `_GIT_AGENTS`（coding/qa/sre）命名的目录，`coding.stale-*` 不在其列；留证后由人工清理。

## 测试

- `python3 -m pytest tests/runtime/test_repogit.py tests/runtime/test_issue_3482_workspace_order.py tests/runtime/test_issue_2245_git_writable.py -q`
- `python3 -m pytest tests/runtime/ -q`（回归范围）
- 编译检查：`python3 -m py_compile src/domains/runtime/engine/{repogit,direct_executor,agent}.py`