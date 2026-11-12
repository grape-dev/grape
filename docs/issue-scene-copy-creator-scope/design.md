# 设计：场景复制——创建者全量复制，非创建者只复制字段声明

- 替代单：#1529（场景复制支持一并复制高级选项与关联仓库，已于 2026-11-09 07:07 关闭，未实现）
- 前序实现：#402（复制场景按钮，纯前端拼装 `POST /scenes`，后端零改动）
- 目标分支：`weekly-mys`（场景中心 + dashboard 前端主干线）
- 基线：`origin/weekly-mys` @ `28f8948ff`；对照基线 `origin/weekly-10` @ `4ebe33b7b`
- 文档日期：2026-11-10

## 1. 需求原文

复制场景时：

- 复制者是该场景的**创建者**：所有设置全量复制（skill、agent 配置、自定义配置等）。
- 复制者**不是创建者**：自定义配置只复制**变量名**，不复制具体值；其余设置正常复制。

## 2. 现状核对（两分支实读）

### 2.1 「复制场景」当前只复制 7 个字段，且是纯前端行为

`src/api/static/js/pages/dashboard.js:1212` `_copySceneFromDetail()`：

- 从 `dashData._scenes` 取 `description / icon / nodes / pr_subscription_enabled / linked_repos`；
- 前端本地清洗 `nodes[].engine_runtime_ids`（只保留当前用户可选引擎），SRE 节点无引擎时置停用；
- 名称冲突本地追加序号；
- 第 1264 行 `API.post('/scenes?repo=…')` 提交，后端零改动。

### 2.2 `POST /scenes` 不接收 advanced_options，所以高级选项从未被复制

- `src/api/routes/scenes.py:1838` `CreateSceneRequest`：`scene_id/name/description/scope/nodes/icon/scene_type/pr_subscription_enabled/linked_repos` —— **没有 advanced_options**。
- `src/api/routes/scenes.py:1970` `create_scene()` → `src/api/pipeline_config.py:660` `create_custom_scene()` 只写 `devops_pipeline_scene` 的基础列，**不写 advanced_options 列**。
- 高级选项只能建后单独保存：`src/api/routes/scenes.py:2683` `save_advanced_options()`（PUT）。

**结论：本需求不是加一个 if，而是新增服务端复制能力。**

### 2.3 自定义配置的存法与可见性

- `devops_pipeline_scene.advanced_options`（TEXT JSON）：`{work_branch, custom_fields:[{name,value,is_secret,scope,preset}], linked_repos, preset_custom_fields}`。
- **值明文存库**（`scenes.py:2548` 注释「明文在库内，接口层不下发」）。
- `GET /scenes/{id}/advanced-options`（`scenes.py:2514`）对 `is_secret` 字段只回 `value:""` + `has_value`（#885 掩码）。
- → **前端手里没有 secret 明文，物理上无法复制**，必须由服务端完成。
- `reveal`（`scenes.py:2588`）只校验 `check_repo_access`，不校验创建者；本次**不改**（已拍板 D3）。

### 2.4 权限现实：`created_by` 目前只是展示字段

- `devops_pipeline_scene.created_by` 在创建时写入（`pipeline_config.py:709` `_try_set_created_by`），列表里用于展示 `@创建者`（`dashboard.js` `@${createdBy}`）。
- `update_scene` / `delete_scene` / `save_advanced_options` **均无创建者校验**——仓库内任何有权限的用户都能修改或删除他人的场景。
- 因此本需求只新增「复制时的取值规则」，**不改变**既有编辑/删除权限模型（明确不在本单范围）。

### 2.5 技能绑定的校验口径（两分支不同，必须按目标分支判定）

- `weekly-mys`：`scenes.py:2235` `_validate_scene_skill_scope(nodes, repo_key)`；#1758 起走单一判官 `src/api/scene_skill_scope.py`，**user scope 一律拒绝（含创建者本人）**，400 附带 `rebind_candidates/rebinds` 改绑建议。
- `weekly-10`：`_validate_scene_skill_scope(nodes, repo_key, username, user_id)`，`allow_own_user=True` 时**允许本人** user-scope 技能。
- 冲突判据取的是 **node["skill"] 名字**（`weekly-mys` `scene_skill_scope._node_skill_name`），因此「保留名字但不绑定」不能只把 `skill_id` 置 0，名字必须移走。
- `SceneNodeInput`（`scenes.py:1826`）是固定字段的 pydantic 模型，未声明的键在 `model_dump()` 落库时会丢失 → 留名必须新增声明字段。

