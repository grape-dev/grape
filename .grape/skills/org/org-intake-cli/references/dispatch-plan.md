# Dispatch Plan Contract

> 本文件是 `org-intake-cli` 的输入/输出契约。所有平台交互只走 grape CLI；
> 本文件中的 HTTP 路径仅用于说明契约来源，禁止用 curl/httpx 直连。

## Shape

```json
{
  "org_issue_number": 123,
  "targets": ["gitcode:org/repo-a", "gitcode:org/repo-b"],
  "title": "组织需求标题",
  "description": "组织需求正文",
  "scene_id": "",
  "done_when": "issue_done",
  "context": {
    "summary": "string",
    "constraints": ["string"],
    "decisions": ["string"],
    "knowledge_refs": ["string"],
    "acceptance": ["string"]
  }
}
```

## Field Rules

- `org_issue_number`：组织仓库中的 Issue 编号，必填（数字或平台原生字符串 ident）。
- `targets`：下派目标 `repo_key` 列表。每项为 `provider:owner/repo` 字符串；**留空 `[]` 表示由服务端按仓库地图全自动召回**（全自动派发模式推荐）。
- `title` / `description`：子 Issue 的标题与正文来源；缺省时由下派层按组织 Issue 补齐。
- `scene_id`：批次级可选场景；留空时由下派层按每个目标仓库解析默认场景。
- `done_when`：目标完成判定，可选 `issue_done`（默认）/`pr_merged`/`task_terminal`；
  非法值服务端以 `INVALID_DONE_WHEN` 拒绝。
- `context.summary`：组织需求的稳定摘要，直接用于子 Issue 正文。
- `context.constraints`：范围、技术、时间或合规约束。
- `context.decisions`：已拍板事项，避免目标仓库重复决策。
- `context.knowledge_refs`：可透传的知识引用，不复制完整知识库。
- `context.acceptance`：可由目标仓库验证的验收条件。

## CLI Contract

计划必须通过 Issue #2112 暴露的 `grape org dispatches create` 提交：

```bash
grape org dispatches create --org_id "$ORG_ID" --json '<dispatch-plan>'
# 长 JSON 优先写文件，避免 shell 转义：
grape org dispatches create --org_id "$ORG_ID" --json-file /tmp/org-dispatch.json
```

提交前先执行 `grape refresh` 与 `grape commands "org dispatches"`。命令缺失时返回
`CLI_CONTRACT_UNAVAILABLE`，不得改用 curl 或裸 HTTP。

命令组由 `org_*` operationId 生成，固定为 `grape org dispatches ...`；读取组织仓库使用
`grape org repos list ...`；读取 AI 仓库分析文档使用
`grape org repo analysis docs --org_id "$ORG_ID" --json-out`。两者均为 **dual scope**，
允许 worker 的 `$GRAPE_SESSION_TOKEN`，但服务端仍校验组织成员/管理员权限。

> `cancel` / `target retry` 是 user_only（JWT/API Key），worker token 调用必 403；
> intake 不执行这两类操作。

## Target Selection（全自动）

- 显式 `targets` 优先；
- 没有显式 targets 时**不要**回退到全部已关联仓库：提交 `targets: []`，由服务端按
  仓库地图 `purpose/domains/keywords/interfaces` 召回目标仓库（默认 top-3），并写入
  `plan.routing`（命中原因/置信度）；
- 召回使用中文 n-gram 与关键词包含匹配，多域需求可以同时返回多个仓库；每个目标会
  独立建子单，并把该仓的职责/接口/`not_owns` 写入子单；
- 地图无命中或得分低于阈值时，服务端返回 `NO_CONFIDENT_TARGET`，**不会**回落全部
  关联仓库。此时显式选择候选仓库写进 `targets`，或标记需求需要补充能力地图；
- 可以用 `grape org repo map recall --org_id ... --q ... --json-out` 读取推荐明细，用于
  评论/审计说明，但最终以服务端下派结果为准；
- 显式目标必须是组织已关联仓库，否则返回 `INVALID_TARGET`。目标无法解析场景时返回
  `NO_TARGET_SCENE`，不伪造成功。

## Pre-submit Checklist

- [ ] `ORG_ID` / 组织 Issue 编号已从任务上下文确认（非猜测）。
- [ ] 组织 Issue 已用 `grape git-repo issues get` 读取；标题/正文/显式 targets 已解析。
- [ ] 组织场景含 `org_intake`/`org_dispatch`/`org_aggregate`（或简化链路）。
- [ ] 显式 targets 均为 `provider:owner/repo` 且属于当前组织；否则用 `[]` 交给服务端召回。
- [ ] `context.summary` / `context.acceptance` 非空；`context.decisions` 写入选仓理由。
- [ ] 已在组织 Issue 写计划评论（含 targets、理由、验收条件，无 token）。
- [ ] A2A 交办 `coding` 后校验 `handoff_result=ok` 且 `seq>0`。

## A2A Handoff Contract

```bash
grape a2a handoff --json '{
  "agent": "coding",
  "idempotency_key": "org-<issue>:coding",
  "payload": {
    "repository": "'"$REPO_FULL_NAME"'",
    "account": "'"$ACCOUNT"'",
    "issue_number": "'"$ORG_ISSUE_NUMBER"'",
    "context": "组织需求已接收，dispatch plan：{targets, scene_id, done_when, acceptance}。请执行 grape org dispatches create 下派。"
  }
}' --json-out
```

响应判定（不得只看 `status=ok`）：

| 字段 | 含义 | 动作 |
|------|------|------|
| `handoff_result=ok` 且 `seq>0` | 交办成功 | 继续 `tasks complete` |
| `handoff_result=rejected`（含 `409 not_in_scene`）| 门禁/场景拦截 | 输出原因；重试 ≤3 次仍失败 → 评论标注人工介入 |
| `handoff_result=failed` / `seq=0` | 派发链路失败（无引擎等）| 输出原因；重试 ≤3 次，不伪造成功 |
| `idempotent=true` 且 `existing_status` 为终态 | 命中旧事件 | 用 `force=true` 或新轮次键重派，不得停摆 |

## Failure Output

- `NO_TARGETS` / `NO_CONFIDENT_TARGET` / `INVALID_TARGET` / `NO_TARGET_SCENE`
- `PERMISSION_DENIED` / `ORG_NOT_FOUND` / `CLI_CONTRACT_UNAVAILABLE`
- `DISPATCH_IN_PROGRESS`（下派节点侧）：已有同 Issue 批次在途，读取并复用。

失败必须输出错误码 + 可读原因 + 已读取到的证据（targets/候选/状态），
不得伪造成功或 dispatch plan。