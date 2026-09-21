# DevStage 开发环境接口（/api/devstage）

> 对外公开（openapi-full-coverage-design.md D 批收录），认证：`require_no_session_token`。
> scope（x-token-scope）：**全部 = user_only**（`grape_` API Key / JWT；`tk_` → 403）。
> 面向：Grape AI Devspace 模板创建的开发环境管理（查询 / 创建 / 开机 / 关机 / 删除）。

## GET /api/devstage/config — 获取 DevStage 配置状态

> scope：user_only

| 出参 | DevStage 配置状态 |
| 返回 | 200 |

## PUT /api/devstage/config — 更新 DevStage 配置

> scope：user_only

请求头：`Content-Type: application/json`

请求体：DevStage 配置对象。

| 出参 | `{"ok": true}` |
| 返回 | 200 |

---

## GET /api/devstage/templates — 可用模板列表

> scope：user_only

| 出参 | 可用模板数组 |
| 返回 | 200 |

## GET /api/devstage/specs — 可用规格

> scope：user_only

| Query | `template_id`（可选） |
| 出参 | 可用规格数组 |
| 返回 | 200 |

---

## GET /api/devstage/environments — 开发环境列表

> scope：user_only

> 仅返回 Grape AI Devspace 模板创建的环境；附带 `worker_online` 字段（对应 Worker `device_name=env.name` 是否上线）。

| 出参 | `[{id, name, template, spec, status, worker_online, ...}]` |
| 返回 | 200 |

## POST /api/devstage/environments — 创建开发环境

> scope：user_only

请求头：`Content-Type: application/json`

请求体（CreateEnvBody）：环境创建参数（默认 AI DEVSPACE 模板，自动查规格、自动开机、自动注入凭据）。

| 出参 | 创建后的环境对象 |
| 返回 | 200/201；400 参数非法 |

## POST /api/devstage/environments/{action} — 环境开机/关机

> scope：user_only

| Path | `{action}`（`start` / `stop` 等） |
| 请求头：`Content-Type: application/json` | 动作参数 |
| 出参 | `{"ok": true}` |
| 返回 | 200；400 动作非法 |

## DELETE /api/devstage/environments/{devstage_id} — 删除开发环境

> scope：user_only

> 同时删除对应的 Worker 凭据。

| Path | `{devstage_id}` string |
| 出参 | `{"ok": true}` |
| 返回 | 200；404 不存在 |

---

## 错误返回（全端点通用）

| 码 | 场景 |
|----|------|
| 400 | 参数/动作非法 |
| 401 | 未认证 / token 无效 |
| 403 | `tk_` 会话 token 不适用（user_only） |
| 404 | 环境不存在 |

```json
{"detail": {"code": 404, "message": "Not Found"}}
```