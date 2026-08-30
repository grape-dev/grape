# Scene Center 改造方案

> 2026-08-28 · 基于 dev 分支 e4b44172

## 一、需求概述

将 Dashboard 模块改为 Scene Center，核心变化：

1. **场景选择驱动展示** — 选择开发场景后，不在场景中的 agent 变灰，skill 和 engine 列表替代当前 events-today 和 backend 模块
2. **场景配置持久化到 DB** — 从 `configs/pipelines.json` 迁移到数据库，支持运行时增删改
3. **Engine 绑定可配** — 场景中每个 agent 可选绑定 engine（不选=广播竞争，选了=限定引擎）
4. **自定义场景** — 用户可创建个人级/仓库级场景，除 vod/issue 外的 agent 可标记为"不需要"（叉号/对号切换）
5. **官方场景保护** — pipelines.json 中的场景为官方场景，所有人可见，不可编辑

## 二、现状分析

### 2.1 场景数据流（当前）

```
configs/pipelines.json (7 个官方场景)
  ↓ mtime cache
pipeline_config.py
  ├── get_scenes() → [{id, name}]  → scenes 路由 → 前端下拉
  ├── get_pipeline_config(scene_id) → {name, nodes, onboarding}
  └── get_node_by_tag(scene_id, tag) → {id, tag, skill, mode, next, handoff}
       ↑
event_pipeline.py::resolve_skill_from_pipeline(repo_key, agent_type, scene_id)
  ↑
run_pipeline() / _enqueue_pipeline_task() / _dispatch_subscription_agent()
  ↑
webhook.py::a2a_endpoint() — A2A 交办时查 skill
```

### 2.2 场景选择入口（当前）

- **仓库级**：`managed_repos.dev_scene` 字段，WebUI Dashboard 下拉选择 → `POST /api/scenes/apply/{owner}/{repo}`
- **Issue 级**：`issue_state.scene_id` 字段，VOD agent 分析后通过 A2A payload 的 `recommended_scene` 设置

### 2.3 Dashboard 布局（当前）

```
┌─────────────────────────────────────────────┐
│ 仓库信息卡（owner/repo · role · metrics）     │
├─────────────────────────────────────────────┤
│ DevOps Agent Workflow                       │
│ [场景下拉] [应用]                            │
│ ┌─────┐ ┌─────┐ ┌──────┐ ┌────┐ ┌─────┐    │
│ │ VOD │ │Issue│ │Coding│ │ QA │ │ SRE │    │
│ │desc │ │desc │ │desc  │ │desc│ │desc │    │
│ │backend│backend│backend│backend│backend│  │
│ │events│ │events│ │events│ │events│ │events│ │
│ └─────┘ └─────┘ └──────┘ └────┘ └─────┘    │
│ ☎ Chat Agent                                │
├─────────────────────────────────────────────┤
│ Recent Activity (last 24h)                  │
└─────────────────────────────────────────────┘
```

每个 agent 卡片当前显示：
- 描述文字
- Backend（引擎名或"无 engine"）
- Events today / Processing / Idle
- 最近 2 个事件摘要

### 2.4 Engine 选择逻辑（当前）

```
pick_engine(repo_key, agent_type, fallback_runtime_id)
  → get_binding(repo_key, agent_type)  // devops_agent_runtime_binding 表
  → _filter_by_tags(members, agent_type)  // engine_capabilities.tag
  → 按 scope 优先级选健康引擎
  → binding 为空 + plugin 模式 → 返回 0（广播）
  → binding 为空 + legacy 模式 → 返回 None
```

`devops_agent_runtime_binding` 表是 `(repo_key, agent_type) → [runtime_ids]`，与场景无关。

### 2.5 VOD 场景检测路径（当前）

```
skills/vod/SKILL.md B5.5 节
  → VOD agent 分析 Issue 内容 + 项目知识基线
  → JSON 产出中输出 recommended_scene + scene_reason
  → A2A payload 传 recommended_scene
  → webhook.py::a2a_endpoint() 调 set_issue_scene() 持久化到 issue_state.scene_id
  → 后续 agent 从 get_issue_scene() 读场景
  → resolve_skill_from_pipeline(repo_key, agent_type, scene_id) 查 pipelines.json
```

