# Dispatch API Contract

## CLI Availability Gate

```bash
grape refresh
grape commands "org dispatches"
```

必须列出 `create`、`list`、`detail`、`cancel`、`target retry`、`scene catalog`。
缺失时返回 `CLI_CONTRACT_UNAVAILABLE`，禁止 curl 或裸 HTTP 兜底。

命令组由 `org_*` operationId 生成，固定为 `grape org dispatches ...`。组织运行态接口为
dual scope，允许 worker 的 `$GRAPE_SESSION_TOKEN`；服务端仍按组织成员与 owner/admin
校验区分读写权限。

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
  "target_scenes": {
    "gitcode:org/repo-a": "custom-xxxx",
    "gitcode:org/repo-b": "custom-yyyy"
  },
  "scene_reasons": {
    "gitcode:org/repo-a": "需求匹配后端开发场景"
  }
}'
```

```json
{
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
  "target_scenes": {
    "gitcode:org/repo-a": "custom-xxxx",
    "gitcode:org/repo-b": "custom-yyyy"
  },
  "scene_reasons": {
    "gitcode:org/repo-a": "需求匹配后端开发场景"
  }
}
```

成功返回 `dispatch_id`、`status` 与 `targets[]`。`targets[]` 至少包含 `repo_key`、
`child_issue_number`、`status`、`error`。

## Read

Endpoint: `GET /api/orgs/{id}/dispatches`

```bash
grape org dispatches list --org_id "$ORG_ID" --limit 50 --offset 0 --json-out
```

Endpoint: `GET /api/orgs/{id}/dispatches/{did}`

```bash
grape org dispatches detail --org_id "$ORG_ID" --dispatch_id "$DISPATCH_ID" --json-out
```

返回 dispatch 字段与 `targets[]`。状态以服务端为准。

## Cancel

Endpoint: `POST /api/orgs/{id}/dispatches/{did}/cancel`

```bash
grape org dispatches cancel --org_id "$ORG_ID" --dispatch_id "$DISPATCH_ID" --json-out
```

仅允许运行中的 dispatch。返回 `{ok, status}`。

## Retry

Endpoint: `POST /api/orgs/{id}/dispatches/{did}/targets/{repo_key}/retry`

```bash
grape org dispatches target retry --org_id "$ORG_ID" --dispatch_id "$DISPATCH_ID" \
  --repo_key "$REPO_KEY" --json-out
```

仅重试失败 target，并复用原有 `child_issue_number`。返回 `{ok, target}`。

## Errors

- `400 INVALID_TARGET`
- `400 NO_TARGET_SCENE`
- `403 PERMISSION_DENIED`
- `404 ORG_NOT_FOUND`
- `404 ORG_REPO_NOT_FOUND`
- `409 DISPATCH_IN_PROGRESS`
- `CLI_CONTRACT_UNAVAILABLE`（本地前置门禁）

所有写操作必须幂等。认证只使用 `$GRAPE_SESSION_TOKEN`；不得打印 token，禁止 curl 直连 API。
