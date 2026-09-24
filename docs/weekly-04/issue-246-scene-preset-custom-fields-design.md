# Issue #246 技术方案：场景增加预置自定义参数能力

> 状态：技术方案（基于 Issue #246 + Spec ready）
> 分支：weekly-04-lyz
> 关联：#236（custom_fields 直接注入 + scope + is_secret 基础）、#246（预置参数声明）→ 应用带出参数名，用户只填值

---

## 1. 背景与目标

### 1.1 需求

场景（`scenes/*/scene.json`）**声明预置自定义参数**（custom_fields），应用场景时前端自动带出参数名（只读 label + 值输入框），用户只填值；Agent 运行时通过 `os.environ.get` 直接读取用户填写的值（复用 #236 注入链路）。

核心价值：避免用户应用场景时**误填多/少参数名**——参数名由场景预置，用户专注填值。

### 1.2 现状基线（#236 已实现）

- `src/scene_custom_env.py`：name 大写/保留冲突校验、is_secret 三态、scope 数组归一化
- `src/api/routes/scenes.py`：
  - `get_advanced_options` / `save_advanced_options`：`devops_pipeline_scene.advanced_options.custom_fields` 读写 `{name, value, is_secret, scope}`
  - `apply_market_scene`（L373，市场场景应用）：创建 `devops_pipeline_scene` 行
  - `apply_scene`（L1601，工作流场景应用）：只配 agent 节点，**不碰 advanced_options**
- `src/api/event_pipeline.py` `_inject_scene_env`（L2059）：按 scope/agent_type 注入 custom_fields
- 前端 `dashboard.js` `_addCustomFieldInline` + `scene-market.js` `_applyAddCustomField`：录入/回填 custom_fields（含 scope 多选）

---

## 2. 与 Spec 的差异核对（重要修正）

Spec 文档基于 issue-agent 分析，**两处与实际代码不符**，本方案按其修正：

| Spec 表述 | 实际 | 修正 |
|----------|------|------|
| 预置参数同步到 `scene_market.advanced_options.preset_custom_fields` | `scene_market` 表**无 `advanced_options` 列**（`db_setup.py:631` DDL、`get_scene_market_by_id` `user/db.py:2394` 均无该字段） | 预置声明不落 `scene_market`，改为**应用场景时动态从 scene.json 来源解析**（见 3.3） |
| 注入层由 `_inject_custom_fields_env` 复用 | 实际函数名 `_inject_scene_env`（event_pipeline.py:2059） | 命名统一为 `_inject_scene_env`，**无改动** |
| `apply_scene` 合并预置 | 该函数（L1601）不管理 advanced_options；真正应用是 `apply_market_scene`（L373） | 合并落点在 `apply_market_scene`（+ `_doApplySceneWithOptions` 前端协调） |

> 额外风险：前端 `_doApplySceneWithOptions`（scene-market.js:846）apply 后**整体 PUT advanced-options**，会覆盖后端合并的 preset——需协调（见 3.4）。

---

## 3. 方案概述

### 3.1 数据流总览

```
scene.json 声明 custom_fields（grape-skills 仓库）
   │ scene_sync.py 定时同步（content_hash 自动纳入 scene.json）
   ▼
scene_market 表（nodes_json 含声明；preset 以 source 场景节点解析）
   │ 用户应用场景（apply_market_scene）
   ▼
devops_pipeline_scene.advanced_options.custom_fields
   └─ preset:true 项（预置参数名，只读）  + 用户自定义项（preset:false）
   │ 前端高级选项页：preset 只读 label + 值输入框
   ▼
保存（save_advanced_options）→ _inject_scene_env 按 scope 注入 agent env
```

### 3.2 声明层：scene.json 新增 `custom_fields`

grape-skills 仓库 `scenes/*/scene.json` 顶层新增：

