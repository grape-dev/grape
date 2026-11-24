# Skills 技能接口（/api/skills）

> 对外公开，认证：`get_current_user_or_api_key`。
> scope（x-token-scope）：多数 = **user_only**（`grape_` API Key / JWT；`tk_` → 403）；
> `skills_upload`、`skills_detail` 与**子技能三端点**（`sub`/`sub get`/`package-sub`）为 **dual**
> （会话 token 可调，agent 运行时按需加载）；其余（含 `skills_list`）均为 **user_only**。

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

| 出参 | `{categories: [...], tree: [...]}`（`categories` 为 Issue #179 旧一级列表；`tree` 为 Issue #2761 统一分类树） |
| 返回 | 200 |

---

## GET /api/skills/tag-suggest — 技能标签推荐

> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

| Query | 类型 | 必填 | 说明 |
|------|------|:---:|------|
| `name` | string | 否 | 技能名称（关键词候选） |
| `description` | string | 否 | 技能描述（关键词候选） |

| 出参 | `{tags: [string]}`（规则型候选，最多 3 个） |
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
> CLI：`grape skills featured`（operation_id=`skills_featured`）。

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
> scope（x-token-scope）：**dual**（`grape_` API Key / JWT / `tk_` 会话 token 均可）
> CLI：`grape skills detail --id {skill_id}`（operation_id=`skills_detail`）。

| Path | `{skill_id}` |
| 出参 | `{id, skill_name, display_name, description, agent_type, skill_path, scope, owner_id, ...}` |
| 返回 | 200；404 |

---

## GET /api/skills/by-id/{skill_id}/download — 下载技能 ZIP
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）
> CLI：`grape skills download --id {skill_id}`（operation_id=`skills_download`）。

| Path | `{skill_id}` |
| 出参 | ZIP（application/zip） |
| 返回 | 200 |

---

## GET /api/skills/by-id/{skill_id}/versions — 版本历史
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）
> operation_id=`skills_versions`（Issue #3168 详情页「版本历史」Tab）。

| Path | `{skill_id}` |
| Query | `page` / `page_size`（可选，分页，最新在前） |
| 出参 | `{versions: [{id, skill_id, version, change_note, uploaded_at, uploaded_by}], total, page, page_size}` |
| 返回 | 200；404 |

---

## GET /api/skills/by-id/{skill_id}/files — 包内文件清单
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）
> operation_id=`skills_files`（Issue #3168 详情页「资源」Tab）。

| Path | `{skill_id}` |
| 出参 | `{files: [{path, size}], file_count}`（官方技能从包内清单读取，用户技能解包枚举） |
| 返回 | 200；404 |

---

## GET /api/skills/by-id/{skill_id}/related — 相关技能
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）
> operation_id=`skills_related`（Issue #3168 详情页「相关技能」Tab）。

| Path | `{skill_id}` |
| 出参 | `{related: [{id, skill_name, display_name, description, agent_type, summary, tags, like_count}]}`（共享标签优先，同 agent_type 兜底，最多 6 条；本人可见范围内过滤） |
| 返回 | 200；404 |

---

## GET /api/skills/by-id/{skill_id}/comments — 评论列表
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）
> operation_id=`skills_comments_list`（Issue #3168 详情页「评论」Tab）。

| Path | `{skill_id}` |
| Query | `page`（默认 1）、`page_size`（默认 20，上限 50） |
| 出参 | `{comments: [{id, user_id, user_name, content, created_at}], total, page, page_size}`（最新在前） |
| 返回 | 200；404 |

---

## POST /api/skills/by-id/{skill_id}/comments — 发表评论
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403；需登录）
> operation_id=`skills_comments_create`（Issue #3168 D11：纯文本评论，≤1000 字）。

| Path | `{skill_id}` |
| Body | `{content: string}`（必填，≤1000 字） |
| 出参 | `{comment: {id, user_id, user_name, content, created_at}}` |
| 返回 | 200；400（空/超长）；404 |

---

## PATCH /api/skills/{skill_id} — 免传包更新元信息
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）
> operation_id=`skills_patch_meta`（Issue #3202：tags/summary/description/category/agent_type
> 原地更新，不产生新版本行；与 `POST /api/skills/by-id/{skill_id}/update`（需重传 ZIP 包）
> 互补，权限口径一致：official 403、user 本人、repo 托管人）。

