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
- [x] T10 提交 feat/issue-3482、更新 PR #1825 说明、Issue 评论告知、A2A QA 重新审查

## 第三轮（QA 第二轮复审：2 Required + 3 Optional + 1 Nit 全处置）

- [x] T11 **Required（读侧随写侧下移）**：#564/#887 agent_logs 标注读取块从
      `_execute_agent_inner`（物化之前、恒读空值）搬进 `_materialize_workdir_and_skill_gate()`
      物化之后执行（`agent_logs` 参数传入原地追加），direct 会话事件恢复
      `skill_materialize` / `skill_source` 两行标注（与 worker 物化后上报同口径）
- [x] T12 **Required（只读判据扩探测面）**：`readonly_errno_reason(target, base)` 同时探测
      目标目录与共享基线 `repo_base_dir`（命中 EROFS/EACCES/EPERM 即只读，返回值
      `target:`/`base:` 前缀区分位置）；`ensure_issue_worktree` 建树失败分支传 `base`——
      #2245「只读基线 + 可写目标」恢复会话级 gitdir 降级可达，不再退化为整任务
      `workspace_unavailable`
- [x] T13 **Optional（双路径行为用例）**：验收 4 升级行为级——monkeypatch
      `ensure_issue_worktree` 返回 None，direct 断言 `fail_task` + `_fail_event(code=workspace_unavailable)`、
      worker 断言 `_error`/`_started=False`
- [x] T14 **Optional（同名旧副本移除）**：event_pipeline 的过期 `_execute_agent`/`_execute_agent_inner`
      副本（旧顺序、缺 #2514/#2225/#3482 修复）与第二套 direct 池/影子锁/提交入口删除，
      完成调用方迁移（routes/events ×5、routes/webhook、api/a2a_dispatch 直连
      direct_executor），旧 import 路径经 `src.api.event_pipeline` 兼容层 re-export 同对象；
      #699 迁移契约测试（ReExport/TC31/TC32）由红转绿
- [x] T15 **Optional（交付记录同步）**：task.md 勾选 T10、PR 描述同步分支 tip
- [x] T16 **Nit**：`_materialize_workdir_and_skill_gate` 前补 PEP8 E302 空行
- [x] T17 经验沉淀：`AGENTS.md` 写入「先建 worktree、后物化技能」仓内可执行约束
- [x] T18 回归：#3482/#2245/repogit 相关 6 测试文件通过；`make lint`、`scripts/check-arch.py` PASS