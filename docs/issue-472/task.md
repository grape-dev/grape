# Tasks: Issue #472

- [ ] T1 后端红测试：tests/test_issue_472_global_stats.py（engines/workers health_stats 全局口径、筛选参数、跨页稳定）
- [ ] T2 后端实现：agent-runtimes 增加 q/type/scene/tag + health_stats + avail_*；runtime-devices 增加 health_stats
- [ ] T3 前端红测试：更新 test_runtimes_stats_i18n.py 断言（health_stats 数据源、后端筛选参数、renderEngines 去本地过滤）
- [ ] T4 前端实现：runtimes.js 统计栏切换全局口径
- [ ] T5 回归：pytest 相关用例通过；自审查 + pre-PR 检查；建 PR + 评论 + A2A QA
