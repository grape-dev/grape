# 技术方案：DevOps 市场（场景市场 + 技能市场）

> 关联 Issue：grape-dev/grape#11
> 需求文档：docs/new-version3/issue-11-scene-skill-market-requirement.md
> 分支：feat/devops-skills-redesign（基于 weekly-01 e8090390）
> 状态：技术方案讨论中

## 一、整体架构

```
侧边栏菜单变更
  原"知识库"组下"DevOps 技能" → 新"DevOps 市场"组下"场景市场"+"技能市场"

新增页面
  scene-market.js  → 场景市场页面（场景卡片+详情+应用+体验）
  skills.js 重写   → 技能市场页面（精选+点赞+使用统计+查找+现有能力）

新增后端
  scenes.py 扩展   → 场景市场 API（市场列表+详情+体验+应用+搜索）
  skills.py 扩展   → 点赞 API + 精选 API + 使用统计 API + 技能查找 API

新增数据库
  新建 scene_market 表      → 场景市场（完全独立，自包含，不和 devops_pipeline_scene 耦合）
  devops_pipeline_scene 加字段 → market_scene_id（记录用户场景的市场来源，单向关联）
  skill 表加字段             → is_featured, like_count
  新建 skill_likes 表         → user_id + skill_id 联合唯一
```

## 二、数据库变更

### 2.1 新建 scene_market 表（核心——完全独立，自包含）

```sql
CREATE TABLE IF NOT EXISTS scene_market (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,          -- 主键自增
    name        VARCHAR(256) NOT NULL,                      -- 场景名称（通用开发 / IaC开发 / ...）
    description TEXT DEFAULT '',                            -- 简短描述
    market_description TEXT DEFAULT '',                     -- 市场展示用的详细功能介绍（2-3 句话）
    thumbnail   VARCHAR(512) DEFAULT '',                    -- 缩略图存储key（OBS模式存对象key，本地模式存/static/img/scenes/xxx.svg相对路径）
    is_featured INTEGER DEFAULT 0,                         -- 精选标记 0=否 1=是
    source      VARCHAR(32) DEFAULT 'official',             -- 来源：official/user/third_party（可扩展）
    source_user_id INTEGER DEFAULT 0,                      -- source=user 时记录创建者用户ID
    recommended_template VARCHAR(191) DEFAULT '',           -- 推荐模板ID（体验按钮用此模板创建项目）
    nodes_json  TEXT DEFAULT '[]',                          -- 技能链路配置（完整nodes，自包含不依赖外部表）
    onboarding  TEXT DEFAULT 'repo-onboarding',             -- 首次托管引导技能名
    is_active   INTEGER DEFAULT 1,                         -- 是否启用 0=否 1=是
    created_by  VARCHAR(64) DEFAULT '',                     -- 创建者
    updated_by  VARCHAR(64) DEFAULT '',                     -- 更新者
    created_at  TEXT DEFAULT (datetime('now')),             -- 创建时间
    updated_at  TEXT DEFAULT (datetime('now'))              -- 更新时间
)
```

设计原则：
- **完全独立**：scene_market 自包含全部场景信息（name/description/nodes_json/onboarding 等），不依赖 devops_pipeline_scene 表
- **高内聚低耦合**：场景市场的所有操作（CRUD/搜索/精选/展示）只碰 scene_market 一张表
- **可扩展**：source 字段区分来源（official/user/third_party），将来用户自建场景上市场只加一行
- **审计字段**：created_by/updated_by VARCHAR(64)，和现有 skill 表等系统表一致

7 个初始场景数据：

| id | name | recommended_template | source |
|----|------|---------------------|--------|
| 1 | 通用开发 | hello-flask | official |
| 2 | 云原生开发 | micro-svc | official |
| 3 | IaC开发 | tf-infra | official |
| 4 | Agent开发 | ai-interview-swarm | official |
| 5 | 华为云Skill开发 | hello-skill | official |
| 6 | 通用Skill开发 | hello-skill | official |
| 7 | 博客发布 | (需要补一个博客模板) | official |

### 2.2 devops_pipeline_scene 表加字段（单向关联）

