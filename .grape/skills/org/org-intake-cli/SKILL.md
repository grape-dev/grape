---
name: org-intake-cli
version: 1.0.0
description: |
  组织需求接收：读取组织 Issue、组织仓库上下文与关联仓库，选择下派目标并产出
  dispatch plan。供组织流水线 org_intake 节点使用。
tools:
  - grape
---

# org-intake-cli

## 目标

把一条组织需求转换为可供 `org-dispatch-cli` 执行的 dispatch plan。入口必须是组织仓库
Issue 与组织场景，且调用方处于对应组织上下文。

## 输入

- `ORG_ID`：当前组织 ID。
- `ORG_ISSUE_NUMBER`：组织仓库中的 Issue 编号。
- `GRAPE_SCENE_ID`：组织场景 ID。
- `REPO_FULL_NAME`：组织仓库全名。
- Issue 正文或调用参数中的显式 `targets`，可选。

## 执行流程

命令组由 `org_*` operationId 生成，固定为 `grape org ...`，不是 `grape orgs ...`。
组织运行态接口是 dual scope：worker 的 `$GRAPE_SESSION_TOKEN` 可调用，具体权限仍由
组织成员/管理员校验决定。

1. 使用 CLI 读取组织 Issue 标题、正文和目标提示：

```bash
grape git-repo issues get --owner "$REPO_OWNER" --repo "$REPO_NAME" \
  --number "$ORG_ISSUE_NUMBER" --json-out
```

2. 使用 `grape scenes detail --scene_id "$GRAPE_SCENE_ID" --json-out` 确认场景可读；
   场景必须包含 `org_intake`、`org_dispatch`、`org_aggregate` 或对应的简化链路。
3. 选择下派目标（全自动）：

```bash
# 可选：读取召回详情用于计划摘要/原因说明
grape refresh
grape commands "org repo map"
grape org repo map recall --org_id "$ORG_ID" \
  --q "$ORG_ISSUE_TITLE $ORG_ISSUE_BODY" --limit 5 --json-out
```

   - 请求中存在显式 `targets` 时，以显式列表为准；
   - 否则 **不要**自行使用全部已关联仓库，也 **不要**把仓库列表当 targets：
     dispatch plan 中 `targets` 留空 `[]`，由服务端按组织仓库地图自动召回并下派；
   - 多仓需求允许 plan 中 `targets` 包含多个 `repo_key`；服务端会为每个目标独立建子单，
     并把该仓的职责/接口/不负责边界写入子单，按各自仓库流水线执行；
   - 可以调用 `org repo map recall` 读取推荐结果，用于在 plan/评论里解释“为什么派给
     这些仓库”；最终仍以服务端 `org dispatches create` 的自动召回结果为准；
   - 服务端只返回达到置信度阈值的目标仓库；无命中时返回 `NO_CONFIDENT_TARGET`，
     不会回落全部仓库。此时由 intake agent 基于候选与 `summary.md` 显式选择 targets
     （写进 plan），或标记需求需要补充仓库能力地图，禁止把全部关联仓库当 targets。
4. 对每个目标读取仓库级知识引用与可解析场景。显式 `scene_id` 优先，否则交给下派层按
   目标仓库默认场景解析。
5. 生成 dispatch plan，并写入组织 Issue 评论或结构化 artifact。禁止在未读取目标前
   伪造成功结果。

## Dispatch Plan

计划 JSON 必须包含 `org_issue_number`、`targets`、`context`、`acceptance`。完整字段约束见
`references/dispatch-plan.md`。

```json
{
  "org_issue_number": 123,
  "targets": ["gitcode:org/repo-a", "gitcode:org/repo-b"],
  "title": "组织需求标题",
  "description": "组织需求正文",
  "scene_id": "",
  "context": {
    "summary": "需求摘要",
    "constraints": [],
    "decisions": [],
    "knowledge_refs": [],
    "acceptance": ["验收条件"]
  }
}
```

## 失败输出

- `NO_TARGETS`：请求没有显式 targets，组织也没有可召回的关联仓库。
- `INVALID_TARGET`：目标格式不是 `provider:owner/repo`。
- `NO_TARGET_SCENE`：目标既无显式可用场景，也无默认或唯一可用场景。
- `PERMISSION_DENIED`：调用方不是组织成员或没有组织上下文。

失败时输出错误码与可读原因，不得伪造成功或 dispatch plan。所有 Grape/GitCode 访问只使用
注入的 `$GRAPE_SESSION_TOKEN`，不得打印、写盘或回显 token。

## 评论

```bash
grape git-repo issues comments create --owner "$REPO_OWNER" --repo "$REPO_NAME" \
  --number "$ORG_ISSUE_NUMBER" --json '{"body":"[Org Intake] dispatch plan ..."}'
```

评论内容只包含计划摘要、目标仓库与验收条件，不包含 token 或内部请求头。

## A2A 交接（流水线推进）

产出 dispatch plan 后必须通过 A2A 交给 `org_dispatch` 节点，否则流水线停在
`org_intake`。组织节点 tag 复用 `coding`，因此 A2A `agent` 固定为 `coding`；
服务端按 Issue 场景的 tag/id 匹配节点，不需要传 `org_dispatch`（不在公开
agent 白名单内）。

```bash
grape a2a handoff --json '{
  "agent": "coding",
  "payload": {
    "repository": "'"$REPO_FULL_NAME"'",
    "account": "'"$ACCOUNT"'",
    "issue_number": '"$ORG_ISSUE_NUMBER"',
    "context": "组织需求已接收，dispatch plan：{targets, scene_id, acceptance}。请执行 grape org dispatches create 下派。"
  }
}' --json-out
```

- 交办失败（含 `409 not_in_scene`、无可用引擎等）必须显式输出失败原因，不得伪造成功；
  重试 ≤3 次，仍失败则写入组织 Issue 评论并标注需人工介入。
- 交办成功后不得再声称流水线当前节点未完成；下一步由 `org-dispatch-cli` 执行。