VOD skill 中的"可用开发场景"列表通过 prompt 注入，来源是 `get_scenes()` → `pipelines.json`。

## 三、改动方案

### 3.1 数据库设计

#### 新建表：`devops_pipeline_scene`

```sql
CREATE TABLE IF NOT EXISTS devops_pipeline_scene (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scene_id    TEXT NOT NULL,          -- 逻辑 ID，如 "default"、"my-custom-scene"
    name        TEXT NOT NULL,          -- 显示名，如"通用开发"
    description TEXT DEFAULT '',        -- 场景描述
    scope       TEXT DEFAULT 'official',-- official(官方) / repo(仓库级) / user(个人级)
    repo_key    TEXT DEFAULT '',        -- scope=repo 时为仓库 key；official/user 时为空
    user_id     INTEGER DEFAULT 0,      -- scope=user 时为用户 ID
    nodes_json  TEXT DEFAULT '[]',      -- JSON: [{id, tag, skill, mode, next, handoff, skip, enabled, engine_runtime_ids}]
    onboarding  TEXT DEFAULT 'repo-onboarding',
    is_active   INTEGER DEFAULT 1,      -- 软删除
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now')),
    UNIQUE (scene_id, scope, repo_key, user_id)
)
```

`nodes_json` 结构（每个 node）：
```json
{
  "id": "vod",
  "tag": "vod",
  "skill": "vod",
  "mode": "emit",
  "next": "issue",
  "handoff": "a2a",
  "skip": [],
  "enabled": true,          // false = 灰色/不需要（仅非 vod/issue 可设 false）
  "engine_runtime_ids": []  // 空 = 广播竞争；非空 = 限定这些引擎
}
```

#### 迁移逻辑（`_migrate_tables`）

1. 建表
2. 首次运行时从 `configs/pipelines.json` 导入官方场景（`scope='official'`），meta 标志 `scenes_seeded` 防重跑
3. 后续 `pipelines.json` 仅作为官方场景的种子文件，运行时不再读取（改读 DB）

#### `managed_repos` 表无变化

`dev_scene` 字段保留，但存储的 scene_id 现在指向 `devops_pipeline_scene` 表。

### 3.2 后端 API 改动

#### `src/api/pipeline_config.py` — 改为 DB 读取

| 函数 | 改动 |
|------|------|
| `_load_pipelines()` | 删除 mtime cache，改为从 DB 查询 |
| `get_scenes()` | 查 DB：`scope='official'` + `scope='repo' AND repo_key=?` + `scope='user' AND user_id=?` |
| `get_pipeline_config(scene_id, repo_key, user_id)` | 查 DB，按 official → repo → user 优先级 |
| `get_node_by_tag(scene_id, tag, repo_key, user_id)` | 同上，从 DB 结果中查 node |
| `_load_user_scene()` | 删除（不再从成员上传文件读） |

新增函数：
```python
def get_scene_nodes_with_engines(scene_id, repo_key, user_id) -> list[dict]:
    """返回场景 nodes，每个 node 附带 engine_runtime_ids 和引擎详情"""

def create_custom_scene(scene_id, name, description, scope, repo_key, user_id, nodes) -> int:
    """创建自定义场景，返回 DB id"""

def update_custom_scene(scene_id, scope, repo_key, user_id, nodes, name, description):
    """更新自定义场景（官方场景不可更新）"""

def delete_custom_scene(scene_id, scope, repo_key, user_id):
    """软删除自定义场景"""
```