### 2.6 引擎与关联仓库的既有约束

- `engine_runtime_ids` 是每人私有的 runtime id；`create_scene` 会调 `_validate_scene_engine_ids`，他人引擎必 400 → 服务端复制必须复刻前端的清洗逻辑。
- `linked_repos` 为 `[{repo_key, branch}]`，上限 5；跨仓引用可能指向复制者无权访问的仓库。

## 3. 已拍板口径

| 编号 | 决策 |
|---|---|
| D1 | 非创建者复制：**所有** `custom_fields` 只留 `name / is_secret / scope / preset`，`value=""`、`has_value=false`（非 secret 的明文值也不复制） |
| D2 | 技能若无法通过目标分支的 scope 校验：**保留技能名到 `skill_pending`、清空 `skill`/`skill_id`，复制不中断** |
| D3 | 本需求目的是**防误传**，不改变 `reveal` 端点（仍为仓库内可见 + 审计日志） |

## 4. 方案

### 4.1 新端点

```
POST /api/scenes/{scene_id}/copy?repo={owner}/{repo}
body: {"name": "<可选，副本名；缺省 <源名>-copy>"}
-> 200 {"scene_id","db_id","name","is_creator",
        "redacted_fields":[...],"cleared_skills":[{"node_id","skill"}],
        "dropped_engines":[...],"dropped_linked_repos":[...],
        "pr_subscription_disabled":bool}
```

服务端流程：

1. `_resolve_repo_key(repo)` → `_query_scene_by_id(scene_id, repo_key)`；不存在 → 404。
2. `is_creator = (user.username == existing.created_by) and created_by 非空`。
3. 组装 `nodes`（深拷贝）：
   - 引擎清洗：只留当前用户在该仓库可选的 runtime id；被剔除记 `dropped_engines`；
   - SRE 节点：`enabled` 且清洗后无引擎 → `enabled=false`（与前端既有规则一致）；
   - 技能：先按**目标分支的** `_validate_scene_skill_scope` 口径判定该节点是否通过（单节点判定，不抛异常）；不通过 → `skill_pending = 原 skill`、`skill = ""`、`skill_id = 0`，记 `cleared_skills`。
4. 组装 `advanced_options`：
   - `work_branch` 原样复制（非秘密）；
   - `custom_fields`：逐项复制 `name/is_secret/scope/preset`；`value` 在 `is_creator` 时原样、否则 `""`，并置 `has_value=false`；非创建者把被置空的 name 收集进 `redacted_fields`；
   - `preset_custom_fields`（声明）原样保留；
   - `linked_repos`：过滤当前用户无权访问的仓，记 `dropped_linked_repos`。
5. `pr_subscription_enabled`：原值；若为 true 但复制后的 QA 节点不满足「启用 + 有技能」→ 置 0，记 `pr_subscription_disabled=true`（否则 `create_scene` 会 400）。
6. 名称：缺省 `<源 display_name 或 name>-copy`，与同仓库现有场景名（name/display_name）冲突时追加 `-2/-3…`，长度截断 40；最终名回填响应。
7. 落库：`create_custom_scene(...)` + 写回 `advanced_options` + `_try_set_created_by(当前用户)`；`scene_id` 用新 uuid。

### 4.2 字段复制矩阵

| 字段 | 创建者 | 非创建者 |
|---|---|---|
| name | 用户输入 / 自动去重 | 同 |
| description / icon / scene_type | 全量 | 全量 |
| nodes[].id/tag/mode/next/handoff/skip/enabled | 全量 | 全量 |
| nodes[].skill / skill_id | 校验通过则保留；不通过则清空 + `skill_pending` | 同规则（实际多为他人 user-scope，必清） |
| nodes[].engine_runtime_ids | 过滤到当前用户可选；SRE 无引擎则停用 | 同 |
| advanced_options.work_branch | 全量 | 保留 |
| custom_fields[].name/is_secret/scope/preset | 全量 | 全量 |
| custom_fields[].value | 全量（secret 亦然，库内明文） | 一律 `""`、`has_value=false` |
| advanced_options.linked_repos | 全量 | 过滤无权仓库 |
| preset_custom_fields | 全量 | 全量 |
| pr_subscription_enabled | 原值（QA 不满足则降级为 0） | 同 |
| created_by | = 当前用户 | 同 |

