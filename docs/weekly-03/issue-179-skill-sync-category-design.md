# Issue #179：Skill 远程同步缺失 + 技能分类表独立化 — 详细设计

## 一、问题概述

### 1.1 根因

两条独立的同步链路，skill 同步缺失远程拉取能力：

| 模块 | 远程同步 | 本地同步 | 问题 |
|------|---------|---------|------|
| scene_sync.py | 有（下载 zip → 解压 → 同步 scene_market 表） | 无 | 正常 |
| skill_sync.py | 有（下载 zip → 合并 json → 拷贝 skill 目录 → 写 DB → 上传 OBS） | 有（读 JSON → 写 DB → 上传 OBS） | 已修复（sync_official_skills 统一同步） |

scene_sync 的 `_check_skills_exist()` 预检发现 skill 表缺失 → 场景同步 skip。

### 1.2 skill_sync 调度入口的多余 OBS 检查

`start_skill_sync_scheduler()` 第一行 `if get_storage() is None: return`，非 OBS 模式下整个定时任务不启动。但 DB 同步不需要 OBS，上传 OBS 步骤本身已有 `storage is None` 守卫（第 144 行），入口检查多余。

### 1.3 技能分类硬编码

`routes/skills.py` 的 `_SUB_SCENE_LABELS` 字典写死 11 个分类，前端标签筛选依赖 `agent-stats` 接口返回的 `SELECT DISTINCT sub_scene`。对比 scene 模块有独立的 `scene_category` 表 + 分类管理 API，skill 模块缺少这套机制。

## 二、方案设计

### 2.1 Part 1：skill 远程同步

#### 2.1.1 复用 scene_sync 的远程下载能力

scene_sync.py 已有：
- `_load_config()`：读 configs/scene-sync.json，返回 repo_url / repo_branch / sync_interval
- `_get_sync_token()`：读 GRAPE_DEV_TOKEN 环境变量 → configs/scene-sync.json 的 gitcode_api_token
- `_download_repo_zip(client, token)`：下载 zip → 解压到临时目录 → 返回 repo_root 路径

这三个函数直接 import 复用，不提取、不搬文件。skill_sync.py 调用方式：

```python
from src.storage.scene_sync import _download_repo_zip, _get_sync_token
```

#### 2.1.2 新增 `sync_official_skills_from_remote()` 函数

位置：`src/storage/skill_sync.py`

执行步骤：

```
1. 获取 token（复用 _get_sync_token()）
2. 创建 httpx.AsyncClient 上下文（async with httpx.AsyncClient(timeout=60.0) as client）
3. 下载 zip（复用 _download_repo_zip(client, token)）→ 得到 repo_root
4. 读 zip 里的 skill-description.json
5. 合并到本地 configs/skill-description.json（远程覆盖本地，本地自定义保留）
6. 遍历 zip 里的 skills/ 目录（分布式锁保护）：
   a. 对每个 skill 目录算 content_hash
   b. 和本地同路径目录 hash 比对
   c. hash 不变 → 跳过
   d. hash 变了或本地不存在 → 删除旧目录 → 拷贝新目录
   e. 每处理一个 skill 续期分布式锁
7. finally: 清理临时目录（shutil.rmtree，保存 _download_repo_zip 返回的路径）
```

> DB 同步不在本函数内执行 — 统一由 `_skill_sync_worker` 在远程同步之后、OBS 同步之前调 `_sync_skills_to_db()` 执行一次，避免重复调用。
> `_download_repo_zip` 需要 `httpx.AsyncClient` 参数，参考 scene_sync.py:315 的用法在函数内创建上下文。
> 临时目录清理：保存 `_download_repo_zip` 返回的路径，在 finally 中 `shutil.rmtree` 清理。
> 目录同步用分布式锁（`skill_remote_sync` key，TTL 300s）保护，多实例部署时避免并发 rmtree+copytree 导致目录损坏。每处理一个 skill 续期一次锁。

#### 2.1.3 skill-description.json 合并策略

