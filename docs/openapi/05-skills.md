# Skills 技能接口（/api/skills）

> 对外公开，认证：`get_current_user_or_api_key`。
> scope（x-token-scope）：全部 = **user_only**（`grape_` API Key / JWT；`tk_` → 403）。

## GET /api/skills — 技能列表

> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）
> CLI：`grape skills list`（operation_id=`skills_list`，命令名 ≠ 路径，见 README「CLI 命令映射」）。

| Query | 参数 | 类型 | 必填 | 说明 |
|------|------|------|:---:|------|
| `repo` | string | 否 | 仓库（scope 过滤） |
| `scope` | string | 否 | official / repo / user（缺省 = 全部；同 scope 维度过滤） |
| `agent_type` | string | 否 | vod/issue/coding/qa/sre/chat |
| `sub_scene` | string | 否 | 子场景英文 id（scaffolding/feedback-loop 等） |
| `q` | string | 否 | 关键字（skill_name / display_name / description 模糊匹配） |
| `sort` | string | 否 | `latest`（默认）/ `hot`（like_count） |
| `page` / `page_size` | int | 否 | 分页（page_size=0 时不分页返回全量） |

> 扩展字段：`is_featured, like_count, liked_by_me, scene_count, repo_count, scenes, sub_scene, sub_scene_id`。

| 出参 | 技能数组（见上过滤规则） |
| 返回 | 200 |

---

## GET /api/skills/categories — 技能分类

> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）
> CLI：`grape skills categories`（operation_id=`skills_categories`）。

| 出参 | `{categories: [...]}`（Issue #179 skill-sync-category） |
| 返回 | 200 |

---

## GET /api/skills/agent-stats — Agent 维度技能统计

> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）
> CLI：`grape skills agent-stats`。

| Query | `scope`（可选）、`repo`（可选；过滤规则与列表一致） |
| 出参 | 技能总数 + 各 agent_type 的技能数量 |
| 返回 | 200 |

---

## GET /api/skills/featured — 精选技能

> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

| Query | `limit`（可选，默认 5）、`repo`（可选，scope 过滤） |
| 出参 | 精选技能数组（is_featured=1 且 official，按 like_count 排序） |
| 返回 | 200 |

---

## GET /api/skills/stats — 技能统计
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）
> CLI：`grape skills stats`。

| 出参 | `{skill_name: {scene_count: N, repo_count: N}}`（scene_skill_ref 聚合） |
| 返回 | 200 |

---

## GET /api/skills/search — 搜索技能
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）
> CLI：`grape skills search`。

| Query | `q`（必填） |
| 出参 | `[{skill_id, name, display_name, description, match_reason}]`（先 skill_name 精准，再 description+display_name+agent_type 模糊） |
| 返回 | 200 |

---

## GET /api/skills/by-id/{skill_id} — 技能详情
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

| Path | `{skill_id}` |
| 出参 | `{id, skill_name, display_name, description, agent_type, skill_path, scope, owner_id, ...}` |
| 返回 | 200；404 |

---

## GET /api/skills/by-id/{skill_id}/download — 下载技能 ZIP
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

| Path | `{skill_id}` |
| 出参 | ZIP（application/zip） |
| 返回 | 200 |

---

## POST /api/skills/upload — 上传技能
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）
> CLI：`grape skills upload`（命令路径即端点，见 README「CLI 命令映射」）。

请求头：`multipart/form-data`，字段：

| 字段 | 类型 | 必填 | 说明 |
|------|------|:---:|------|
| `file` | ZIP | ✅ | 技能包（校验通过后落盘/存 OBS） |
| `scope` | string | 否 | `user`（个人技能，默认）/ `repo`（仓库技能） |
| `repo` | string | repo 时必填 | 仓库技能归属仓库（`owner/repo`） |
| `agent_type` | string | ✅ | 技能类型（vod/issue/coding/qa/sre/chat） |

> **owner 由服务端派生，不接受客户端传入**：
> - `scope=user` → 归属 `user["id"]`（当前登录用户）
> - `scope=repo` → 归属 `repo`（repo_key）
>
> 客户端**不传 owner_id**——owner 是服务端鉴权后的身份（技能所有者），若允许客户端指定会破坏越权隔离
> （他人 token 可借 owner_id 写他人段）。user 技能与 token 身份天然绑定，不存在「token 与 owner 不一致」。

| 出参 | `{"message": "...", ...}` |
| 返回 | 200；400 包结构非法/scope 非法/agent_type 非法；409 同名冲突；502 OBS 存储失败 |

---

## DELETE /api/skills/by-id/{skill_id} — 删除技能
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

| Path | `{skill_id}` |
| 返回 | 200/204；400 被引用；404 |

---

## GET /api/skills/template — 技能模板 ZIP
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

| 出参 | ZIP（含 my-skill/ 示例） |
| 返回 | 200 |

---

## GET /api/skills/{skill_id}/sub — 子技能列表
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

## GET /api/skills/{skill_id}/sub/{sub_name} — 读子技能
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

## GET /api/skills/package-sub/{pkg_id}/{sub_name} — 包内子技能
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

> 子技能接口返回 SKILL.md/references 内容，供 agent 运行时按需加载。

---

## POST /api/skills/{skill_id}/like — 点赞技能
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

| 出参 | `{"liked": true, "likes": n}` |
| 返回 | 200 |

---

## POST /api/skills/convert — 转换技能（跨模式）
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

请求头：`Content-Type: application/json`

请求体（ConvertSkillRequest）：`{"skill_name": "x", "repo": "owner/repo"}`

| 出参 | `{"ok": true, ...}` |
| 返回 | 200；400 |