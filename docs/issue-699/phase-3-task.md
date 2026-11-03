# Phase 3 Task Breakdown（Issue #699）

| # | 任务 | 产出 | 状态 |
|---|------|------|------|
| 1 | 文档 | docs/issue-699/phase-3-{spec,plan,task}.md | ☑ |
| 2 | direct_executor.py | _DIRECT_POOL/_DIRECT_FUTURES/start_direct_agent/_execute_agent/_execute_agent_inner 迁移 | ☑ |
| 3 | import 路径 | local_claimer/webhook/events/pr_merged_sre_retry/tests → direct_executor；src/api shim 补 re-export | ☑ |
| 4 | 5 处调用点 | event_pipeline start_direct_agent → _enqueue_pipeline_task | ☑ |
| 5 | 认领闭环 | service.claim(deterministic_only) + local_claimer 确定性认领 + legacy 本机回退 | ☑ |
| 6 | 测试 | tests/test_issue_699_phase3_direct_executor.py（TC-3.1/3.2/3.3/3.4/3.5） | ☑ |
| 7 | 回归 | 相关 pytest 子集 + scripts/check-arch.py 通过（既有失败不含新失败） | ☑ |
| 8 | 收尾 | push feat/issue-699 → PR #836 追加 Phase 3 → Issue 评论 → A2A QA | ☑ |