#### `src/api/routes/scenes.py` — 新增 CRUD 端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/scenes` | GET | 返回可见场景列表（official + repo + user） |
| `/api/scenes/{scene_id}` | GET | 返回场景详情（nodes + engine 信息） |
| `/api/scenes` | POST | 创建自定义场景（body: name, description, scope, nodes） |
| `/api/scenes/{scene_id}` | PUT | 更新自定义场景 |
| `/api/scenes/{scene_id}` | DELETE | 删除自定义场景 |
| `/api/scenes/apply/{owner}/{repo}` | POST | 应用场景到仓库（保留现有，改读 DB） |
| `/api/scenes/with-pipeline` | GET | 保留，改读 DB |
| `/api/scenes/skills/{agent_type}` | GET | 返回该 agent_type 可选的 skill 列表（从 skill 表查询，scope=official/user/repo） |

#### `src/api/routes/dashboard.py` — 改造返回数据

`get_dashboard()` 返回数据变化：

| 字段 | 变化 |
|------|------|
| `_scenes` | 改为从 DB 查（official + repo + user），含 scope 字段 |
| `dev_scene` | 不变（scene_id） |
| `agents` | 每个 agent 增加 `skill`（当前场景中的 skill 名）和 `enabled_in_scene`（是否在场景中） |
| `agent_engines` | 新增：每个 agent 可用的引擎列表 `[{id, name, device, selected}]` |
| `activity` | 删除（Recent Activity 去掉） |
| `agent_event_counts` | 删除（events-today 模块去掉） |
| `agent_recent_events` | 删除 |

### 3.3 Engine 选择逻辑改动

#### 核心变化：场景 node 的 `engine_runtime_ids` 限定广播范围（不改变广播机制）

Plugin 模式下，engine 选择后仍然是广播（runtime_id=0 + pool_tag），只是 claim_by_tag 时只有选中的引擎能匹配到任务。传统模式完全不受影响。

```
pick_engine(repo_key, agent_type, fallback_runtime_id, scene_id="", user_id=0)
  ① 查 devops_agent_runtime_binding（保留，向后兼容）
  ② binding 空 → 查场景 node 的 engine_runtime_ids
     → 传统模式：照原逻辑（resolve_default_engine 或返回 None）
     → Plugin 模式：
       → engine_runtime_ids 非空 → 仍然返回 0（广播），但把限定列表写入 pool_engine_ids 字段
       → engine_runtime_ids 为空 → 返回 0（全量广播）
  ③ 无场景信息 → 返回 0（plugin 全量广播）/ None（legacy）
```

**任务入队增加 `pool_engine_ids` 字段**：`agent_tasks` 表新增列 `pool_engine_ids TEXT DEFAULT ''`，存储 JSON 数组（如 `[76,77]`）。空字符串=全量广播。

**claim_by_tag 改动**：认领时检查 `pool_engine_ids`——非空且当前引擎不在列表中 → 跳过（不认领）。

**改动文件**：
- `src/runtime/engine_select.py` — pick_engine 逻辑
- `src/runtime/service.py` — enqueue_task 增加 pool_engine_ids 参数 + claim_by_tag 增加过滤
- `src/api/event_pipeline.py` — 入队时传入 pool_engine_ids
- `src/api/db_setup.py` — agent_tasks 新增 pool_engine_ids 列

### 3.4 VOD 场景检测路径改动

#### VOD Skill Prompt 改动

`skills/vod/SKILL.md` B5.5 节的"可用开发场景"列表当前通过 prompt 注入。改动后：

1. **prompt 注入来源改为 DB** — `build_prompt()` 中注入的场景列表从 `get_scenes()` 获取（已改为 DB 读取）
2. **场景列表格式不变** — VOD agent 看到的场景列表格式不变，只是数据来源从文件变为 DB
3. **`recommended_scene` 机制不变** — VOD 输出 `recommended_scene` → A2A → `set_issue_scene()` → 后续 agent 读取

**改动文件**：
- `src/prompt_builder.py` — 注入场景列表的代码路径（如果有的话，需确认当前是否注入）
- `src/api/event_pipeline.py` — `resolve_skill_from_pipeline()` 改为从 DB 读场景

#### Task Center 影响点

Task Center 显示任务列表时可能需要关联场景信息。当前 `issue_state.scene_id` 存储场景 ID，改动后：
- `issue_state.scene_id` 仍存储场景 ID（如 "default" 或 "my-custom"）
- Task Center 查 `issue_state` 时加入场景名（JOIN `devops_pipeline_scene` 或单独查）
- 自定义场景被删除后，`issue_state.scene_id` 指向不存在的场景 → 回退到仓库默认场景（现有逻辑已处理）