```json
{
  "name": "agent-dev",
  "nodes": [ ... ],
  "custom_fields": [
    {"name": "AI_MODEL",     "is_secret": false, "scope": ["all"],  "default_value": ""},
    {"name": "AI_BASE_URL",  "is_secret": false, "scope": ["all"],  "default_value": ""},
    {"name": "AI_API_KEY",   "is_secret": true,  "scope": ["coding"], "default_value": ""}
  ]
}
```

字段与原 custom_fields 结构一致（name/is_secret/scope），新增 `default_value`。`name` 复用 #236 大写/保留校验规则。

### 3.3 同步与预置来源

- `scene_sync.py`：
  - `_compute_scene_hash`（scene_sync.py:71）遍历场景目录全部文件（含 scene.json），新增 custom_fields **自动纳入 hash**；
  - UPDATE/INSERT `scene_market` 时新增字段 `preset_custom_fields`：`json.dumps(scene_data.get("custom_fields", []))`；
  - `nodes_json` 保持节点数组**不变**（多处消费方按节点数组解析：`db_setup.py:1421`、`event_pipeline.py:47`、`scenes.py:148/246/401/676`），避免塞入声明破坏消费方。
- **scene_market 加列**：`preset_custom_fields TEXT DEFAULT '[]'`（`db_setup.py` DDL + `_ensure_column` 幂等迁移，兼容既有库），存 scene.json 的 custom_fields 原始声明数组。
- 数据流：scene.json → scene_sync → `scene_market.preset_custom_fields` → apply_market_scene 合并 → `devops_pipeline_scene.advanced_options.custom_fields`（preset:true 项）。

### 3.4 应用合并层：apply_market_scene（scenes.py L373）

应用场景时（新建/更新 `devops_pipeline_scene` 行后）：

```
preset 声明（scene_market.preset_custom_fields）
   → 合并到 devops_pipeline_scene.advanced_options
```

- 合并策略（**仅追加不覆盖**，验收要求）：对每个 preset `{name, is_secret, scope, default_value}`，若 `custom_fields` 中不存在同名 name，则追加 `{name, value: default_value, is_secret, scope, preset: true}`；已存在同名项保持用户值不动。
- 合并时机：`apply_market_scene` 创建/更新场景后立即执行；同时写回 `devops_pipeline_scene.advanced_options.preset_custom_fields`（存原始声明，供 preset 变更或重新应用时补合并）。

### 3.5 前端覆盖冲突协调（关键修复点）

现状 `_doApplySceneWithOptions`（scene-market.js:846）：
```
POST /scenes/market/{id}/apply  →  返回 scene_id
PUT  /scenes/{scene_id}/advanced-options  {work_branch, custom_fields: 前端收集}
```
前端收集的是**仅用户输入的字段**，PUT 整体覆盖会清掉后端刚合并的 preset 项。

**方案**（后端保底 + 前端透传）：
- `PUT advanced-options` 改为**合并语义**：read 现有 `advanced_options.custom_fields`，`preset:true` 项仅更新 value（name 不可改、不可删，preset 标记保留），用户自定义项按现有逻辑增删。即后端 `save_advanced_options` 对 preset 项做 upsert 保护。
- 前端：`scene-market.js` 应用弹窗**预填 preset 只读行**（apply 前从市场详情/advanced-options 拉 preset 声明渲染为只读 label + 值输入框），PUT 时带 preset 行；`dashboard.js` 高级选项页渲染 preset 只读行。

### 3.6 API 返回 preset 标记

`get_advanced_options`（scenes.py L1698）返回 custom_fields 项增加 `preset: true/false`：
- preset 项：`{name, value(明文/掩码), is_secret, scope, preset: true}`，前端渲染只读 name label + 值输入框（不可删、不可改名、scope 不可改）
- 用户自定义项：`preset: false`，保持现有可编辑行

### 3.7 保存校验（防绕过）

