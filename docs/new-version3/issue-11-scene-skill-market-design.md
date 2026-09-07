# Issue #11：重新设计场景&技能页面

> 分支：feat/devops-skills-redesign（基于 weekly-01 e8090390）
> 关联 Issue：grape-dev/grape#11
> 状态：需求讨论中

## 一、背景

当前 devops skills 页面采用平铺方式罗列技能，开发者不知道哪些技能好、能解决什么问题，查阅困难。同时缺少场景化介绍，用户无法了解技能组合的使用场景。

参考 WorkBuddy 的技能市场和专家中心，希望增加精选、推荐、场景介绍等能力，让开发者更容易发现和使用技能。

## 二、现状分析

### 2.1 现有技能页面

- 文件：src/api/static/js/pages/skills.js
- 3 个页签：官方技能 / 仓库技能 / 个人技能
- 按 agent_type 分组（vod/issue/coding/qa/sre/chat），每组内平铺卡片网格
- 每张卡片：图标 + 名称 + scope 标签 + 描述 + 查看/删除/转仓库按钮
- 搜索框按名称/agent_type 过滤
- 38 个官方技能 + 2 仓库技能 + 2 个人技能

问题：
1. 平铺罗列，无精选/推荐机制，开发者无法判断技能质量
2. 缺乏点赞/使用反馈，无法体现技能受欢迎程度
3. 无场景组合介绍，用户不知道哪些技能可以组合使用
4. 查阅困难，只有名称和一句话描述，缺乏决策信息

### 2.2 现有场景中心

- 文件：src/api/routes/scenes.py + src/api/static/js/pages/dashboard.js
- 定位：操作面，用户对已托管仓库配置/编辑/应用场景
- 数据：repo/user scope 的场景（devops_pipeline_scene 表）
- 7 个 official 场景（通用开发/云原生/IaC/Agent开发/Skill开发/博客发布）
- 每个 official 场景包含 5 个 agent 节点（vod→issue→coding→qa→sre），每节点关联一个 skill

### 2.3 现有模板系统

- 文件：src/api/routes/templates.py
- configs/project-templates.json：8 个分类，173 个模板（含 GitCode/GitHub 优选项目）
- featured_templates 表：从 GitCode/GitHub 抓取的优选项目（目前为空）
- POST /api/project-templates/create：一键创建（建仓库→写代码→托管→应用场景→onboarding）

### 2.4 菜单结构（当前）

```
工作区：任务中心 / 场景中心 / 流水线
DevOps Agents：VOD / Issue / Coding / QA / SRE
知识库：DevOps 技能 / 项目知识库
用户中心：Chat Agent
设置：通用设置 / GitCode 仓库 / Agent 运行时 / 系统监控
```

## 三、需求设计

### 3.1 菜单调整

侧边栏新增一级菜单"技能市场"，从"知识库"组中移出技能（技能不是知识，是可执行能力包）。

调整后：
```
工作区：任务中心 / 场景中心 / 流水线
DevOps Agents：VOD / Issue / Coding / QA / SRE
技能市场：场景市场 / 技能市场
知识库：项目知识库
用户中心：Chat Agent
设置：通用设置 / GitCode 仓库 / Agent 运行时 / 系统监控
```

说明：
- "技能市场"作为一级菜单，下设"场景市场"和"技能市场"两个子页面
- 原"知识库"组只保留"项目知识库"
- 原"场景中心"（工作区）不变，职责是仓库级场景配置

### 3.2 场景市场页面

定位：发现面，浏览官方场景、了解功能、一键应用/体验。

与现有"场景中心"的区别：

| 维度 | 场景中心（现有） | 场景市场（新） |
|------|-----------------|---------------|
| 目的 | 对已托管仓库配置场景 | 浏览/发现官方场景 |
| 数据范围 | repo/user scope | official scope |
| 用户阶段 | 已有仓库 | 可能没有仓库 |
| 操作 | 编辑/应用场景 | 查看/应用/体验 |

#### 3.2.1 场景卡片设计

每个场景卡片包含：
- 顶部色条：场景分类的渐变色标识
- 场景图标：emoji 标识
- 场景名称：通用开发 / 云原生开发 / IaC开发 等
- 功能介绍：场景详细描述（扩充现有 description）
- 技能链路：列出场景用到的技能名称（VOD → Issue → Coding → QA → SRE）
- 技能数 + 在用仓库数（如"5 技能 · 5 仓库在用"）
- 点赞按钮 + 数量（❤️ 图标 + 数字，点击 toggle）
- 精选徽章：标星场景显示 🌟 徽章
- "应用"按钮：将场景应用到当前选中仓库
- "体验"按钮：基于该场景关联的模板项目，一键创建+托管+应用场景+进入项目

#### 3.2.2 场景分类 tab

