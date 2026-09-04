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
  scenes.py 扩展   → 场景市场 API（市场列表+详情+体验+应用+搜索+点赞）
  skills.py 扩展   → 点赞 API + 精选 API + 使用统计 API + 技能查找 API

新增数据库
  新建 scene_category 表     → 场景分类（独立管理，图标/排序/启用禁用）
  新建 scene_market 表      → 场景市场（完全独立，自包含，不和 devops_pipeline_scene 耦合）
  新建 scene_likes 表        → 场景点赞（user_id + scene_id 联合唯一）
  新建 scene_skill_ref 表    → 场景-技能关联关系（预计算，统计高性能）
  devops_pipeline_scene 加字段 → market_scene_id（记录用户场景的市场来源，单向关联）
  skill 表加字段             → is_featured, like_count
  新建 skill_likes 表         → user_id + skill_id 联合唯一
```

## 二、数据库变更

### 2.1 新建 scene_category 表（场景分类，独立管理）

```sql
CREATE TABLE IF NOT EXISTS scene_category (
    id          INT NOT NULL AUTO_INCREMENT,               -- 主键自增
    name        VARCHAR(64) NOT NULL COMMENT '分类名称（通用/移动开发/基础设施/技能开发/AI应用/内容创作）',
    slug        VARCHAR(64) DEFAULT '' COMMENT '分类slug（英文标识，如 general/mobile/iac，场景包 scene.json 的 category 用此字段引用）',
    icon        VARCHAR(32) DEFAULT '' COMMENT '分类图标（emoji）',
    order_num   INT DEFAULT 0 COMMENT '排序序号',
    is_active   INT DEFAULT 1 COMMENT '是否启用 0=否 1=是',
    created_by  VARCHAR(64) DEFAULT '' COMMENT '创建者',
    updated_by  VARCHAR(64) DEFAULT '' COMMENT '更新者',
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_name (name),
    UNIQUE KEY uk_slug (slug)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

设计原则：
- **独立管理**：分类有自己的元信息（图标、排序、启用/禁用），不依赖场景数据
- **外键引用**：scene_market.category_id → scene_category.id。场景包 scene.json 中用 category slug（如 "general"）引用分类，后端根据 slug 查 scene_category 表拿 ID。第三方不需要知道数据库 ID
- **和 agent_type 表模式一致**：agent_type 是独立表，skill 通过 agent_type 字段引用；scene_category 同样独立，scene_market 通过 category_id 引用
- **可控扩展**：新增分类走独立流程，不会因场景导入意外产生垃圾分类

初始 6 个分类数据：

| id | name | slug | icon | order_num |
|----|------|------|------|-----------|
| 1 | 通用 | general | ⚙ | 1 |
| 2 | 移动开发 | mobile | 🍃 | 2 |
| 3 | 基础设施 | iac | ☁ | 3 |
| 4 | 技能开发 | skill-dev | 🔧 | 4 |
| 5 | AI应用 | ai | 🤖 | 5 |
| 6 | 内容创作 | content | 📝 | 6 |

### 2.2 新建 scene_market 表（核心——完全独立，自包含）

```sql
CREATE TABLE IF NOT EXISTS scene_market (
    id          INT NOT NULL AUTO_INCREMENT,               -- 主键自增
    name        VARCHAR(256) NOT NULL COMMENT '场景标识（内部用，如 default / iac，仅小写字母数字连字符）',
    display_name VARCHAR(256) DEFAULT '' COMMENT '展示名称（通用开发 / IaC开发 / ...，市场卡片展示用）',
    version     VARCHAR(32) DEFAULT '1.0.0' COMMENT '版本号（语义化版本，用户贡献场景迭代用）',
    description TEXT COMMENT '简短描述',
    market_description TEXT COMMENT '市场展示用的详细功能介绍（README.md 正文解析而来，无 README.md 时取 description）',
    icon        VARCHAR(512) DEFAULT '' COMMENT '场景图标（用户上传的图片文件，OBS模式存对象key，本地模式存/static/img/scenes/xxx相对路径，空时用 scene_category.icon）',
    category_id INT DEFAULT 1 COMMENT '分类ID（外键 → scene_category.id）',
    is_featured INT DEFAULT 0 COMMENT '精选标记 0=否 1=是',
    like_count  INT DEFAULT 0 COMMENT '点赞数（冗余计数，点赞/取消时同步更新）',
    source      VARCHAR(32) DEFAULT 'official' COMMENT '来源：official/user/third_party（可扩展）',
    source_user_id INT DEFAULT 0 COMMENT 'source=user 时记录创建者用户ID',
    recommended_template VARCHAR(191) DEFAULT '' COMMENT '推荐模板ID（体验按钮用此模板创建项目）',
    nodes_json  TEXT COMMENT '技能链路配置（完整nodes，自包含不依赖外部表）',
    onboarding  VARCHAR(191) DEFAULT 'repo-onboarding' COMMENT '首次托管引导技能名',
    tags        VARCHAR(256) DEFAULT '' COMMENT '标签，逗号分隔（如 build,deploy,ci。scene.json 的 tags 数组入库时转为逗号分隔字符串，搜索时首尾补逗号 LIKE 匹配）',
    author      VARCHAR(64) DEFAULT '' COMMENT '场景包贡献者标识（official场景留空或grape-team，第三方上传时填作者名）',
    content_hash VARCHAR(64) DEFAULT '' COMMENT '内容hash（定时同步增量检测用，SHA-256）',
    obs_icon_key VARCHAR(256) DEFAULT '' COMMENT 'OBS桶icon对象key（查询时动态生成签名URL，非OBS模式为空）',
    is_active   INT DEFAULT 1 COMMENT '是否启用 0=否 1=是（定时同步下架时置0）',
    created_by  VARCHAR(64) DEFAULT '' COMMENT '创建者',
    updated_by  VARCHAR(64) DEFAULT '' COMMENT '更新者',
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_name_source (name, source, source_user_id),
    INDEX idx_category_id (category_id),
    INDEX idx_is_featured (is_featured),
    INDEX idx_source (source)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

设计原则：
- **完全独立**：scene_market 自包含全部场景信息（name/description/nodes_json/onboarding 等），不依赖 devops_pipeline_scene 表
- **高内聚低耦合**：场景市场的所有操作（CRUD/搜索/精选/展示）只碰 scene_market 一张表
- **可扩展**：source 字段区分来源（official/user/third_party），将来用户自建场景上市场只加一行
- **审计字段**：created_by/updated_by VARCHAR(64)，和现有 skill 表等系统表一致

8 个初始场景数据：

| id | name | display_name | version | category_id | recommended_template | source |
|----|------|-------------|---------|-------------|---------------------|--------|
| 1 | harmony | 鸿蒙开发 | 1.0.0 | 2（移动开发） | harmony-ecommerce | official |
| 2 | default | 通用开发 | 1.0.0 | 1（通用） | hello-flask | official |
| 3 | iac | IaC开发 | 1.0.0 | 3（基础设施） | tf-infra | official |
| 4 | skill-dev | 华为云Skill开发 | 1.0.0 | 4（技能开发） | hello-skill | official |
| 5 | cloud-native | 云原生开发 | 1.0.0 | 3（基础设施） | micro-svc | official |
| 6 | agent-dev | Agent开发 | 1.0.0 | 5（AI应用） | ai-interview-swarm | official |
| 7 | api-skill-dev | 通用Skill开发 | 1.0.0 | 4（技能开发） | hello-skill | official |
| 8 | blog-publish | 博客发布 | 1.0.0 | 6（内容创作） | (需要补一个博客模板) | official |

### 2.3 devops_pipeline_scene 表加字段（单向关联）

```sql
-- DDL（_PIPELINE_SCENE_DDL 末尾加）
ALTER TABLE devops_pipeline_scene ADD COLUMN market_scene_id INTEGER DEFAULT NULL;

-- 索引（统计在用仓库数时按 market_scene_id 查询）
CREATE INDEX idx_market_scene_id ON devops_pipeline_scene (market_scene_id);

-- migration
"ALTER TABLE devops_pipeline_scene ADD COLUMN market_scene_id INTEGER DEFAULT NULL",
"CREATE INDEX IF NOT EXISTS idx_market_scene_id ON devops_pipeline_scene (market_scene_id)",
```

- market_scene_id：记录该用户场景是从市场哪个场景复制来的（指向 scene_market.id）
- 用户自建场景不走市场时 market_scene_id=NULL
- 统计"在用仓库数"：COUNT devops_pipeline_scene WHERE market_scene_id=?
- **devops_pipeline_scene 不加任何展示字段**（is_featured/icon 等全在 scene_market 表里）

数据流：

```
scene_market（市场，独立完整，自包含）
  id=1  name=通用开发  nodes=[vod→issue→coding→qa→sre]  recommended_template=hello-flask  ...
    ↓ 用户点"应用"
    ↓ 从 scene_market 复制配置到用户场景表（scope='user'）
devops_pipeline_scene（用户场景配置，记录来源）
  market_scene_id=1  scope=user  scene_id=custom-{uuid}  ← 从市场 id=1 复制来的（用户级，所有仓库共享）
  market_scene_id=NULL  scope=user  scene_id=custom-{uuid}  ← 用户在场景中心自建的（不走市场）
```

设计要点：
- 市场应用（apply）和体验（experience）都创建 **scope='user'** 的场景，与场景中心创建自定义场景行为一致
- scene_id 用 custom-{uuid}，不用 scene_market.name，避免与 official 场景自动 fork 的记录冲突（唯一键 scene_id+scope+repo_key+user_id）
- apply_scene 只更新当前仓库的 devops_agent_session（agent 绑定是仓库级的），场景配置本身是用户级的

### 2.4 skill 表加字段

```sql
-- DDL（_SKILL_DDL 末尾加）
ALTER TABLE skill ADD COLUMN is_featured INTEGER DEFAULT 0;
ALTER TABLE skill ADD COLUMN like_count INTEGER DEFAULT 0;

-- 索引（精选技能查询用）
CREATE INDEX idx_skill_is_featured ON skill (is_featured);

-- migration
"ALTER TABLE skill ADD COLUMN is_featured INTEGER DEFAULT 0",
"ALTER TABLE skill ADD COLUMN like_count INTEGER DEFAULT 0",
"CREATE INDEX IF NOT EXISTS idx_skill_is_featured ON skill (is_featured)",
```

- is_featured：精选标记，0=否 1=是，仅 official 技能会设为 1
- like_count：点赞冗余计数，默认 0，点赞/取消时同步更新

### 2.5 新建 skill_likes 表

```sql
CREATE TABLE IF NOT EXISTS skill_likes (
    id          INT NOT NULL AUTO_INCREMENT,               -- 主键自增
    user_id     INT NOT NULL COMMENT '用户ID',
    skill_id    INT NOT NULL COMMENT '技能ID',
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_user_skill (user_id, skill_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

- user_id + skill_id 联合唯一，防重复点赞
- 点赞：INSERT，同时 UPDATE skill SET like_count=like_count+1
- 取消：DELETE，同时 UPDATE skill SET like_count=like_count-1

### 2.6 新建 scene_likes 表

```sql
CREATE TABLE IF NOT EXISTS scene_likes (
    id          INT NOT NULL AUTO_INCREMENT,               -- 主键自增
    user_id     INT NOT NULL COMMENT '用户ID',
    scene_id    INT NOT NULL COMMENT '场景市场ID（scene_market.id）',
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_user_scene (user_id, scene_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

- user_id + scene_id 联合唯一，防重复点赞
- 点赞：INSERT，同时 UPDATE scene_market SET like_count=like_count+1
- 取消：DELETE，同时 UPDATE scene_market SET like_count=like_count-1

### 2.7 新建 scene_skill_ref 表（场景-技能关联关系）

```sql
CREATE TABLE IF NOT EXISTS scene_skill_ref (
    id          INT NOT NULL AUTO_INCREMENT,               -- 主键自增
    scene_id    INT NOT NULL COMMENT '场景市场ID（scene_market.id）',
    skill_id    INT DEFAULT 0 COMMENT '技能ID（指向 skill.id，以 ID 为准）',
    skill_name  VARCHAR(256) NOT NULL COMMENT '技能名（指向 skill.skill_name，冗余便于统计）',
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_scene_skill (scene_id, skill_name),
    INDEX idx_skill_name (skill_name),
    INDEX idx_skill_id (skill_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

设计原则：
- **预计算关联表**：场景初始化/上传时解析 nodes_json，拆出每个 skill_name + 查 skill 表拿 skill_id，一起写入此表，避免每次统计都解析 JSON
- **以 skill_id 为准**：skill_name 是冗余字段（便于统计聚合），跳转和关联以 skill_id 为准。初始化时查 skill 表填入 skill_id；已有数据 skill_id=0 时启动回填
- **统计高性能**：技能使用统计直接 SQL 聚合，不解析 JSON 字符串（2026-09-04 改为按 skill_id 关联）
  - 被多少场景使用 + 被多少仓库间接使用（一条 LEFT JOIN SQL）：SELECT sr.skill_id, COUNT(DISTINCT sr.scene_id) AS scene_count, COUNT(DISTINCT d.id) AS repo_count FROM scene_skill_ref sr LEFT JOIN devops_pipeline_scene d ON sr.scene_id = d.market_scene_id WHERE sr.skill_id IN (...) GROUP BY sr.skill_id
- **场景变更时同步**：场景 nodes_json 变更时（初始化/上传/同步），先 DELETE WHERE scene_id=? 再重新 INSERT
- **和 skill 表的 skill_name 关联**：不存 skill_id，因为 nodes_json 里用的是 skill_name（如 "implementation"），直接对应
- 与"在用仓库数"维度互补：点赞=主观认可（好不好用），在用仓库数=客观使用量（多少团队在用）

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

初始化方式：db_setup.py 里写一个 _init_scene_market_data() 函数，建表后检查是否已有数据，无数据则 INSERT 8 条。有数据则跳过（幂等）。

### 3.3 scene_skill_ref 初始数据

scene_skill_ref 表的初始数据在 scene_market 初始化后同步写入：

```python
def _init_scene_skill_ref():
    """初始化场景-技能关联关系。
    遍历 scene_market 全部场景的 nodes_json，拆出每个 skill_name 写入。
    幂等：已有数据则跳过。
    """
    # 1. 查 scene_market 全部记录的 id + nodes_json
    # 2. 对每条记录：解析 nodes_json，提取每个 node 的 skill 字段
    # 3. INSERT INTO scene_skill_ref (scene_id, skill_name) VALUES (?, ?)
    #    跳过已存在的（UNIQUE 约束）
```

同步时机：scene_market 数据变更时（初始化/上传/同步），先 DELETE WHERE scene_id=? 再重新 INSERT。

### 3.4 场景图标

场景图标有两种来源：
1. **用户上传**：场景包 ZIP 里放 icon.png / icon.svg / icon.jpg，解压后存储到 OBS 或本地
2. **分类默认**：用户没上传图标时，用 scene_category 表的 icon 字段（emoji）作为默认图标

存储模式和技能包一致——通过 `get_storage()` 判断 OBS 是否启用，两种模式自动兼容：

**兼容机制**（复用现有 storage 层）：

```python
def resolve_scene_icon(icon: str, category_id: int) -> str:
    """解析场景图标为可访问的 URL 或 emoji。
    icon 非空 → 用户上传的图片（OBS 签名 URL 或本地路径）。
    icon 为空 → 查 scene_category 表拿分类 emoji 图标。
    """
    if icon:
        storage = get_storage()
        if storage is not None and not icon.startswith('/static/'):
            # OBS 模式：icon 是对象 key，签发临时签名 URL
            return storage.temporary_url(icon)
        # 本地模式：icon 是 /static/xxx 相对路径，直接返回
        return icon
    # icon 为空，用分类默认图标（emoji）
    cat = get_scene_category(category_id)
    return cat.get('icon', '⚙') if cat else '⚙'
```

- **系统预置场景**：icon 存 OBS 对象 key 或本地 /static/img/scenes/xxx 路径
- **用户上传场景**：
  - 本地模式：保存到 src/api/static/img/scenes/ 目录，icon 存相对路径
  - OBS 模式：上传到 OBS，icon 存对象 key（scenes/custom/{user_id}/{scene_name}.png）
- **图标为空时**：用 scene_category.icon（emoji）兜底，保证每个场景都有图标
- **集群兼容**：OBS 模式下所有实例通过 OBS 临时签名 URL 访问，天然共享
- **前端无感知**：后端返回场景列表时统一调 resolve_scene_icon，前端拿到的都是可直接展示的

系统预置场景用 SVG 生成初始图标（苹果风格渐变背景 + 场景图标 + 场景名称）。

## 四、后端 API 设计

### 4.1 场景市场 API（src/api/routes/scenes.py 扩展）

#### GET /api/scenes/market/categories

返回场景分类列表（从 scene_category 表读取，前端动态渲染分类 tab）。

```python
@router.get("/market/categories")
def list_scene_categories(user: dict = Depends(get_current_user)):
    """场景分类列表：按 order_num 排序，只返回 is_active=1 的"""
    # SELECT id, name, slug, icon, order_num FROM scene_category WHERE is_active=1 ORDER BY order_num
    # 返回 [{id, name, slug, icon, order_num}]
    # 注：分类禁用（is_active=0）时不显示 tab，但该分类下的场景仍在"全部"里展示
    # slug 供场景包 scene.json 的 category 字段引用参考
```

#### GET /api/scenes/market

返回场景市场列表（从 scene_market 表读取，完全独立）。

```python
@router.get("/market")
def list_scenes_market(user: dict = Depends(get_current_user), q: str = "", category_id: int = 0, sort: str = "latest"):
    """场景市场列表：精选场景 + 全部场景，支持搜索 + 分类过滤 + 排序"""
    # 1. 查 scene_market WHERE is_active=1
    # 2. 搜索：q 非空时按 name/market_description 过滤
    # 3. 分类过滤：category_id>0 时 WHERE category_id=?
    # 4. 排序：
    #    - latest（默认）：按 created_at 降序
    #    - hot：按 like_count 降序
    #    - 精选场景始终置顶（is_featured=1 排在前面）
    # 5. 统计在用仓库数：查 devops_pipeline_scene WHERE market_scene_id=sm.id GROUP BY
    # 6. 解析 nodes_json 拿技能名称列表
    # 7. 返回 [{id, name, display_name, version, market_description, icon, category_id, is_featured, like_count, liked_by_me,
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
    # 5. 返回 {id, name, display_name, version, market_description, icon, category_id, category_name, is_featured, like_count, liked_by_me,
    #         skills: [{name, display_name, agent_type, role_desc}],
    #         template: {id, name, description, language, stars},
    #         repos: [{repo_key, full_name}]}
```

#### POST /api/scenes/market/{market_id}/apply

应用场景：从 scene_market 复制配置到用户级 devops_pipeline_scene（scope='user'），与场景中心创建自定义场景一致。

```python
@router.post("/market/{market_id}/apply")
def apply_market_scene(market_id: int, user: dict = Depends(get_current_user),
                       owner: str = "", repo: str = ""):
    """应用场景：从市场复制配置到仓库级 devops_pipeline_scene（scope='repo'）。
    - scope='repo', repo_key=当前仓库, scene_id=custom-{uuid}
    - 幂等：同一仓库同一市场场景不重复创建（按 market_scene_id + repo_key 查重）
    - 同时调 apply_scene 更新当前仓库的 agent 配置
    """

#### POST /api/scenes/market/{market_id}/experience

体验场景：基于场景关联的推荐模板创建/复用项目并应用场景，场景配置复制到用户级 devops_pipeline_scene（scope='user'）。

前端交互流程：
1. 点击"体验"→ 弹出进度弹框（6 步定时器推进：检查环境→创建仓库→下载模板→推送代码→托管→应用场景）
2. API 返回成功（无论新建还是复用已有仓库）→ 800ms 后自动关闭弹框，跳转到仓库场景中心
3. API 返回失败 → 显示错误信息，按钮变为"关闭"
4. 请求超时 300 秒（体验流程含建仓库+推代码+托管，较长）

后端逻辑：
1. 查 scene_market 拿 recommended_template + nodes_json + onboarding
2. 拿模板的 default_repo_name + 用户的 gitcode_login
3. 查 user_repos 里是否已有 {gitcode_login}/{default_repo_name}
   - 已有 → 检查是否已托管（managed_repos），未托管则调 host_repo 托管
   - 没有 → 调 create_from_template 创建+托管
4. 创建/复用用户级 devops_pipeline_scene（scope='user', scene_id=custom-{uuid}）
   - 幂等：同一用户同一市场场景只创建一条 user scope 记录，已存在则更新
5. 调 apply_scene 应用场景配置到仓库的 agent_session
6. 返回 {full_name, scene_id, already_exists, message}

```python
@router.post("/market/{market_id}/experience")
async def experience_market_scene(market_id: int, user: dict = Depends(get_current_user)):
    """体验场景：基于场景关联的推荐模板创建/复用项目并应用场景。
    - 创建 scope='repo' 的 devops_pipeline_scene，repo_key=体验创建的仓库
    - 幂等：同一仓库同一市场场景不重复创建（按 market_scene_id + repo_key 查重）
    """
```

#### POST /api/scenes/market/{market_id}/like

场景点赞/取消点赞（toggle）。

```python
@router.post("/market/{market_id}/like")
def toggle_scene_like(market_id: int, user: dict = Depends(get_current_user)):
    """场景点赞 toggle：已赞→取消，未赞→点赞"""
    # 在 db_lock 事务内执行，保证 INSERT/DELETE + UPDATE like_count 原子性
    # 1. 查 scene_likes WHERE user_id=? AND scene_id=?
    # 2. 已赞 → DELETE + UPDATE scene_market SET like_count=like_count-1
    # 3. 未赞 → INSERT + UPDATE scene_market SET like_count=like_count+1
    # 4. 返回 {liked: true/false, like_count: N}
```

### 4.2 技能市场 API（src/api/routes/skills.py 扩展）

#### POST /api/skills/{skill_id}/like

点赞/取消点赞（toggle）。

```python
@router.post("/{skill_id}/like")
def toggle_like(skill_id: int, user: dict = Depends(get_current_user)):
    """点赞 toggle：已赞→取消，未赞→点赞"""
    # 在 db_lock 事务内执行，保证 INSERT/DELETE + UPDATE like_count 原子性
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
    """精选技能：is_featured=1 的 official 技能，按 like_count 降序。
    补充 scope/scenes/scene_count/repo_count/liked_by_me 字段（与 /skills 接口对齐）。
    """
```

#### GET /api/skills/stats

技能使用统计（被多少场景使用 + 被多少仓库间接使用）。

```python
@router.get("/stats")
def get_skill_stats(user: dict = Depends(get_current_user)):
    """技能使用统计（通过 scene_skill_ref 关联表 SQL 聚合）"""
    # 1. 被多少场景使用：SELECT skill_name, COUNT(DISTINCT scene_id) FROM scene_skill_ref GROUP BY skill_name
    # 2. 被多少仓库间接使用：SELECT sr.skill_name, COUNT(*) FROM scene_skill_ref sr
    #    JOIN devops_pipeline_scene d ON sr.scene_id = d.market_scene_id WHERE d.market_scene_id IS NOT NULL GROUP BY sr.skill_name
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

### 4.3 sync-official 接口保留，内部逻辑重构

sync-official 接口（POST /skills/sync-official）保留，但前端不再每次进页面调用。DB 同步逻辑从 skills.py 移到 skill_sync.py，修复 3 个问题（见 §5.4）。定时任务两种模式都启动（OBS 模式 DB+OBS 同步，非 OBS 模式只做 DB 同步）。

skill 表的 is_featured 不通过 sync-official 同步，通过 db_setup.py 初始化。

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

nav 函数权限处理：
- scene-market 和 scene-detail 加入"不需要选仓库也能访问"白名单（和 repos/settings/tasks/runtimes/observability 一样）
- 现有 nav 函数里 `if (!repo && view !== 'repos' && view !== 'settings' ...)` 需要加 `&& view !== 'scene-market' && view !== 'scene-detail'`
- 但 scene-market 仍需登录（get_current_user 鉴权）

场景详情技能跳转技能详情：
- `app.nav('skill', skill_id)` — skill_id 从 scene_skill_ref 或 skill 表查

技能详情场景跳转场景详情：
- `app.nav('scene-detail', market_id)` — market_id 从 scenes 返回字段里拿

apply 接口传参：
- 前端调 POST /api/scenes/market/{market_id}/apply 时，通过 header 或 body 传当前仓库的 owner + repo
- 和现有 apply_scene 一致（路径参数 /{owner}/{repo}）

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
  - 注意：原型 market-prototype-v2.html 中技能名称跳转未正常工作，实现时需修复
- 推荐模板（名称、描述、语言、stars）
- 在用仓库列表
- [应用] [体验] 按钮

"应用"操作：调 POST /api/scenes/market/{market_id}/apply
"体验"操作：调 POST /api/scenes/market/{market_id}/experience

前端交互状态：
- 应用/体验按钮：点击后显示 loading 状态（按钮禁用+加载动画），成功后 toast 提示，失败后 toast 报错
- 体验按钮：recommended_template 为空时禁用（灰显+tooltip"该场景暂无体验模板"）
- 点赞按钮：点击后立即 toggle 图标（❤️↔🤍）+ 数字+1/-1，后台异步调 like 接口，失败回滚
- 场景市场需要登录才能访问（get_current_user 鉴权）
- 场景市场不要求选仓库也能浏览（和场景中心不同，场景中心需要先选仓库）

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
| 1 | src/api/db_setup.py | 新建 _SCENE_CATEGORY_DDL + _SCENE_MARKET_DDL（含 category_id/like_count/display_name/version）+ _SCENE_LIKES_DDL + _SCENE_SKILL_REF_DDL + _SKILL_LIKES_DDL + _SKILL_DDL 加 is_featured/like_count + _PIPELINE_SCENE_DDL 加 market_scene_id + _init_scene_category_data 初始化 6 条分类 + _init_scene_market_data 初始化 8 条场景 + _init_scene_skill_ref 初始化关联关系 + _init_skill_featured 初始化精选标记 + migration 追加 |
| 2 | src/api/routes/scenes.py | 新增 GET /market/categories、GET /market、GET /market/{id}、POST /market/{id}/apply、POST /market/{id}/experience、POST /market/{id}/like |
| 3 | src/api/routes/skills.py | 新增 POST /{id}/like、GET /featured、GET /stats、GET /search。现有 sync-official 接口的 DB 同步逻辑移到 skill_sync.py（接口保留但不再被前端每次调用）。现有 DELETE /skills/{skill_name} 改为 DELETE /skills/by-id/{skill_id}（按主键 id 删除，scope 从数据库读实际值不信任前端参数，新增 official 不允许删除校验） |
| 4 | src/api/routes/templates.py | 可能微调，支持按 recommended_template 查模板详情 |
| 5 | src/api/pipeline_config.py | 新增从 scene_market 复制配置到 devops_pipeline_scene 的函数 |
| 6 | src/user/db.py | 新增点赞 CRUD 函数 + 技能统计查询函数 + scene_market CRUD 函数 + scene_category CRUD 函数 |
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
# 1. 查 scene_market WHERE id=? 拿 name + nodes_json + onboarding
# 2. 查当前选中仓库的 repo_key
# 3. 幂等：先查 devops_pipeline_scene WHERE market_scene_id=? AND repo_key=? AND scope='repo'
#    - 已存在 → 更新 nodes_json/onboarding（同一仓库重复应用同一场景，更新不新建）
#    - 不存在 → 新建，scene_id = f"custom-{uuid.uuid4().hex[:8]}"（UUID 唯一标识，引擎内部用）
#      和现有 create_custom_scene 逻辑一致（scenes.py:156）
# 4. 以上操作在 db_lock 事务内执行，保证原子性
# 5. 调现有 apply_scene 逻辑（遍历 nodes 更新 devops_agent_session）：
#    对每个 node（如 vod→issue→coding→qa→sre）：
#    - agent_type = node.tag（如 "coding"）
#    - skill = node.skill（如 "implementation"）
#    - 查 skill_id（skill 表的主键 id）
#    - 查/分配 runtime_id（引擎运行时 ID，从现有 devops_agent_session 读或调 resolve_default_engine）
#    - upsert_agent_session：写入 repo_key + agent_type + runtime_id + skill + skill_id
#    - 跳过 enabled=false 的 node
# 6. 更新 managed_repos.dev_scene = scene_id
# 7. 返回 {message: "场景已应用"}
# 注：scene_id 是引擎内部唯一标识，不展示给用户，用户看到的是 scene_market.display_name
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
# 通过 scene_skill_ref 关联表直接 SQL 聚合，按 skill_id 关联（2026-09-04 改）

# 一条 SQL 同时拿 scene_count + repo_count（按本页 skill_ids 过滤）：
# SELECT sr.skill_id,
#        COUNT(DISTINCT sr.scene_id) AS scene_count,
#        COUNT(DISTINCT d.id) AS repo_count
# FROM scene_skill_ref sr
# LEFT JOIN devops_pipeline_scene d ON sr.scene_id = d.market_scene_id
# WHERE sr.skill_id IN (?, ?, ...)
# GROUP BY sr.skill_id

# 合并为 {skill_id: {scene_count: N, repo_count: N}}
# list_skills 查出技能列表后，用这个字典按 skill_id 补字段，不每个技能单独查
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
# + scene_count: int（被多少场景使用，从 scene_skill_ref 统计）
# + repo_count: int（被多少仓库间接使用，从 scene_skill_ref + devops_pipeline_scene 统计）
# + scenes: [{id, name, display_name}, ...]（所属场景列表，从 scene_skill_ref JOIN scene_market 查）

# 实现：list_skills 查询时 LEFT JOIN skill_likes 拿 liked_by_me，
# 使用统计单独批量查一次 scene_skill_ref（不每个 skill 单独查）
# 所属场景列表单独批量查一次 scene_skill_ref JOIN scene_market

### 7.6 现有 get_skill_detail 接口扩展（GET /skills/by-id/{skill_id}）

```python
# 现有返回字段：{id, name, display_name, description, agent_type, scope, content, is_obs}

# 新增返回字段：
# + is_featured: int
# + like_count: int
# + liked_by_me: bool
# + scenes: [{id, name, display_name}, ...]（所属场景列表，从 scene_skill_ref JOIN scene_market 查）
#   技能详情页展示"所属场景"标签，点击可跳转场景详情
```
# 使用统计单独批量查一次（不每个 skill 单独查）
```

## 八、场景导入（后续迭代）

一期不做场景上传/导入。official 场景通过 db_setup.py 初始化，后续迭代会有场景同步定时任务从官方仓库同步。
以下设计保留供后续迭代参考。

参考 skill 导入模式（ZIP 包），支持外部用户和第三方贡献场景。
场景包采用 **JSON 定义 + Markdown 描述分离** 的双文件格式：scene.json 负责结构化数据（解析和校验的唯一来源），README.md 负责对外展示文案（可选，富文本渲染）。

### 8.1 场景包格式

ZIP 包结构：
```
my-custom-scene.zip
└── my-custom-scene/
    ├── scene.json        ← 场景定义文件（必须，JSON 格式，结构化数据的唯一来源）
    ├── README.md         ← 场景展示文案（可选，Markdown 正文，存入 market_description）
    ├── icon.png          ← 场景图标图片（可选，支持 png/svg/jpg）
    └── references/       ← 可选，详细文档目录（按需加载展示，不入数据库）
        ├── flow-detail.md
        └── config-guide.md
```

**设计原则：数据与展示分离**
- scene.json：所有结构化数据，程序解析和校验的唯一来源
- README.md：纯展示内容，正文存入 scene_market.market_description，前端渲染时 parse Markdown。可选——未提供时 market_description 取 scene.json 的 description 字段
- references/：详细文档目录（借鉴 skill 包的 references 子目录），README.md 中可引用，前端按需加载展示。不入数据库，不强制
- 第三方程序生成场景包时只需序列化 scene.json，README.md 和 references/ 可以不写

**为什么选择 JSON 而非 YAML frontmatter（SCENE.md 方案）：**
1. 场景包核心是 nodes 链路定义（id/tag/skill/mode/next/handoff），纯结构化数据，JSON 表达更准确紧凑
2. 第三方程序生成——从已有 DevOps 系统导出流程定义直接序列化为 JSON，无需处理 YAML 缩进
3. JSON Schema 可做严格结构校验 + IDE 自动补全，YAML 缩进错误难排查
4. json.load 一步解析，无需 split("---") + yaml.safe_load 两步
5. 数据与展示分离——JSON 只管数据结构，Markdown 只管展示文案，互不干扰

**借鉴 skill 包的设计：**
- version 字段：skill 包用 `version: 1.0.0` 做版本追踪，场景包同样需要——场景包升级覆盖时可做版本比对
- references/ 子目录：skill 包用 references/ 放外部引用文档（如 API 参考、配置说明），技能执行时按需加载。场景包同样支持 references/ 目录放详细文档
- author 字段：标识场景包贡献者，第三方上传时知道来源
- tags 字段：补充 category_id 的不足，一个场景可同时属于多个标签（如 build + deploy），前端搜索时按标签匹配

scene.json 格式：
```json
{
  "name": "my-custom-scene",
  "version": "1.0.0",
  "display_name": "我的自定义场景",
  "description": "解决 xxx 问题的开发场景",
  "category": "general",
  "recommended_template": "",
  "onboarding": "repo-onboarding",
  "tags": ["build", "deploy"],
  "author": "zhangsan",
  "nodes": [
    {"id": "vod", "tag": "vod", "skill": "vod", "mode": "emit", "next": "issue", "handoff": "a2a"},
    {"id": "issue", "tag": "issue", "skill": "issue-advance", "mode": "emit", "next": "coding", "handoff": "a2a"},
    {"id": "coding", "tag": "coding", "skill": "implementation", "mode": "emit", "next": "qa", "handoff": "a2a"},
    {"id": "qa", "tag": "qa", "skill": "pr", "mode": "emit", "next": "sre", "handoff": "a2a"},
    {"id": "sre", "tag": "sre", "skill": "cloud-deploy", "mode": "emit", "next": "done", "handoff": "complete"}
  ]
}
```

> **技能清单参考**：nodes 里的 skill 字段值必须对应 grape-skills 仓库（https://gitcode.com/grape-dev/grape-skills）中已有的技能。可用技能清单见仓库根目录 `skill-description.json`，按 `agent_type/skill_name` 路径查找。例如 `issue-advance` 对应 `skills/issue/issue-advance/`。

scene.json 字段说明：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| name | string | 是 | 仅小写字母数字连字符，必须和 ZIP 内目录名一致 |
| version | string | 是 | 语义化版本号（如 1.0.0），场景包升级覆盖时做版本比对 |
| display_name | string | 是 | 展示名称，不能为空 |
| description | string | 是 | 简短描述，不超过 500 字。无 README.md 时作为 market_description |
| category | string | 是 | 分类名称（slug，如 "general"/"harmony"/"iac"），入库时后端根据 slug 查 scene_category 表拿 ID。第三方不需要知道数据库 ID |
| recommended_template | string | 否 | 关联模板 ID，空字符串表示无。仅 official 场景使用，第三方场景包留空 |
| onboarding | string | 否 | 引导技能名，必须在 skill 表中存在 |
| tags | string[] | 否 | 标签数组（如 ["build", "deploy"]），补充分类，用于搜索和筛选。scene.json 里用 JSON 数组格式（第三方生成方便），入库时转为逗号分隔字符串存储（如 "build,deploy"），搜索时首尾补逗号 `LIKE '%,build,%'` 精确匹配 |
| author | string | 否 | 作者标识，第三方上传时标识贡献者。official 场景留空或写 "grape-team" |
| nodes | array | 是 | 场景节点数组，至少 2 个节点 |

nodes 节点字段说明：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| id | string | 是 | 节点唯一标识，同一场景内不能重复 |
| tag | string | 是 | Agent 类型，决定此节点由哪个 Agent 执行。合法值：vod / issue / coding / qa / sre / chat。tag 与 skill 的关系：tag 决定"谁来执行"（Agent 类型），skill 决定"执行什么"（具体技能）。同一个 Agent 类型可以执行不同技能，如 tag=vod 可对应 skill=vod / iac-vod / blog-vod |
| skill | string | 是 | 技能名，必须在 grape-skills 仓库中存在。参考 skill-description.json 获取完整技能清单 |
| mode | string | 是 | 执行模式。合法值：emit（当前仅支持 emit） |
| next | string | 是 | 下一节点 id，最后一个节点为 "done" |
| handoff | string | 是 | 交接方式：a2a（节点间 A2A 交接）/ complete（最后一个节点，流程完成） |

nodes 枚举值参考：

**mode**
| 值 | 说明 |
|------|------|
| emit | 触发执行模式，节点被激活后执行技能并产出结果。当前仅支持此值 |

**next 取值规则**
- 填下一个节点的 id（如 "issue"），当前节点完成后跳转到该节点
- 最后一个节点填 "done" 表示流程结束
- 必须形成完整链路：从第一个节点出发沿着 next 能走到 done，不能断链、不能循环

**handoff**
| 值 | 适用位置 | 说明 |
|------|---------|------|
| a2a | 链路中间节点 | Agent-to-Agent 交接，当前节点完成后通过 A2A 协议将上下文传递给下一个节点 |
| complete | 最后一个节点 | 流程完成，表示场景执行结束。仅 next="done" 的节点使用 |

**tag 合法值**
| 值 | Agent | 职责 |
|------|-------|------|
| vod | VOD Agent | 开发者声音分析，需求收集与澄清 |
| issue | Issue Agent | Issue 运营，需求拆分与分配 |
| coding | Coding Agent | 代码实现 |
| qa | QA Agent | 代码审查与质量验证 |
| sre | SRE Agent | 构建部署与运维 |
| chat | Chat Agent | 对话与 IM 通知 |

**完整节点示例（带注释）：**
```json
{
  "id": "vod",           // 节点唯一标识，同一场景内不能重复
  "tag": "vod",          // Agent类型：此节点由 VOD Agent 执行
  "skill": "vod",        // 技能名：执行 vod 技能（开发者声音分析）
  "mode": "emit",        // 执行模式：emit
  "next": "issue",       // 下一节点：完成后交给 id=issue 的节点
  "handoff": "a2a"       // 交接方式：a2a=节点间A2A交接，complete=最后一个节点流程完成
}
```

README.md 格式（可选）：
```markdown
# 我的自定义场景

这里写场景的详细介绍，会作为 market_description 展示在场景市场。
支持 **Markdown** 语法，包括适用场景、解决的问题、开发流程等。
```

### 8.2 校验规则

```python
def validate_scene(upload_dir: str, scene_name: str) -> list[str]:
    """校验自定义场景，返回错误列表。空列表表示通过。"""
    errors = []

    # 1. 检查 scene.json 是否存在
    scene_path = os.path.join(upload_dir, "scene.json")
    if not os.path.exists(scene_path):
        return ["缺少 scene.json 文件"]

    # 2. 解析 JSON（json.load 一步完成，无需 split + yaml.safe_load）
    # 3. 校验必填字段：
    #    - name：仅允许小写字母数字连字符，必须和目录名一致
    #    - version：语义化版本号格式（X.Y.Z），必填
    #    - display_name：不能为空
    #    - description：不能为空，不超过 500 字
    #    - category：分类 slug，必须在 scene_category 表中存在（按 slug 查）
    #    - nodes：数组非空，至少 2 个节点
    # 4. 校验 nodes：
    #    - 每个 node 的 tag 必须是合法值：vod / issue / coding / qa / sre / chat
    #    - 每个 node 的 skill 字段必须在 skill 表中存在（official 或当前用户可访问的）
    #    - 每个 node 的 mode 必须是合法值：emit（当前仅支持）
    #    - next 链路完整性：从第一个节点出发能走到 done，不能断链、不能循环
    #    - handoff 字段合法值：a2a / complete
    #    - handoff 和 next 一致性：handoff=complete 时 next 必须为 "done"；handoff=a2a 时 next 不能为 "done"
    # 5. 校验 onboarding 字段的技能名必须在 skill 表中存在
    # 6. 校验 tags（如果存在）：必须是字符串数组，每个标签不超过 20 字，总长度不超过 256 字符
    #     入库时转为逗号分隔字符串（如 "build,deploy"），非 JSON
    # 7. 校验 scene.json 文件大小不超过 1MB
    # 8. 校验 README.md 文件（如果存在）：不超过 1MB，解析为 market_description
    # 9. 校验 references/ 目录（如果存在）：每个 .md 文件不超过 1MB
    # 10. 校验图标文件（如果存在）：icon.png / icon.svg / icon.jpg，不超过 2MB
    # 11. 禁止可执行文件（复用 skill 的 FORBIDDEN_EXTS 校验）

    return errors
```

### 8.3 上传流程（复用 skill upload 模式）

```python
@router.post("/market/upload")
async def upload_scene(file: UploadFile = File(...), user: dict = Depends(get_current_user)):
    """上传场景 ZIP 包。"""
    # 1. 读取文件，大小限制 10MB
    # 2. 解压 ZIP（zip 炸弹防护，复用 skill 的 MAX_EXTRACT_BYTES）
    # 3. 取第一个真实目录（过滤 __MACOSX/.DS_Store，和 skill 一样）
    # 4. validate_scene 校验 scene.json
    # 5. 解析 scene.json → 拿 name/version/display_name/description/category/nodes/onboarding/recommended_template/tags/author
    # 6. 解析 README.md（如果存在）→ 作为 market_description；无 README.md 时 market_description 取 description
    # 7. 图标处理：
    #    - 有 icon 文件 → 存储到 OBS/本地，icon 存对象 key 或路径
    #    - 无 icon 文件 → icon 字段为空，展示时用 scene_category.icon 兜底
    # 8. 根据 scene.json 的 category（分类名称 slug）查 scene_category 表拿 category_id
    # 9. scene_market 表写一行（幂等：同 name + source + source_user_id 视为更新）：
    #    - 先查 SELECT id, version FROM scene_market WHERE name=? AND source='user' AND source_user_id=?
    #    - 已存在 → 比对 version：新版本 > 旧版本时更新（覆盖 display_name/description/market_description/
    #      category_id/nodes_json/onboarding/icon/tags/author/version），版本相同或更低时返回提示"版本未更新"
    #    - 不存在 → INSERT 新行
    #    - source='user', source_user_id=用户id
    #    - name/version/display_name/description/market_description/category_id/nodes_json/onboarding/icon/tags/author
    #    - is_featured=0（用户上传的不自动精选）
    # 10. 解析 nodes_json 拆出每个 skill_name，写入 scene_skill_ref 表（先 DELETE WHERE scene_id=? 再 INSERT）
    # 11. 返回 {message: "场景 xxx 上传成功"}
```

### 8.4 和技能导入的关键区别

| 维度 | 技能导入 | 场景导入 |
|------|---------|---------|
| 定义文件 | SKILL.md（YAML frontmatter + Markdown 正文，单文件自包含） | scene.json（结构化数据）+ README.md（展示文案，可选），数据与展示分离 |
| 图标来源 | agent_type 表（不自己定义） | 用户上传 icon 文件，空时用分类 emoji |
| 外部依赖 | 无（自包含） | 有（nodes 引用 skill 表的技能名，需校验存在） |
| 文件存储 | 需存 OBS/本地（有脚本文件） | 只有图标文件需存储（scene.json 数据入 DB） |
| 重打包 | 需要（_normalize_skill_zip） | 不需要 |
| 校验重点 | frontmatter 格式 + 文件类型 | nodes 链路完整性 + skill 引用合法性 + 图标文件 |
| 精选标记 | 通过 db_setup.py 初始化 | 用户上传的 is_featured=0，不自动精选 |
| 解析方式 | split("---") + yaml.safe_load 两步 | json.load 一步 |
| version 追踪 | SKILL.md frontmatter version 字段 | scene.json version 字段，上传时做版本比对 |
| tags | 无 | scene.json tags 数组，入库转逗号分隔字符串 |
| author | 无 | scene.json author 字段，标识贡献者 |
| 展示文案 | SKILL.md 正文 | README.md（可选），数据与展示分离 |

### 8.5 场景包下载模板

提供场景模板 ZIP 下载（和技能模板一样），让用户参考格式：

```
scene-template.zip
└── scene-template/
    ├── scene.json        ← 模板示例（带完整字段）
    ├── README.md         ← 模板示例（Markdown 展示文案）
    └── icon.png          ← 示例图标
```

### 8.6 官方场景定时同步

**触发方式**：服务启动后 asyncio 定时任务，启动即执行一次，之后每 30 分钟同步一次。和 skill_sync_scheduler 模式完全对齐。

**同步源**：grape-skills 仓库（https://gitcode.com/grape-dev/grape-skills）dev 分支的 scenes/ 目录。

**拉取方式**：GitCode API v5 逐文件下载（`/api/v5/repos/{owner}/{repo}/contents/{path}?ref={branch}`）。用 bot token 认证，列 scenes/ 下子目录，每个子目录再列文件，下载 scene.json + README.md + icon 文件到临时目录。

> 为什么选 API v5 逐文件下载而非 ZIP 归档下载：
> - GitCode 的 ZIP 归档 URL（`/-/archive/{ref}/{repo}-{ref}.zip`）返回 HTML 页面而非 ZIP 文件，无法直接下载
> - API v5 支持 `access_token` 参数认证，bot token 可直接使用
> - 场景数量少（当前 8 个），每个场景 2-3 个文件，总请求数 20+ 完全可接受
> - 不依赖 git 命令（生产环境 Docker 容器不装 git）

**token 获取**：优先级：环境变量 `GRAPE_DEV_TOKEN` → `configs/scene-sync.json` 的 `gitcode_api_token` → `configs/global-bot-token.json` 的 `gitcode_api_token`。三个都没有时打 WARNING 跳过同步。不查 managed_repos 表。

**仓库配置**：`configs/scene-sync.json`（模板见 `configs/scene-sync.example.json`，实际文件在 .gitignore 里不提交）。配置项：repo_url（仓库地址）、repo_branch（同步分支）、sync_interval（同步间隔秒数，默认 1800）、gitcode_api_token（GitCode API token，私有仓库认证用）。环境变量 `GRAPESKILLS_REPO_URL` / `GRAPESKILLS_REPO_BRANCH` / `SCENE_SYNC_INTERVAL` / `GRAPE_DEV_TOKEN` 优先覆盖配置文件。

**同步流程**：

```python
async def sync_official_scenes():
    """定时同步官方场景从 grape-skills 仓库到 scene_market 表。"""
    # 0. 获取 token：优先 GRAPE_DEV_TOKEN 环境变量 → configs/scene-sync.json → configs/global-bot-token.json
    #    - 三个都没有时打 WARNING 日志，return（不中断服务）
    # 1. API v5 列 scenes/ 目录（GET /api/v5/repos/{owner}/{repo}/contents/scenes?ref=dev）
    #    - 过滤 _template 和 third-party
    # 2. 逐场景目录：API v5 列文件 → 下载 scene.json / README.md / icon.* 到临时目录
    #    - 递归下载 references/ 子目录全部文件（深度限制 5 层）
    # 3. 遍历下载到本地的场景目录：
    #    a. 读 scene.json → 解析 + 校验
    #       - 校验 tag 合法值（vod/issue/coding/qa/sre/chat）
    #       - 校验 mode 合法值（emit）
    #       - 校验 handoff/next 一致性（complete→next="done"，a2a→next≠"done"）
    #       - 校验 next 链路完整性（从首节点能走到 done，不断链不循环）
    #       - 预检 skill 引用：每个 node 的 skill 必须在 skill 表中存在（按 skill_name 查）
    #         → 不存在打 WARNING 日志"场景 X 引用了不存在的技能 Y"，跳过此场景
    #    b. 读 README.md（如有）→ market_description；无则取 description
    #    c. 读 icon 文件（如有）→ OBS 模式上传到 OBS 桶（grape/scenes/icons/{name}{ext}），obs_icon_key 存对象 key（查询时 _resolve_scene_icon 动态生成签名 URL）；非 OBS 模式 copy 到 src/api/static/img/scenes/，icon 存 /static/img/scenes/{name}{ext} 路径
    #       无 icon 文件 → icon 字段为空，前端展示用 scene_category.icon 兜底
    #    d. 计算场景目录的 content_hash（SHA-256，遍历所有文件按文件名排序）
    #    e. 根据 scene.json 的 category（slug）查 scene_category 表拿 category_id
    #    f. 查 DB：SELECT id, version, content_hash FROM scene_market WHERE name=? AND source='official'
    #       - 不存在 → INSERT 新行（source='official', is_featured=0）
    #       - 存在且 content_hash 相同 → 跳过（无变更）
    #       - 存在但 content_hash 不同：
    #         → version 升了（新 version > 旧 version）→ UPDATE（正常更新）
    #         → version 没升 → 打 WARNING 日志"场景 X 内容变更但版本号未更新"，仍然 UPDATE
    #    g. 更新 scene_skill_ref 表（先 DELETE WHERE scene_id=? 再 INSERT，含 skill_id）
    # 4. 处理下架：DB 中 source='official' 且 is_active=1 但本次仓库 scenes/ 里不存在的场景
    #    → is_active 置 0（软删除/下架），不硬删（保留 like_count 等历史数据）
    #    → 打 INFO 日志"场景 X 已从官方仓库移除，下架处理"
    # 5. 打 INFO 日志总结：新增 N 条、更新 N 条、跳过 N 条、下架 N 条、失败 N 条
    # 6. 初始化精选场景：如果 official 场景没有任何 is_featured=1 的，设置默认精选（default/cloud-native/iac）
    #    - 只在首次（没有任何精选时）才设置，不覆盖管理员手动设的精选
    # 7. 清理临时目录
```

**content_hash 计算**（复用 skill_sync 的 _compute_skill_hash 模式）：
```python
def _compute_scene_hash(scene_dir: str) -> str:
    """计算场景目录的内容 hash（遍历所有文件，按文件名排序，取完整 SHA-256）。"""
    h = hashlib.sha256()
    for root, dirs, files in os.walk(scene_dir):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", "__MACOSX", ".git")]
        for f in sorted(files):
            rel = os.path.relpath(os.path.join(root, f), scene_dir)
            h.update(rel.encode())
            with open(os.path.join(root, f), "rb") as fp:
                h.update(fp.read())
    return h.hexdigest()
```

**关键设计点**：
- **content_hash 做增量同步**：hash 不变就跳过，避免无谓 DB 写入
- **version 比对**：hash 变了但 version 没升 → 打 WARNING 日志提醒，仍然更新（内容优先于版本号）
- **skill 引用预检**：同步前校验 scene.json 的 nodes 里每个 skill 是否在 skill 表中存在，不存在打 WARNING 跳过此场景
- **只同步 source='official'**：不动 user/third-party 的场景
- **_template 和 third-party 目录不同步**
- **下架处理**：仓库中不存在的 official 场景，DB 里 is_active 置 0（软删除），不硬删保留历史数据
- **单个场景失败不中断整体**：try/catch 每个场景，失败打 WARNING 继续下一个
- **同步失败不阻塞服务**：启动时 try/catch，失败只打 WARNING 日志
- **优雅关闭**：lifespan shutdown 时 cancel 定时任务（和 skill_sync_scheduler 对齐）
- **token 获取**：优先级：环境变量 `GRAPE_DEV_TOKEN` → `configs/scene-sync.json` 的 `gitcode_api_token` → `configs/global-bot-token.json` 的 `gitcode_api_token`。三个都没有时打 WARNING 跳过同步。不查 managed_repos 表。
- **skill_id 回填**：更新 scene_skill_ref 时查 skill 表按 skill_name 拿 skill_id 一并写入
- **默认精选场景**：同步完成后检查 official 场景是否有 is_featured=1 的，如果没有任何精选场景，把 default（通用开发）、cloud-native（云原生开发）、iac（IaC开发）设为精选。只在首次（没有任何精选时）才设置，不覆盖管理员手动设的精选

**scene_market 表新增 content_hash 字段**：
```sql
ALTER TABLE scene_market ADD COLUMN content_hash VARCHAR(64) DEFAULT '';
ALTER TABLE scene_market ADD COLUMN obs_icon_key VARCHAR(256) DEFAULT '';
```

**启动与关闭**（main.py lifespan，和 skill_sync_scheduler 并列）：
```python
# 官方场景定时同步：每 30 分钟从 grape-skills 仓库同步场景到 DB
try:
    from src.storage.scene_sync import start_scene_sync_scheduler
    start_scene_sync_scheduler()
except Exception as e:
    log.warning(f"Scene sync scheduler start failed: {e}")

# shutdown
try:
    from src.storage.scene_sync import stop_scene_sync_scheduler
    await stop_scene_sync_scheduler()
except Exception:
    pass
```

**新建文件**：`src/storage/scene_sync.py`
- `_load_config()` — 加载配置（优先环境变量，回退 configs/scene-sync.json）
- `_get_sync_token()` — 获取 GitCode token（优先级：GRAPE_DEV_TOKEN 环境变量 → configs/scene-sync.json → configs/global-bot-token.json）
- `_api_list_dir(client, path, token)` — GitCode API v5 列目录
- `_api_get_file(client, path, token)` — GitCode API v5 下载文件（base64 解码）
- `_download_dir_recursive(client, api_path, local_path, token, is_references, depth)` — 递归下载目录（根目录只下载 scene.json/README.md/icon.*，references/ 下全部下载，深度限制 5 层）
- `_compute_scene_hash(scene_dir)` — 计算 content_hash
- `_validate_scene_nodes(nodes)` — 校验 nodes 合法性（tag/mode/handoff/next）
- `_check_skills_exist(nodes, scene_name)` — 预检 skill 引用是否在 skill 表中存在
- `_parse_version(v)` — 语义化版本号转可比较元组（补齐到 3 位）
- `_refresh_cat_cache()` — 刷新 category slug → id 缓存
- `sync_official_scenes()` — 核心同步逻辑
- `_scene_sync_worker(interval)` — 定时 worker（启动即执行，之后每 interval 秒）
- `start_scene_sync_scheduler()` — 启动调度
- `stop_scene_sync_scheduler()` — 优雅关闭

## 九、实现迭代记录（代码已落地）

### 9.1 技能详情智能返回（2026-09-04）

技能详情页返回按钮根据来源页决定返回目标，不固定返回技能列表：
- 技能列表 → 技能详情 → 返回技能列表（_backToSkillList 直接渲染不 push history）
- 场景详情 → 技能详情 → 返回场景详情（renderSceneDetail + replaceState 更新 history.state）
- 无来源（直接 URL 进入）→ 兜底 app.nav('scene-market')
- 华为云技能包和子技能详情页使用同样逻辑（_backFromSkillDetail / _backFromSubSkill）

关键点：返回场景详情后用 history.replaceState 更新当前历史记录，否则连续两次进入技能详情时来源判断错误。

### 9.2 Agent tab 固定显示（2026-09-04）

_buildAgentGroups 不再 filter 掉 count=0 的 agent。6 个 Agent tab（VOD/Issue/Coding/QA/SRE/Chat）固定显示，count=0 时不显示数字徽章。

### 9.3 卡片描述截断 + 底部对齐（2026-09-04）

场景卡片（.scn-card-desc）和技能卡片（.rc .rd）描述统一 2 行截断（-webkit-line-clamp:2），hover title 看完整描述。
卡片用 flex column 布局（.scn-card / .scn-card-body / .scn-card-foot margin-top:auto），所有卡片高度一致、底部对齐。

### 9.4 技能卡片场景展示优化（2026-09-04）

- 非官方技能（scope !== 'official'）不显示"所属场景"行和"场景在用/仓库在用"统计行
- 场景超过 2 个时只显示前 2 个 + (+N)，hover title 看全部
- scene_count=0 且 repo_count=0 时不显示统计行

### 9.5 /featured 接口字段补全（2026-09-04）

精选技能接口（GET /api/skills/featured）补充 scope/scenes/scene_count/repo_count 字段，与 /skills 接口对齐。前端精选卡片现在能正确显示所属场景和使用统计。

### 9.6 场景-技能关联改用 skill_id（2026-09-04）

scene_skill_ref 表已有 skill_id 字段但代码全用 skill_name 关联。改为以 skill_id 为准：
- get_skill_usage_stats(skill_ids) — 按本页 skill_ids 过滤，不再全表聚合；两条 SQL 合并为一条 LEFT JOIN
- batch_skill_scenes(skill_ids) — WHERE skill_id IN(...)
- get_skill_scenes(skill_id) — WHERE skill_id=?
- /skills 和 /featured 接口传 skill_ids 而不是 skill_names

### 9.7 技能详情页场景展开 + 点赞同步（2026-09-04）

- 所属场景超过 3 个时只显示前 3 个 + (+N) 可点击展开
- 非官方技能不显示所属场景行
- 点赞后顶部按钮和右侧信息栏同步更新（detailInfoLikes id 联动）

### 9.8 上传按钮对齐 + 模板下载修复（2026-09-04）

- 上传技能按钮去掉 margin-left:8px，和其他按钮统一 flex gap 间距
- 下载技能模板 404 修复：创建 skills/_template/ 目录（含标准 SKILL.md + references/ + scripts/）

### 9.9 apply/experience 改为 repo scope + 脏数据清理（2026-09-04）

**问题**：apply 和 experience 接口创建 scope='user' 的场景配置，repo_key 写空字符串。user scope 是用户级配置，同一用户对同一市场场景只保留一条记录，不同仓库应用会覆盖不新建。导致：
- "在用仓库数"统计不准（user scope 行 repo_key 为空被 COUNT 算进去）
- 无法区分哪些仓库实际应用了场景

**脏数据清理**：执行 `UPDATE devops_pipeline_scene SET is_active=0 WHERE scope='user' AND market_scene_id IS NOT NULL`，软删除 5 条 user scope 脏数据。

**改动**：
- apply 接口：幂等查重从 `market_scene_id + user_id + scope='user'` 改为 `market_scene_id + repo_key + scope='repo'`。INSERT 时 scope='repo'，repo_key 写实际仓库。
- experience 接口：同样改为 scope='repo'，repo_key 写体验创建的仓库。
- 统计函数：去掉 `repo_key!=''` 过滤（脏数据已清理，user scope 行已 is_active=0）。
- 不影响：场景中心手动创建自定义场景（仍 user scope）、fork_official_scene（托管自动 fork，仍 repo scope）。
- 幂等：同一仓库重复应用同一场景更新不新建，不同仓库各自独立记录。

### 9.10 在用仓库列表权限标记 + 分页展示（2026-09-04）

- 后端 `get_scene_market_detail` 返回 repos 时标记 `accessible: True/False`（当前用户是否有权限访问该仓库，通过 get_user_repos 集合匹配）
- 前端：accessible 的仓库可点击跳转 GitCode，不可访问的显示"无权限"且不可点击
- 超过 6 个仓库时默认显示前 6 个，"查看全部(N)"按钮展开剩余

### 9.11 样例项目模板来源标识（2026-09-04）

- git 模板（source='git' + source_repo 有值）显示"GitCode 远程"标识 + "查看项目 ↗"按钮可跳转
- local 模板显示"本地模板"标识，不可跳转
- 文案从"样例模板"改为"样例项目模板"

### 9.12 搜索框文案优化（2026-09-04）

- 场景市场搜索框："搜索场景或技能..." → "搜索场景名称或描述"（场景市场只搜场景，不搜技能）
- 技能市场搜索框："搜索技能或描述任务..." → "搜索技能名称或描述"（实际是关键词匹配，不是 AI 推荐）

### 9.13 场景详情页去掉在用仓库列表（2026-09-04）

- 场景详情页删除"在用仓库"列表区块（原 det-suite-card 网格）
- 头部"X 仓库在用"计数和卡片列表的计数保留

### 9.14 Apple 风格分段控件 + 卡片布局美化（2026-09-04）

- 场景/技能 tab 改为 Apple segmented control（.seg-bar）：灰色容器 + 白色活跃药丸 + 阴影
- 技能市场/仓库技能/个人技能 3 个子 tab 同样改为药丸形式
- 去掉旧的 .stabs 下划线 tab 样式（.seg-bar 替代）
- 顶部 tab + 提示文案用白色卡片包裹（圆角 + 柔和阴影）
- 精选和全部（场景/技能）各自用独立 .apple-card 卡片（无边框 + 柔和阴影）
- 搜索/排序按钮靠左对齐（justify-content:flex-start）
- 搜索输入框用浅灰背景融入式设计（.apple-search）

### 9.15 从技能详情返回时样式统一（2026-09-04）

- skills.js 的 _backToSkillList() 原来硬编码了旧的 .stabs + 提示框模板
- 改为复用 _renderMarketTab('skills')，确保返回时和首次进入的布局一致

### 9.16 应用/体验前确认提示（2026-09-04）

新增后端接口：
- GET /api/scenes/market/{id}/applied — 检查场景是否已应用到当前仓库
- GET /api/scenes/market/{id}/experience-check — 检查体验项目仓库是否存在 + 是否已应用场景

前端逻辑（列表卡片 + 详情页共用）：
- **应用按钮**：点击前先调 /applied 检查。已应用过 → 弹确认框"确认覆盖应用 / 当前仓库已应用过此场景（场景名），再次应用将覆盖更新已有的场景配置"。未应用过 → 直接执行
- **体验按钮**：点击前先调 /experience-check 检查：
  - 项目已存在且已应用过此场景 → 弹确认框"确认覆盖场景配置 / 项目 X 已存在且已应用过此场景，再次体验将覆盖已有的场景配置"
  - 项目已存在但未应用过 → 弹提示框"项目已存在 / 项目 X 已存在，体验将在此项目上应用场景配置"
  - 全新项目 → 直接体验
- 检查接口失败时不阻塞，直接执行原流程

i18n 新增 key（zh + en）：
- apply_confirm_title / apply_confirm_desc / apply_confirm_warning / apply_confirm_btn
- exp_confirm_overwrite_title / exp_confirm_overwrite_desc / exp_confirm_overwrite_warning
- exp_confirm_exists_title / exp_confirm_exists_desc / exp_confirm_exists_warning
- exp_confirm_btn

## 十、设计原则和注意事项

1. **scene_market 表完全独立**：自包含全部场景信息，不依赖 devops_pipeline_scene。场景市场所有操作只碰 scene_market 一张表，高内聚低耦合
2. **单向关联**：devops_pipeline_scene.market_scene_id → scene_market.id，用户场景记录自己的市场来源。scene_market 不知道谁在用自己（统计时反向查）
3. **将来用户自建场景上市场**：往 scene_market 插一行（source='user', source_user_id=用户id），不影响 devops_pipeline_scene
4. **devops_pipeline_scene 不加展示字段**：is_featured/icon/market_description/recommended_template 全在 scene_market 表里，devops_pipeline_scene 只加 market_scene_id 一个字段
5. **审计字段**：每张新表统一加 created_by/updated_by VARCHAR(64)，和现有系统表一致
6. **list_skills 接口兼容**：新增返回字段不影响现有调用方（agents.js 的 skill 下拉、场景中心的 skill 选择），只是多了字段
7. **skills.js 重写风险**：现有 skills.js 500+ 行包含上传/删除/转仓库/华为云包/子技能等完整逻辑，重写时必须保留所有现有功能
8. **场景体验的 create_from_template 复用**：现有 create_from_template 是 async 函数，experience_market_scene 也是 async，直接调用即可
9. **SVG 缩略图**：苹果风格渐变+图标，7 个文件，纯静态资源不影响功能
10. **DB 迁移**：5 个 CREATE TABLE（scene_category + scene_market + scene_likes + scene_skill_ref + skill_likes）+ 多个 ALTER（scene_market 加 tags/author/content_hash、scene_category 加 slug、devops_pipeline_scene 加 market_scene_id、scene_skill_ref 加 skill_id、skill 加 is_featured/like_count），按现有 _migrate_tables 模式追加，幂等执行