| 情况 | 处理 |
|------|------|
| 远程有、本地没有 | 新增到本地 |
| 远程有变化 | 覆盖本地 |
| 远程没有、本地有 | 保留（不删除） |

合并后写回 `configs/skill-description.json`，再执行 DB 同步。

#### 2.1.4 skill 文件目录同步

- 路径映射：zip 里 `skills/{path}` → 本地 `SKILLS_DIR/{path}`（path 字段来自 skill-description.json）
- 增量比对：复用已有的 `_compute_skill_hash()` 函数算目录 hash
- 不处理删除：远程删了某 skill 目录，本地保留

#### 2.1.5 修改调度入口

`start_skill_sync_scheduler()`：
- 删除 `if get_storage() is None: return`
- `_skill_sync_worker` 三步独立 try/except，任一步失败不阻塞后续：

```python
async def _skill_sync_worker(interval: float = 1800.0):
    while True:
        try:
            # 1. 远程同步：失败不阻塞后续
            try:
                await sync_official_skills_from_remote()
            except Exception:
                logger.exception("skill remote sync failed, continuing")

            # 2. DB 同步：统一在此处执行一次（_sync_skills_to_db 提取为独立函数）
            try:
                json_path = os.path.join(CONFIG_DIR, "skill-description.json")
                if os.path.exists(json_path):
                    with open(json_path) as f:
                        _sync_skills_to_db(json.load(f))
            except Exception:
                logger.exception("skill DB sync failed, continuing")

            # 3. OBS 同步
            await sync_official_skills_to_obs()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("skill sync run failed")
        await asyncio.sleep(interval)
```

> `_sync_skills_to_db(data: dict) -> int`：从 skill-description.json 同步元信息到 DB，含 category_id。被三处复用：worker、db_setup._sync_official_skills、db_setup._insert_seed_data。
> DB 同步采用逐字段变更检测 — 只在实际数据变更时才 UPDATE + 刷新 updated_at，避免 blanket UPDATE 架空 sort=latest。
> per-skill try/except + INSERT OR IGNORE：单个 skill SQL 失败不中止整批。
> `sync_official_skills_to_obs` 内部不再调 `_sync_skills_to_db`（由 worker 统一调）。

#### 2.1.6 启动顺序

main.py 中 skill_sync（第 136 行）已在 scene_sync（第 143 行）之前注册。skill_sync 启动即执行首次同步，DB 写入完成后 scene_sync 的 `_check_skills_exist()` 自然通过。无需改 main.py 启动顺序。

### 2.2 Part 2：skill_category 独立分类表

#### 2.2.1 表结构

对齐 `scene_category` 表，SQLite 语法，靠 mysql_compat.py 翻译：

```sql
CREATE TABLE IF NOT EXISTS skill_category (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    slug        VARCHAR(64) NOT NULL,
    icon        TEXT DEFAULT '',
    order_num   INTEGER DEFAULT 0,
    is_active   INTEGER DEFAULT 1,
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now')),
    UNIQUE (name),
    UNIQUE (slug)
)
```

> slug 设 NOT NULL：11 个分类都有 slug，业务上 slug 是必须的。原方案 DEFAULT '' + UNIQUE 在 SQLite 下多条空串会冲突。

#### 2.2.2 表注册与索引

- `skill_category` 表 DDL 加入 `_NEW_TABLES` 列表（紧跟 `scene_category` 之后）
- `_init_skill_category_data()` 在 init_db 流程中调用，紧跟 `_init_scene_category_data()` 之后（第 1384/1415 行附近）
- 索引 `idx_skill_category_id` 加入 `_create_indexes()` 的 indexes 列表

#### 2.2.3 skill 表加 category_id

用 `_ensure_column` 幂等迁移（对齐项目惯例）：

```python
_ensure_column(c, 'skill', 'category_id', 'INTEGER DEFAULT 0')
```

加索引：

```sql
CREATE INDEX IF NOT EXISTS idx_skill_category_id ON skill(category_id)
```

#### 2.2.4 初始化数据

对齐 `_init_scene_category_data()` 的 meta 幂等机制：