全部场景区域上方有分类 tab，按场景类型过滤：
- 全部 / 移动开发 / 通用 / 基础设施 / 技能开发 / AI 应用 / 内容创作

#### 3.2.3 场景搜索

搜索框 + 查询按钮，按场景名称/功能介绍过滤。

#### 3.2.4 "应用"功能

- 前提：用户已选中一个已托管仓库
- 行为：调用现有 POST /api/scenes/apply/{owner}/{repo}，将该场景应用到当前仓库
- 未选仓库时：提示"请先在仓库管理页选择一个仓库"

#### 3.2.5 "体验"功能

- 行为：基于场景关联的最佳实践模板，一键创建项目
  1. 查该场景关联的模板列表（从 project-templates.json 按 scene_id 过滤）
  2. 取推荐的模板（第一个或标记为 recommended 的）
  3. 检查当前用户是否已体验过该场景（查 managed_repos 中 dev_scene=该场景 且该用户创建的仓库）
  4. 已体验 → 直接跳转到那个项目
  5. 未体验 → 调用 /api/project-templates/create 建项目 → 跳转到新项目
- 限制：每个用户每个场景只能体验一次，已体验过的直接跳转

#### 3.2.6 场景详情页

点击场景卡片进入场景详情页：
- 场景大图标 + 名称 + 精选徽章 + 完整功能介绍
- 执行流程图：横向卡片 + 箭头展示技能链路，每个技能卡片含编号、Agent 标签、技能名称、角色描述、输入/输出/产出
  - 技能名称可点击跳转到技能市场对应技能详情
- 技能套件：纵向列表展示每个技能的编号、Agent 标签、名称、角色描述、输入/输出/产出
  - 技能名称可点击跳转到技能市场对应技能详情
- 样例模板：关联的最佳实践模板项目（名称、描述、语言、stars）
- 在用仓库列表
- "应用""体验"按钮

#### 3.2.7 场景精选

- 在 configs/agent-skill-mapping.json（或新建 configs/scene-market.json）中配置 `"featured": true`
- 同步到 devops_pipeline_scene 表（新增 is_featured 字段）
- 精选场景在市场页面置顶展示（横向滚动），带 🌟 徽章
- 场景点赞和在用仓库数维度互补：点赞=主观认可（好不好用），在用仓库数=客观使用量（多少团队在用）

### 3.3 技能市场页面

定位：技能市场化展示，精选推荐 + 点赞 + 场景关联。

#### 3.3.1 技能卡片增强

现有卡片：图标 + 名称 + scope标签 + 描述 + 查看/删除按钮

增强为：
- Agent 图标（卡片标题前，复用 _AGENT_TYPES 配色：VOD 🔍、Issue 📋、Coding 💻、QA 🧪、SRE ⚙️）
- agent_type 标签（彩色）
- 精选徽章（🌟，is_featured=true 的技能）
- 描述（最多 2 行截断，hover 看完整）
- 所属场景（仅官方技能显示，超过 2 个显示前 2 个 + (+N)）
- 使用统计（仅官方技能且 scene_count>0 或 repo_count>0 时显示）
- 点赞按钮 + 数量（❤️ 12，点击 toggle，详情页顶部和信息栏同步更新）
- 查看详情按钮

卡片布局：flex column，高度一致，底部对齐。
Agent tab：6 个固定显示（VOD/Issue/Coding/QA/SRE/Chat），count=0 隐藏数字不隐藏 tab。

#### 3.3.2 精选推荐

- 来源：configs/skill-description.json 中加 `"featured": true` 字段
- 同步机制：复用现有 /api/skills/sync-official 流程，sync 时写入 skill 表 is_featured 字段
- 精选技能置顶展示，独立"精选技能"区域（横向滚动）

#### 3.3.3 点赞

- 每个登录用户可对每个技能点赞/取消点赞（toggle）
- 防重复：skill_likes 表（user_id + skill_id 联合主键）
- skill 表冗余 like_count 字段，高性能读取
- 点赞数展示在卡片上，按点赞数排序精选区域

#### 3.3.4 技能查找

- 入口：搜索框旁边的"查找技能"按钮（复用搜索框，不单独弹窗）
- 用户在搜索框输入任务描述，点击"查找技能"按钮
  - 输入能匹配技能名称时 → 按名称过滤（和普通搜索效果一致）
  - 匹配不到名称时 → 按任务描述关键词匹配技能 description，推荐相关技能
- 匹配结果直接在全部技能区域展示，每个技能卡片下方显示匹配关键词

#### 3.3.5 技能详情页