### 3.5 前端改动

#### `dashboard.js` → `scene-center.js`（或重命名保持 dashboard.js）

**布局变化**：

```
┌─────────────────────────────────────────────┐
│ 仓库信息卡（保留）                           │
├─────────────────────────────────────────────┤
│ Scene Center                                │
│ [场景下拉（official/repo/user 分组）] [保存] │
│ ┌─────┐ ┌─────┐ ┌──────┐ ┌────┐ ┌─────┐    │
│ │ VOD │ │Issue│ │Coding│ │ QA │ │ SRE │    │
│ │ ✓   │ │ ✓   │ │ ✗/✓  │ │ ✗/✓│ │ ✗/✓ │    │ ← 叉号/对号（非 vod/issue）
│ │skill│ │skill│ │skill │ │skill│ │skill│    │ ← 替代 events-today
│ │engine│engine│engine│engine│engine│       │ ← 替代 backend
│ │  ▾  │ │  ▾  │ │  ▾  │ │  ▾ │ │  ▾  │    │ ← engine 下拉
│ └─────┘ └─────┘ └──────┘ └────┘ └─────┘    │
│ ☎ Chat Agent（保留）                        │
├─────────────────────────────────────────────┤
│ ❌ Recent Activity（删除）                   │
└─────────────────────────────────────────────┘
```

**Agent 卡片状态**：

| 状态 | 条件 | 显示 |
|------|------|------|
| 正常 | 场景中 enabled=true | 正常颜色 + skill 下拉 + engine 下拉 |
| 灰色 | 场景中 enabled=false 或不在场景中 | 灰色 + skill: none + engine: none（不可操作） |

**Skill 下拉**：

- 官方场景：只读显示 skill 名（不可改）
- 自定义场景编辑模式：下拉选择，从 `skill` 表按 `agent_type` 过滤可选项（如 coding agent 只能选 agent_type='coding' 的 skill）
- 灰色 agent 的 skill 下拉禁用，显示 none
- 选中后写入 `nodes_json` 的 `node.skill` 字段

**官方场景 vs 自定义场景**：

| 行为 | 官方场景 | 自定义场景 |
|------|---------|-----------|
| 查看 | ✅ | ✅ |
| 选择 | ✅ | ✅ |
| 编辑 skill 下拉 | ❌（只读） | ✅ |
| 编辑 agent enabled | ❌（只读） | ✅ |
| 编辑 engine 绑定 | ❌（只读） | ✅ |
| 叉号/对号切换 | ❌ | ✅ |
| 保存 | ❌（不可改） | ✅ |

**自定义场景创建流程**：

1. 下拉选择"➕ 创建自定义场景"
2. 进入编辑模式：所有 agent 默认 enabled=true，vod/issue 不可关闭
3. 非 vod/issue agent 右上角显示叉号 → 点击置灰 → 变对号
4. 灰色 agent 的 skill 和 engine 显示 none
5. 正常 agent 的 engine 下拉可选引擎（多选，不选=广播）
6. 点击"保存" → 弹出命名弹窗（场景名 + 描述 + scope 选择：个人级/仓库级）
7. 保存后场景出现在下拉列表

**Engine 下拉**：

- 展示当前仓库可见的所有引擎（global + repo + local scope）
- 多选（checkbox 或 tag 风格）
- 不选 = 广播竞争（显示"广播竞争"标签）
- 选了 = 限定引擎（显示选中的引擎名）
- 灰色 agent 的 engine 下拉禁用，显示 none

**保存逻辑**：

- 官方场景：选择即应用（`POST /api/scenes/apply/{owner}/{repo}`），无保存按钮
- 自定义场景编辑：点"保存" → 弹命名弹窗 → `POST /api/scenes` 创建 → 再 `POST /api/scenes/apply/{owner}/{repo}` 应用
- 自定义场景已存在：点"保存" → `PUT /api/scenes/{scene_id}` 更新