```sql
-- DDL（_PIPELINE_SCENE_DDL 末尾加）
ALTER TABLE devops_pipeline_scene ADD COLUMN market_scene_id INTEGER DEFAULT NULL;

-- migration
"ALTER TABLE devops_pipeline_scene ADD COLUMN market_scene_id INTEGER DEFAULT NULL",
```

- market_scene_id：记录该用户场景是从市场哪个场景复制来的（指向 scene_market.id）
- 用户自建场景不走市场时 market_scene_id=NULL
- 统计"在用仓库数"：COUNT devops_pipeline_scene WHERE market_scene_id=?
- **devops_pipeline_scene 不加任何展示字段**（is_featured/thumbnail 等全在 scene_market 表里）

数据流：

```
scene_market（市场，独立完整，自包含）
  id=1  name=通用开发  nodes=[vod→issue→coding→qa→sre]  recommended_template=hello-flask  ...
    ↓ 用户点"应用"
    ↓ 从 scene_market 复制配置到用户场景表
devops_pipeline_scene（用户场景配置，记录来源）
  market_scene_id=1  scope=user  nodes=[从市场复制的]  ← 从市场 id=1 复制来的
  market_scene_id=1  scope=repo  ← 同一个场景应用到另一个仓库
  market_scene_id=NULL            ← 用户自建场景（不走市场）
```

### 2.3 skill 表加字段

```sql
-- DDL（_SKILL_DDL 末尾加）
ALTER TABLE skill ADD COLUMN is_featured INTEGER DEFAULT 0;
ALTER TABLE skill ADD COLUMN like_count INTEGER DEFAULT 0;

-- migration
"ALTER TABLE skill ADD COLUMN is_featured INTEGER DEFAULT 0",
"ALTER TABLE skill ADD COLUMN like_count INTEGER DEFAULT 0",
```

- is_featured：精选标记，0=否 1=是，仅 official 技能会设为 1
- like_count：点赞冗余计数，默认 0，点赞/取消时同步更新

### 2.4 新建 skill_likes 表

```sql
CREATE TABLE IF NOT EXISTS skill_likes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,          -- 主键自增
    user_id     INTEGER NOT NULL,                           -- 用户ID
    skill_id    INTEGER NOT NULL,                           -- 技能ID
    created_at  TEXT DEFAULT (datetime('now')),             -- 创建时间
    UNIQUE (user_id, skill_id)                              -- 联合唯一，防重复点赞
)
```

- user_id + skill_id 联合唯一，防重复点赞
- 点赞：INSERT，同时 UPDATE skill SET like_count=like_count+1
- 取消：DELETE，同时 UPDATE skill SET like_count=like_count-1

## 三、数据初始化

### 3.1 skill 表精选标记

skill 表的 is_featured 初始数据通过 db_setup.py 幂等 UPDATE 设置（和 scene_market 表初始数据同样的模式），不依赖 skill-description.json，不改 sync-official 逻辑。

初始推荐精选的技能（基于通用性和核心程度）：
- vod（VOD 核心能力）
- issue-advance（Issue 高级运营）
- implementation（代码实现）
- pr（PR 管理）
- cloud-deploy（云部署）
- skill-creator（华为云 Skill 创建）

初始化方式：db_setup.py 里写一个 _init_skill_featured() 函数，migration 后检查 is_featured 字段是否已初始化（查 meta 表标记），未初始化则 UPDATE skill SET is_featured=1 WHERE skill_name IN (...) AND scope='official'。

### 3.2 scene_market 表初始数据

scene_market 表的初始 7 条数据通过 db_setup.py 建表后 INSERT 初始化（和建表同一套 _create_tables 幂等模式）。数据完全在数据库里维护，不依赖外部 JSON 配置文件。

初始数据内容（从现有 devops_pipeline_scene official 场景的 nodes_json 复制 + 补充展示字段）：

