# Issue #39 修复方案：仓库自定义开发场景被 VOD 识别为通用开发场景

> Issue：https://gitcode.com/grape-dev/grape/issues/39（bug / spec-ready / triaged，P1）
> 现状：open，assignee=grape_bot，A2A 交办 coding 被 `skipped:not_in_scene`（自定义场景 `custom-e7ffba11` 未注册 coding agent）
> 作者：VOD/Issue Agent 根因分析 + 本地代码复核
> 日期：2026-09-05

## 一、问题概述

仓库自定义了开发场景（如 grape-dev/grape 的唯一自定义场景「grape需求 场景」），
但 VOD 智能识别时**看不到该仓库级自定义场景**，仍输出 `recommended_scene="default"`（通用开发场景），
导致后续所有 agent（issue/coding/qa/sre）走通用技能链而非仓库配置的专用技能链。

## 二、核心约束（需求方明确）

**场景只与仓库（`repo_key`）关联，没有"官方/用户/仓库"来源之分，也没有 `default` 场景。**

1. **场景形态只有一种：仓库场景**。
   - 官方场景不独立存在——它是**模板/种子**，所有官方场景（含 default）"应用到仓库"后即成为该仓库的场景（复刻一份 `repo_key` 关联的记录）。
   - **用户级场景（scope='user'）废弃**：不再有"某个用户私人场景"，场景一律挂在仓库上。
2. **场景关联键 = `repo_key`**，不需要 `scope`、`user_id` 字段区分来源。
   - 同一 `scene_id` 可同时存在于多个仓库（各仓库一份副本），由 `repo_key` 唯一归属。
3. **VOD 的 `recommended_scene` 只能从该仓库所配置的场景中选取**：
   - **仓库有配置场景** → VOD 只能在这些场景中选择，不得输出 `default`；
   - **仓库没有配置任何场景** → **VOD 直接终止**，输出「未配置场景」，无法继续处理。
4. **没有 `default` 场景**：任何情形 `recommended_scene` 不允许为 `default`。
5. **`scene_market`（场景技能市场）保持现状**：它是展示层，`source` 字段（official/user/repo）仅用于市场浏览，不属于仓库场景归属。

## 三、现状数据模型（需要演进）

### 3.1 现有表结构：`devops_pipeline_scene`

```sql
CREATE TABLE IF NOT EXISTS devops_pipeline_scene (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scene_id    TEXT NOT NULL,
    name        TEXT NOT NULL,
    description TEXT DEFAULT '',
    scope       TEXT DEFAULT 'official',   -- official(模板) / repo(仓库级) / user(用户级) ← 需移除
    repo_key    TEXT DEFAULT '',           -- scope='repo' 时归属仓库
    user_id     INTEGER DEFAULT 0,         -- scope='user' 时归属用户 ← 需移除
    nodes_json  TEXT DEFAULT '[]',
    onboarding  TEXT DEFAULT 'repo-onboarding',
    is_active   INTEGER DEFAULT 1,
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now')),
    UNIQUE (scene_id, scope, repo_key, user_id)
)
```
（`market_scene_id` 为 Issue-11 迁移补充列，保留）

### 3.2 现状查询逻辑（多 scope 分支）

- `_query_scenes_from_db(repo_key, user_id)`：按 `scope='user' AND user_id=?` + `scope='repo' AND repo_key=?` 分别查询，official 不展示。
- `_query_scene_by_id(scene_id, repo_key, user_id)`：按 user→repo→official 三级优先级匹配。
- `create/update_custom_scene(...)`：`scope=='repo'` 用 repo_key、`scope=='user'` 用 user_id 判断冲突与归属。
- `fork_official_scene(...)`：官方模板 fork 为 user 或 repo scope 副本。

> **代码已在演进路上**：`db_setup.py`（~L2205）已有迁移逻辑
> `UPDATE devops_pipeline_scene SET scope='repo', user_id=0 WHERE scope='user' AND is_active=1`，
> 即已把存量 user 级场景降级为 repo 级——正是本方案要收敛的方向。

### 3.3 相关其他表（展示层，保持不变）

| 表 | 说明 |
|----|------|
| `scene_market` | 场景技能市场（Issue-11）：`source` 标记 official/user/repo 仅用于市场展示 |
| `scene_category` | 场景分类（id/name/slug/icon/order_num） |
| `scene_skill_ref` | 场景→技能引用（scene_id/skill_id/skill_name） |
| `issue_state.scene_id` + `scene_user_id` | Issue 级场景判定落库列（VOD 写入处） |