#### `app.js` 导航

`nav('dashboard')` → 标题改为 "Scene Center"（或保留 dashboard 路由，仅改标题文字）

### 3.6 改动文件清单

| 文件 | 改动类型 | 说明 |
|------|---------|------|
| `src/api/db_setup.py` | 新建表 + 迁移 | `devops_pipeline_scene` 表 DDL + 种子导入 |
| `src/api/pipeline_config.py` | 重写 | 从文件读取改为 DB 读取 + 新增 CRUD 函数 |
| `src/api/routes/scenes.py` | 扩展 | 新增 POST/PUT/DELETE 端点 + 改读 DB |
| `src/api/routes/dashboard.py` | 修改 | 返回数据加 skill/engine/scene 信息，去掉 activity |
| `src/runtime/engine_select.py` | 修改 | `pick_engine` 增加 scene_id 参数 + 场景引擎限定 |
| `src/api/event_pipeline.py` | 修改 | `resolve_skill_from_pipeline` 改读 DB + 传 scene_id 给 pick_engine |
| `src/api/routes/webhook.py` | 修改 | A2A 路径传 scene_id 给 pick_engine |
| `src/api/pipeline_executor.py` | 修改 | `_enqueue_via_pipeline` 传 scene_id 给 pick_engine |
| `src/prompt_builder.py` | 修改 | 场景列表注入改读 DB（如有） |
| `src/api/static/js/pages/dashboard.js` | 重写 | Scene Center 布局 + 自定义场景 UI |
| `src/api/static/index.html` | 修改 | 导航标题改名 |
| `src/api/static/css/app.css` | 修改 | 灰色 agent 样式 + engine 下拉样式 |

## 四、改动后完整数据流

### 4.1 场景选择 → 任务分发

```
用户在 Scene Center 选择场景
  → POST /api/scenes/apply/{owner}/{repo} {scene_id}
  → managed_repos.dev_scene = scene_id

Issue 被 webhook 触发
  → run_pipeline() → 路由到 vod agent
  → resolve_skill_from_pipeline(repo_key, "vod", scene_id)
    → get_pipeline_config(scene_id, repo_key, user_id) ← DB 查询
    → get_node_by_tag() → node.skill = "vod"
  → pick_engine(repo_key, "vod", scene_id=scene_id, user_id=user_id)
    → get_binding() ← binding 表（向后兼容）
    → binding 空 → get_scene_engine_ids(scene_id, "vod", repo_key, user_id)
      → engine_runtime_ids 非空 → 选限定引擎
      → engine_runtime_ids 为空 → 返回 0（广播）
  → enqueue_task(runtime_id=0 或具体, pool_tag="vod", ...)
```

### 4.2 VOD 场景检测 → A2A 交办

```
VOD agent 执行
  → 分析 Issue 内容
  → prompt 中的场景列表（DB 来源）→ recommended_scene="skill-dev"
  → A2A POST /api/a2a {agent:"issue", recommended_scene:"skill-dev"}
  → set_issue_scene(repo_key, "#12", "skill-dev")
  → 后续 issue agent 从 get_issue_scene() 读场景
  → resolve_skill_from_pipeline(repo_key, "issue", scene_id="skill-dev")
    → DB 查询 → node.skill = "skill-issue"
  → pick_engine(..., scene_id="skill-dev")
    → 场景中 issue node 的 engine_runtime_ids 决定路由
```

### 4.3 自定义场景创建

```
用户点"创建自定义场景"
  → 编辑模式：勾选/取消 agent（非 vod/issue）
  → 选 engine 绑定（可选）
  → 保存 → 弹命名弹窗 → 输入名称/描述/选择 scope
  → POST /api/scenes {name, description, scope, nodes}
    → create_custom_scene() → INSERT devops_pipeline_scene
  → POST /api/scenes/apply/{owner}/{repo} {scene_id}
  → 场景生效
```

## 五、对现有逻辑的影响

### 5.1 广播逻辑