| name | market_description | is_featured | recommended_template | nodes_json 来源 |
|------|-------------------|-------------|---------------------|----------------|
| 通用开发 | 通用的全链路开发场景，从需求收集到部署运维，适合大多数项目 | 1 | hello-flask | devops_pipeline_scene scene_id=default |
| 云原生开发 | 云原生开发场景，适合微服务和容器化项目 | 0 | micro-svc | devops_pipeline_scene scene_id=cloud-native |
| IaC开发 | IaC 基础设施即代码开发场景，使用 Terraform 管理云资源 | 1 | tf-infra | devops_pipeline_scene scene_id=iac |
| Agent开发 | Agent 开发场景，适合构建 AI Agent 项目 | 0 | ai-interview-swarm | devops_pipeline_scene scene_id=agent-dev |
| 华为云Skill开发 | 华为云 Skill 开发场景，创建和发布华为云技能 | 1 | hello-skill | devops_pipeline_scene scene_id=skill-dev |
| 通用Skill开发 | 通用 Skill 开发场景，创建和发布通用技能 | 0 | hello-skill | devops_pipeline_scene scene_id=api-skill-dev |
| 博客发布 | 博客系统开发与发布场景 | 0 | (待补) | devops_pipeline_scene scene_id=blog-publish |

初始化方式：db_setup.py 里写一个 _init_scene_market_data() 函数，建表后检查是否已有数据，无数据则 INSERT 7 条。有数据则跳过（幂等）。

### 3.3 场景缩略图

缩略图存储模式和技能包一致——通过 `get_storage()` 判断 OBS 是否启用，两种模式自动兼容：

**兼容机制**（复用现有 storage 层）：

```python
def resolve_thumbnail(thumbnail: str) -> str:
    """解析缩略图为可访问的 URL"""
    storage = get_storage()
    if storage is not None and thumbnail and not thumbnail.startswith('/static/'):
        # OBS 模式：thumbnail 是对象 key，签发临时签名 URL
        return storage.temporary_url(thumbnail)
    # 本地模式：thumbnail 是 /static/xxx 相对路径，直接返回
    return thumbnail or '/static/img/scenes/default.svg'
```

- **系统预置场景**：
  - 本地模式：thumbnail 存 `/static/img/scenes/default.svg`（静态文件已存在）
  - OBS 模式：部署时上传 SVG 到 OBS，thumbnail 存 `scenes/default.svg`（对象 key）
- **用户自建场景**（将来）：
  - 本地模式：保存到 src/api/static/img/scenes/ 目录，thumbnail 存相对路径
  - OBS 模式：上传到 OBS，thumbnail 存对象 key
- **集群兼容**：OBS 模式下所有实例通过 OBS 临时签名 URL 访问，天然共享
- **前端无感知**：后端返回场景列表时统一调 resolve_thumbnail，前端拿到的都是可直接访问的 URL
- **复用现有机制**：和技能包下载的 temporary_url 是同一套 storage 层逻辑

用 SVG 生成 7 个初始缩略图（苹果风格渐变背景 + 场景图标 + 场景名称），矢量图体积小自动适配。

## 四、后端 API 设计

### 4.1 场景市场 API（src/api/routes/scenes.py 扩展）

#### GET /api/scenes/market

返回场景市场列表（从 scene_market 表读取，完全独立）。

```python
@router.get("/market")
def list_scenes_market(user: dict = Depends(get_current_user), q: str = ""):
    """场景市场列表：精选场景 + 全部场景，支持搜索"""
    # 1. 查 scene_market WHERE is_active=1
    # 2. 搜索：q 非空时按 name/market_description 过滤
    # 3. 统计在用仓库数：查 devops_pipeline_scene WHERE market_scene_id=sm.id GROUP BY
    # 4. 解析 nodes_json 拿技能名称列表
    # 5. 返回 [{id, name, market_description, thumbnail, is_featured,
    #          skills: [skill_name, ...], recommended_template, repo_count}]
```

#### GET /api/scenes/market/{market_id}

场景详情页数据（完整信息，从 scene_market 表读取）。

```python
@router.get("/market/{market_id}")
def get_scene_market_detail(market_id: int, user: dict = Depends(get_current_user)):
    """场景详情：完整介绍 + 技能链路详解 + 推荐模板 + 在用仓库列表"""
    # 1. 查 scene_market WHERE id=?
    # 2. 解析 nodes_json，每个 node 的 skill 查 skill 表拿 display_name + description
    # 3. 查 recommended_template 对应的模板信息（从 project-templates.json 查模板详情）
    # 4. 查 devops_pipeline_scene WHERE market_scene_id=? 拿在用仓库列表
    # 5. 返回 {id, name, market_description, thumbnail, is_featured,
    #         skills: [{name, display_name, agent_type, role_desc}],
    #         template: {id, name, description, language, stars},
    #         repos: [{repo_key, full_name}]}
```

