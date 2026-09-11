# Skills 技能接口（/api/skills）

> 对外公开，认证：`get_current_user_or_api_key`。

## GET /api/skills/featured — 精选技能

| Query | `repo`（可选，scope 过滤） |
| 出参 | 技能数组（featured） |
| 返回 | 200 |

---

## GET /api/skills/stats — 技能统计

| 出参 | 使用统计（被场景/仓库引用次数） |
| 返回 | 200 |

---

## GET /api/skills/search — 搜索技能

| Query | `q`、`scope`、`category` 等 |
| 出参 | 技能数组 |
| 返回 | 200 |

---

## GET /api/skills/by-id/{skill_id} — 技能详情

| Path | `{skill_id}` |
| 出参 | `{id, skill_name, display_name, description, agent_type, skill_path, scope, owner_id, ...}` |
| 返回 | 200；404 |

---

## GET /api/skills/by-id/{skill_id}/download — 下载技能 ZIP

| Path | `{skill_id}` |
| 出参 | ZIP（application/zip） |
| 返回 | 200 |

---

## POST /api/skills/upload — 上传技能

请求头：`multipart/form-data`，字段 `file`（ZIP）+ `scope` + `owner_id`

| 出参 | `{id, skill_name, ...}` |
| 返回 | 200；400 包结构非法 |

---

## DELETE /api/skills/by-id/{skill_id} — 删除技能

| Path | `{skill_id}` |
| 返回 | 200/204；400 被引用；404 |

---

## POST /api/skills/sync-official — 同步官方技能

| 出参 | `{"added": n, "updated": n, "skipped": n}` |
| 返回 | 200 |

---

## GET /api/skills/template — 技能模板 ZIP

| 出参 | ZIP（含 my-skill/ 示例） |
| 返回 | 200 |

---

## GET /api/skills/{skill_id}/sub — 子技能列表

## GET /api/skills/{skill_id}/sub/{sub_name} — 读子技能

## GET /api/skills/package-sub/{pkg_id}/{sub_name} — 包内子技能

> 子技能接口返回 SKILL.md/references 内容，供 agent 运行时按需加载。

---

## POST /api/skills/{skill_id}/like — 点赞技能

| 出参 | `{"liked": true, "likes": n}` |
| 返回 | 200 |

---

## POST /api/skills/convert — 转换技能（跨模式）

请求头：`Content-Type: application/json`

请求体（ConvertSkillRequest）：`{"skill_name": "x", "repo": "owner/repo"}`

| 出参 | `{"ok": true, ...}` |
| 返回 | 200；400 |