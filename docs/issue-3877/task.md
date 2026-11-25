# Issue #3877 任务拆解

1. [x] 需求文档 + 方案设计（`docs/issue-3877/spec.md` / `plan.md`）
2. [x] 新增 `src/domains/runtime/migrations/events_cascade_columns.py`（幂等补五列 + post_check）
3. [x] `src/api/db_setup.py` 字面量登记 `202611250001`（kind=safe, domains=("runtime",)）
4. [x] 新增 `src/kernel/cascade_converge_metrics.py` 并在 insight registry 安装
5. [x] `service._cascade_event_terminal_impl`：列集过滤 + 1054 重试 + guard 移入 finally + 结构化日志/指标
6. [x] `service.reconverge_terminal_gaps()` 存量失败 seq 幂等补偿入口
7. [x] 回归用例：
   - [x] `tests/api/test_issue_3877_events_cascade_columns_migration.py`（存量库登记式迁移，6 例）
   - [x] `tests/runtime/test_issue_3877_cascade_events_columns.py`（收敛写入 / 缺列降级 / 1054 重试 / guard / 指标 / 补偿，6 例）
8. [x] 门禁：`tests/api/test_migration_track_guard.py`、`tests/api/test_migration_domain_ownership.py`、
   `tests/runtime/test_repogit.py`、`tests/runtime/test_issue_3482_workspace_order.py`、
   `tests/runtime/test_issue_2245_git_writable.py`、`make lint`、`python3 scripts/check-arch.py` 全绿
9. [ ] 自审查（5 轴 + 开源前置增量检查）→ 建 PR → Issue 评论 → A2A QA