## 四、根因

### 4.1 VOD prompt 未注入仓库场景（实际返回空）

`src/prompt_builder.py:108` `_inject_vod_scenes()` 调用 `get_scenes()` 与 `get_pipeline_config()` 时**未传入 `repo_key`**：

```python
scenes = get_scenes()                      # → repo_key=""，返回 []
cfg = get_pipeline_config(s["id"])         # → 同样无上下文
```

由于 `_query_scenes_from_db` 中 `if repo_key:` 守卫不满足，**仓库场景一条都不会被查出**，
`get_scenes()` 返回空 → VOD prompt 无任何场景 → 只能按旧兜底输出 `default`。

### 4.2 VOD done handler 用 default 覆写仓库默认场景

`src/api/event_pipeline.py:1879-1895` VOD done handler 在 `allow_scene_override` 开启时，
将 `"default"` 写入 `issue_state.scene_id`，覆盖 `set_issue_stage()` 从 `managed_repos.dev_scene` 带入的正确场景。

### 4.3 影响链

```
VOD prompt 未注入任何场景（get_scenes() 空）
  → VOD 看不到仓库自定义场景 → 旧逻辑输出 recommended_scene="default"
  → VOD done handler 覆写 issue_state.scene_id = "default"
  → 后续 agent 读 get_issue_scene() → "default" → 使用通用开发技能链
```

## 五、修复方案（方案 B：场景数据模型演进 + VOD 行为修正，一次落地）

> 采用**方案 B 直接实施**：表结构移除 `scope`/`user_id`、场景仅 `repo_key` 关联，
> 同时修正 VOD 可见性与"无场景即终止"行为。不再分阶段。

### B1. 目标表结构

```sql
CREATE TABLE IF NOT EXISTS devops_pipeline_scene (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scene_id    TEXT NOT NULL,
    name        TEXT NOT NULL,
    description TEXT DEFAULT '',
    repo_key    TEXT NOT NULL DEFAULT '',   -- 唯一归属仓库
    nodes_json  TEXT DEFAULT '[]',
    onboarding  TEXT DEFAULT 'repo-onboarding',
    is_active   INTEGER DEFAULT 1,
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now')),
    UNIQUE (scene_id, repo_key)
)
```
- 移除 `scope`、`user_id` 列；`repo_key` 成为唯一归属，`UNIQUE(scene_id, repo_key)`。
- **官方场景载体直接删除**：`devops_pipeline_scene` 不再保留 `scope='official'` 的官方模板记录。
  官方场景已有专门场景库承载：`src/storage/scene_sync.py` `sync_official_scenes()` 定时从独立官方场景库
  （`grape-skills` 仓库 `scenes/` 目录）同步到 `scene_market(source='official')`。
  应用官方场景到仓库时，从 `scene_market` 复刻为 `repo_key` 关联记录即可。
- **`scene_id` 命名**：**直接去掉 `repo:` 前缀**。演进后 `devops_pipeline_scene` 只有仓库场景（无跨 scope 冲突），
  `scene_id` 直接用官方场景原名（如 `default`、`cloud-native`），既简化又兼容场景市场引用。

### B2. 迁移脚本（db_setup / backfill）

1. **存量 `scope='repo'`**：去掉 `user_id` 置 0、`scope` 列；`scene_id` 去掉 `repo:` 前缀；重组 `UNIQUE(scene_id, repo_key)`。
2. **存量 `scope='user'`**：按现有降级逻辑并入所属仓库（经 `user_repos` 反查 repo_key）；无法反查到仓库的孤儿 user 场景删除（用户级场景已废弃）。
3. **存量 `scope='official'`**：**直接清理**该场景表内的官方记录——官方场景以 `scene_market(source='official')` 为准（由 `scene_sync` 从官方场景库同步，属另一独立同步链路，不写入 `devops_pipeline_scene`）。

### B2.1 新托管仓库默认场景（决策：从 scene_market 取「通用开发」，configs 可配）

**行为**：新托管仓库时，自动从 `scene_market(source='official')` 复刻默认场景到 `devops_pipeline_scene(repo_key=...)`，
作为该仓库的默认开发场景（默认取 `name='default'`/`display_name='通用开发'`）。

