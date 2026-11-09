# Plan — Issue #656 末节点自动结单（issue 级收尾迁移 + VCS Issue 关闭联动）

> 目标分支：weekly-08-mys｜改动文件：issues_center.py、runtime/service.py、event_pipeline.py、
> routes/issues.py（description）、help/api/11-issues.md + 新测试。可整体 revert 回滚。

## 1. 共享函数（src/api/issues_center.py）

新增 `auto_finalize_issue_on_last_node(repo_key, issue_id, agent_type, source="pipeline") -> str`：

判定（全部满足才自动结单）：
1. issue_id 规范化为纯数字编号（PR 订阅伪 issue `#pr_...` 自动 noop）;
2. issue.status ∈ (completed/cancelled) → `noop:terminal`；== pending_review → `noop:pending_review`（人工确认路径，spec 保留）;
3. get_issue_stage：当前 stage == 完成的 agent_type，且该节点为流水线末节点
   （`next == "done"` 或 next 为空/不在节点 id 集合中，复用 `_is_last_stage_node` 思路并扩展无下游判定）;
4. 事件全完成检测：按 (repo_key, "#N") 查 events，按 agent_type 取最新事件——
   任一 running/dispatched → `noop:running`；任一 failed → 回退 `pending_review`（保留 #51 人工确认语义）返回
   `pending_review_fallback`；每个流水线节点须存在最新 completed 事件，否则 `noop:node_pending:<tag>`。

迁移（幂等）：
- `UPDATE issue SET status='completed', completed_reason='auto_finalized', completed_at=datetime('now') WHERE status IN ('active','processing','awaiting_input')`；
- `set_issue_stage(repo_key, "#N", "done", source)`；
- `_close_vcs_issue_auto()`：bot token（gitcode: managed_repos.bot_gitcode_token；github: get_github_token）
  → VcsApiClient.close_issue，先 get_issue_state 幂等检查，失败仅记日志不阻断（降级，与 confirm-done 同语义）。

## 2. 双路径接入（替换原「末节点→pending_review」硬编码）

- `src/runtime/service.py complete_task()`：Issue #51 末节点 pending_review 块 → 调
  `auto_finalize_issue_on_last_node`（函数内部含 failed 阻断回退 pending_review）;
- `src/api/event_pipeline.py` 本地 exec 完成回调：仅 sre 硬编码块 → 泛化为任意 agent 完成时调用同一函数
  （函数自带 stage/末节点守卫，非末节点 noop 无副作用）。

## 3. 人工确认触发条件标注

- `routes/issues.py` confirm-done 路由 description：补充「仅显式需人工确认场景使用（自动结单为默认路径）」;
- `help/api/11-issues.md`：confirm-done 小节与「task complete ≠ Issue 完结」口径更新为
  「末节点完成且无阻断 → 自动结单；pending_review/阻断场景 → 人工 confirm done」。

## 4. 测试（tests/test_issue_656_auto_finalize.py）

- 末节点完成 + 全节点 completed → auto_done：status=completed、stage=done、completed_at 非空、
  completed_reason=auto_finalized、VCS close 被调用;
- failed 阻断 → pending_review 回退，不关 VCS;
- running 事件 → noop（保持 processing）;
- issue 已 pending_review → noop（人工路径不自动结单）;
- 中间节点完成 → noop;
- 幂等：已 completed 再次调用 noop:terminal。

## 风险

- 误触发：仅末节点 + stage 匹配 + 全节点 completed + 非 pending_review 多重守卫，中间节点/显式待确认场景均 noop;
- VCS 关闭失败：降级不阻断（与 confirm-done 同语义），issue 本地已完成;
- 与 #51 语义反转：spec 明确默认自动结单，人工确认仅保留显式场景。
