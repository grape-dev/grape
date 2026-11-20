# Dispatch API Contract

## CLI Availability Gate

```bash
grape refresh
grape commands "org dispatches"
```

必须列出 `create`、`list`、`detail`、`cancel`、`target retry`。缺失时返回
`CLI_CONTRACT_UNAVAILABLE`，禁止 curl 或裸 HTTP 兜底。

命令组由 `org_*` operationId 生成，固定为 `grape org dispatches ...`。组织运行态接口为
dual scope，允许 worker 的 `$GRAPE_SESSION_TOKEN`；服务端仍按组织成员与 owner/admin
校验区分读写权限。

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
  "scene_id": ""
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
  "scene_id": ""
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