- 左右两栏布局：左栏 SKILL.md 预览，右栏技能信息面板
- 技能名称、Agent 图标、scope 标签、精选徽章
- 描述
- SKILL.md 全文内容
- 下载技能包按钮（OBS 模式返回临时签名 URL）
- 点赞按钮 + 数量（顶部按钮和右侧信息栏同步更新）
- 所属场景标签：仅官方技能显示，超过 3 个显示前 3 个 + (+N) 可点击展开
- 返回按钮：智能返回来源页（技能列表 / 场景详情）

### 3.4 页面布局

"DevOps 市场"一级菜单点击后默认进入"场景"页面，顶部 tab 可切换到"技能"。

#### 场景市场页面布局

```
┌───────────────────────────────────────────────────┐
│  [场景] [技能]  tab 切换                             │
├───────────────────────────────────────────────────┤
│                                                     │
│  🌟 精选场景（横向滚动）                             │
│  ┌─────────┐ ┌─────────┐                          │
│  │色条+图标 │ │色条+图标 │                          │
│  │ 通用开发 │ │ IaC开发  │  (置顶)                  │
│  │ 功能介绍 │ │ 功能介绍 │                          │
│  │ VOD→...  │ │ VOD→...  │                          │
│  │ 5技能·3用│ │ 5技能·5用│                          │
│  │[应用][体验]│ │[应用][体验]│                       │
│  └─────────┘ └─────────┘                          │
│                                                     │
│  全部场景          [搜索框] [查询]                   │
│  [全部][移动][通用][基础设施][技能][AI][内容]        │
│  ┌─────────┐ ┌─────────┐ ┌─────────┐              │
│  │ 云原生   │ │ Agent   │ │ 博客发布 │  ...         │
│  └─────────┘ └─────────┘ └─────────┘              │
│                                                     │
└───────────────────────────────────────────────────┘
```

#### 技能市场页面布局

```
┌───────────────────────────────────────────────────┐
│  [场景] [技能]  tab 切换                             │
│  [技能市场][仓库技能][个人技能]                      │
│  💡 场景是多个技能按流程组合的开发模式...（提示框）   │
├───────────────────────────────────────────────────┤
│                                                     │
│  🌟 精选技能（横向滚动）                             │
│  ┌────┐ ┌────┐ ┌────┐                              │
│  │🔍VOD│ │💻Cod│ │🔍VOD│  (精选 + 点赞数排序)       │
│  │❤️ 12│ │❤️ 8 │ │❤️ 5 │                              │
│  └────┘ └────┘ └────┘                              │
│                                                     │
│  全部技能   [搜索] [查找技能] [最新][最热] [上传]    │
│  [全部(14)] [🔍VOD(2)] [📋Issue(2)] [💻Coding(3)]  │
│  [🧪QA(3)] [⚙️SRE(3)]                              │
│  ┌────┐ ┌────┐ ┌────┐                              │
│  │卡片│ │卡片│ │卡片│                              │
│  └────┘ └────┘ └────┘                              │
│                                                     │
└───────────────────────────────────────────────────┘
```

## 四、改动范围

### 4.1 后端

| 文件/模块 | 改动 |
|-----------|------|
| src/api/routes/scenes.py | 新增 GET /api/scenes/market（official 场景列表+模板数+在用数+精选标记） |
| src/api/routes/skills.py | 新增 POST /api/skills/{id}/like（点赞 toggle）、GET /api/skills/featured（精选列表） |
| src/api/routes/templates.py | 可能微调，支持按 scene_id 查模板列表 |
| src/user/db.py | skill 表 DDL 变更（is_featured, like_count）、新建 skill_likes 表 |
| src/storage/skill_sync.py | sync 时同步 featured 字段 |
| configs/skill-description.json | 部分技能加 `"featured": true` |
| configs/agent-skill-mapping.json 或新建 configs/scene-market.json | 场景加 featured 标记 + 扩充 description |
| 数据库迁移 | skill 表加字段 + devops_pipeline_scene 加字段 + 新建 skill_likes 表 |

### 4.2 前端

| 文件 | 改动 |
|------|------|
| src/api/static/index.html | 菜单结构调整（新增"技能市场"一级菜单，移除知识库下的"DevOps 技能"） |
| src/api/static/js/pages/skills.js | 重写为技能市场页面（卡片增强+精选+点赞） |
| 新建 src/api/static/js/pages/scene-market.js | 场景市场页面 |
| src/api/static/js/app.js | 路由注册新页面 |
| src/api/static/locales/zh/skills.json + en/skills.json | 补充文案 |
| src/api/static/img/scenes/（可选） | 场景缩略图静态资源 |

### 4.3 数据库变更

