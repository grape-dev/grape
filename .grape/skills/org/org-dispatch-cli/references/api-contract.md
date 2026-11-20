# Dispatch API Contract

> 本文件同时是 CLI 映射与契约说明。**执行一律走 grape CLI**；下文 HTTP 路径仅用于
> 核对契约来源，禁止 curl/httpx 直连。

## CLI Availability Gate

```bash
grape refresh
grape commands "org dispatches"
```

必须列出 `create`、`list`、`detail`、`cancel`、`target retry`。
`scene catalog` 若尚未随 CLI 契约生成，**不阻断下派**：跳过目录预检，直接调用
`create` 由服务端解析场景，并在输出标注 `SCENE_CATALOG_UNAVAILABLE`；
禁止 curl 或裸 HTTP 兜底。

命令组由 `org_*` operationId 生成，固定为 `grape org dispatches ...`。组织运行态接口为
**dual scope**，允许 worker 的 `$GRAPE_SESSION_TOKEN`；服务端仍按组织成员与 owner/admin
校验区分读写权限。

### Worker 权限矩阵

| 命令 | worker token（tk_）| 说明 |
|------|:---:|------|
| `create` | ✅ | 需组织成员；服务端校验 targets/场景 |
| `list` / `detail` / `scene catalog` | ✅ | 只读，需组织成员 |
| `cancel` | ❌ 403 | user_only（JWT/API Key），交人工/Web 端 |
| `target retry` | ❌ 403 | user_only（JWT/API Key），交人工/Web 端 |

> worker 调 `cancel`/`target retry` 返回 403 属预期，禁止换 token 或改用 curl 绕过。

## AI Repo Analysis Docs（选仓依据）

Endpoint: `GET /api/orgs/{id}/repo-analysis/docs`

```bash
grape org repo analysis docs --org_id "$ORG_ID" --json-out
```

返回 `repos[]`：`repo_key / full_name / purpose / domains / keywords / interfaces /
depends_on / confidence / summary_md`（文档分支 `grape-org`）。
用于 AI 选仓与 `context.decisions` 理由；命令不可用时降级为仓库地图召回并标注。

## Scene Catalog

Endpoint: `GET /api/orgs/{id}/dispatch/scene-catalog`

```bash
grape org dispatches scene catalog --org_id "$ORG_ID" --repo_key "$REPO_KEY" \
  --include executable --json-out
```

返回关联仓库的场景目录（`repos[]`）：

```json
{
  "org_id": "...",
  "repos": [
    {
      "repo_key": "gitcode:org/repo-a",
      "full_name": "org/repo-a",
      "dev_scene": "custom-xxx",
      "scene_recommend_config": {
        "fallback_scene": "custom-yyy",
        "rules": [{"scene_id": "custom-zzz", "condition": "..."}]
      },
      "scenes": [
        {
          "scene_id": "custom-zzz",
          "name": "后端开发",
          "display_name": "后端开发",
          "description": "...",
          "skill_chain": ["issue:issue-cli", "coding:coding-cli"],
          "is_default": false,
          "executable": true,
          "problems": []
        }
      ]
    }
  ]
}
```

- `repo_key` 为空时返回全部关联仓库（不含组织自动仓库）；
- `include=executable` 才返回 `executable/problems`，默认不跑预检查；
- 不返回 `advanced_options`、custom field 值、token 等运行期敏感配置；
- 只允许读取当前组织已关联仓库。

### 场景选择顺序（写进 `scene_reasons`）

1. `scene_recommend_config.rules` 条件命中；
2. `executable=true` 中按需求语义匹配；
3. `dev_scene`（若可执行）；
4. 唯一可执行场景；
5. 仍无 → 排除该 target 或交服务端回退，并在 `scene_reasons` 写明原因。

`executable=false` 不得选择。组织自动仓库不参与下派。

## Create

Endpoint: `POST /api/orgs/{id}/dispatches`

