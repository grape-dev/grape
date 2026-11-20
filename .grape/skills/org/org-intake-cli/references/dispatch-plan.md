# Dispatch Plan Contract

## Shape

```json
{
  "org_issue_number": 123,
  "targets": ["gitcode:org/repo-a", "gitcode:org/repo-b"],
  "title": "组织需求标题",
  "description": "组织需求正文",
  "scene_id": "",
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

- `org_issue_number`：组织仓库中的 Issue 编号，必填整数。
- `targets`：下派目标 `repo_key` 列表。每项为 `provider:owner/repo` 字符串；**留空 `[]` 表示由服务端按仓库地图全自动召回**（全自动派发模式推荐）。
- `title` / `description`：子 Issue 的标题与正文来源；缺省时由下派层按组织 Issue 补齐。
- `scene_id`：批次级可选场景；留空时由下派层按每个目标仓库解析默认场景。
- `context.summary`：组织需求的稳定摘要，直接用于子 Issue 正文。
- `context.constraints`：范围、技术、时间或合规约束。
- `context.decisions`：已拍板事项，避免目标仓库重复决策。
- `context.knowledge_refs`：可透传的知识引用，不复制完整知识库。
- `context.acceptance`：可由目标仓库验证的验收条件。

## CLI Contract

计划必须通过 Issue #2112 暴露的 `grape org dispatches create` 提交：

```bash
grape org dispatches create --org_id "$ORG_ID" --json '<dispatch-plan>'
```

提交前先执行 `grape refresh && grape commands "org dispatches"`。命令缺失时返回
`CLI_CONTRACT_UNAVAILABLE`，不得改用 curl 或裸 HTTP。

命令组由 `org_*` operationId 生成，固定为 `grape org dispatches ...`；读取组织仓库使用
`grape org repos list ...`。两者均为 dual scope，允许 worker 的
`$GRAPE_SESSION_TOKEN`，但服务端仍校验组织成员/管理员权限。

## Target Selection（全自动）

- 显式 `targets` 优先；
- 没有显式 targets 时**不要**回退到全部关联仓库：提交 `targets: []`，由服务端调用
  `GET /api/orgs/{org_id}/repo-map/recall` 等价逻辑，按仓库地图
  `purpose/domains/keywords/interfaces` 召回目标仓库（默认 top-3），并写入
  `plan.routing`（命中原因/置信度）；
- 召回使用中文 n-gram 与关键词包含匹配，多域需求可以同时返回多个仓库；每个目标会
  独立建子单，并把该仓的职责/接口/`not_owns` 写入子单；
- 地图无命中或得分低于阈值时，服务端返回 `NO_CONFIDENT_TARGET`，**不会**回落全部
  关联仓库。此时显式选择候选仓库写进 `targets`，或标记需求需要补充能力地图；
- 可以用 `grape org repo map recall --org_id ... --q ... --json-out` 读取推荐明细，用于
  评论/审计说明，但最终以服务端下派结果为准；
- 显式目标必须是组织已关联仓库，否则返回 `INVALID_TARGET`。目标无法解析场景时返回
  `NO_TARGET_SCENE`，不伪造成功。
