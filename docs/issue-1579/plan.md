# Plan — Issue #1579 note_hook_pr PR_NUMBER 注入

## 方案

改动内聚于 `src/api/event_pipeline.py` 的 env 注入链路，`_enqueue_pipeline_task` 与
`src/runtime/direct_executor._execute_agent_inner` 共用 `_inject_issue_pr_env`，一次修复
覆盖 worker/direct 两条执行路径。

```
统一解析（note_hook_pr 与普通事件共用 _extract_pr_number）：
  payload.pr_number
  → evt._pr_sub_pr_number
  → note_hook_pr: merge_request.iid/number → pull_request.iid/number
    → object_attributes.noteable_iid
  → 普通事件: pull_request.number → merge_request.number → object_attributes.iid
  → 结构化 URL 兜底（object_attributes.url / merge_request.web_url|url
    / pull_request.web_url|url，形如 .../merge_requests/<N>、.../pulls/<N>）
```

- 不再对 `note_hook_pr` 读取 `object_attributes.iid`（Note Hook 中为评论 id）。
- 移除标题猜号与 `_validate_pr_issue_binding`：真实事件标题是 MR 标题，不含
  `note_hook_pr #<N>` 标记，标题分支生产不可达；URL 来自事件 payload，属可信结构化来源，
  无需再跨 Issue 校验（同时消除 fail-open 口径歧义）。
- 解析失败：`env_extra["PR_NUMBER_PARSE_FAILED"]="1"` + `log.warning`，不设置
  `PR_NUMBER`，绝不复用 `ISSUE_NUMBER`。
- 非 `note_hook_pr` 事件复用同一 helper（含 Issue #595 嵌套键兜底与新增 URL 兜底），
  `a2a_event` 行为不变。

## 改动清单

| 文件 | 改动 |
|------|------|
| `src/api/event_pipeline.py` | 新增统一 `_extract_pr_number` / `_pr_number_from_url` helper；`_inject_issue_pr_env` 复用该链并保留失败标记 |
| `help/usage/agent-environment-variables.md` | PR_NUMBER 提取来源补充 note_hook_pr 结构化 URL 兜底与失败标记 |
| `tests/test_issue_1579_note_hook_pr_number.py` | 新增 payload / URL 兜底 / 标题不猜号 / 失败 / 不回归用例 |
| `docs/issue-1579/*` | 需求、方案、任务拆解 |

## 测试计划

- `noteable_iid` / `merge_request.iid` / 顶层 `pr_number` → 注入。
- payload 无 PR 字段、结构化 URL 含 .../merge_requests/<N> → 注入。
- 事件标题为 MR 标题（不含 `note_hook_pr` 标记）→ 不从标题猜号；无结构化来源则标记失败。
- 全来源缺失 → 标记失败且不注入 `PR_NUMBER`。
- `a2a_event`、普通 Issue 评论事件不回归。
- 回归运行 `tests/test_issue_595_pr_number_nested_payload.py`、
  `tests/test_issue_528_pr_sub_env_injection.py`。

## 风险与缓解

- 标题不含标记 → 移除标题猜号，改用事件 payload 的结构化 URL 兜底。
- 误用评论 id → note_hook_pr 分支不读取 `object_attributes.iid`。
- 解析链分叉 → note_hook_pr 与普通事件统一走 `_extract_pr_number` 单一 helper。
- grape-skills 仓 runbook Step A0 的标题假设 → 属跨仓资产，本单不改该仓，
  在本单 PR/Issue 评论中明确提示不得声明可从 `note_hook_pr #<N>` 标题提取。