```python
def _init_skill_category_data():
    if get_meta_value("skill_category_seeded") == "1":
        return
    categories = [
        ("AI辅助编码与评审", "ai-coding-review", "🤖", 1),
        ("应用发布", "app-release", "📦", 2),
        ("自动化测试", "auto-testing", "🧪", 3),
        ("持续集成CI", "ci", "🔧", 4),
        ("代码质量与安全", "code-quality", "🛡️", 5),
        ("容器与集群部署", "container-deploy", "☸️", 6),
        ("反馈闭环", "feedback-loop", "🔄", 7),
        ("IM", "im", "💬", 8),
        ("日志与诊断", "log-diagnostics", "🔍", 9),
        ("可观测性", "observability", "📊", 10),
        ("环境与脚手架", "scaffolding", "🏗️", 11),
    ]
    # INSERT OR IGNORE + set_meta_value("skill_category_seeded", "1")
```

调用位置：`db_setup.py` 初始化流程中，紧跟 `_init_scene_category_data()` 之后。

#### 2.2.5 sub_scene → category_id 映射

skill 表同时保留 `sub_scene`（英文 slug）和 `category_id`（数字 ID）：

- `sub_scene`：来源字段，skill-description.json 里已有
- `category_id`：同步时通过 `sub_scene` 查 `skill_category.slug` 获得
- 映射失败（slug 在 skill_category 表找不到）：`category_id=0`，打 warning 日志
- `sub_scene` 字段保留不废弃，作为 fallback

映射逻辑在 `_sync_skills_to_db()` 中执行，唯一调用方是 `sync_official_skills()`（worker 每 30 分钟调一次）。

> skill 已和服务分离，集群场景不依赖本地文件。init_database 不做 skill DB 同步，
> 启动后由 worker 异步完成（worker `while True` 第一轮立即执行，不等 sleep）。
> 原有 `db_setup._sync_official_skills()` 和 `_insert_seed_data` skill 补种已删除。

> `_sync_skills_to_db` 内部构建 slug→category_id 缓存（cat_map），sub_scene 非空但 category_id=0 时打 warning 日志。
> `_seed_from_json` 补种分支也独立构建 cat_map 写 category_id（不依赖 _sync_skills_to_db）。
> DB 缺失 skill 时自动补入：`_sync_skills_to_db` 遍历 JSON 每个 skill，SELECT 查 DB → 有则逐字段比对 UPDATE → 无则 INSERT OR IGNORE（含 category_id）。已验证：删除 DB 中的 skill 后重新同步，skill 自动补回且 category_id 正确。

#### 2.2.6 数据回填执行时机

回填逻辑放在 init_db 流程中，`_init_skill_category_data()` 之后、`_sync_official_skills()` 之前执行。作为幂等步骤（`WHERE category_id=0`），每次启动都会执行，但只有首次或新增分类时才实际命中。

```python
def _backfill_skill_category_id():
    """一次性回填：sub_scene → category_id"""
    try:
        with db_lock:
            c = db.cursor()
            c.execute("""
                UPDATE skill SET category_id = (
                    SELECT sc.id FROM skill_category sc WHERE sc.slug = skill.sub_scene
                ) WHERE sub_scene != '' AND category_id = 0
            """)
            updated = c.rowcount
            db.commit()
        if updated:
            log.info(f"Backfilled skill category_id: {updated} rows")
    except Exception as e:
        log.warning(f"Backfill skill category_id failed: {e}")
```

调用位置：init_db 流程，`_init_skill_category_data()` → `_backfill_skill_category_id()` → `_sync_official_skills()`。

### 2.3 Part 3：后端 API 适配

#### 2.3.1 新增 `/api/skills/categories` 接口

对齐 `/api/scenes/market/categories`：

```
GET /api/skills/categories
返回：{"categories": [{"id": 1, "name": "AI辅助编码与评审", "slug": "ai-coding-review", "icon": "🤖", "order_num": 1}]}
```

实现：新增 `list_skill_categories()` 函数（`user/db.py`），对齐 `list_scene_categories()`。

