# Knowledge 知识接口（/api/knowledge）

> 对外公开，认证：`get_current_user_or_api_key`。
> scope（x-token-scope）：读接口 `list/stats/detail` = **dual**；写操作
> （entry/refs/delete/update）= **user_only**（`grape_` API Key / JWT；`tk_` → 403）。
> 概念：知识 = 仓库/场景/Agent 注入的说明文档。**文件型**（`source=system/agent`，按 repo+file 落 OBS/工作区）
> 与**条目型**（`source=manual`，用户 Web 直接录入，以 `knowledge_id` 定位）两类并存。

## 读取（list / stats / detail）

### GET /api/knowledge — 知识清单（统一列表 + 分页）

> scope（x-token-scope）：**dual**（三通道）。

| Query | 参数 | 类型 | 必填 | 说明 |
|------|------|------|:---:|------|
| `repo` | string | ✅ | repo_key（无前缀亦接受） |
| `source` | string | 否 | system / agent / manual |
| `page` / `page_size` | int | 否 | 分页，默认 1 / 50 |
| `sort` | string | 否 | `latest` = 按更新时间倒序 |
| `q` | string | 否 | 关键字（匹配 name/description） |

| 出参 | `{items, total, page, page_size}` |
| 返回 | 200；400 缺 repo |

### GET /api/knowledge/stats — 知识统计

> scope（x-token-scope）：**dual**（三通道）。

| Query | `repo`（必填） |
| 出参 | 按 source（system/agent/manual/unlinked）分类的统计计数 |
| 返回 | 200；400 缺 repo |

### GET /api/knowledge/{knowledge_id} — 知识详情

> scope（x-token-scope）：**dual**（三通道）。

| Path | `{knowledge_id}` int |
| 出参 | 知识完整对象（含 content、refs 摘要） |
| 返回 | 200；404 不存在 |

---

## 条目（source=manual）写操作

### POST /api/knowledge/entry — 创建知识条目

> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）。

请求头：`Content-Type: application/json`

请求体（KnowledgeEntryBody）：

| 字段 | 类型 | 必填 | 说明 |
|------|------|:---:|------|
| `name` | string | ✅ | 名称（≤50） |
| `content` | string | ✅ | 正文（≤10KB） |
| `repo` | string | ✅ | 归属仓库（repo_key / owner-repo） |
| `description` | string | 否 | 描述（≤200） |
| `agent_type` | string | 否 | 关联 agent 类型 |

| 出参 | `{"ok": true, "message", "file_path", "id"}` |
| 返回 | 200；400 缺 name/content/repo 或超长 |

### PUT /api/knowledge/{knowledge_id} — 编辑知识

> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）。

| Path | `{knowledge_id}` int |
| 请求体（KnowledgeUpdateBody） | `name` / `content` / `description`（source=manual 可编辑） |
| 出参 | `{"ok": true, "message": "知识已更新"}` |
| 返回 | 200；400；403 系统生成不可编辑/非创建者；404 |

### DELETE /api/knowledge/{knowledge_id} — 删除知识条目

> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）。

| Path | `{knowledge_id}` int |
| 出参 | `{"ok": true, "message": "知识已删除"}` |
| 返回 | 200；403 系统生成不可删除/非创建者；404 |

---

## 关联（refs：场景/Agent）

### GET /api/knowledge/{knowledge_id}/refs — 关联列表

> scope（x-token-scope）：**user_only**。

| Path | `{knowledge_id}` int |
| 出参 | `{refs: [{id, scene_id, agent_type, enabled, ...}]}` |
| 返回 | 200 |

### POST /api/knowledge/{knowledge_id}/refs — 添加关联

> scope（x-token-scope）：**user_only**。

| Path | `{knowledge_id}` int |
| 请求体（KnowledgeRefBody） | `{scene_id, agent_type}` |
| 出参 | `{"ok": true}` |
| 返回 | 200；400；403 非创建者/非仓库托管者；409 已存在 |

### POST /api/knowledge/{knowledge_id}/refs/batch — 批量添加关联

> scope（x-token-scope）：**user_only**。

| Path | `{knowledge_id}` int |
| 请求体（KnowledgeRefBatchBody） | `{scene_ids: [], agent_types: []}`（幂等，已存在跳过） |
| 出参 | `{"ok": true, "added": n}` |
| 返回 | 200 |

### PUT /api/knowledge/ref/{ref_id} — 修改关联

> scope（x-token-scope）：**user_only**。

| Path | `{ref_id}` int |
| 请求体（KnowledgeRefUpdateBody） | `{enabled: bool}` |
| 出参 | `{"ok": true}` |
| 返回 | 200；404 不存在 |

### DELETE /api/knowledge/ref/{ref_id} — 删除关联

> scope（x-token-scope）：**user_only**。

| Path | `{ref_id}` int |
| 出参 | `{"ok": true}` |
| 返回 | 200；404 不存在 |

---

## 错误返回（全端点通用）

| 码 | message（英文） | 场景 |
|----|--------------|------|
| 400 | Bad Request / Invalid parameter | 缺 repo / 参数校验失败 |
| 401 | Unauthorized | 未认证 / token 失效 |
| 403 | Forbidden | 越权（`tk_` 不适用、非创建者、系统生成不可编辑/删除） |
| 404 | Not Found | 知识/关联不存在 |
| 409 | Conflict | 关联已存在 |
| 500 | Internal Server Error | OBS 读写失败 |

```json
{"detail": {"code": 404, "message": "Not Found"}}
```
