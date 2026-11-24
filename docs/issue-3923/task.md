# Issue #3923 任务拆解与完成状态

| # | 任务 | 状态 | 载体 |
|---|------|------|------|
| 1 | 复现：HEAD 指 `refs/remotes/*` 时 `worktree add` 必报 `HEAD not found` | ✅ | 本地 bare 仓实测 |
| 2 | 归因三态（resolved/none/unavailable/unattributed） | ✅ | `src/kernel/token_resolver.py` |
| 3 | 裸仓 HEAD 校验 + 自愈（建树前） | ✅ | `repogit.ensure_bare_head` / `ensure_repo_base` |
| 4 | `worktree add` 失败自愈重试一次 | ✅ | `repogit.ensure_issue_worktree` |
| 5 | 失败现场 + 错误面透传 git 原文与阶段 | ✅ | `worktree_failure_error` / `agent.worktree_block_result` |
| 6 | 删除猜测性文案 | ✅ | `worktree_credential_error` |
| 7 | 新增 15 条用例（自愈/重试/三态/反向回归） | ✅ | `tests/runtime/test_issue_3923_worktree_head_and_attribution.py` |
| 8 | 既有相关测试回归 | ✅ | 118 passed（含 #3713 双入口、#2245、#2514） |
| 9 | 自审查（五轴）+ 开源前置增量检查 | ✅ | 见 PR 说明 |
| 10 | 建 PR + Issue 评论 + A2A 交办 QA | ⏳ | 见 Issue 评论 |