**新增配置文件**：`configs/default-scene.json`（example 模板：`configs/default-scene.example.json`，gitignore 规则同其他 configs）：

```json
{
  "default_scene_market_name": "default",
  "description": "新托管仓库自动应用的默认开发场景。default_scene_market_name：scene_market 中 source='official' 的场景 name（默认 'default'=通用开发）；配置为 '' 表示新托管仓库不自动配置场景（VOD 对无场景仓库输出「未配置场景」终止）。环境变量 DEFAULT_SCENE_MARKET_NAME 优先覆盖此文件。"
}
```

**实现位置**：
- 读取：新增 `src/api/pipeline_config.py`（或 config 模块）读取 `configs/default-scene.json` + 环境变量覆盖。
- 应用：`repos.py` host_repo 两处，原 `_auto_fork_official_scenes_to_repo(repo_key, user_id)` 改为
  `apply_default_scene_to_repo(repo_key)`——从 `scene_market(source='official', name=配置值)` 查找默认场景，
  复刻为 `devops_pipeline_scene(repo_key=repo_key, scene_id=场景 name,sans`repo:`前缀`)、并写 `managed_repos.dev_scene`（见 B2.2）。
- `managed_repos.dev_scene` 默认值置空（见 B2.2）；应用默认场景时写入实际 scene_id。
- 若配置为 `''`：新托管仓库不自动配置场景（对应"无场景 → VOD 终止"路径）。

**注意**：默认场景也走"从 scene_market 复刻"，不是写死场景表——与官方场景载体决策一致。

### B2.2 `dev_scene` 默认值置空 + 读取兜底调整

- `user/db.py`（~L367）：`dev_scene TEXT DEFAULT 'default'` → `DEFAULT ''`（置空默认）。
- `set_repo_scene(...)`（~L1034）：去掉 `scene_id or "default"` 兜底 → `scene_id or ""`。
- 迁移：已存在的 `managed_repos.dev_scene='default'` 存量行保留（兼容），仅新建行默认置空。
- 依赖方校验：`dashboard.py`（~L968 `dev_scene = "default"` 兜底）、`event_pipeline.py`（~L594 `or "default"`）、
  `onboarding.py`、`tasks.py`、`session_store.py` 全部改为：`dev_scene` 为空时不再回退 default，
  按"仓库无场景/未配置"处理（对应 VOD 终止或场景缺省逻辑）。

### B3. VOD 行为修正（随表结构一起落地）

**改动 B3-1**：`src/prompt_builder.py` `_inject_vod_scenes()` 传入 `repo_key`：

```python
scenes = get_scenes(repo_key=repo_key)
...
cfg = get_pipeline_config(s["id"], repo_key=repo_key)
```

**改动 B3-2**：prompt 注入场景选择硬约束 + 无场景即终止语义：

