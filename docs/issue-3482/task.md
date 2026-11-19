# Tasks: Issue #3482（按 comment 202239010 修订范围实施 A→B→C→D）

## 第一轮（下游兜底，已合入本分支 d4cbf129f，保留为最后防线）

- [x] T1 repogit：`already exists` 特征识别 + `_recover_worktree_already_exists()`（迁移/重建/合并回收，会话级 gitdir 兜底）
- [x] T2 repogit：`ensure_issue_worktree` 失败分支接入恢复（不进 refuse degrade）

## 第二轮（按修订 Spec，主修根因）

- [x] T3 **A 物化整体下移**：`_materialize_workdir_and_skill_gate()` 抽出 `.skills`/`.project-info`/`.knowledge`
      物化 + `_skill_consistency` 校验 + prompt 回写，`_execute_agent_inner` 改为建树之后调用
- [x] T4 **B 建树前置处置**：`dispose_occupied_workdir_target()`（stat 判据）接入 `ensure_issue_worktree`：
      仅平台白名单产物 → 清空重建；含未知内容 → `<dir>.stale-<ts>` 隔离留证；处置失败 → None
- [x] T5 **C 关闭 fail-open**：direct `fail_task` + `_fail_event(code=workspace_unavailable)`；
      worker `_error` 返回；`inject_git_write_context` 对 `git dir not found` 不再置 `GRAPE_GIT_READONLY`
- [x] T6 **D 判据改判定源**：`readonly_errno_reason()`（stat + EROFS/EACCES/EPERM），
      `_is_readonly_git_failure` 降为日志线索
- [x] T7 测试：`tests/runtime/test_repogit.py` 重写 #3482 用例（B 清空/隔离/失败→None、最后防线、D errno、C missing-gitdir）
- [x] T8 测试：新增 `tests/runtime/test_issue_3482_workspace_order.py`（direct/worker 双路径顺序看护 + fail-closed）
- [x] T9 回归：`tests/runtime/` 全量通过
- [ ] T10 提交 feat/issue-3482、更新 PR #1825 说明、Issue 评论告知、A2A QA 重新审查