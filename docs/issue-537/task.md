# Task: Issue #537

- [x] S1 状态常量 + pending_since 列迁移 + insert 契约（红→绿）
- [x] S2 terminate_active_agents REMOTE → pending_self_report（红→绿）
- [x] S3 service 状态机放行（终态集/validate_run_token/活跃计数/回炉排除）（红→绿）
- [x] S4 scheduler：pending 超时回收 + zombie 离线宽限 + poll 放行（红→绿）
- [x] S5 deploy_evidence 收口钩子 + poll/zombie 接入（红→绿）
- [x] S6 前端 observability 状态图标
- [x] S7 回归（test_shutdown_force_fail 预期更新 / #308 口径源码断言）+ 全量测试
- [x] 自审查（5 轴 + pre-pr-release-check）→ PR → A2A QA