`save_advanced_options`（L1741）对 preset 项强化：
- name 不可改：preset 项忽略前端提交的 name 变化（以库内 preset 声明为准）
- 不可删：preset 项必须保留（前端未提交某个 preset 行 → 后端仍以旧值保留）
- is_secret/scope 不可改：以 preset 声明为准
- 值可为空（用户不填则 default_value / 留待 agent 侧默认）

### 3.8 注入层

`_inject_scene_env`（event_pipeline.py:2059）**零改动**——preset 项与用户自定义项同存于 `advanced_options.custom_fields`，按现有 scope/agent_type 过滤 + is_secret 明文注入逻辑执行。

---

## 4. 各模块改动清单

| 模块 | 改动类型 | 说明 |
|------|---------|------|
| `scenes/*/scene.json`（grape-skills） | 新增 | 顶层 `custom_fields` 声明数组 |
| `src/api/db_setup.py` | 修改 | `scene_market` 加 `preset_custom_fields TEXT DEFAULT '[]'`（DDL + `_ensure_column` 迁移） |
| `src/storage/scene_sync.py` | 修改 | 同步时写 `scene_market.preset_custom_fields`（nodes_json 不变） |
| `src/api/routes/scenes.py` | 修改 | `apply_market_scene` 预置合并；`get_advanced_options` 返回 preset 标记；`save_advanced_options` preset 保护（upsert value） |
| `src/api/static/js/pages/scene-market.js` | 修改 | 应用弹窗预填 preset 只读行；PUT 透传 preset 行 |
| `src/api/static/js/pages/dashboard.js` | 修改 | 高级选项页渲染 preset 只读行（只读 label + 值输入框，scope/is_secret 锁定） |
| `src/api/event_pipeline.py` | 无改动 | `_inject_scene_env` 复用 |
| `src/scene_custom_env.py` | 微调(可选) | 暴露 preset 字段透传工具 |

---

## 5. 兼容与安全

- **存量数据**：已应用的场景 advanced_options 无 preset 标记 → 视为全部用户自定义（preset:false），行为不变；再次 apply 时按 3.4 合并补 preset。
- **content_hash**：`_compute_scene_hash` 遍历场景目录，scene.json 改 custom_fields → hash 变 → 会触发 UPDATE（version 检查已有 WARNING 兜底）。
- **is_secret**：preset 的 secret 项 value 走 #236 既有掩码（get 返回明文 but 前端 password 输入框），不落 prompt。
- **preyset 防覆盖**：后端 save 的 preset upsert 保护是安全底线，前端只读渲染是体验层。

---

## 6. 测试计划

新增测试（`tests/test_scene_env.py` 或新建 `tests/test_scene_preset.py`）：
- scene_sync：scene.json 含 custom_fields → nodes_json 保留声明（content_hash 变化触发更新）
- apply_market_scene：应用后 devops_pipeline_scene.advanced_options.custom_fields 合并 preset（仅追加缺失项；已有同名项不被覆盖）
- get_advanced_options：preset 项返回 preset:true + 掩码
- save_advanced_options：preset 项名/scope/is_secret 不可改、值可更新、不可删
- 注入：preset 项按 scope 注入（复用 #236 用例扩展）

回归：`tests/test_scene_env.py`、`tests/test_runtime.py`

---

## 7. 验收对照

| 验收项（Spec） | 方案覆盖 |
|--------|---------|
| 应用 `agent-dev` 场景 → 高级选项页带出 `AI_MODEL`/`AI_BASE_URL`/`AI_API_KEY` | ✅ 3.4 合并 + 3.6 API preset 标记 + 前端只读渲染 |
| 用户只填值不改名 → 保存后 env 出现该值（按 scope 注入） | ✅ 3.7 保存 + 3.8 注入复用 |
| 用户无法增/删预置参数名（可增自定义项） | ✅ 3.6/3.7 preset 保护 |
| 已有项目用户配置不被覆盖 | ✅ 3.4 仅追加缺失项 |
| secret 在页面掩码、日志不出现 | ✅ 5 is_secret 既有机制 |
| 现有测试通过 + 新增测试 | ✅ 6 |