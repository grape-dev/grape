# Aggregation Contract

> 本文件是 `org-aggregate-cli` 的字段/终态契约与输出模板。平台交互只走 grape CLI；
> 不得 curl 直连。

## Inputs

| 输入 | 来源 | 说明 |
|------|------|------|
| `ORG_ID` | `$ORG_ID` / `$GRAPE_ORG_ID` | 组织 ID，不得猜测 |
| `DISPATCH_ID` | `grape tasks current` / `grape events detail --seq` / `grape org dispatches list` | 批次 ID |
| `ORG_ISSUE_NUMBER` | `$ORG_ISSUE_NUMBER` 或任务上下文 | 组织 Issue 编号（评论用）|
| `REPO_OWNER` / `REPO_NAME` | `$REPO_FULL_NAME` 拆分 | 组织仓库（评论用）|

`dispatch_id` 缺失 → 输出 `DISPATCH_ID_REQUIRED` 并结束，不聚合、不写结论。

## Target Fields

`grape org dispatches detail` 返回的 `targets[]` 至少包含：

- `repo_key`
- `scene_id`
- `child_issue_number`
- `child_task_id`
- `pr_number`
- `status`
- `error`
- `done_when`
- `started_at`
- `finished_at`

## Terminal States

`completed`、`needs_review`、`failed`、`cancelled` 是终态。

- `completed`：目标完成（子 Issue done / PR 已合入 / 任务成功）；
- `needs_review`：子任务已结束但待人工审核（如 QA 通过待合入、等待合入超时、
  `awaiting_input`、`pending_review`），属于终态，**不得**再按「未终态」无限重排；
- `failed` / `cancelled`：目标失败或取消。

非终态：`pending`、`creating`、`running`。

## Detail Shape（读取后先核对）

```json
{
  "dispatch_id": "dp_...",
  "status": "running",
  "org_issue_number": 123,
  "targets": [
    {
      "repo_key": "gitcode:org/repo-a",
      "scene_id": "custom-xxx",
      "child_issue_number": 321,
      "child_task_id": "t_...",
      "pr_number": 0,
      "status": "completed",
      "error": "",
      "done_when": "issue_done",
      "started_at": "...",
      "finished_at": "..."
    }
  ]
}
```

状态字段一律以服务端返回为准；本地不得根据「看起来应该完成」推断终态。

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

**等待语义必须显式出现在最终答复中**：
`仍有 target 未终态，等待下一轮聚合（next_step: a2a-qa）`。

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

## Comment Template（幂等）

组织 Issue 评论前缀固定 `[Org Aggregate]`：

```markdown
[Org Aggregate] 批次 <dispatch_id> 聚合结论：<completed|needs_review|partial_failed|failed|cancelled>

| 仓库 | 子 Issue | 任务 | PR | 状态 | 错误 |
|------|---------|------|----|------|------|
| ... | #321 | t_... | #12 | completed | - |

- 成功目标：<n>
- 失败目标：<n>（原因：...）
- 待人工审核：<n>（...）
- 建议下一步：<无 / 人工审核合入 / 修复失败目标后 retry>
```

- 写评论前先确认是否已存在本批次 `[Org Aggregate]` 评论（服务端终态钩子已幂等写入）；
  已存在则**只复核不重复写**。
- 评论中不得包含 token、内部请求头或未证实的 claim。

## Task Completion (required each round)

未全终态：

```bash
grape tasks complete --json '{
  "result_text": "本轮聚合未完成：仍有 target 处于非终态，等待下一轮聚合。",
  "actions": ["读取 dispatch detail", "汇总 target 状态"],
  "conclusion": "仍有 target 未终态，输出 remaining 并等待下一轮聚合",
  "artifacts": ["remaining 列表"],
  "issue_summary": "组织聚合等待中 | remaining=<N>",
  "next_step": "a2a-qa"
}'
```

全终态：写评论后 `grape tasks complete`，`next_step` 留空或 `done`。服务端按场景
`next=done` 收尾，不产生额外 A2A 事件。

## Forbidden

- 调用 `grape a2a handoff`（末节点，handoff=complete）。
- 未全终态就输出完成结论或置 Issue 完成。
- 修改子 Issue、重试 target、取消批次（管理操作，交人工/Web 端）。
- 伪造 target 状态 / PR / 子 Issue / 聚合结论。