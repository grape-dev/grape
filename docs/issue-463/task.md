# Task: Issue #463

- [ ] T1(P0) 红：扩展 tests/test_issue357_health.py — 10 字段置空 + 7 天边界
- [ ] T1(P0) 绿：table_maintenance.py 保留期 7 天 + 10 字段清理
- [ ] T2(P1) 红：新增 delete_old_agent_tasks 测试（90 天终态删行、非终态/新行保留、task_messages 连带删）
- [ ] T2(P1) 绿：delete_old_agent_tasks 实现 + main.py 挂载
- [ ] T3(P2) 红：enqueue_task URL 单写测试
- [ ] T3(P2) 绿：service.py URL 优先单写
- [ ] T4(P3) 红：alert 指标/规则测试
- [ ] T4(P3) 绿：alerts.py 指标 + 种子规则
- [ ] 全量回归 + 自审查 + PR
