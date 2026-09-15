# Scenes 场景接口（/api/scenes）

> 对外公开，认证：`get_current_user_or_api_key`。
> scope（x-token-scope）：`GET /api/scenes`（list）= **dual**（唯一例外：VOD 技能用 session 调，
> 故意不归 D 类，见 `docs/weekly-03/token-scope-classification-design.md`）；其余（管理/市场/应用/体验等） =
> **user_only**（`grape_` API Key / JWT；`tk_` → 403）。

## GET /api/scenes/with-pipeline — 场景列表（含流水线）

| Query | `repo`（可选） |
| 出参 | `[{scene_id, name, description, display_name, nodes, onboarding, is_active}]` |
| 返回 | 200 |

---

## GET /api/scenes/market/categories — 场景市场分类

| 出参 | `[{id, name, slug, icon, order_num}]` |
| 返回 | 200 |

---

## GET /api/scenes/market — 场景市场列表

| Query | `page`、`scene_id`、`keyword` 等 |
| 出参 | `{items: [...], total}` |
| 返回 | 200 |

---

## GET /api/scenes/market/{market_id} — 市场场景详情

| Path | `{market_id}` |
| 出参 | 场景详情 |
| 返回 | 200；404 |

---

## GET /api/scenes/market/{market_id}/applied — 是否已应用

| Path | `{market_id}` |
| Query | `repo` |
| 出参 | `{"applied": bool}` |
| 返回 | 200 |

---

## POST /api/scenes/market/{market_id}/apply — 应用市场场景

| Path | `{market_id}` |
| Query | `repo` |
| 出参 | `{"ok": true}` |
| 返回 | 200；400 冲突 |

---

## POST /api/scenes/market/{market_id}/experience — 体验市场场景

| Path | `{market_id}` |
| 出参 | `{"ok": true, ...}` |
| 返回 | 200 |

---

## GET /api/scenes/market/{market_id}/experience-check — 体验前检查

| Path | `{market_id}` |
| 出参 | `{"ok": bool, "reason": ""}` |
| 返回 | 200 |

---

## POST /api/scenes/market/{market_id}/like — 点赞场景

| 出参 | `{"liked": true, "likes": n}` |
| 返回 | 200 |

---

## GET /api/scenes/{scene_id} — 场景详情

| Path | `{scene_id}` |
| 出参 | `{scene_id, name, nodes, onboarding, ...}` |
| 返回 | 200；404 |

---

## GET /api/scenes/skills/{agent_type} — Agent 可用技能

| Path | `{agent_type}` |
| 出参 | 技能数组 |
| 返回 | 200 |

---

## PUT /api/scenes/{scene_id} — 更新场景

请求头：`Content-Type: application/json`

请求体（UpdateSceneRequest）：`{"name","description","nodes":[...]}`

| 出参 | `{"ok": true}` |
| 返回 | 200；404 |

---

## DELETE /api/scenes/{scene_id} — 删除场景

| 返回 | 200/204；404 |

---

## POST /api/scenes/apply/{owner}/{repo} — 应用场景到仓库

请求头：`Content-Type: application/json`

请求体（ApplySceneRequest）：`{"scene_id": "default"}`

| Path | `{owner}` `{repo}` |
| 出参 | `{"ok": true}` |
| 返回 | 200；400 |

---

## 场景节点（SceneNodeInput）字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | string | 节点 id |
| `tag` | string | 标签 |
| `skill` | string | 技能 |
| `mode` | string | emit 等 |
| `next` | string | 下游节点（done 表示结束） |
| `handoff` | string | a2a/complete |
| `skip` | list | 跳过条件 |
| `enabled` | bool | 启用 |
| `engine_runtime_ids` | list | 限定引擎 |