```bash
grape org dispatches create --org_id "$ORG_ID" --json '{
  "org_issue_number": 123,
  "targets": ["gitcode:org/repo-a", "gitcode:org/repo-b"],
  "context": {
    "summary": "string",
    "constraints": [],
    "decisions": [],
    "knowledge_refs": [],
    "acceptance": ["string"]
  },
  "title": "string",
  "description": "string",
  "scene_id": "",
  "done_when": "issue_done",
  "target_scenes": {
    "gitcode:org/repo-a": "custom-xxxx",
    "gitcode:org/repo-b": "custom-yyyy"
  },
  "scene_reasons": {
    "gitcode:org/repo-a": "需求匹配后端开发场景"
  }
}'
```

- 批次级 `scene_id` 必须留空（按 target 解析）；不要传 `org_scene_id`。
- `done_when`：`issue_done`（默认）/`pr_merged`/`task_terminal`；非法 →
  `400 INVALID_DONE_WHEN`。
- 长 JSON 用 `--json-file <path>`；短 JSON 用 `--json '<plan>'`。
- 服务端按「组织 + 组织 Issue + 稳定 targets 集合」幂等，客户端不额外传幂等键。

成功响应（字段以服务端为准）：

```json
{
  "dispatch_id": "dp_...",
  "status": "running",
  "targets": [
    {
      "repo_key": "gitcode:org/repo-a",
      "scene_id": "custom-xxxx",
      "child_issue_number": 321,
      "child_task_id": "t_...",
      "pr_number": 0,
      "status": "creating",
      "error": ""
    }
  ]
}
```

`targets[]` 至少包含 `repo_key`、`scene_id`、`child_issue_number`、`status`、`error`。

## Read

Endpoint: `GET /api/orgs/{id}/dispatches`

```bash
grape org dispatches list --org_id "$ORG_ID" \
  --org_issue_number "$ORG_ISSUE_NUMBER" --limit 50 --offset 0 --json-out
```

Endpoint: `GET /api/orgs/{id}/dispatches/{did}`

```bash
grape org dispatches detail --org_id "$ORG_ID" --dispatch_id "$DISPATCH_ID" --json-out
```

返回 dispatch 字段与 `targets[]`。状态以服务端为准，禁止本地推断。

## Cancel

Endpoint: `POST /api/orgs/{id}/dispatches/{did}/cancel`

```bash
grape org dispatches cancel --org_id "$ORG_ID" --dispatch_id "$DISPATCH_ID" --json-out
```

仅允许运行中的 dispatch。返回 `{ok, status}`。**worker token → 403，交人工。**

## Retry

Endpoint: `POST /api/orgs/{id}/dispatches/{did}/targets/{repo_key}/retry`

```bash
grape org dispatches target retry --org_id "$ORG_ID" --dispatch_id "$DISPATCH_ID" \
  --repo_key "$REPO_KEY" --json-out
```

仅重试失败 target，并复用原有 `child_issue_number`。返回 `{ok, target}`。
**worker token → 403，交人工。**

## Idempotency & DISPATCH_IN_PROGRESS

- 同一组织 + 组织 Issue + 稳定 targets 集合重复 create → 服务端返回已有批次
  （`409 DISPATCH_IN_PROGRESS` 或等价响应）。
- 收到后按顺序：`list --org_issue_number` → 取最新在途批次 → `detail` 核对 →
  复用/输出，**不得**再 create 第二个编排。
- 幂等判定以服务端返回为准，不依赖本地缓存。

## Errors

- `400 INVALID_TARGET`
- `400 NO_TARGET_SCENE`
- `400 INVALID_DONE_WHEN`
- `403 PERMISSION_DENIED`（含 worker 调管理操作）
- `404 ORG_NOT_FOUND`
- `404 ORG_REPO_NOT_FOUND`
- `409 DISPATCH_IN_PROGRESS`
- `CLI_CONTRACT_UNAVAILABLE`（本地前置门禁）

所有写操作必须幂等。认证只使用 `$GRAPE_SESSION_TOKEN`；不得打印 token，禁止 curl 直连 API。
失败必须显式输出错误码 + 原因 + 已获取的证据，不得包装成成功。