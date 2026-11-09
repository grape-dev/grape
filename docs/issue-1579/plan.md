# Plan — Issue #1579 note_hook_pr PR_NUMBER 注入

## 方案

改动内聚于 `src/api/event_pipeline.py` 的 env 注入链路，`_enqueue_pipeline_task` 与
`src/runtime/direct_executor._execute_agent_inner` 共用 `_inject_issue_pr_env`，一次修复
覆盖 worker/direct 两条执行路径。

```
note_hook_pr 独立解析：
  payload.pr_number
  → evt._pr_sub_pr_number
  → merge_request.iid / number
  → pull_request.number
  → object_attributes.noteable_iid
  → 标题 note_hook_pr #<N>（兜底，需关联校验）
```

- 不再对 `note_hook_pr` 读取 `object_attributes.iid`（Note Hook 中为评论 id）。
- 标题来源调用 `_validate_pr_issue_binding`：PR 详情按 head 分支 `issue-N` / 标题 /
  issues 列表解析关联 Issue 与 `ISSUE_NUMBER` 比对；无法校验时 fail-open，明确不匹配才拒绝。
- 解析失败/校验失败：`env_extra["PR_NUMBER_PARSE_FAILED"]="1"` + `log.warning`，不设置
  `PR_NUMBER`，绝不复用 `ISSUE_NUMBER`。
- 非 `note_hook_pr` 事件保持原有提取链（含 Issue #595 嵌套键兜底），`a2a_event` 不变。

## 改动清单

| 文件 | 改动 |
|------|------|
| `src/api/event_pipeline.py` | 新增 note_hook_pr 解析/校验 helper；`_inject_issue_pr_env` 增加该分支与失败标记 |
| `help/usage/agent-environment-variables.md` | PR_NUMBER 提取来源补充 note_hook_pr 分支与失败标记 |
| `tests/test_issue_1579_note_hook_pr_number.py` | 新增 payload / 标题 / 跨 Issue / 失败 / 不回归用例 |
| `docs/issue-1579/*` | 需求、方案、任务拆解 |

## 测试计划

- `noteable_iid` / `merge_request.iid` / 顶层 `pr_number` → 注入。
- 标题 `note_hook_pr #1011` + 校验通过 → 注入；校验明确不匹配 → 标记失败。
- 全来源缺失 → 标记失败且不注入 `PR_NUMBER`。
- `a2a_event`、普通 Issue 评论事件不回归。
- 回归运行 `tests/test_issue_595_pr_number_nested_payload.py`、
  `tests/test_issue_528_pr_sub_env_injection.py`。

## 风险与缓解

- 标题格式不稳定 → payload 优先 + 严格 `note_hook_pr #N` 正则 + 关联校验。
- PR 详情 API 不可用 → fail-open 使用标题结果并告警，不阻断流水线。
- 误用评论 id → note_hook_pr 分支不读取 `object_attributes.iid`。
- grape-skills 仓 runbook 属跨仓资产 → 本单不改该仓，在 PR/Issue 评论中同步协同项。