#### 2.3.2 `list_skills` 接口改动

| 改动点 | 当前 | 改后 |
|--------|------|------|
| 过滤参数 | `sub_scene`（英文 slug） | `category_id`（数字 ID） |
| SELECT 字段 | `sub_scene` | `sub_scene, category_id` |
| 返回字段 | `sub_scene`(中文label) + `sub_scene_id`(英文slug) | `category_id` + `category_name` |
| sort=latest | `ORDER BY agent_type, skill_name` | `ORDER BY updated_at DESC` |

> sort=latest 改为 `ORDER BY updated_at DESC`。`_sync_skills_to_db` 的 UPDATE 采用逐字段变更检测，只在实际数据变更时才刷新 updated_at，避免 blanket UPDATE 架空排序。

category_name 通过查 skill_category 表获得（批量查，避免 N+1）。

#### 2.3.3 `agent-stats` 接口改动

当前返回 `sub_scenes: [{id, label}]`（从 `SELECT DISTINCT sub_scene` 查）。
改为返回 `categories: [{id, name, slug, icon, order_num}]`（从 skill_category 表查）。

直接替换，不保留 `sub_scenes` 字段。前端后端同步改、同步部署。

#### 2.3.4 `upload` 接口改动

新增 `category_id` Form 参数（可选，默认 0）。

`upsert_skill_obs()` 函数增加 `category_id` 参数，INSERT 和 UPDATE 都写入。

#### 2.3.5 `get_skill_detail_by_id` 接口改动

返回字段增加 `category_id` + `category_name`，保留 `sub_scene`。

#### 2.3.6 删除 `_SUB_SCENE_LABELS` 硬编码字典

`routes/skills.py` 的 `_SUB_SCENE_LABELS` 字典和 `_sub_scene_label()` 函数删除。分类数据改为从 `skill_category` 表动态读取（通过 `list_skill_categories()` / `get_skill_category()`）。

#### 2.3.7 `convert_skill` 接口改动

个人技能转仓库技能时，`upsert_skill_obs("repo", ...)` 调用要补传源技能的 `category_id`，避免转换后分类丢失。

`user/db.py` 的 `list_skills()` SQL 增加 `category_id` 列，返回 dict 加 `category_id` 字段。

#### 2.3.8 `get_featured_skills` / `list_featured_skills` 接口改动

- `get_featured_skills()` SQL 增加 `category_id` 列
- `list_featured_skills()` API 补充 `category_name`（批量查 cat_map）

#### 2.3.9 `search_skills` 接口改动

两处 SQL（精确匹配 + 关键词匹配）都增加 `category_id` 列，返回 `category_id` + `category_name`。cat_map 在 `with db_lock` 块外查（避免 SQLite cursor 复用）。

#### 2.3.10 `list_skills` cursor 复用修复

`list_skill_categories()` 调用移到 `with db_lock` 块外面。SQLite 单连接单活跃 statement，在 `c.execute()` 和 `c.fetchall()` 之间调 `list_skill_categories()`（内部又 `c.execute()`）会重置旧 cursor 结果集。MySQL 不受影响（pymysql 缓冲式游标）。

### 2.4 Part 4：前端适配

#### 2.4.1 适配点清单