```python
lines.append("> 约束：`recommended_scene` 只能从上方候选场景中选择（均为该仓库的场景）。")
lines.append("> **没有 `default` 场景**：禁止输出 `default`。")
lines.append("> 若仓库没有任何已配置场景：**立即终止，输出 `{\"terminate\": true, \"reason\": \"未配置场景\"}`**，不继续处理。")
```

**改动 B3-3**：`src/api/event_pipeline.py` VOD done handler：
- 识别 `terminate`/「未配置场景」信号 → 终止处理、不落场景、不触发后续 agent；
- 任何情况 `recommended_scene in ("", "default")` 都**不写入** `issue_state.scene_id`。

### B4. 全链路简化（pipeline_config.py 及调用方）

| 函数 | 现状 | 演进后 |
|------|------|--------|
| `get_scenes(repo_key, user_id)` | 双 scope + user 查询 | `get_scenes(repo_key)` 单查询 |
| `_query_scenes_from_db(repo_key, user_id)` | user + repo | 仅 `WHERE repo_key=?` |
| `_query_scene_by_id(scene_id, repo_key, user_id)` | user→repo→official 三级 | 仅 `WHERE scene_id=? AND repo_key=?`；官方场景由 `scene_market(source='official')` 提供（不存在于场景表） |
| `get_pipeline_config/get_node_by_tag` | 透传 user_id | 仅 `repo_key` |
| `create/update_custom_scene` | scope 分支 | 仅 repo_key |
| `fork_official_scene` | fork 官方模板到 user/repo scope | 从 `scene_market(source='official')` 复刻到仓库（`scene_id` 无前缀） |
| 调用方（scenes/dashboard/pipeline/tasks/webhook/event_pipeline/prompt_builder） | 传 `user.get("id",0)` | 仅传 `repo_key` |

> `_repo_user_id`、`resolve_skill_from_pipeline` 的 `user_id` 参数在演进后不再需要（仓库归属场景不受 user 影响）。
> 官方场景由 `src/storage/scene_sync.py` `sync_official_scenes()` 独立同步到 `scene_market`，不进入 `devops_pipeline_scene`。

## 六、改动与测试清单

| # | 项 | 说明 | 优先级 |
|---|----|------|--------|
| B1 | `db_setup.py` | 表结构移除 scope/user_id，`UNIQUE(scene_id, repo_key)` | 必须 |
| B2 | 迁移脚本 | 存量 repo 场景去前缀 + user 场景并仓 + official 记录清理 | 必须 |
| B2.1 | 默认场景配置 | 新增 `configs/default-scene.json`(+example)；host_repo 从 scene_market 复刻默认「通用开发」 | 必须 |
| B2.2 | `dev_scene` 置空 | 建表默认 `''`；`set_repo_scene` 去 `or "default"`；依赖方去 default 回退 | 必须 |
| B3-1 | `prompt_builder.py` `_inject_vod_scenes()` | `get_scenes`/`get_pipeline_config` 传 `repo_key` | 必须 |
| B3-2 | `prompt_builder.py` | 注入"仅仓库场景 + 无 default + 无场景终止"约束 | 必须 |
| B3-3 | `event_pipeline.py` VOD done | 终止信号处理 + default 永不写入 scene_id | 必须 |
| B4 | `pipeline_config.py` 全链路 | 查询/CRUD/fork 仅 repo_key，官方场景走 scene_market | 必须 |
| B4 | 调用方（scenes/dashboard/pipeline/tasks/webhook/event_pipeline） | 去掉 user_id 传参 | 必须 |
| B5 | 单测 | 默认场景复刻；VOD 场景可见；无场景终止；default 不写入；迁移后查询一致 | 必须 |

## 七、验收标准

- [ ] 仓库配置了场景时，VOD 能看到全部仓库场景（仅 `repo_key` 关联）
- [ ] 仓库配置了场景时，VOD `recommended_scene` 只从仓库场景中选，**任何情况不输出 `default`**
- [ ] 仓库**没有任何配置场景**时，VOD **直接终止**，输出「未配置场景」，不触发后续 agent
- [ ] 任何情况下 `recommended_scene="default"` 都不写入 `issue_state.scene_id`
- [ ] `devops_pipeline_scene` 无 `scope`/`user_id` 列，`UNIQUE(scene_id, repo_key)` 生效
- [ ] 存量 user 级场景并入所属仓库；`scene_id` 去掉 `repo:` 前缀；官方记录已从场景表清理（官方场景在 `scene_market(source='official')` 且由 `scene_sync` 正常同步）
- [ ] **新托管仓库自动从 scene_market 复刻「通用开发」为默认场景**（configs/default-scene.json 可配置；配置 `''` 时不自动配场景）
- [ ] `managed_repos.dev_scene` 新建行默认值为空；无场景仓库不再回退 `default`
- [ ] 场景中心/场景市场/技能链解析全部只依赖 `repo_key` 正常工作
- [ ] 现有测试回归通过

## 八、测试用例设计

> 基础设施：沿用现有 `tests/test_scene_skill_market.py` 模式——`TestClient(app)` + 测试 DB
> (`DEVOPS_DB_PATH=data/test_sessions.db`) + `_jwt_access_token(user_id, username)`。
> 约定：用 `_mk_repo(repo_key)` 往 `managed_repos` + `user_repos` 插一行；用 `_add_repo_scene(repo_key, scene_id)` 直接插 `devops_pipeline_scene(repo_key=...)`。

### 8.1 数据模型（B1/B2）测试

| # | 用例 | 验证点 | 数据准备 |
|---|------|--------|----------|
| T1 | `test_scene_table_no_scope_userid` | 表结构：`PRAGMA table_info(devops_pipeline_scene)` 无 `scope`、`user_id` 列；唯一索引含 `(scene_id, repo_key)` | 空 |
| T2 | `test_scene_table_unique_repo_key` | 同 `(scene_id, repo_key)` 允许跨仓库并存、同仓库重复插入被唯一约束拦截（或 upsert 幂等） | 插 2 仓库同名场景 |
| T3 | `test_migrate_user_scene_to_repo` | 存量 `scope='user', user_id=?` 场景执行迁移后并入所属仓库（经 user_repos 反查 repo_key），`devops_pipeline_scene` 无 user 行 | user 场景 + user_repos |
| T4 | `test_migrate_repo_scene_drop_prefix` | 存量 `scene_id='repo:default'` 迁移后 `scene_id='default'`、REPO_KEY 保留 | repo 前缀场景 |
| T5 | `test_migrate_official_scene_removed` | 存量 `scope='official'` 记录被清理；`scene_market(source='official')` 数据不受影响 | official 行 + market 行 |

### 8.2 默认场景（B2.1/B2.2）测试

| # | 用例 | 验证点 | 数据准备 |
|---|------|--------|----------|
| T6 | `test_host_repo_apply_default_scene` | 新托管仓库后 `devops_pipeline_scene` 存在该 repo 的 `scene_id='default'` 记录，且 `managed_repos.dev_scene='default'` | config 默认 |
| T7 | `test_host_repo_default_scene_disabled` | `default-scene.json` 配置 `''` 或环境变量 `DEFAULT_SCENE_MARKET_NAME=''` 时，托管后**不生成**任何场景 | config 空 |
| T8 | `test_host_repo_custom_default_scene` | 配置 `default_scene_market_name='iac'` 时，托管后场景为 `iac`（从 scene_market 复刻） | config=iac |
| T9 | `test_default_scene_forked_from_market` | 默认场景的 `nodes_json` 与 `scene_market(source='official', name='default')` 一致（复刻而非写死） | market 有 default |
| T10 | `test_dev_scene_default_empty` | 新建 `managed_repos` 行 `dev_scene=''`（非 'default'） | 空 |
| T11 | `test_set_repo_scene_empty` | `set_repo_scene(repo_key, '')` 不落 'default'，写空串 | 空 |

### 8.3 VOD 行为（B3）测试

| # | 用例 | 验证点 | 数据准备 |
|---|------|--------|----------|
| T12 | `test_vod_prompt_has_repo_scenes` | `_inject_vod_scenes(lines, 'vod', repo_key)` 后 lines 含仓库场景名（如「grape需求」），含"没有 default 场景"约束文本 | repo 有自定义场景 |
| T13 | `test_vod_prompt_empty_no_scenes` | 仓库无场景时 lines 含「未配置场景」终止约束文本 | repo 无场景 |
| T14 | `test_vod_prompt_non_vod_skip` | `agent_type != 'vod'` 时函数直接返回，不注入 | 任意 |
| T15 | `test_vod_recommended_uses_repo_scene` | VOD done 解析 `recommended_scene`（如 `custom-xxx`）时写入该场景 | issue_state 有行 |
| T16 | `test_vod_terminate_no_scene` | VOD done 输出 `{"terminate":true,"reason":"未配置场景"}` → 不写 scene_id、不触发后续 node | repo 无场景 |
| T17 | `test_vod_default_never_written` | VOD done 输出 `recommended_scene='default'` → `issue_state.scene_id` 保持原值不被覆写 | scene_id 已有值 |

### 8.4 全链路（B4）测试

| # | 用例 | 验证点 | 数据准备 |
|---|------|--------|----------|
| T18 | `test_get_scenes_by_repo` | `get_scenes(repo_key='gitcode:o/r')` 只返回该仓库场景；无参调用返回 `[]` | 多仓库场景 |
| T19 | `test_query_scene_by_id_repo_only` | `_query_scene_by_id('default', repo_key='x')` 命中 repo 场景；无 repo_key 返回 None（不落到 official） | repo 有 default |
| T20 | `test_create_custom_scene_repo_only` | `create_custom_scene(scene_id, name, description, repo_key, nodes)` 写成功，无 scope/user_id 分支 | 空 |
| T21 | `test_pipeline_config_needs_repo` | `get_pipeline_config('default')` 无 repo_key → 走 scene_market 官方场景兜底；带 repo_key → 命中仓库场景 | 双份数据 |
| T22 | `test_resolve_skill_repo_scene` | `resolve_skill_from_pipeline(repo_key, agent, scene_id)` 返回仓库场景节点技能 | repo 场景有节点 |
| T23 | `test_api_scenes_list_not_require_userid` | `GET /api/scenes?repo=...` 无需 user_id 也能列出仓库场景（调用方去掉 user_id 传参后） | 仓库有场景 |

### 8.5 回归（存量场景市场）

| # | 用例 | 验证点 |
|---|------|--------|
| T24 | `test_market_list_still_works` | 场景市场列表/详情/应用 API 不受表结构变更影响（复用 `test_scene_skill_market.py` 既有用例） |
| T25 | `test_scene_sync_still_works` | `sync_official_scenes()` 正常同步官方场景到 `scene_market`，不写 `devops_pipeline_scene` |

### 8.6 必跑命令

```bash
# 新用例
python -m pytest tests/test_scene_skill_market.py tests/test_scene_sync.py -v
# 相关回归
python -m pytest tests/test_scene_apply_enable.py tests/test_chat_task_scene.py tests/test_pipeline.py tests/test_dashboard.py -q
# 迁移模拟（dev 测试库）
python -m pytest tests/test_scene_migration.py -v   # 新增 T3/T4/T5 所在文件
```

> 说明：`test_scene_migration.py` 为新增文件，集中 B1/B2 迁移类用例；其余用例可并入现有
> `test_scene_skill_market.py` 或新建 `test_scene_repo_model.py`（按实现时文件组织决定）。

## 九、风险

- **行为变更**：废弃 `default` 兜底；新托管仓库默认自动应用「通用开发」场景（可配置/可关）；`dev_scene` 默认置空。
- **数据模型重构**：涉及存量数据迁移，需先在测试环境 `grape-dev-db` 验证迁移脚本与回归；生产需备份 `data/sessions.db`。
- 官方场景从 `devops_pipeline_scene` 移除后，展示与引用一律依赖 `scene_market(source='official')`（由 `scene_sync` 独立同步），需确保该同步链路正常、应用官方场景走"从 scene_market 复刻到仓库"路径。
- `dev_scene` 置空后，存量 `managed_repos.dev_scene='default'` 行若其仓库无对应场景，dashboard/pipeline 需按"仓库未配置场景"处理而非回退，避免 NPE。
- 本次一次性变更 VOD 行为 + 表结构 + 默认场景机制，改动面较大：需完整跑迁移 + 场景/技能链相关测试后再上线。

## 十、处理流水线状态

- ✅ VOD Agent：根因已定位并结构化（影响链完整）
- ✅ Issue Agent：四维可开发性分析 → ready，Spec 已发布
- ⚠️ A2A 交办 coding：`skipped:not_in_scene`（自定义场景 `custom-e7ffba11` pipeline 未含 coding 节点）
  → 本方案即 coding 直接落地依据；另需在场景配置补充 coding agent 节点以便 A2A 派发

## 附：关键函数位置

| 函数 | 位置 |
|------|------|
| `_inject_vod_scenes` | `src/prompt_builder.py:102`（`get_scenes()` 调用于 ~L108） |
| `get_scenes` | `src/api/pipeline_config.py`（支持 `repo_key`） |
| `get_pipeline_config` | `src/api/pipeline_config.py`（支持 `repo_key`） |
| `_query_scenes_from_db` / `_query_scene_by_id` | `src/api/pipeline_config.py` |
| `create/update_custom_scene` / `fork_official_scene` | `src/api/pipeline_config.py` |
| `_repo_user_id` | `src/runtime/engine_select.py`（B 阶段演进后移除使用） |
| `get_repo_scene` | `src/user/db.py`（读 `managed_repos.dev_scene`） |
| VOD done handler | `src/api/event_pipeline.py:1879-1895`（`set_issue_scene` 覆写处） |
| 场景表 DDL | `src/api/db_setup.py` `_PIPELINE_SCENE_DDL`（~L452） |
| 存量 user→repo 迁移 | `src/api/db_setup.py`（~L2205） |
| `_auto_fork_official_scenes_to_repo` | `src/api/pipeline_config.py`（B 阶段改为 `apply_default_scene_to_repo`，从 scene_market 复刻） |
| host_repo 自动默认场景 | `src/api/routes/repos.py`（~L421/438） |
| `scene_market` 默认场景种子 | `src/api/db_setup.py`（~L940，`default`/通用开发） |
| 官方场景同步 | `src/storage/scene_sync.py` `sync_official_scenes()` |
| `managed_repos.dev_scene` | `src/user/db.py`（建表 ~L367、`set_repo_scene` ~L1034） |