**影响：场景 engine_runtime_ids 限定广播范围，但不改变广播机制本身。**

Plugin 模式下，任务始终以 `runtime_id=0 + pool_tag` 入队（广播），`engine_runtime_ids` 只影响 claim_by_tag 时的过滤：
- `engine_runtime_ids=[]` → `pool_engine_ids=''` → 所有带匹配 tag 的引擎都能抢（全量广播，现有行为）
- `engine_runtime_ids=[76,77]` → `pool_engine_ids='[76,77]'` → 只有 76 和 77 能抢
- `engine_runtime_ids=[76]` → `pool_engine_ids='[76]'` → 只有 76 能抢

传统模式完全不受影响——不走广播，照原逻辑返回 None。

**向后兼容**：`devops_agent_runtime_binding` 表优先级最高。binding 有记录时直接用 binding，不查场景 engine_runtime_ids。

### 5.2 VOD 场景检测

**影响：场景列表来源从文件改为 DB，VOD 看到的场景列表可能包含用户自定义场景。**

- VOD agent 分析时可以看到自定义场景（如用户创建了"博客发布"场景，VOD 可能推荐它）
- `recommended_scene` 指向自定义场景时，后续 agent 从 DB 查到对应 skill
- 自定义场景被删除后，`recommended_scene` 指向不存在的场景 → `get_pipeline_config()` 回退到 default

### 5.3 Task Center

**影响：任务关联的场景信息来源改为 DB。**

- Task Center 查 `issue_state.scene_id` → 需 JOIN `devops_pipeline_scene` 获取场景名
- 自定义场景删除后，Task Center 显示的场景名可能丢失 → 显示 scene_id 作为兜底

### 5.4 Redispatch

**影响：redispatch 时需要携带 scene_id。**

- 当前 redispatch 从旧 task 复制信息，新 task 的 scene_id 需要从 `issue_state` 重新读取
- 改动后 `enqueue_task` 增加 `scene_id` 参数，redispatch 时传入

## 六、测试用例

### 6.1 DB 迁移测试

| 用例 | 验证点 |
|------|--------|
| 首次启动 | `devops_pipeline_scene` 表创建，7 个官方场景导入（scope=official） |
| 二次启动 | 不重复导入（meta `scenes_seeded=1`） |
| 场景查询 | `get_scenes()` 返回 official + repo + user 场景 |
| 场景详情 | `get_pipeline_config("default")` 返回正确的 nodes 列表 |

### 6.2 API 测试

| 用例 | 验证点 |
|------|--------|
| `GET /api/scenes` | 返回 official + 当前仓库 repo + 当前用户 user 场景 |
| `POST /api/scenes` | 创建自定义场景，返回 scene_id |
| `PUT /api/scenes/{id}` | 更新自定义场景 nodes |
| `DELETE /api/scenes/{id}` | 软删除自定义场景（is_active=0） |
| `POST /api/scenes` 官方场景 | 返回 403（不可创建 official scope） |
| `PUT /api/scenes/{id}` 官方场景 | 返回 403（不可编辑 official） |
| `POST /api/scenes/apply` | managed_repos.dev_scene 更新 |

### 6.3 Engine 选择测试

| 用例 | 验证点 |
|------|--------|
| 场景 engine_runtime_ids=[] + plugin | pick_engine 返回 0，pool_engine_ids='' → 全量广播 |
| 场景 engine_runtime_ids=[76,77] + plugin | pick_engine 返回 0，pool_engine_ids='[76,77]' → 限定广播 |
| 场景 engine_runtime_ids=[76] + plugin | pick_engine 返回 0，pool_engine_ids='[76]' → 只有 76 能抢 |
| 引擎 77 claim 时 pool_engine_ids=[76] | 77 不在列表中 → claim_by_tag 跳过 |
| 引擎 76 claim 时 pool_engine_ids=[76] | 76 在列表中 → 正常认领 |
| 场景 engine_runtime_ids=[] + legacy | pick_engine 返回 None（照原逻辑） |
| 场景 engine_runtime_ids=[76] + legacy | pick_engine 返回 None（legacy 不走广播） |
| binding 表有记录 | binding 优先，不走场景 engine 限定 |
| 无场景信息 + plugin | pick_engine 返回 0，pool_engine_ids='' → 全量广播（向后兼容） |
| node enabled=false | 该 agent 不被分发（skill 查不到 → 跳过） |

