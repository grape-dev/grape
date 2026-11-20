# Aggregation Contract

## Target Fields

- `repo_key`
- `child_issue_number`
- `child_task_id`
- `pr_number`
- `status`
- `error`
- `started_at`
- `finished_at`

## Terminal States

`completed`、`needs_review`、`failed`、`cancelled` 是终态。

- `completed`：目标完成（子 Issue done / PR 已合入 / 任务成功）；
- `needs_review`：子任务已结束但待人工审核（如 QA 通过待合入、等待合入超时、
  `awaiting_input`、`pending_review`），属于终态，**不得**再按「未终态」无限重排；
- `failed` / `cancelled`：目标失败或取消。

## Remaining

只要存在 `pending`、`creating` 或 `running` target，就必须返回：

```json
{
  "remaining": [
    {"repo_key": "gitcode:org/repo-a", "status": "running"}
  ]
}
```

有 `remaining` 时不得输出最终完成结论，也不得提前把聚合节点标记为完成。
本轮仍需调用 `grape tasks complete` 释放执行资源，并在结构化字段里设置
`next_step=a2a-qa`（自指 `org_aggregate`）；服务端据此安排下一轮聚合，而不是把
Issue 提前置为 `pending_review`。目标全部进入终态后才输出最终结论并正常收尾。

## Final Mapping

- 全部 target 为 `completed` → dispatch `completed`。
- 全部 target 为 `failed` → dispatch `failed`。
- 存在 `failed` 且非全部 → dispatch `partial_failed`；若仍有非终态 target，同时输出
  `remaining`，等待终态后再收口。
- 存在 `pending` / `creating` / `running`（且无 `failed`）→ dispatch `running`，输出 `remaining`。
- 全部 target 为 `cancelled` → dispatch `cancelled`。
- 部分 `cancelled` 且其余为终态 → dispatch `partial_failed`。
- 其余全部终态（含 `needs_review`）→ dispatch `needs_review`，列出待人工审核目标。

聚合评论需要列出 `child_issue_number`、`child_task_id`、`pr_number` 和错误摘要，保证组织
Issue 可直接追溯到各目标仓库。
