---
name: org-dispatch-cli
version: 1.0.0
description: |
  组织需求下派：调用 #2112 下派 API，透传组织 Issue、targets、context 与 acceptance，
  处理幂等和错误并输出 dispatch_id 与目标状态。
tools:
  - grape
---

# org-dispatch-cli

## 输入

- `ORG_ID`：组织 ID。
- dispatch plan：由 `org-intake-cli` 产出的结构化对象。
- 调用方必须处于目标组织上下文，且为 owner/admin。

## CLI 前置门禁

本技能只允许通过 `grape` CLI 调用 Issue #2112 契约，禁止 curl 或裸 HTTP 绕过 CLI。

```bash
grape refresh
grape commands "org dispatches"
```

第二条命令必须列出 `create`、`list`、`detail`、`cancel`、`target retry` 五条命令。
若缺少任一条，返回 `CLI_CONTRACT_UNAVAILABLE` 并停止；若命令返回 session token 不可用，
返回 `PERMISSION_DENIED` 并停止。不得改用其它 token 或直连 API。

命令组由 `org_*` operationId 生成，固定为 `grape org dispatches ...`，不是
`grape orgs dispatches ...`。组织运行态接口是 dual scope：`create`/`list`/`detail`/
`context` 可由 worker 的 `$GRAPE_SESSION_TOKEN` 调用；`cancel` / `target retry` 是
管理操作，走 user_only（JWT/API Key），且仍必须通过组织 owner/admin 校验。

## CLI 调用

```bash
grape org dispatches create --org_id "$ORG_ID" --json '{
  "org_issue_number": 123,
  "targets": ["gitcode:org/repo-a", "gitcode:org/repo-b"],
  "context": {
    "summary": "需求摘要",
    "constraints": [],
    "decisions": [],
    "knowledge_refs": [],
    "acceptance": ["验收条件"]
  },
  "title": "组织需求标题",
  "description": "组织需求正文",
  "scene_id": ""
}'

grape org dispatches list --org_id "$ORG_ID" --limit 50 --offset 0 --json-out
grape org dispatches detail --org_id "$ORG_ID" --dispatch_id "$DISPATCH_ID" --json-out
grape org dispatches cancel --org_id "$ORG_ID" --dispatch_id "$DISPATCH_ID" --json-out
grape org dispatches target retry --org_id "$ORG_ID" --dispatch_id "$DISPATCH_ID" \
  --repo_key "$REPO_KEY" --json-out
```

认证只使用 `$GRAPE_SESSION_TOKEN`。不得打印、写盘或评论 token。完整请求与响应字段见
`references/api-contract.md`。

## 执行流程

1. 校验 plan：组织 Issue 编号、目标列表、context 与 acceptance 完整。
2. 调用 `grape org dispatches create`。服务端按组织、组织 Issue 与稳定 targets 集合幂等，
   客户端不额外传递幂等键。
3. 输出 `dispatch_id`、dispatch 状态和每个 target 的 `repo_key`、`child_issue_number`、
   `status`、`error`。
4. 若返回 `DISPATCH_IN_PROGRESS`，先用 `grape org dispatches list` 读取已有 dispatch 并
   复用，不新建第二个编排。
5. 轮询或后续聚合统一使用 `grape org dispatches detail`，不根据本地推断伪造状态。

## 错误处理

- `INVALID_TARGET`：目标未关联组织或格式非法。输出错误码和目标，不重试。
- `NO_CONFIDENT_TARGET`：仓库地图没有达到置信度阈值的目标。不要回落全部关联仓库；
  把候选明细写回计划/评论，由 intake 显式选择 targets 或标记需补充能力地图。
- `NO_TARGET_SCENE`：目标无可用场景。保留 target 失败状态，其他目标继续。
- `PERMISSION_DENIED`：调用方无权下派。停止写操作并返回可读原因。
- `ORG_NOT_FOUND` / `ORG_REPO_NOT_FOUND`：组织或组织仓库不存在。
- `CLI_CONTRACT_UNAVAILABLE`：当前 `grape` 契约未暴露下派命令。停止，不调用裸 HTTP。

失败结果必须显式输出，不得把 CLI/HTTP 错误转换为成功。取消与 retry 只调用对应命令，
并复用已有 `child_issue_number`。

## 流水线推进（重要：不要提前交办聚合）

`create` 成功后**不要**调用 `grape a2a handoff` 交给 `org_aggregate`。

原因：组织流水线的设计是

```
org_intake → org_dispatch（创建批次）
   → 等待批次内所有目标仓库的子任务终态
   → 系统自动触发 org_aggregate
```

`create` 只代表「批次已创建」，不代表「子任务已完成」。服务端在批次进入终态时会
自动触发 `org_aggregate` 节点（持久化复核事件，进程重启不丢）。如果本节点提前
A2A，聚合节点会在子任务还 running 时提前执行一遍，导致组织流水线看起来「已经
走完」，而关联仓库的任务仍在运行。

因此本节点的正确收口是：

1. 输出 `dispatch_id`、每个 target 的 `repo_key` / `child_issue_number` /
   `status` / `error`；
2. 结束本节点任务，不交办下一个节点；
3. 若批次在创建时已经全部终态（极少数同步失败场景），也不需要手动交办——
   服务端终态钩子会触发聚合。

禁止：
- 为了「推进流水线」而手动 A2A 到 `qa`/`org_aggregate`；
- 轮询等待所有子任务完成（本节点不负责等待，服务端终态钩子负责）；
- 根据 target 的 running 状态伪造聚合结论。
