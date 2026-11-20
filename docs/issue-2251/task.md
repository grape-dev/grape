# Task: Issue #2251 — 子任务完成判定与组织流水线闭环

## Phase 1: 契约（先红）

- [x] T1: 文档 Spec/Plan/Task 落库。
- [x] T2: `tests/integration/test_org_flow_closure.py` 覆盖：
  - [x] 全 completed → dispatch completed + 组织 Issue pending_review + 聚合评论
  - [x] 子 Issue pending_review → target/dispatch needs_review
  - [x] awaiting_input → needs_review
  - [x] 未终态 sweep：组织 Issue 不提前 pending_review、无聚合评论/触发
  - [x] failed/timeout → partial_failed/failed + 失败清单
  - [x] done_when=pr_merged：无 merge 事件保持 running；有 merge 事件 completed
  - [x] done_when=task_terminal：task completed → completed；task failed → failed
  - [x] 重复 sweep 幂等：聚合触发行/事件/评论只 1 条
- [x] T3: 运行 `pytest -m integration tests/integration/test_org_flow_closure.py`，确认红灯（能力缺失）。

## Phase 2: 实现（转绿）

- [x] T4: `db_setup`：`done_when` 列迁移 + `org_dispatch_aggregate` 表。
- [x] T5: `org_dispatch`：done_when 判定/refresh 归一/汇总语义/sweeper 终态触发。
- [x] T6: `issues_center.finalize_org_issue_aggregation`：评论 + 状态收口（未终态保护）。
- [x] T7: `a2a_dispatch.trigger_org_aggregate`：幂等聚合重排事件。
- [x] T8: `engine/service`：聚合节点终态兜底收口。
- [x] T9: `org_dispatch` 路由透出 done_when；聚合技能终态集合加 needs_review。
- [x] T10: 集成用例全绿；`tests/mgmt/test_org_dispatch.py`、`tests/runtime/test_issue_2118*` 回归。

## Phase 3: 收尾

- [x] T11: 自审查（5 轴 + 增量敏感扫描）。
- [x] T12: 建 PR #1312 + Issue 评论告知 + A2A 通知 QA（seq 69761）。