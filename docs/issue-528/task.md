# Tasks: Issue #528

- [x] T1 helper `_inject_issue_pr_env` 实现（纯数字校验 + PR_NUMBER 兜底）
- [x] T2 `_enqueue_pipeline_task` 注入点替换
- [x] T3 `_execute_agent_inner` 注入点替换
- [x] T4 `build_task_env` 重派路径数字守卫
- [x] T5 qa runbook（openspec-pr-reviewer / grape-architecture-guardian）降级路径
- [x] T6 单元测试 test_issue_528_pr_sub_env_injection.py（15 用例）
- [x] T7 集成测试 test_issue_528_pr_sub_env_integration.py（run_pipeline 全链路 2 用例）
- [x] T8 回归：PR 订阅/场景 env/372 审计等既有套件通过（test_pipeline 等套件 10 个失败为基线既有，与本改动无关）