#### POST /api/scenes/market/{market_id}/apply

应用场景：从 scene_market 复制配置到当前仓库的 devops_pipeline_scene。

```python
@router.post("/market/{market_id}/apply")
def apply_market_scene(market_id: int, user: dict = Depends(get_current_user)):
    """应用场景：从市场复制配置到当前仓库"""
    # 1. 查 scene_market WHERE id=? 拿 nodes_json + onboarding
    # 2. 前提：用户已选中已托管仓库（从 localStorage.currentRepo 读）
    # 3. 在 devops_pipeline_scene 创建/更新 repo scope 副本：
    #    - scene_id = f"repo:{market_id}"（或保持现有 fork 模式）
    #    - scope = "repo"
    #    - market_scene_id = market_id（记录来源）
    #    - nodes_json = 从 scene_market 复制的
    # 4. 调现有 apply_scene 逻辑（更新 devops_agent_session 的 skill 配置）
    # 5. 更新 managed_repos.dev_scene
    # 6. 返回 {message: "场景已应用"}
```

#### POST /api/scenes/market/{market_id}/experience

体验场景：一键创建项目。

```python
@router.post("/market/{market_id}/experience")
async def experience_market_scene(market_id: int, user: dict = Depends(get_current_user)):
    """体验场景：基于场景关联的推荐模板创建项目，每用户每场景限一次"""
    # 1. 查 scene_market WHERE id=? 拿 recommended_template + nodes_json
    # 2. 检查去重：查 devops_pipeline_scene WHERE market_scene_id=? 
    #    AND user_id=用户id（或 repo_owner=用户 username）
    #    已体验 → 返回 {already_experienced: true, full_name: "xxx/yyy"}
    # 3. 未体验 → 调用现有 create_from_template（template_id=recommended_template）
    #    建仓库 → 写代码 → 托管 → onboarding
    # 4. 把 scene_market 的场景配置复制到新仓库的 devops_pipeline_scene：
    #    - market_scene_id = market_id（记录来源）
    #    - nodes_json = 从 scene_market 复制的
    # 5. 调 apply_scene 应用场景配置到 agent_session
    # 6. 返回 {full_name, message}
```

### 4.2 技能市场 API（src/api/routes/skills.py 扩展）

#### POST /api/skills/{skill_id}/like

点赞/取消点赞（toggle）。

```python
@router.post("/{skill_id}/like")
def toggle_like(skill_id: int, user: dict = Depends(get_current_user)):
    """点赞 toggle：已赞→取消，未赞→点赞"""
    # 1. 查 skill_likes WHERE user_id=? AND skill_id=?
    # 2. 已赞 → DELETE + UPDATE skill SET like_count=like_count-1
    # 3. 未赞 → INSERT + UPDATE skill SET like_count=like_count+1
    # 4. 返回 {liked: true/false, like_count: N}
```

#### GET /api/skills/featured

精选技能列表。

```python
@router.get("/featured")
def list_featured_skills(user: dict = Depends(get_current_user)):
    """精选技能：is_featured=1 的 official 技能，按 like_count 降序"""
```

#### GET /api/skills/stats

技能使用统计（被多少场景使用 + 被多少仓库间接使用）。

```python
@router.get("/stats")
def get_skill_stats(user: dict = Depends(get_current_user)):
    """技能使用统计"""
    # 1. 查 scene_market 全部场景的 nodes_json，统计每个 skill_name 出现在几个场景
    # 2. 查 devops_pipeline_scene WHERE market_scene_id IS NOT NULL → 关联 scene_market 的 nodes
    #    → 统计每个 skill 被多少仓库间接使用
    # 3. 返回 {skill_name: {scene_count: N, repo_count: N}}
```

也可以合并到 list_skills 接口返回，避免额外请求。

#### GET /api/skills/search?q=任务描述

技能查找（输入任务描述推荐相关技能，复用搜索框，不单独弹窗）。