| # | 函数 | 改动 |
|---|------|------|
| 1 | `renderSkills()` | 初始化时调 `/api/skills/categories` 获取分类列表，缓存到 `_skillCategories` |
| 2 | `_loadAgentStats()` | `agent-stats` 返回的 `categories` 写入 `_skillCategories`（替代 `_allSubScenes`） |
| 3 | `_renderTagFilters()` | 从 `_skillCategories` 渲染标签（id + name + icon），替代 `_allSubScenes` |
| 4 | `_switchTagFilter(label, id)` | API 传 `category_id=数字ID`，替代 `sub_scene=英文slug` |
| 5 | `_reloadSkills()` | 请求参数 `sub_scene=` 改为 `category_id=` |
| 6 | `_renderSkillCard()` | 卡片标签显示 `s.category_name`，替代 `s.sub_scene` |
| 7 | `renderSkillDetail()` | `tagsStr = data.category_name`，替代 `data.sub_scene` |
| 8 | `showSkillUpload()` | 上传表单增加分类下拉选择器，异步调 `/api/skills/categories` 获取列表（不依赖 agent-stats 缓存） |
| 9 | `_renderAgentSkills()` 第 449 行 | 删除客户端二级过滤，统一走 API 查询 |
| 10 | `_renderTagFilters()` 选中 agent 分支（第 402-411 行） | 统一用 `_skillCategories` 渲染全量分类标签，不再从当前页数据提取 |
| 11 | `_renderTagFilters()` 标签渲染 | 标签按钮前置分类图标 `<span>{icon}</span>` |
| 12 | `_renderSkillCard()` | 卡片标题行加分类标签（蓝色，agent_type 标签后面） |
| 13 | `showSkillUpload()` i18n | 分类 label 用 `I18N.t('skills.upload.category')`，「未分类」option 用 `I18N.t('skills.upload.uncategorized')` |

> 分类标签固定展示全量 11 个（来自 `_skillCategories`），不随数据动态变化。选中 agent 后点分类标签走 API 查询（传 category_id + agent_type），不做客户端过滤。无数据的分类显示"暂无技能"。

#### 2.4.2 三 Tab 分类一致性

| Tab | scope | 分类数据来源 | 标签筛选 |
|-----|-------|------------|---------|
| 技能市场 | official | skill_sync 写入 category_id | ✅ 可用 |
| 仓库技能 | repo | 上传时选分类，upsert_skill_obs 写入 category_id | ✅ 可用 |
| 个人技能 | user | 上传时选分类，upsert_skill_obs 写入 category_id | ✅ 可用 |

agent-stats 接口的 `categories` 查询条件随 scope 变化：
- official：查 `SELECT DISTINCT category_id FROM skill WHERE scope='official'`
- repo：查 `SELECT DISTINCT category_id FROM skill WHERE scope='repo' AND owner_id=?`
- user：查 `SELECT DISTINCT category_id FROM skill WHERE scope='user' AND owner_id=?`

再关联 skill_category 表返回分类详情。category_id=0 不返回（未分类不展示为筛选标签）。

### 2.5 Part 5：Admin 后端适配（本期延后）

Admin 后端（SkillRo / SkillService / SkillManage）本期不做，后续迭代补充。

## 三、涉及文件

| 文件 | 改动内容 |
|------|---------|
| `src/storage/skill_sync.py` | `sync_official_skills()` 统一同步（远程下载 → DB 同步 → OBS 上传）+ `_sync_skills_to_db()` 变更检测 + per-skill 错误处理 + INSERT OR IGNORE + category_id warning + 分布式锁（含续期）+ 路径安全 + 原子 json 写入 + hash 目录排序 |
| `src/storage/scene_sync.py` | 无改动（_download_repo_zip / _get_sync_token 直接被 import 复用） |
| `src/api/db_setup.py` | 新建 skill_category 表 DDL + 注册 _NEW_TABLES + skill 表加 category_id（_ensure_column）+ _init_skill_category_data() + _backfill_skill_category_id() + 索引注册 _create_indexes() |
| `src/api/routes/skills.py` | list_skills 加 category_id 过滤 + cat_map 移到 db_lock 外（cursor 修复）+ agent-stats 改查 skill_category + 新增 /categories 接口 + upload 加 category_id + detail 加 category_id + convert_skill 补传 category_id + featured 加 category_id/category_name + search 加 category_id/category_name + sort=latest 改 updated_at DESC + 删除 _SUB_SCENE_LABELS |
| `src/user/db.py` | 新增 list_skill_categories() / get_skill_category() + upsert_skill_obs 加 category_id + list_skills 加 category_id + get_featured_skills 加 category_id |
| `src/api/static/js/pages/skills.js` | 13 处前端适配（见 2.4.1） |
| `src/api/static/index.html` | skills.js?v=41 |
| `src/api/static/locales/zh/skills.json` | 新增 upload.category + upload.uncategorized |
| `src/api/static/locales/en/skills.json` | 新增 upload.category + upload.uncategorized |
| `src/api/main.py` | 无改动（启动顺序已正确） |
| `configs/skill-description.json` | 无需改动 |
| 远程 grape-skills 仓库 | 无需改动 |