### 4.3 模型与存储改动

- `SceneNodeInput` 新增 `skill_pending: str = ""`（1 行）；仅在复制产物与前端提示中使用。
- `CreateSceneRequest` 增加可选 `advanced_options: dict | None = None`，使创建与复制共用一条落库路径；缺省 `None` 时行为不变（向后兼容）。
- `create_custom_scene()` 增加 `advanced_options` 形参（约 8 行），写 `devops_pipeline_scene.advanced_options`。
- **无 DB 迁移**：`advanced_options` 列已存在（`db_setup.py` 的 `_ensure_column`）。

### 4.4 前端（dashboard.js，`_copySceneFromDetail`）

- 第 1264 行改为调用 `POST /scenes/{id}/copy`，请求体只带 name；
- 复制完成后按响应渲染明细：`已留空的配置项 N 个 / 已剔除引擎 M 个 / 需重选技能 K 处 / 已关闭 PR 订阅`；
- 场景详情节点渲染 `skill_pending` 提示「⚠ 需重选技能（原：xxx）」；用户选定新技能时清空该字段。

## 5. 验收标准

| # | 断言 | 验证方式 |
|---|---|---|
| A1 | 创建者复制：`custom_fields` 逐项 `name/is_secret/scope/value` 与源相等（含 secret 明文），`work_branch` 相等 | 单测 + DB 断言 |
| A2 | 非创建者复制：字段名/顺序/`is_secret`/`scope` 全等，`value` 全为 `""`、`has_value=false`；`work_branch` 相等 | 单测 |
| A3 | 源场景含无法通过的 user-scope 技能 → 复制 200 不中断；副本 `skill=""`、`skill_id=0`、`skill_pending=原名`，响应 `cleared_skills` 有明细 | 单测 |
| A4 | 源场景含他人私有引擎 → 副本不含该 id，响应 `dropped_engines` 有明细；SRE 节点降级停用 | 单测 |
| A5 | 源场景开启 PR 订阅但 QA 节点技能被清空 → 副本 `pr_subscription_enabled=0` 且响应标记，创建不 400 | 单测 |
| A6 | `linked_repos` 含无权仓库 → 副本中剔除且响应有明细；有权仓库原样保留 | 单测 |
| A7 | 名称缺省/冲突：`<源名>-copy`、冲突追加序号、≤40 字符 | 单测 |
| A8 | 回归：既有 `POST /scenes`（不带 advanced_options）行为不变；`PUT /scenes/{id}/advanced-options` 不变；market apply 不变 | 既有测试全绿 |
| A9 | `created_by` = 当前用户；老场景（created_by 为空）按「非创建者」处理（从严） | 单测 |

## 6. 风险与边界

- **不是安全边界**：仓库内成员仍可编辑/删除他人场景，也可用 `reveal`（需密码 + 审计日志）看到 secret。本需求只防「复制时把别人的密钥顺走」，文档与 UI 文案都要这样表述。
- **副本即归复制者**：非创建者复制得到的副本值全空，不会因「成为副本创建者」而绕过留空规则。
- **两分支技能口径不同**：实现必须调用目标分支既有的 scope 判定（不复制一套判据），否则 weekly-mys（#1758 严口径）与 weekly-10（允许本人 user-scope）会出现同一份代码不同结果。
- **`skill_pending` 透传风险**：前端保存节点时若重建节点对象会丢掉该字段，需在前端保存路径里显式保留。
- **跨分支合并**：本单在 weekly-mys 落地，场景中心的同一段代码在 weekly-10 也存在（`scenes.py` 相差约 240 行），合入 weekly-10 时须按函数名重新对齐，不能整段 cherry-pick。

## 7. 明确不做（不要顺手改）

- 不改 `update_scene` / `delete_scene` / `save_advanced_options` 的权限模型（是否加创建者校验另开单）。
- 不改 `reveal` 端点与审计行为。
- 不改 market scene 的 `apply` / `experience` 路径（那条只合并字段声明、从不带值）。
- 不改前端老复制路径对「无 advanced_options 场景」的既有行为（老场景 `advanced_options='{}'` → 副本工作分支回退 `main`、无自定义项）。
- 不新增 DB 列/迁移。