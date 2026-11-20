---
name: org-aggregate-cli
version: 1.0.0
description: |
  组织结果聚合：读取 #2112 dispatch 详情，汇总目标仓库的子 Issue、任务、PR 与状态，
  全终态时输出结论，未全终态时输出 remaining。
tools:
  - grape
---

# org-aggregate-cli

## 输入

- `ORG_ID`
- `DISPATCH_ID`
- `ORG_ISSUE_NUMBER`
- `REPO_OWNER`
- `REPO_NAME`
- `$GRAPE_SESSION_TOKEN` 由平台自动注入，不得打印或写盘。

## CLI 前置门禁

```bash
grape refresh
grape commands "org dispatches"
```

输出必须包含 `detail` 命令。缺失时返回 `CLI_CONTRACT_UNAVAILABLE` 并停止；禁止 curl
或裸 HTTP 兜底。

命令组由 `org_*` operationId 生成，固定为 `grape org dispatches ...`，不是
`grape orgs dispatches ...`。组织运行态接口是 dual scope，worker 的
`$GRAPE_SESSION_TOKEN` 可调用；detail 仍必须通过组织成员校验。

## 执行流程

1. 读取 dispatch 与 targets：

```bash
grape org dispatches detail --org_id "$ORG_ID" --dispatch_id "$DISPATCH_ID" --json-out
```

2. 对每个 target 提取 `repo_key`、`child_issue_number`、`child_task_id`、`pr_number`、
   `status`、`error`、`started_at`、`finished_at`。
3. 终态集合为 `completed`、`needs_review`、`failed`、`cancelled`。
   `needs_review` = 子任务已结束但待人工审核（如 QA 通过待合入/待确认），属于终态，
   不得再按「未终态」无限重排。
4. target 未全部进入终态时，输出 `remaining` 列表，明确每个剩余 target 的 repo 与当前
   status；不得提前完成聚合。**最终答复必须显式包含这句等待语义**：
   `仍有 target 未终态，等待下一轮聚合（next_step: a2a-qa）`，让服务端终止钩子
   即使早于 `tasks complete` 收到结果，也不会误置 `pending_review`。
5. 全部 target 终态时，按 dispatch 状态输出：
   - `completed`：全部完成，给出完成结论。
   - `needs_review`：子任务已结束但有目标待人工审核，列出待审目标与子 Issue。
   - `partial_failed`：部分 target failed，列出成功与失败目标。
   - `failed`：全部 target failed，给出失败原因。
   - `cancelled`：编排已取消，列出取消范围。
6. 将聚合结果写入组织 Issue：

```bash
grape git-repo issues comments create --owner "$REPO_OWNER" --repo "$REPO_NAME" \
  --number "$ORG_ISSUE_NUMBER" --json '{"body":"[Org Aggregate] ..."}'
```

   评论只包含结论、目标状态、子 Issue/任务/PR 引用和错误摘要。
   **评论幂等**：服务端在批次进入终态时已幂等写入聚合评论（含 `[Org Aggregate]`
   结论与目标清单）并归一组织 Issue 状态；若组织 Issue 已存在本批次的聚合评论，
   本步骤只做复核，不重复评论。

## 输出

```json
{
  "dispatch_id": "dp_...",
  "status": "running",
  "remaining": [
    {"repo_key": "gitcode:org/repo-b", "status": "running"}
  ],
  "targets": []
}
```

字段语义与终态映射见 `references/aggregation.md`。失败或非终态不得伪造完成评论。

## 流水线收尾

`org_aggregate` 是组织流水线的末节点（`next=done`、`handoff=complete`），
**不得**再调用 `grape a2a handoff`：A2A 白名单只接受 issue/coding/qa 等公开角色，
且末节点由服务端在 `tasks complete` 时判定并置 `pending_review` 等待人工审核。

- 全部 target 未终态时：输出 `remaining` 并明确说明本轮未完成，**本轮仍须调用
  `grape tasks complete` 释放执行资源**，并把 `next_step` 写成 `a2a-qa`（自指聚合节点）。
  服务端识别到 `remaining`/`next_step=a2a-qa` 后不会把 Issue 提前置 `pending_review`，
  而是延迟安排下一轮 `org_aggregate` 重新读取 dispatch；目标进入终态后自然收口。
  不得把 Issue 置为完成。

```bash
grape tasks complete --json '{
  "result_text": "本轮聚合未完成：仍有 target 处于非终态，等待下一轮聚合。",
  "next_step": "a2a-qa",
  "actions": ["读取 dispatch detail", "汇总 target 状态"],
  "conclusion": "仍有 target 未终态，输出 remaining 并等待下一轮聚合",
  "artifacts": ["remaining 列表"],
  "issue_summary": "组织聚合等待中 | remaining=<N>"
}'
```

- 全部 target 终态时：写入组织 Issue 结论评论后调用 `grape tasks complete`
  自报完成（`next_step` 留空或写 `done`）；服务端按场景 `next=done` 收尾，
  不产生额外 A2A 事件。