| Path | `{skill_id}` |
| Body | `{tags?, summary?, description?, category_id?, cat_l1_id?, cat_l2_id?, agent_type?, clear_category?, intro?}`（至少一项） |
| 出参 | `{message, skill_id, summary, tags, agent_type, category_id, cat_l1_id, cat_l2_id, ...}`（updated_at 已刷新） |
| 返回 | 200；400（无字段/非法值）；403；404 |

---

## POST /api/skills/upload — 上传技能
> scope（x-token-scope）：**dual**——用户（`grape_` API Key / JWT）可传个人或仓库技能；
> 会话 token（`tk_`）仅支持 **scope=repo** 且 `repo` 必须与 token 绑定的仓库一致（越权 → 403）。
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
> CLI：`grape skills delete --id {skill_id}`（operation_id=`skills_delete`）。

| Path | `{skill_id}` |
| 返回 | 200/204；400 被引用；404 |

---

## GET /api/skills/template — 技能模板 ZIP
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）
> CLI：`grape skills template`。

| 出参 | ZIP（含 my-skill/ 示例） |
| 返回 | 200 |

---

## GET /api/skills/{skill_id}/sub — 子技能列表
> scope（x-token-scope）：**dual**（`grape_` API Key / JWT / `tk_` 会话 token 均可）
> CLI：`grape skills sub --id {skill_id}`（operation_id=`skills_sub_list`）。

## GET /api/skills/{skill_id}/sub/{sub_name} — 读子技能
> scope（x-token-scope）：**dual**（同左；agent 运行时按需加载子技能）
> CLI：`grape skills sub get --id {skill_id} --name {sub_name}`（operation_id=`skills_sub_get`）。

## GET /api/skills/package-sub/{pkg_id}/{sub_name} — 包内子技能
> scope（x-token-scope）：**dual**（同左）
> CLI：`grape skills package-sub --package {pkg_id} --name {sub_name}`（operation_id=`skills_package_sub_get`）。

> 子技能接口返回 SKILL.md/references 内容，供 agent 运行时按需加载（三端点 session token 均可读）。

---

## POST /api/skills/{skill_id}/like — 点赞技能
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）
> CLI：`grape skills like --id {skill_id}`（operation_id=`skills_like_toggle`）。

| 出参 | `{"liked": true, "likes": n}` |
| 返回 | 200 |

---

## POST /api/skills/convert — 转换技能（跨模式）
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）
> CLI：`grape skills convert --json '{"skill_name":"x","repo":"owner/repo"}'`（operation_id=`skills_convert`）。

请求头：`Content-Type: application/json`

请求体（ConvertSkillRequest）：`{"skill_name": "x", "repo": "owner/repo"}`

| 出参 | `{"ok": true, ...}` |
| 返回 | 200；400 |

---

## POST /api/skills/by-id/{skill_id}/convert — 技能原地互转 scope（Issue #3976）
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）
> CLI：未开放（内部 Web 端点，operation_id=`skills_convert_scope`）。

请求头：`Content-Type: application/json`

请求体（ConvertScopeRequest）：`{"target": "user" | "repo", "repo": "owner/repo"}`（target=repo 时必填 repo）

| 出参 | `{"message", "skill_id", "scope", "owner_id"}` |
| 返回 | 200；400/403/404/409 |

原地转换：技能 ID、历史版本与场景引用保持不变，仅切换归属。

---

## PUT /api/skills/by-id/{skill_id}/content — 保存 SKILL.md（版本号不变，Issue #3976）
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）
> CLI：未开放（内部 Web 端点，operation_id=`skills_save_content`）。

请求头：`Content-Type: application/json`

请求体（SkillContentRequest）：`{"content": "<SKILL.md 全文>"}`（≤1MB，仅个人技能）

| 出参 | `{"message", "skill_id", "content_hash", "version"}` |
| 返回 | 200；400/403/404 |

不写入版本表：保存后版本号不变；官方/仓库技能 403。

---

## POST /api/skills/by-id/{skill_id}/ai-fill — AI 填写技能元信息（Issue #3976）
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）
> CLI：未开放（内部 Web 端点，operation_id=`skills_ai_fill`）。

读取 SKILL.md（截断至 `GRAPE_AI_FILL_CONTENT_MAX_CHARS`，默认 20000 字符）交平台 LLM 生成建议。

| 出参 | `{"summary", "description", "intro": {scope,steps,products,constraints,review}, "tags": [...]}` |
| 返回 | 200；400/403/404/502/503 |

仅返回建议、不落库；时延由上游 LLM 决定，受 `GRAPE_AI_FILL_TIMEOUT_SECONDS`（默认 60s）约束。
