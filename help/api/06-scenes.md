# Scenes 场景接口（/api/scenes）

> 对外公开，认证：`get_current_user_or_api_key`。
> scope（x-token-scope）：**全部为 `user_only`（管理/市场/应用/体验等）**，唯一例外——
> `GET /api/scenes/{scene_id}`（场景详情）为 **dual**（会话 token `tk_` 可读，供 agent 按
> `GRAPE_SCENE_ID` 按需取当前场景流水线/节点）。场景清单 `GET /api/scenes`（list）为 user_only，
> agent 不需要清单（当前场景经 env `GRAPE_SCENE_ID` 注入）。
> A2A 交办：服务端校验目标 agent 是否在当前 Issue 场景的 `nodes` 内，不在则返回 `skipped: not_in_scene` 且不派发（nodes=单一事实源）。

## GET /api/scenes — 场景列表

> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）。
> agent 无需清单——当前场景经 env `GRAPE_SCENE_ID` 注入，取详情走 `GET /api/scenes/{scene_id}`。
> CLI：`grape scenes list`（operation_id=`scenes_list`，命令名 ≠ 路径，见 README「CLI 命令映射」）。

| Query | `repo`（可选；带 repo 时返回仓库级场景，否则 official） |
| 出参 | 可用场景数组 |
| 返回 | 200 |

---

## POST /api/scenes — 创建场景

> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

请求头：`Content-Type: application/json`

请求体（CreateSceneRequest）：`{scene_id, name, description, scope: "repo"|"user", nodes: [SceneNodeInput], icon}`

| Query | `repo`（可选） |
| 出参 | `{"ok": true, ...}` |
| 返回 | 200；400 scope 非法/参数缺失 |

---

## GET /api/scenes/branches/{owner}/{repo} — 仓库分支列表
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

> 场景配置中「工作分支」选择器用。

| Path | `{owner}` `{repo}` |
| 出参 | 分支名数组 |
| 返回 | 200；401 未配仓库 bot token |

## POST /api/scenes/branches/{owner}/{repo} — 创建仓库分支
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

请求头：`Content-Type: application/json`

请求体（CreateBranchRequest）：

| 字段 | 类型 | 必填 | 说明 |
|------|------|:---:|------|
| `branch_name` | string | ✅ | 新分支名 |
| `refs` | string | 否 | 源分支/引用（默认 main） |

| Path | `{owner}` `{repo}` |
| 出参 | `{"ok": true, ...}` |
| 返回 | 200；400 |

---

## GET /api/scenes/check-name — 校验场景名
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

| Query | `name`（必填）、`scope`（必填，user/repo）、`repo`（可选）、`exclude_id`（可选） |
| 出参 | `{"available": bool, ...}`（同 scope+用户+仓库名唯一） |
| 返回 | 200 |

---

## GET /api/scenes/with-pipeline — 场景列表（含流水线）
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

| Query | `repo`（可选） |
| 出参 | `[{scene_id, name, description, display_name, nodes, onboarding, is_active}]` |
| 返回 | 200 |

---

## GET /api/scenes/market/categories — 场景市场分类
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

| 出参 | `[{id, name, slug, icon, order_num}]` |
| 返回 | 200 |

---

## GET /api/scenes/market — 场景市场列表
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

| Query | 参数 | 类型 | 必填 | 说明 |
|------|------|------|:---:|------|
| `q` | string | 否 | 关键字 |
| `category_id` | int | 否 | 分类 id |
| `sort` | string | 否 | 排序 |
| `scope` | string | 否 | official / repo（缺省=全部） |
| `repo` | string | 否 | scope=repo 时的 repo_key 过滤（owner/repo） |
| `featured_only` | int | 否 | 仅推荐 |
| `limit` | int | 否 | limit>0 且不传 page 时不翻页 |
| `page` / `page_size` | int | 否 | 分页 |

| 出参 | `{items: [...], total}` |
| 返回 | 200 |

---

## POST /api/scenes/market — 创建市场场景
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

请求头：`Content-Type: application/json`

请求体（CreateMarketSceneRequest）：`{name, display_name, description, scope: "repo", nodes, icon, obs_icon_key, file_id, category_id, recommended_template}`

| Query | `repo`（可选） |
| 出参 | `{"ok": true, ...}` |
| 返回 | 200；400 参数非法 |

## GET /api/scenes/market/{market_id} — 市场场景详情
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

| Path | `{market_id}` |
| 出参 | 场景详情（含基本信息 + 推荐模板 + 可用仓库列表） |
| 返回 | 200；404 |

## PUT /api/scenes/market/{market_id} — 更新市场场景
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

请求头：`Content-Type: application/json`

请求体（UpdateMarketSceneRequest）：字段同 create（scope != 'official' 的可改）。

| Path | `{market_id}` |
| 出参 | `{"ok": true}` |
| 返回 | 200；400；404 |

## DELETE /api/scenes/market/{market_id} — 删除市场场景
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

> 硬删除用户自定义场景（`scope != 'official'`）。

| Path | `{market_id}` |
| 出参 | `{"ok": true}` |
| 返回 | 200；404 |

## POST /api/scenes/market/icon-upload — 上传场景图标
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

请求头：`multipart/form-data`，字段 `file`

> 格式 jpg/png/webp/gif/svg，大小 ≤ 2MB；产生临时文件（自定义有效期）。

| 出参 | `{file_id, icon, obs_icon_key}` |
| 返回 | 200；400 格式/大小超限 |

---

## GET /api/scenes/market/{market_id}/applied — 是否已应用
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