```python
@router.get("/search")
def search_skills(q: str, user: dict = Depends(get_current_user)):
    """技能查找：根据任务描述推荐相关技能。
    先按 skill_name 精确匹配，匹配到则按名称过滤；
    匹配不到则按 description + display_name + agent_type 做关键词匹配。
    不调 LLM（第一期简单实现）——分词后匹配技能描述。
    返回 [{skill_id, name, display_name, description, match_reason}]
    """
```

### 4.3 sync-official 不改

现有 sync-official 逻辑不变，不加 featured 字段同步。skill 表的 is_featured 通过 db_setup.py 初始化，不依赖 JSON 同步。

## 五、前端设计

### 5.1 菜单结构变更（src/api/static/index.html）

```html
<!-- 原"知识库"组 -->
<div class="grp" data-i18n="app.sidebar.knowledge">知识库</div>
<a id="nav-skills" onclick="app.nav('skills')">...DevOps 技能</a>
<a id="nav-knowledge" onclick="app.nav('knowledge')">...项目知识库</a>

<!-- 改为"DevOps 市场"组 + "知识库"组 -->
<div class="grp">DevOps 市场</div>
<a id="nav-scene-market" onclick="app.nav('scene-market')">...场景市场</a>
<a id="nav-skills" onclick="app.nav('skills')">...技能市场</a>
<div class="grp">知识库</div>
<a id="nav-knowledge" onclick="app.nav('knowledge')">...项目知识库</a>
```

### 5.2 路由变更（src/api/static/js/app.js）

```javascript
const VIEWS = ['dash','pipe','vod','issue','coding','qa','sre','chat','settings',
               'scene-market','skills','knowledge','runtimes','event','repos',
               'skill','scene-detail','tasks','observability'];

// scene-market → 场景市场列表
// scene-detail → 场景详情页（参数 market_id）
// skills → 技能市场列表（重写）
// skill → 技能详情页（现有，增强）
```

### 5.3 场景市场页面（新建 src/api/static/js/pages/scene-market.js）

页面结构：

```
顶部 tab：[场景市场] [技能市场]
搜索框
精选场景区域（is_featured=1 的场景卡片）
全部场景区域（网格布局）
```

场景卡片：
- 缩略图（SVG）
- 场景名称
- 精选徽章 🌟
- 功能介绍（market_description）
- 包含技能（VOD → Issue → Coding → QA → SRE）
- 推荐模板名称 + 在用仓库数
- [应用] [体验] 按钮
- 点击卡片 → app.nav('scene-detail', market_id)

场景详情页（scene-market.js 内渲染）：
- 完整功能介绍
- 技能链路详解（每个技能名称+Agent类型+角色描述，可跳转技能详情）
- 推荐模板（名称、描述、语言、stars）
- 在用仓库列表
- [应用] [体验] 按钮

"应用"操作：调 POST /api/scenes/market/{market_id}/apply
"体验"操作：调 POST /api/scenes/market/{market_id}/experience

### 5.4 技能市场页面（重写 src/api/static/js/pages/skills.js）

保留现有所有功能（上传/删除/转仓库/下载/华为云包），新增精选/点赞/统计/场景介绍/查找。

**去掉每次进页面调 sync-official**：现有 skills.js 每次切到官方页签都调 POST /skills/sync-official（读 JSON + 遍历 38 个技能查 DB + 触发 OBS 全量扫描），这是历史遗留逻辑。重写时去掉这个调用，页面只负责读数据展示。

**定时任务增强**：现有 start_skill_sync_scheduler 在非 OBS 模式下直接 return（不启动定时任务）。改为两种模式都启动：
- OBS 模式：定时 DB 同步（读 skill-description.json → INSERT/UPDATE skill 表）+ OBS 上传
- 非 OBS 模式：只做 DB 同步（skill-description.json 有新增技能自动入库，不做 OBS 上传）
- 定时间隔不变（默认 30 分钟，SKILL_SYNC_INTERVAL 可配）

**sync-official DB 同步逻辑修复**（从 skills.py 移到 skill_sync.py，修复 4 个问题）：

现有问题：
1. WHERE 条件不用主键 id——用 skill_name+scope 查，同名不同 agent_type 会更新到错误的行
2. file_path 分支判断不合理——有 file_path 时不更新 skill_path，两个字段混在一起判断
3. fetchone() 多行问题——理论上 official scope 只有一行但无防御

