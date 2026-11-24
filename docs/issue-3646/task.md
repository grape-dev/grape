# Issue #3646 任务拆解

按「一竖切片一测试」TDD 推进，红/绿分提交：

| # | 切片 | 红（先提交失败测试） | 绿（最小实现） | 验收 |
|---|------|---------------------|----------------|------|
| 1 | 责任人过滤纯函数 | `tests/runtime/test_3646_current_owner.py::TestInternalAccountPredicate`、`TestResolveCurrentOwner` | 新增 `src/domains/runtime/pipeline/owner_display.py` | 判定矩阵（bot 名单 / `*_bot` / `gcw_*` / Agent 显示名 / worker 形态 / 真人）全绿 |
| 2 | 流水线 current_owner 优先级 | `tests/runtime/test_3646_current_owner.py::TestCurrentOwnerResolution`（含 #3644 事件链：`gcw_0000` 穿透到 `alice`） | 重写 `routes/pipeline.py` 责任人解析块 | 人类 assignee 优先、内部账号不进展示位、全内部回退 Agent 显示名 |
| 3 | assignee 不被推进抹掉 | `...::TestIssueAssigneeKeepsValue::test_stage_advance_keeps_assignee` | `session_store.set_issue_stage` 仅非空才写 assignee | 推进后 assignee 仍为原值 |
| 4 | 指派/更新回填 assignee | `...::TestIssueAssigneeBackfill::test_backfill_when_empty` / `test_do_not_override_human` | `session_store.sync_issue_assignee` + `event_pipeline` 3.2 块接线 | 空值回填；含人类责任人时不覆盖 |

## 回归与门禁

- `python3 -m pytest tests/runtime/test_3646_current_owner.py tests/runtime/test_pipeline.py tests/runtime/test_issue_lifecycle_routing.py -q`
  与 baseline 对比：新增用例全绿，存量 8 个红用例中 4 个（mock 签名）本次修好，其余为
  baseline 既有环境失败（缺 `GRAPE_SESSION_TOKEN`、共享 `issue` 表串扰），不新增红。
- 前端不改（`Pipeline.vue` 仅拼 `current_owner` 后缀），无需 UI 回归。

## 交付

- 分支 `feat/issue-3646` → PR base `arch-weekly-11-ljt`；
- PR 说明列出改动文件、责任人口径与测试结果；
- 建 PR 后 Issue 评论告知 + A2A 通知 QA 审查。