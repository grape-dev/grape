# Spec: Issue #2118 组织流水线节点修正与组织技能

## Objective

修复 `ORG_SCENE_NODES` 缺少派发字段的问题，补齐 `org-intake-cli`、`org-dispatch-cli`、
`org-aggregate-cli` 三份技能契约，并约束组织需求只能从组织仓库与组织场景进入。

## Commands

```bash
python3 -m pytest tests/mgmt/test_org_scene_template.py -q
python3 -m pytest tests/contracts/test_org_skills_contract.py -q
python3 -m pytest tests/mgmt/test_org_provisioning.py tests/mgmt/test_org_repo_binding.py -q
python3 -m pytest tests/runtime/test_issue_2118_org_pipeline_flow.py tests/contracts/test_org_skills_contract.py -q
```

## Project Structure

- `src/api/pipeline_config.py`: 组织节点模板与组织场景创建。
- `configs/pipelines.json`: 组织场景的权威文件配置。
- `.grape/skills/org/`: 组织接收、下派、聚合技能。
- `src/api/org_context.py`: 组织需求入口校验。
- `tests/mgmt/test_org_scene_template.py`: 节点、场景与入口契约。
- `tests/contracts/test_org_skills_contract.py`: 技能静态契约。

## Contract

组织节点固定为：

| id | tag | skill | mode | next | handoff |
|---|---|---|---|---|---|
| `org_vod` | `vod` | `vod-cli` | `emit` | `org_intake` | `a2a` |
| `org_intake` | `issue` | `org-intake-cli` | `emit` | `org_dispatch` | `a2a` |
| `org_dispatch` | `coding` | `org-dispatch-cli` | `emit` | `org_aggregate` | `a2a` |
| `org_aggregate` | `qa` | `org-aggregate-cli` | `emit` | `done` | `complete` |

每个节点同时包含 `skip=[]`、`enabled=true`、`engine_runtime_ids=[]`。空
`engine_runtime_ids` 表示 unbound，保持全量广播。

dispatch plan JSON 字段为 `org_issue_number`、`targets[repo_key]`、可选
`title`/`description`/`scene_id` 与 `context{summary,constraints,decisions,knowledge_refs,
acceptance}`。三技能只通过 `grape` CLI 执行：#2112 契约必须暴露
`grape org dispatches create/list/detail/cancel/target retry`，否则返回
`CLI_CONTRACT_UNAVAILABLE`，禁止 curl 或裸 HTTP。下派失败错误码为
`400 INVALID_TARGET`、`400 NO_TARGET_SCENE`、`403 PERMISSION_DENIED`。聚合未全终态时
必须返回 `remaining`，不得提前完成。

CLI 命令组由 operationId 的 `org_*` 前缀生成，固定为 `grape org ...`（不是
`grape orgs ...`）。组织运行态接口使用 dual scope，允许 worker 的
`$GRAPE_SESSION_TOKEN`，但服务端仍执行组织成员与 owner/admin 校验。

## Testing Strategy

使用 pytest、FastAPI TestClient、真实 SQLite 测试库和 monkeypatch 隔离外部 API。测试以
公开行为为 seam：节点形状、场景读取、组织入口校验、技能 CLI 命令与文档静态契约。

## Boundaries

- Always: 保留 `org_*` 节点 id，复用公开 `issue/coding/qa` tag，技能只走 `grape` CLI，
  失败不伪造成功。
- Ask first: 改动公开 A2A 白名单或下派 API 本身。
- Never: 修改跨组织下派、前端页面或组织知识库完整管理。

## Success Criteria

- 三个组织节点字段完整且可被现有 tag 路由命中。
- `configs/pipelines.json` 与代码常量节点一致。
- 组织仓库与组织场景交叉校验生效，普通仓库不受影响。
- 三份技能存在且 CLI 命令、字段、错误与 `remaining` 语义与 #2112 对齐。
- 2 节点简化模板可创建并可使用 `skip` 表达接收节点合并。
- 组织节点可通过 tag（issue/coding/qa）被 A2A/阶段推进命中；组织场景建单与
  issue_opened 首节点启动端到端可验证；complete_task 在未显式交办时自动推进。
- issue_opened 在场景绑定竞态下仍按组织首节点启动，不回落 vod。
- org_aggregate 未全终态时输出 remaining 并触发下一轮聚合，不提前 `pending_review`；
  自动推进仅在派发成功后写 stage，失败保持原节点并留痕。
- 存量 org 场景在升级启动后 `scene_type=org`，组织入口与 org-only 模板解析重新生效。
- 普通仓库不能按 `scene_id=org` 解析组织官方模板。
- 新增测试通过，组织 provisioning 与仓库绑定测试不回归。

## Open Questions

无。组织技能的平台注册 `skill_id` 由维护者按实际注册结果回写。
