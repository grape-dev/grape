# Agents Agent 配置接口（/api/agents）

> 对外公开，认证：`get_current_user_or_api_key`。
> scope（x-token-scope）：全部 = **user_only**（`grape_` API Key / JWT；`tk_` → 403）。

## GET /api/agents/types — Agent 类型列表

| 出参 | `[{agent_type, display_name, description, icon, order_num, ...}]` |
| 返回 | 200 |

---

## GET /api/agents/{name}/{owner}/{repo} — 查询 Agent 配置

| Path | `{name}`（vod/issue/coding/qa/sre/chat）`{owner}` `{repo}` |
| 出参 | `{id, runtime, skill, skill_id, repo_gitcode_token(脱敏), runtime_session_id, status, last_active_at, enable, subscribe_events, im_*}` |
| 返回 | 200；404 未配置 |

---

## PUT /api/agents/{name}/{owner}/{repo} — 更新 Agent 配置

请求头：`Content-Type: application/json`

请求体（AgentConfig）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `backend` | string | 引擎名（兼容） |
| `backends` | array | 引擎集合 |
| `skill` | string | 技能名（兼容旧前端） |
| `skill_id` | int | 技能主键 id（新） |
| `skill_scope` | string | official/repo/user |
| `enable` | bool | 启停 |
| `subscribe_events` | array | 订阅事件 |
| `im_app_id` / `im_app_secret` | string | 飞书配置 |

| 出参 | `{"ok": true, "id": ...}` |
| 返回 | 200；400 skill 无效；404 |

---

## POST /api/agents/{name}/{owner}/{repo}/clean — 清理 Agent 会话

| Path | `{name}` `{owner}` `{repo}` |
| 出参 | `{"message": "Agent 会话已清理（runtime_session_id 已置空）", "cleaned": true, "agent_type": "vod"}` |
| 返回 | 200；400 未知 Agent |