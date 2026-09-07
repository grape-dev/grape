# 场景展示名修正：default 场景应展示 display_name 而非 name

> 背景：Issue #39 方案 B 落地后，场景默认配置 `configs/default-scene.json` 的 `default_scene_market_name='default'`
> 对应场景市场「通用开发」。但项目场景展示用的是 `name`（标识符，如 `default`）而非 `display_name`（展示名，如「通用开发」），
> 需修正为展示 `display_name`。

## 一、问题

### 1.1 现象

新托管仓库自动应用「通用开发」场景后，项目场景列表 / VOD prompt / dashboard 显示的却是 **`default`**（内部标识符），
而非「通用开发」（展示名）。

### 1.2 数据对照（已确认）

`scene_market` 表中（source='official'）：

| name | display_name |
|------|--------------|
| `default` | 通用开发 |

期望展示「通用开发」，实际展示 `default`。

### 1.3 根因（代码流转追踪）

| 环节 | 代码位置 | 取用字段 | 结果 |
|------|----------|----------|------|
| 复刻到仓库 | `pipeline_config.py` `apply_default_scene_to_repo` L343 | `official["name"]` | 仓库场景存 `name='default'` |
| 读 market | `pipeline_config.py` `_load_official_scene_from_market` L79 | `SELECT name, ...`（**未查 display_name**） | 返回 `name='default'` |
| 建场景 | `pipeline_config.py` `create_custom_scene` | `name=` 参数 | 写入 `devops_pipeline_scene.name='default'` |
| 场景列表 | `pipeline_config.py` `get_scenes` L156 | `s["name"]` | 展示 `default` |
| VOD prompt | `prompt_builder.py` `_inject_vod_scenes` | `s['name']` | prompt 内 `default` |
| dashboard | `routes/dashboard.py` L982+ | `get_scenes` / `dev_scene` | 展示 `default` |

**核心根因**：
1. `_load_official_scene_from_market` 的 SQL **未查 `display_name`**（复刻即丢失展示名）
2. `devops_pipeline_scene` 表**无 `display_name` 列**（新表仅 `name`），即便 market 有也无处存储
3. `get_scenes` 返回 `s["name"]`，前端/dashboard/prompt 按此渲染

## 二、修复方案

### 改动 1：`devops_pipeline_scene` 表新增 `display_name` 列

```sql
ALTER TABLE devops_pipeline_scene ADD COLUMN display_name TEXT DEFAULT '';
```

（`db_setup.py` 加迁移；SQLite `ALTER TABLE ADD COLUMN` 支持，幂等 try/except 沿用现有模式）

### 改动 2：`_load_official_scene_from_market` 读取 display_name

```python
c.execute(
    "SELECT name, display_name, description, nodes_json, onboarding FROM scene_market "
    "WHERE name=? AND source='official' AND is_active=1 LIMIT 1", (scene_id,))
# 返回 dict 增加 "display_name": row[1]
```

### 改动 3：`create_custom_scene` 写入 display_name

- 签名增加 `display_name: str = ""` 参数
- INSERT/UPDATE 同时写入 `display_name` 列

### 改动 4：`apply_default_scene_to_repo` 传入 display_name

```python
create_custom_scene(
    scene_id=default_name,
    name=official["name"] or default_name,        # name 保持标识符（default）
    display_name=official["display_name"] or official["name"],  # 展示名（通用开发）
    ...)
```

### 改动 5：展示层返回 display_name

| 环节 | 改动 |
|------|------|
| `get_scenes` | 返回 `{"id", "name", "display_name"}` |
| `routes/scenes.py` list/with-pipeline | 展示取 `display_name or name` |
| `prompt_builder.py` `_inject_vod_scenes` | 场景表格「名称」列显示 `display_name or name` |
| `routes/dashboard.py` | 场景节点名称显示 `display_name` |

> 兼容：`display_name` 为空时回落 `name`，不破坏存量（老数据只有 name）。

## 三、改动与测试清单

| # | 项 | 说明 | 优先级 |
|---|----|------|--------|
| 1 | `db_setup.py` | 场景表加 `display_name` 列迁移 | 必须 |
| 2 | `pipeline_config._load_official_scene_from_market` | SQL 查 display_name + 返回 | 必须 |
| 3 | `pipeline_config.create_custom_scene` | 支持写入 display_name | 必须 |
| 4 | `pipeline_config.apply_default_scene_to_repo` | 复刻时传 display_name | 必须 |
| 5 | `get_scenes` + `scenes.py` | 返回/展示 display_name | 必须 |
| 6 | `prompt_builder._inject_vod_scenes` | VOD prompt 显示 display_name | 必须 |
| 7 | `routes/dashboard.py` | dashboard 场景展示 display_name | 必须 |
| 8 | 迁移回填 | 存量 repo 场景按 market 回填 display_name | 推荐 |
| 9 | 单测 | 复刻后 display_name='通用开发'；展示层脚本断言 | 必须 |

## 四、验收标准

- [ ] 新托管仓库应用默认「通用开发」后，项目场景显示「通用开发」而非 `default`
- [ ] VOD prompt 场景列表「名称」列显示展示名（如「通用开发」）
- [ ] dashboard 场景名显示展示名
- [ ] `display_name` 为空的存量场景仍然兼容（回落 name）
- [ ] 显式场景（非默认）不受影响（其 display_name 正确或回落 name）
- [ ] 现有场景测试回归通过

## 五、风险

- 低风险：纯展示字段新增 + 展示层切换，不动 `scene_id`/`repo_key`/`nodes_json` 等核心逻辑。
- `display_name` 为空回落 `name`，保证存量兼容。
- 迁移需在测试环境验证（`grape-dev-db`）后再上生产；生产 `data/sessions.db` 先备份。