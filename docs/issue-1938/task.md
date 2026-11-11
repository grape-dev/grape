# Task: Issue #1938

- [x] T1 新增 `src/domains/runtime/engine/cascade.py`：枚举/终态映射/文案渲染/展示判定/指标上报
- [x] T2 `db_setup.py`：events DDL 补 5 列 + `_ensure_column` 幂等迁移块
- [x] T3 `service.py::_cascade_event_terminal` 重写：结构化落库 + 真实结论回填 + 四终态映射 + 归因参数
- [x] T4 `service.py::update_task_status`：`cascade_trigger` 参数 + 触发集合改 `TASK_TERMINAL_STATUSES`
- [x] T5 `service.py::converge_event_to_task`：接受 `timeout_failed` + 透传 trigger/真实结论
- [x] T6 `scheduler.py`：3 个 sweeper 调用点传 `cascade_trigger="sweeper"`
- [x] T7 observability：`CASCADE_COMPENSATION` Counter + `record_cascade_compensation`
- [x] T8 alerts：`_repo_metrics().cascade_compensation` + SEED_RULES 阈值规则 + `deploy/alerts.yml`
- [x] T9 metric_rollup：`cascade_compensation` 进 SUM_METRICS + 桶内计数
- [x] T10 展示层：`events.py` 列表/详情、`issues.py` PR 卡片与 pipeline segments 标注
- [x] T11 测试 `tests/runtime/test_issue_1938_cascade_compensation.py`（三条路径 / timeout_failed / 回填 / 幂等 / 守卫 / 展示 / 指标）
- [x] T12 回归：`test_issue_587_event_convergence.py`、`test_terminate_event.py`、`test_obs_*`、`test_issue_1497_*`
- [x] T13 文档 `docs/issue-1938/{spec,plan,task}.md`
- [ ] T14 自审查（5 轴 + 开源前置增量检查）→ 建 PR（base `arch-weekly-11`）→ Issue 评论 → A2A QA