| Path | `{market_id}` |
| Query | `repo` |
| 出参 | `{"applied": bool}` |
| 返回 | 200 |

---

## POST /api/scenes/market/{market_id}/apply — 应用市场场景
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

| Path | `{market_id}` |
| Query | `repo` |
| 出参 | `{"ok": true}` |
| 返回 | 200；400 冲突 |

---

## POST /api/scenes/market/{market_id}/experience — 体验市场场景
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

| Path | `{market_id}` |
| 出参 | `{"ok": true, ...}` |
| 返回 | 200 |

---

## GET /api/scenes/market/{market_id}/experience-check — 体验前检查
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

| Path | `{market_id}` |
| 出参 | `{"ok": bool, "reason": ""}` |
| 返回 | 200 |

---

## POST /api/scenes/market/{market_id}/like — 点赞场景
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

| 出参 | `{"liked": true, "likes": n}` |
| 返回 | 200 |

---

## GET /api/scenes/{scene_id} — 场景详情
> scope（x-token-scope）：**dual**（`grape_` API Key / JWT / `tk_` 会话 token 均可）
> 会话 token 调用供 agent 按 `GRAPE_SCENE_ID`（env 注入）取当前场景详情（nodes/onboarding 等）。

| Path | `{scene_id}` |
| 出参 | `{id, name, nodes[], onboarding, scene_type}`（`id`=传入的 scene_id；`nodes`=技能链；`onboarding` 默认 `repo-onboarding`；`scene_type` 默认 `standard`） |
| 返回 | 200；404 |

**示例（`default` 场景）**：

```json
{
  "id": "default",
  "name": "通用开发",
  "nodes": [
    {"id": "vod",    "tag": "需求分析", "skill": "vod-cli",    "mode": "emit", "next": "issue",  "handoff": "a2a",     "enabled": true, "engine_runtime_ids": []},
    {"id": "issue",  "tag": "任务拆解", "skill": "issue-cli",  "mode": "emit", "next": "coding", "handoff": "a2a",     "enabled": true, "engine_runtime_ids": []},
    {"id": "coding", "tag": "代码实现", "skill": "coding-cli", "mode": "emit", "next": "qa",     "handoff": "a2a",     "enabled": true, "engine_runtime_ids": []},
    {"id": "qa",     "tag": "质量审查", "skill": "qa-cli",     "mode": "emit", "next": "sre",    "handoff": "a2a",     "enabled": true, "engine_runtime_ids": []},
    {"id": "sre",    "tag": "运维部署", "skill": "deploy-cli", "mode": "emit", "next": "done",   "handoff": "complete","enabled": true, "engine_runtime_ids": []}
  ],
  "onboarding": "repo-onboarding",
  "scene_type": "standard"
}
```

> `nodes[]` 是流水线的单一事实源：每条含 Agent 阶段（`id`）、对应技能（`skill`）、下游（`next`，`done`=结束）、交办方式（`handoff`）。
> 删除某节点（如 `qa`）后：详情不再显示、也不会执行；**须同步把上游 `next` 改到下一个存活节点**，
> 否则该步派发时「node not found」而 `failed`（pipeline_executor）。

---

## GET /api/scenes/skills/{agent_type} — Agent 可用技能
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

| Path | `{agent_type}` |
| 出参 | 技能数组 |
| 返回 | 200 |

---

## PUT /api/scenes/{scene_id} — 更新场景
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

请求头：`Content-Type: application/json`

请求体（UpdateSceneRequest）：`{"name","description","nodes":[...]}`

| 出参 | `{"ok": true}` |
| 返回 | 200；404 |

---

## DELETE /api/scenes/{scene_id} — 删除场景
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

| 返回 | 200/204；404 |

---

## GET /api/scenes/{scene_id}/advanced-options — 获取高级选项
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

| Path | `{scene_id}` |
| Query | `repo`（可选） |
| 出参 | `{work_branch: "main", custom_fields: [{key, value}]}` |
| 返回 | 200；404 |

## PUT /api/scenes/{scene_id}/advanced-options — 保存高级选项
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

请求头：`Content-Type: application/json`

请求体（AdvancedOptionsRequest）：

| 字段 | 类型 | 必填 | 说明 |
|------|------|:---:|------|
| `work_branch` | string | 否 | 工作分支（默认 main） |
| `custom_fields` | list | 否 | 自定义 key:value 数组 `[{key, value}]` |

| Path | `{scene_id}` |
| 出参 | `{"ok": true}` |
| 返回 | 200；400；404 |

---

## GET /api/scenes/{scene_id}/deploy-url — 获取场景访问地址
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

| Path | `{scene_id}` |
| Query | `repo`（必填，owner/repo） |
| 出参 | `{deploy_url: "..."}`（场景级，未登记为空串） |
| 返回 | 200；400（缺 repo）；403（非归属人）；404 |

## PUT /api/scenes/{scene_id}/deploy-url — 登记/编辑场景访问地址
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）
> 校验与需求中心部署 URL 一致：非空必须以 `http://` 或 `https://` 开头、长度 ≤512；空串=清除。

请求头：`Content-Type: application/json`

请求体（SceneDeployUrlBody）：`{"deploy_url": "http://1.2.3.4:5000"}`

| Path | `{scene_id}` |
| Query | `repo`（必填，owner/repo） |
| 出参 | `{"deploy_url": "..."}` |
| 返回 | 200；400（缺 repo / URL 非法）；403（非归属人）；404（场景不存在或未激活） |

---

## POST /api/scenes/apply/{owner}/{repo} — 应用场景到仓库
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

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