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

通过 **Web UI Settings 页「API Tokens」面板（登录会话）** 生成；明文仅创建时返回一次，落库 SHA-256。Token 管理**不对外**（不提供 OpenAPI 文档、不允许 API Key 自调用），见 `docs/openapi/README.md` 状态码表上方说明。

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

## 接口分级

| 级别 | 认证 | 面向 |
|------|------|------|
| **对外公开** | `get_current_user_or_api_key`（JWT 会话或 API Key 任一） | 三方软件 / worker agent |
| 内部管理 | `get_current_user`（JWT 会话） | Web UI / 平台管理 |

对外接口清单见各文档；管理类（auth/settings/dashboard/webhook/templates/runtime/devstage/search/feishu）**不对外**。