修复方案：
- 新增 `_sync_skill_description_to_db()` 函数放 skill_sync.py
- 查询用 `SELECT id FROM skill WHERE skill_name=? AND scope='official'`，拿到主键 id
- 更新用 `WHERE id=?`（主键更新，无歧义）
- skill_path 统一从 JSON 更新，不碰 file_path/content_hash（由 OBS 同步逻辑单独管理）
- 只加不删（保持现有设计，不改成软删除）

```python
def _sync_skill_description_to_db() -> int:
    """从 skill-description.json 同步官方技能元信息到 skill 表。
    返回新增数量。用主键 id 更新，不碰 file_path/content_hash。
    只加不删（保持现有设计）。
    """
    json_path = os.path.join(CONFIG_DIR, "skill-description.json")
    if not os.path.exists(json_path):
        return 0
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    added = 0
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        for skill_name, info in data.items():
            display_name = info.get("name", skill_name)
            description = info.get("description", "")
            agent_type = info.get("agent_type", "")
            skill_path = info.get("path", skill_name)
            row = c.execute(
                "SELECT id FROM skill WHERE skill_name=? AND scope='official'",
                (skill_name,)
            ).fetchone()
            if row:
                c.execute(
                    "UPDATE skill SET display_name=?, description=?, agent_type=?, skill_path=? WHERE id=?",
                    (display_name, description, agent_type, skill_path, row[0])
                )
            else:
                c.execute(
                    "INSERT INTO skill (skill_name, display_name, description, agent_type, skill_path, scope, owner_id, content_hash) VALUES (?,?,?,?,?,'official','',?)",
                    (skill_name, display_name, description, agent_type, skill_path, "")
                )
                added += 1
        db.commit()
    return added
```

定时任务改为：

```python
async def _skill_sync_worker(interval: float = 1800.0):
    while True:
        try:
            added = _sync_skill_description_to_db()
            if added > 0:
                logger.info(f"skill DB sync: added {added} new skills")
            if get_storage() is not None:
                await sync_official_skills_to_obs()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("skill sync run failed")
        await asyncio.sleep(interval)

def start_skill_sync_scheduler():
    """启动定时同步调度。OBS 模式做 DB+OBS 同步，非 OBS 模式只做 DB 同步。"""
    interval = float(os.environ.get("SKILL_SYNC_INTERVAL", "1800"))
    t = asyncio.create_task(_skill_sync_worker(interval), name="skill-sync")
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)
    log.info(f"skill sync scheduler started (interval={interval}s)")
```

页面结构：
```
顶部 tab：[场景] [技能]
scope 页签（技能市场/仓库技能/个人技能）
场景与技能关系说明（提示框）
精选技能区域（is_featured=1，按 like_count 排序，横向滚动）
全部技能区域（搜索框 + 查找技能按钮 + 最新/最热排序 + Agent 类型 tab 切换）
```

技能卡片增强：
- Agent 图标（卡片标题前，复用 _AGENT_TYPES 配色）
- Agent 类型标签（彩色）
- 精选徽章 🌟
- 点赞按钮 ❤️ + 数量
- 所属场景标签（可跳转场景详情）
- 使用统计（3 个场景在用，8 个仓库在用）
- 查看详情 / 上传 / 删除 / 转仓库（现有按钮保留）

技能查找（复用搜索框，不单独弹窗）：
- 用户在搜索框输入任务描述，点击"查找技能"按钮
- 调 GET /api/skills/search?q=xxx
- 先按名称匹配，匹配到则过滤展示；匹配不到则按关键词匹配 description
- 匹配结果直接在全部技能区域展示，卡片下方显示匹配关键词

技能详情页增强（renderSkillDetail 函数扩展）：
- 左右两栏布局（左 SKILL.md 预览 + 右信息面板）
- Agent 图标 + 精选徽章
- 下载技能包按钮
- 新增点赞数 + 精选标识
- 新增所属场景标签（可跳转场景详情）

### 5.5 i18n（src/api/static/locales/{zh,en}/skills.json）

