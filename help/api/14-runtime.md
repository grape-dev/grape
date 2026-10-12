# Runtime 运行时接口（/api/runtime）

> 对外公开（openapi-full-coverage-design.md C/D 批收录），认证：`require_no_session_token`。
> scope（x-token-scope）：**全部 = user_only**（`grape_` API Key / JWT；`tk_` → 403）。
> 面向：引擎（runtime/worker）运维管理——健康度 / 任务 / 会话 / 标签 / 凭据聚合。
> 与 `/api/settings/agent-runtimes` 的差异：本接口聚合「每个引擎实例」的运行视角（任务/会话/健康/标签），
> settings 侧重「凭据管理 + 引擎列表」。

## 机器健康度

### GET /api/runtime/admin/machine-health — 机器健康度概览

> scope：user_only

| 出参 | `[{runtime_id, name, health_score, total_tasks, failed_tasks, consecutive_failures, failure_rate, ...}]` |
| 返回 | 200 |

### GET /api/runtime/admin/machine-health/{runtime_id} — 单引擎健康详情

> scope：user_only

| Path | `{runtime_id}` int |
| 出参 | 单引擎健康度详情 |
| 返回 | 200；404 |

### GET /api/runtime/admin/event-bus — 事件总线调试

> scope：user_only

| 出参 | 事件总线历史与监听器列表（debug） |
| 返回 | 200 |

### POST /api/runtime/admin/batch-recover — 批量恢复陈旧任务

> scope：user_only

> 手动触发批量陈旧任务恢复（Phase 5）。

| 出参 | 恢复结果摘要 |
| 返回 | 200 |

---

## 任务（运行时执行实例）

### GET /api/runtime/tasks/{task_id}/status — 任务状态轮询

> scope：user_only

| Path | `{task_id}` string |
| 出参 | 任务状态（chat 前端 D5 打字机降级用） |
| 返回 | 200；404 |

### POST /api/runtime/tasks/{task_id}/cancel — 取消任务

> scope：user_only

| Path | `{task_id}` string |
| 出参 | `{"status": "cancelled", ...}`（置 cancelled；worker 感知退出） |
| 返回 | 200 |

---

## 引擎实例（runtimes）

### GET /api/runtime/runtimes/{runtime_id}/tasks — 引擎任务列表

> scope：user_only

| Path | `{runtime_id}` int |
| Query | `status`（可选）、`limit`（可选）、`repo`（可选） |
| 出参 | 该引擎执行的任务列表（M4 前端设备「查看任务」） |
| 返回 | 200 |

### GET /api/runtime/runtimes/{runtime_id}/sessions — 引擎会话列表

> scope：user_only

| Path | `{runtime_id}` int |
| Query | `repo`（可选） |
| 出参 | 有匹配任务的 session 列表（按 agent_tasks.runtime_id 过滤） |
| 返回 | 200 |

### GET /api/runtime/credentials/{cid}/tasks — 凭据聚合任务

> scope：user_only

| Path | `{cid}` int |
| Query | `status`（可选）、`limit`（可选） |
| 出参 | 该凭据全部引擎实例的 agent_tasks 聚合 |
| 返回 | 200 |

### POST /api/runtime/runtimes/{runtime_id}/refresh-skills — 重新扫描引擎技能

> scope：user_only

| Path | `{runtime_id}` int |
| Query | `force`（可选，bool） |

> 本机直连引擎（credential_id=0）直接扫描立即返回；远端 worker 标记 `pending_skill_scan=1`，
> 下次 claim 扫描后回报——前端轮询本接口直至 `status != pending`。

| 出参 | `{"status": "pending|done", "skills": [...]}` |
| 返回 | 200 |

---

## 引擎属性（别名 / 并发 / 标签）

### PUT /api/runtime/runtimes/{runtime_id}/alias — 设置引擎别名

> scope：user_only

| Path | `{runtime_id}` int |
| 请求体（AliasBody） | `alias`（空串=清除） |
| 出参 | `{"ok": true}` |
| 返回 | 200 |

### PUT /api/runtime/runtimes/{runtime_id}/max-concurrent — 设置最大并发

> scope：user_only

| Path | `{runtime_id}` int |
| 请求体（MaxConcurrentBody） | `max_concurrent` int |
| 出参 | `{"ok": true}` |
| 返回 | 200 |

### GET /api/runtime/runtimes/{runtime_id}/role-tags — 引擎角色标签

> scope：user_only

| Path | `{runtime_id}` int |
| 出参 | 角色标签列表（web 管理视图；worker 取 tags 用 `GET /runtimes/{id}/tags`） |
| 返回 | 200 |

### PUT /api/runtime/runtimes/{runtime_id}/tags — 设置角色标签

> scope：user_only

| Path | `{runtime_id}` int |
| 请求头：`Content-Type: application/json` | 标签数组（应用互斥规则后写入；以 engine_capabilities 为单一数据源） |
| 出参 | `{"ok": true}` |
| 返回 | 200 |

### GET /api/runtime/runtimes/{runtime_id}/scene-tags — 引擎场景标签

> scope：user_only

| Path | `{runtime_id}` int |
| 出参 | 场景标签列表 |
| 返回 | 200 |

### PUT /api/runtime/runtimes/{runtime_id}/scene-tags — 设置场景标签

> scope：user_only

| Path | `{runtime_id}` int |
| 请求体（SceneTagsBody） | 场景标签数组（元数据，约束可选角色标签范围） |
| 出参 | `{"ok": true}` |
| 返回 | 200 |

### GET /api/runtime/scene-tags — 场景市场场景及可用角色标签

> scope：user_only

| 出参 | `[{name, display_name, roles: ["vod","issue","coding","qa","sre"]}, ...]` |
| 返回 | 200 |

---

## 错误返回（全端点通用）

| 码 | 场景 |
|----|------|
| 400 | 参数非法 |
| 401 | 未认证 / token 无效 |
| 403 | `tk_` 会话 token 不适用（user_only） |
| 404 | runtime_id / task_id / cid 不存在 |

```json
{"detail": {"code": 404, "message": "Not Found"}}
```