## 四、数据迁移

### 4.1 已有 skill 数据回填 category_id

skill 表已有 official 技能的 `sub_scene` 字段有值。建表 + 加列后，执行一次性回填：

```sql
UPDATE skill SET category_id = (
    SELECT sc.id FROM skill_category sc WHERE sc.slug = skill.sub_scene
) WHERE sub_scene != '' AND category_id = 0;
```

user/repo 技能 `sub_scene` 为空，`category_id` 保持 0（未分类），用户后续上传时可选分类。

### 4.2 验证

回填后验证：

```sql
-- 检查 official 技能 category_id 回填情况
SELECT scope, category_id, COUNT(*) FROM skill WHERE scope='official' GROUP BY scope, category_id;
-- 预期：全部 category_id > 0，无 0 值

-- 检查 user/repo 技能
SELECT scope, category_id, COUNT(*) FROM skill WHERE scope IN ('user','repo') GROUP BY scope, category_id;
-- 预期：category_id=0（用户未选分类）
```

## 五、验收标准

1. 远程 grape-skills 仓库新增 skill 后，30 分钟内自动同步到本地 DB（skill 表）和磁盘（SKILLS_DIR）
2. common-skill-dev 场景不再报 missing skills，正常同步
3. skill_category 表独立管理技能分类，前端不再依赖硬编码 `_SUB_SCENE_LABELS` 字典（删除）
4. skill 表的 category_id 通过 sub_scene 字段关联 skill_category 表自动填充
5. `/api/skills` 接口支持按 category_id 过滤，返回 category_id + category_name
6. `/api/skills/categories` 接口返回分类列表
7. 已有 skill 的分类数据迁移正确（sub_scene → category_id 映射）
8. 11 个分类初始化数据与 skill-description.json 中 sub_scene 值一一对应，共覆盖 47 个 skill
9. 非 OBS 模式下远程同步正常启动（不受 `get_storage() is None` 拦截）
10. repo/user 技能支持分类：上传表单可选分类，DB 写入 category_id，前端标签筛选对三 Tab 一致
11. skill 远程同步在 scene_sync 之前执行（main.py 启动顺序已保证），场景同步预检时 skill 表已有数据
12. 远程覆盖本地策略：远程 skill-description.json 新增/变更同步到本地，本地自定义 skill 保留
13. agent-stats 直接替换 sub_scenes 为 categories，无兼容期
14. sort=latest 改为 ORDER BY updated_at DESC，_sync_skills_to_db 逐字段变更检测避免架空排序
15. worker 三步独立 try/except：远程同步/DB 同步/OBS 同步任一失败不阻塞后续
16. 多实例目录同步用分布式锁保护（skill_remote_sync key，TTL 300s，per-skill 续期）
17. list_skills/featured/search 接口均返回 category_id + category_name
18. 前端卡片标题行显示分类标签，标签筛选按钮显示分类图标
19. 上传表单分类选择器异步加载分类列表，i18n 支持 zh/en

## 六、风险点

| 风险 | 影响 | 缓解 |
|------|------|------|
| 远程 zip 下载失败 | skill 表不刷新，scene_sync 仍报 missing | skill_sync 已有 try/except + 日志告警，不影响服务运行；下次 30 分钟自动重试 |
| skill-description.json 合并冲突 | 本地自定义 skill 被覆盖 | 合并策略：远程没有的本地保留，只覆盖远程有的 |
| category_id=0 的 skill 无分类标签 | 前端标签筛选不显示这些 skill | agent-stats 查询 DISTINCT category_id 时过滤 0 值；用户可在上传时补选分类 |
| sub_scene 新增但 skill_category 表没对应 slug | category_id=0 | 同步时打 warning 日志，需手动补 seed 数据 |