### 6.4 VOD 场景检测测试

| 用例 | 验证点 |
|------|--------|
| VOD prompt 注入场景列表 | 包含 official + repo + user 场景 |
| VOD 推荐 custom scene | `set_issue_scene()` 成功持久化 |
| 后续 agent 读 custom scene | `resolve_skill_from_pipeline()` 从 DB 查到正确 skill |
| custom scene 被删除后 | `get_pipeline_config()` 回退到 default |

### 6.5 前端测试

| 用例 | 验证点 |
|------|--------|
| 选择官方场景 | agent 卡片显示正确 skill + engine，不可编辑 |
| 选择自定义场景 | 非 vod/issue agent 显示叉号，可切换灰色 |
| 灰色 agent | skill=none, engine=none, 不可操作 |
| skill 下拉（自定义场景） | 按 agent_type 过滤可选项，选中后保存到 node.skill |
| skill 下拉（官方场景） | 只读，不可改 |
| engine 下拉 | 显示可用引擎，多选，不选=广播 |
| 保存自定义场景 | 弹命名弹窗，输入后保存成功 |
| 场景切换 | agent 卡片状态立即更新 |
| Recent Activity | 不再显示 |

### 6.6 端到端测试

| 用例 | 验证点 |
|------|--------|
| 创建自定义场景（去掉 qa） → 新 Issue 触发 | VOD → Issue → Coding → SRE（跳过 QA） |
| 自定义场景绑定 engine [76] → 触发任务 | 任务 runtime_id=0 + pool_engine_ids='[76]'，只有 76 能 claim |
| 自定义场景绑定 engine [76,77] → 触发任务 | 任务 runtime_id=0 + pool_engine_ids='[76,77]'，76/77 竞争认领 |
| 自定义场景不绑 engine → 触发任务 | 任务 runtime_id=0 + pool_engine_ids=''，全量广播 |
| VOD 推荐自定义场景 → A2A 交办 | 后续 agent 使用自定义场景的 skill |
| 删除自定义场景 → 历史任务 | Task Center 显示 scene_id（兜底），新任务回退 default |

## 七、向后兼容性

| 场景 | 兼容方案 |
|------|---------|
| `configs/pipelines.json` 保留 | 作为官方场景种子文件，首次启动导入 DB，之后不再读取 |
| `devops_agent_runtime_binding` 表保留 | 优先级最高，场景 engine_runtime_ids 是补充 |
| `agent-skill-mapping.json` 保留 | legacy 模式 fallback 不受影响 |
| `issue_state.scene_id` 保留 | 存储场景 ID，指向 DB |
| `managed_repos.dev_scene` 保留 | 存储场景 ID，指向 DB |
| 旧 Worker | 不需要升级（场景信息在服务端解析，worker 只收 task） |

## 八、边界与风险

1. **场景 ID 冲突** — 用户自定义场景 ID 不能与官方场景 ID 重复（API 层校验）
2. **并发编辑** — 多用户同时编辑同一仓库级场景 → DB UNIQUE 约束 + 乐观锁（updated_at 比较）
3. **场景删除后正在执行的任务** — 任务已入队时场景被删 → 任务保持原配置执行完毕，新任务回退 default
4. **VOD 推荐不存在的场景** — `recommended_scene` 指向已删除场景 → `get_pipeline_config()` 回退 default
5. **Engine 下拉为空** — 仓库无可用引擎时 → engine 下拉显示"无可用引擎"，选择后 engine_runtime_ids=[] → 广播
6. **官方场景升级** — pipelines.json 更新后如何同步到 DB？meta `scenes_seeded` 只防首次导入。方案：版本号比对（pipelines.json 文件 hash → meta `official_scenes_hash`），hash 变化时重新导入官方场景（保留用户自定义场景）
