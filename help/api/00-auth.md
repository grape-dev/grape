# 认证与通用约定

> 参考：GitCode 官方 OpenAPI 文档（`Authorization: Bearer` / `PRIVATE-TOKEN` / `access_token` query 三通道）。
> grape 的 token 为 **grape 自签发的用户级 API Key**，传输姿势与 GitCode 官方一致。

## 认证方式（三通道，任一带有效 Token 即通过）

### 通道 1：Authorization header（推荐）

```
Authorization: Bearer <api_key>
```

### 通道 2：PRIVATE-TOKEN header

```
PRIVATE-TOKEN: <api_key>
```

### 通道 3：access_token query

```
GET /api/...?access_token=<api_key>
```

## 优先级

`Authorization: Bearer` > `PRIVATE-TOKEN` > `access_token`。

## 获取 API Key

通过 **Web UI Settings 页「API Tokens」面板（登录会话）** 生成；明文仅创建时返回一次，落库 SHA-256。Token 管理**不对外自调用**
（不提供 API Key 自调用端点，创建/吊销走 Web UI 或 `/api/settings/api-keys`，见 [13-settings.md](13-settings.md)）。

## 请求头通用约定

| Header | 必填 | 说明 |
|--------|------|------|
| `Authorization` | 二选一 | `Bearer <api_key>` |
| `PRIVATE-TOKEN` | 二选一 | GitLab 风格 |
| `Content-Type` | 有 body 时 | `application/json` |
| `Accept` | 否 | 默认 `application/json` |

## 响应结构约定

- 通用错误（`detail.message` 英文，Agent 可读；格式见 `README.md`）：
  ```json
  {"detail": {"code": 401, "message": "Not logged in"}}
  ```
- 多数接口成功返回 JSON 对象/数组；删除类返回 `204`。

## 接口分级（x-token-scope）

契约每个公开 operation 自带 `x-token-scope`（见 README「Token 作用域」），按可用 token 类型分三档：

| 值 | 认证 | 可用 Token | 面向 |
|----|------|-----------|------|
| **user_only** | `require_no_session_token` | `grape_` API Key / Web JWT；`tk_` → **403** | 账户/平台管理（settings/skills/scenes 管理/chat/runtime/agents/devstage/dashboard/pipeline/search/feishu/task-logs + 用户操作类） |
| **session_only** | `get_current_user_or_api_key` + 体内强制 | 仅 `tk_`；API Key / JWT → **403** | Git 仓库透传（`/api/git-repo`）+ CLI git-credential/workspace |
| **dual** | `get_current_user_or_api_key` | 三通道全放行 | 开放读/执行（issues 仓库列表/详情/事件、complete、knowledge 读、events detail、scenes list、a2a） |

> 三通道传法完全一致：`Authorization: Bearer` / `PRIVATE-TOKEN` / `?access_token=`；
> 能否通过取决于 **token 前缀**（`grape_` API Key / `tk_` 会话 / JWT），与传输通道无关。
> 各端的 scope 明细见 README「各文档 scope 一览」；详细设计见 `docs/weekly-03/openapi-x-token-scope-design.md`。