新增文案 key：
- skills.market_title（技能市场）
- skills.scene_market_title（场景市场）
- skills.featured（精选）
- skills.like（赞）
- skills.scenes_in_use（个场景在用）
- skills.repos_in_use（个仓库在用）
- skills.find_skill（查找技能）
- skills.search_placeholder（搜索技能或描述任务...）
- skills.scene_relationship_hint（场景是多个技能按开发流程组合...）
- skills.apply_scene（应用）
- skills.experience（体验）
- skills.already_experienced（已体验过，跳转中...）
- skills.scene_detail（场景详情）
- skills.skill_chain（技能链路）
- skills.recommended_template（样例模板）
- skills.repos_using（在用仓库）
- skills.download_skill（下载技能包）
- skills.sort_latest（最新）
- skills.sort_hot（最热）

### 5.6 版本号管理

改了 JS 文件后必须 bump index.html 中的 ?v=N：
- scene-market.js → v=1
- skills.js → v=16（当前 v=15）
- app.js → bump
- index.html → bump

## 六、改动文件清单

### 后端（8 个文件）

| # | 文件 | 改动 |
|---|------|------|
| 1 | src/api/db_setup.py | 新建 _SCENE_MARKET_DDL + _SKILL_LIKES_DDL + _SKILL_DDL 加 is_featured/like_count + _PIPELINE_SCENE_DDL 加 market_scene_id + _init_scene_market_data 初始化 7 条场景 + _init_skill_featured 初始化精选标记 + migration 追加 |
| 2 | src/api/routes/scenes.py | 新增 GET /market、GET /market/{id}、POST /market/{id}/apply、POST /market/{id}/experience |
| 3 | src/api/routes/skills.py | 新增 POST /{id}/like、GET /featured、GET /stats、GET /search、GET /scenarios。现有 sync-official 接口的 DB 同步逻辑移到 skill_sync.py（接口保留但不再被前端每次调用） |
| 4 | src/api/routes/templates.py | 可能微调，支持按 recommended_template 查模板详情 |
| 5 | src/api/pipeline_config.py | 新增从 scene_market 复制配置到 devops_pipeline_scene 的函数 |
| 6 | src/user/db.py | 新增点赞 CRUD 函数 + 技能统计查询函数 + scene_market CRUD 函数 |
| 7 | src/storage/skill_sync.py | 新增 _sync_skill_description_to_db()（修复用主键id更新+不碰file_path）+ start_skill_sync_scheduler 两种模式都启动 + worker 改为先 DB 同步再 OBS 同步 |
| 8 | src/api/static/img/scenes/（新建目录） | 7 个场景 SVG 缩略图 |

### 前端（6 个文件）

| # | 文件 | 改动 |
|---|------|------|
| 1 | src/api/static/index.html | 菜单结构变更 + 新增 JS 引用 + 版本号 bump |
| 2 | src/api/static/js/app.js | VIEWS 加 scene-market/scene-detail + 路由处理 |
| 3 | src/api/static/js/pages/scene-market.js（新建） | 场景市场页面+场景详情页 |
| 4 | src/api/static/js/pages/skills.js | 重写：技能市场页面（保留现有功能+新增精选/点赞/统计/场景介绍/查找） |
| 5 | src/api/static/locales/zh/skills.json | 新增文案 |
| 6 | src/api/static/locales/en/skills.json | 新增文案 |

### 测试

| # | 文件 | 改动 |
|---|------|------|
| 1 | tests/test_scene_market.py（新建） | 场景市场 API 测试（列表/详情/应用/体验） |
| 2 | tests/test_skill_likes.py（新建） | 点赞/精选/统计 API 测试 |

## 七、关键实现细节

### 7.1 场景"应用"实现

```python
# 从 scene_market 复制配置到 devops_pipeline_scene
# 1. 查 scene_market WHERE id=? 拿 nodes_json + onboarding + name
# 2. 查当前选中仓库的 repo_key
# 3. 在 devops_pipeline_scene 创建/更新 repo scope 副本：
#    - scene_id = f"market:{market_id}"（新命名，避免和 official scene_id 冲突）
#    - scope = "repo"
#    - repo_key = 当前仓库
#    - market_scene_id = market_id（记录来源）
#    - nodes_json = 从 scene_market 复制的
# 4. 调现有 apply_scene 逻辑（遍历 nodes 更新 devops_agent_session）
# 5. 更新 managed_repos.dev_scene = f"market:{market_id}"
```

### 7.2 场景"体验"去重逻辑

