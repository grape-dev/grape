# Skills 技能接口（/api/skills）

> 对外公开，认证：`get_current_user_or_api_key`。
> scope（x-token-scope）：全部 = **user_only**（`grape_` API Key / JWT；`tk_` → 403）。

## GET /api/skills/featured — 精选技能

> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

| Query | `repo`（可选，scope 过滤） |
| 出参 | 技能数组（featured） |
| 返回 | 200 |

---

## GET /api/skills/stats — 技能统计
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

| 出参 | 使用统计（被场景/仓库引用次数） |
| 返回 | 200 |

---

## GET /api/skills/search — 搜索技能

| Query | `q`、`scope`、`category` 等 |
| 出参 | 技能数组 |
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

## POST /api/skills/sync-official — 同步官方技能
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

| 出参 | `{"added": n, "updated": n, "skipped": n}` |
| 返回 | 200 |

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