```sql
-- 新建 scene_category 表（场景分类，独立管理）
CREATE TABLE scene_category (
  id INT NOT NULL AUTO_INCREMENT,
  name VARCHAR(64) NOT NULL COMMENT '分类名称',
  icon VARCHAR(32) DEFAULT '' COMMENT '分类图标（emoji）',
  order_num INT DEFAULT 0 COMMENT '排序序号',
  is_active INT DEFAULT 1 COMMENT '是否启用 0否 1是',
  created_by VARCHAR(64) DEFAULT '' COMMENT '创建者',
  updated_by VARCHAR(64) DEFAULT '' COMMENT '更新者',
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_name (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 新建 scene_market 表（场景市场，完全独立，自包含）
CREATE TABLE scene_market (
  id INT NOT NULL AUTO_INCREMENT,
  name VARCHAR(256) NOT NULL COMMENT '场景标识（内部用）',
  display_name VARCHAR(256) DEFAULT '' COMMENT '展示名称',
  version VARCHAR(32) DEFAULT '1.0.0' COMMENT '版本号',
  description TEXT COMMENT '简短描述',
  market_description TEXT COMMENT '市场展示详细功能介绍',
  icon VARCHAR(512) DEFAULT '' COMMENT '场景图标',
  category_id INT DEFAULT 1 COMMENT '分类ID（外键→scene_category.id）',
  is_featured INT DEFAULT 0 COMMENT '精选标记 0否 1是',
  like_count INT DEFAULT 0 COMMENT '点赞数（冗余计数）',
  source VARCHAR(32) DEFAULT 'official' COMMENT '来源：official/user/third_party',
  source_user_id INT DEFAULT 0 COMMENT 'source=user时记录创建者用户ID',
  recommended_template VARCHAR(191) DEFAULT '' COMMENT '推荐模板ID（体验按钮用）',
  nodes_json TEXT COMMENT '技能链路配置（完整nodes，自包含）',
  onboarding VARCHAR(191) DEFAULT 'repo-onboarding' COMMENT '首次托管引导技能名',
  is_active INT DEFAULT 1 COMMENT '是否启用 0否 1是',
  created_by VARCHAR(64) DEFAULT '' COMMENT '创建者',
  updated_by VARCHAR(64) DEFAULT '' COMMENT '更新者',
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_name_source (name, source, source_user_id),
  INDEX idx_category_id (category_id),
  INDEX idx_is_featured (is_featured),
  INDEX idx_source (source)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- devops_pipeline_scene 表加字段
ALTER TABLE devops_pipeline_scene ADD COLUMN market_scene_id INTEGER DEFAULT NULL;
CREATE INDEX idx_market_scene_id ON devops_pipeline_scene (market_scene_id);

-- skill 表加字段
ALTER TABLE skill ADD COLUMN is_featured INT DEFAULT 0 COMMENT '精选标记 0否 1是';
ALTER TABLE skill ADD COLUMN like_count INT DEFAULT 0 COMMENT '点赞数（冗余计数）';
CREATE INDEX idx_skill_is_featured ON skill (is_featured);

-- 新建 skill_likes 表
CREATE TABLE skill_likes (
  id INT NOT NULL AUTO_INCREMENT,
  user_id INT NOT NULL COMMENT '用户ID',
  skill_id INT NOT NULL COMMENT '技能ID',
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_user_skill (user_id, skill_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 新建 scene_likes 表
CREATE TABLE scene_likes (
  id INT NOT NULL AUTO_INCREMENT,
  user_id INT NOT NULL COMMENT '用户ID',
  scene_id INT NOT NULL COMMENT '场景市场ID（scene_market.id）',
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_user_scene (user_id, scene_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 新建 scene_skill_ref 表（场景-技能关联关系，预计算统计用）
CREATE TABLE scene_skill_ref (
  id INT NOT NULL AUTO_INCREMENT,
  scene_id INT NOT NULL COMMENT '场景市场ID（scene_market.id）',
  skill_name VARCHAR(256) NOT NULL COMMENT '技能名（指向 skill.skill_name）',
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_scene_skill (scene_id, skill_name),
  INDEX idx_skill_name (skill_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

## 五、确认点（已全部确认）

1. ✅ "体验"功能：按 用户+场景 去重（一个场景只能体验一次），已体验直接跳转
2. ✅ 场景缩略图：实现阶段用 SVG 生成（矢量图，苹果风格渐变+图标）
3. ✅ 精选标记：初始推荐一批，精选范围仅官方技能
4. ✅ 场景市场+技能市场第一期全做
5. ✅ 场景的 description 扩充：基于现有场景数据（nodes 中的 skill 列表）自动生成
6. ✅ 场景分类 tab：7 个分类（全部/移动开发/通用/基础设施/技能开发/AI应用/内容创作）
7. ✅ 技能排序：最新/最热
8. ✅ 技能卡片 Agent 图标：复用 _AGENT_TYPES 配色
9. ✅ 技能组合场景介绍：取消（场景市场已覆盖）
10. ✅ 原型文件：src/api/static/market-prototype-v2.html
11. ✅ 场景点赞：需要（与在用仓库数维度互补）