```python
# 检查用户是否已体验过该场景
# 查 devops_pipeline_scene WHERE market_scene_id=? AND user_id=用户id AND scope='user'
# 有记录 → 已体验，查对应仓库 full_name 返回跳转
# 无记录 → 继续 create_from_template

# 体验流程：
# 1. 从 scene_market 拿 recommended_template
# 2. 调 create_from_template（template_id=recommended_template）建项目
# 3. 把 scene_market 的配置复制到新仓库的 devops_pipeline_scene：
#    - market_scene_id = market_id
#    - nodes_json = 从 scene_market 复制的
# 4. 调 apply_scene 应用场景配置
```

### 7.3 技能使用统计查询

```python
# 被多少场景使用：
# 查 scene_market 全部场景的 nodes_json，统计 skill_name 出现次数
# 一条 SQL 无法完成（nodes_json 是 JSON），Python 侧处理

# 被多少仓库间接使用：
# 1. 查 devops_pipeline_scene WHERE market_scene_id IS NOT NULL
# 2. 关联 scene_market 的 nodes_json → 获取该场景用了哪些 skill
# 3. 统计每个 skill 被多少仓库间接使用（一个仓库用一个场景，一个场景有多个 skill）

# 实现：一次查 scene_market + devops_pipeline_scene，Python 侧聚合
# 返回 {skill_name: {scene_count: N, repo_count: N}}
```

### 7.4 技能查找实现（第一期不调 LLM）

```python
# 简单关键词匹配方案：
# 1. 用户输入 "我想自动化部署一个 Flask 应用"
# 2. 分词提取关键词：自动化/部署/Flask/应用
# 3. 遍历所有 official 技能，匹配 display_name + description + agent_type
# 4. 匹配度 = 命中关键词数 / 总关键词数
# 5. 返回匹配度 > 0 的技能，按匹配度降序
# 6. match_reason = "匹配关键词：部署、Flask"

# 第二期可改为调 LLM 做语义匹配
```

### 7.5 现有 list_skills 接口扩展

```python
# 现有 list_skills 返回的字段：
# {id, name, display_name, description, agent_type, type, scope}

# 新增返回字段（不影响现有调用方）：
# + is_featured: int
# + like_count: int
# + liked_by_me: bool（当前用户是否已赞）
# + scene_count: int（被多少场景使用，从 scene_market 统计）
# + repo_count: int（被多少仓库间接使用）
# + scenes: [{id, name}, ...]（所属场景列表，从 scene_market 查）

# 实现：list_skills 查询时 LEFT JOIN skill_likes 拿 liked_by_me，
# 使用统计单独批量查一次（不每个 skill 单独查）
```

## 八、设计原则和注意事项

1. **scene_market 表完全独立**：自包含全部场景信息，不依赖 devops_pipeline_scene。场景市场所有操作只碰 scene_market 一张表，高内聚低耦合
2. **单向关联**：devops_pipeline_scene.market_scene_id → scene_market.id，用户场景记录自己的市场来源。scene_market 不知道谁在用自己（统计时反向查）
3. **将来用户自建场景上市场**：往 scene_market 插一行（source='user', source_user_id=用户id），不影响 devops_pipeline_scene
4. **devops_pipeline_scene 不加展示字段**：is_featured/thumbnail/market_description/recommended_template 全在 scene_market 表里，devops_pipeline_scene 只加 market_scene_id 一个字段
5. **审计字段**：每张新表统一加 created_by/updated_by VARCHAR(64)，和现有系统表一致
6. **list_skills 接口兼容**：新增返回字段不影响现有调用方（agents.js 的 skill 下拉、场景中心的 skill 选择），只是多了字段
7. **skills.js 重写风险**：现有 skills.js 500+ 行包含上传/删除/转仓库/华为云包/子技能等完整逻辑，重写时必须保留所有现有功能
8. **场景体验的 create_from_template 复用**：现有 create_from_template 是 async 函数，experience_market_scene 也是 async，直接调用即可
9. **SVG 缩略图**：苹果风格渐变+图标，7 个文件，纯静态资源不影响功能
10. **DB 迁移**：2 个 ALTER + 2 个 CREATE TABLE，按现有 _migrate_tables 模式追加，幂等执行
