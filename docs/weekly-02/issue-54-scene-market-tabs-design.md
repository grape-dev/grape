# Issue #54 完整方案：场景页双子Tab + scene_sync ZIP 下载优化

> 日期：2026-09-07（初版）/ 2026-09-08（修订：移除个人场景/导入导出/编排页模板，改用 Select2 技能选择）
> 关联 Issue：GitCode #54 — 支持创建仓库场景，场景页参照技能页双子Tab结构
> 关联优化：scene_sync.py 从逐文件 API 下载改为 ZIP 归档下载
> 分支：feat/issue-54-scene-market-tabs（从 weekly-02 拉取）
> MR 目标：grape-dev/weekly-02

---

## 一、代码基线分析

### 1.1 技能页三子Tab实现（参照模板）

文件：`src/api/static/js/pages/skills.js`（661 行）

结构：
- 顶部一级 Tab：场景 / 技能（seg-bar 控件，`_renderMarketTab`）
- 技能页内二级子 Tab：技能市场(official) / 仓库技能(repo) / 个人技能(user)（seg-bar，`_skillTab` 变量控制）
- 切换 Tab：`_switchSkillTab(tab)` → `renderSkills()` → `_reloadSkills(searchInit)`
- 数据加载：`API.get('/skills?scope=xxx&repo=xxx&q=xxx&sort=xxx&page=xxx&page_size=xxx')`
- 列表展示：Agent tab 分组 + 卡片网格（rgrid）
- repo/user Tab 有「上传技能」按钮；卡片有「删除」「转换」操作

关键模式：
```
_skillTab = 'official' / 'repo' / 'user'
_switchSkillTab(tab) → _skillPage=1 → renderSkills()
```

### 1.2 场景页当前实现

文件：`src/api/static/js/pages/scene-market.js`（616 行）

当前状态：
- 只有场景市场（official），没有子 Tab
- 精选场景横向滚动 + 全部场景（搜索+分类过滤+排序+分页）
- API：`GET /scenes/market`（只查 scene_market 表 source='official'）
- 场景详情页：`renderSceneDetail(marketId)` → `GET /scenes/market/{id}`
- 应用/体验：`POST /scenes/market/{id}/apply` / `POST /scenes/market/{id}/experience`

缺失：
- 没有仓库场景/个人场景子 Tab
- 没有创建/编辑/删除/导入入口
- 没有可视化编排入口

### 1.3 后端 scenes API + scene_market 表（已就绪）

文件：`src/api/routes/scenes.py`（866 行）、`src/api/db_setup.py`

scene_market 表结构（第 549-575 行）：
```
id, name, display_name, version, description, market_description,
icon, category_id, is_featured, like_count,
source (默认 'official'), source_user_id,
recommended_template, nodes_json, onboarding, tags, author,
content_hash, obs_icon_key, is_active, created_by, updated_by,
created_at, updated_at
UNIQUE (name, source, source_user_id)
```

**关键发现**：scene_market 表已有 source + source_user_id + icon/obs_icon_key 等全部字段，用户自定义场景也存这张表。

字段语义：
- `source` — 场景来源：'official'（官方同步）/ 'user'（用户创建）
- `source_user_id` — 创建者用户 ID（official=0，用户创建=当前用户 ID）

但 scene_market 表需要加两列来区分仓库场景和个人场景：

```sql
ALTER TABLE scene_market ADD COLUMN scope VARCHAR(32) DEFAULT 'official';
ALTER TABLE scene_market ADD COLUMN repo_key VARCHAR(191) DEFAULT '';
```

字段语义：
- `source` — 场景来源：'official'（官方同步）/ 'user'（用户创建）
- `scope` — 可见范围：
  - 'official' — 官方场景（source='official'，repo_key 为空）
  - 'repo' — 仓库场景（当前仓库下所有用户可见）
  - 'user' — 个人场景（只有创建者本人在当前仓库可见）
- `repo_key` — 仓库标识（official 为空，repo/user 为当前仓库）
- `source_user_id` — 创建者用户 ID（official=0，用户创建=当前用户 ID）

仓库场景和个人场景的区别：
- repo — source='user', scope='repo', repo_key=当前仓库 → 该仓库下所有用户可见
- user — source='user', scope='user', repo_key=当前仓库, source_user_id=创建者 → 只有创建者本人在该仓库可见

UNIQUE 约束调整为 `UNIQUE (name, scope, source_user_id, repo_key)`（同 scope 同用户同仓库下名称唯一）。

scene_market 表已有 created_by/updated_by/created_at/updated_at 字段，不用加。

icon/obs_icon_key/category_id/tags 等字段全有，不用加其他列。

已有 CRUD（scenes.py）：
| 接口 | 方法 | 说明 |
|------|------|------|
| `GET /api/scenes?repo=xxx` | list_scenes | 返回当前仓库的场景列表（查 devops_pipeline_scene，repo scope） |
| `POST /api/scenes?repo=xxx` | create_scene | 创建仓库级场景（写入 devops_pipeline_scene，scope 强制 repo） |
| `PUT /api/scenes/{scene_id}?repo=xxx` | update_scene | 更新场景 |
| `DELETE /api/scenes/{scene_id}?repo=xxx` | delete_scene | 软删除场景 |
| `POST /api/scenes/apply/{owner}/{repo}` | apply_scene | 应用场景到仓库 |
| `GET /api/scenes/market` | list_scenes_market | 查 scene_market WHERE is_active=1（全量，不过滤 source） |
| `GET /api/scenes/market/{id}` | get_scene_market_detail | 查 scene_market 详情 |
| `POST /api/scenes/market/{id}/apply` | apply_market_scene | 从市场场景复制到 devops_pipeline_scene |
| `POST /api/scenes/market/{id}/experience` | experience_market_scene | 体验市场场景 |

现状问题：
- `create_scene` 写入 devops_pipeline_scene（不是 scene_market），body.scope 仅做参数校验（第 698 行拒绝非 repo），不写入 DB——devops_pipeline_scene 表的 scope 列已被 Issue-39 迁移删除
- `GET /api/scenes/market` 查的是 scene_market 全量（is_active=1），但没有按 scope 过滤——目前 scene_market 里只有 source='official' 的数据
- 仓库场景/个人场景的 CRUD 应该直接操作 scene_market 表（scope='repo'/'user'），而不是 devops_pipeline_scene

调整方向：
- 仓库场景/个人场景的 CRUD 直接写 scene_market 表（source='user', scope='repo'/'user', repo_key=当前仓库, source_user_id=当前用户）
- `GET /api/scenes/market` 加 scope 过滤参数
- devops_pipeline_scene 表保持不变（只存仓库应用场景的运行配置，不存场景元信息）

### 1.4 场景中心 dashboard 节点编排组件（参照不复用）

文件：`src/api/static/js/pages/dashboard.js`（876 行）

dashboard.js 已有编排能力（enterCreateMode / saveCustomScene / editSceneInfo / deleteCustomScene / renderAgentCards），但**不复用**，原因：

- 所有编排函数定义在 `renderDashboard` 函数体内部（768 行的大闭包），通过 `window.xxx = function()` 挂到全局
- 这些函数闭包捕获了 renderDashboard 的局部变量（a/agentOrder/currentScene/editNodes/owner/name 等），外部无法传参
- 要复用必须先从 renderDashboard 提取为模块级函数——改造量比自己写还大
- dashboard 的编排和它的页面骨架耦合（场景选择器、引擎配置、指标展示），场景页只需要编排部分

**决策：scene-market.js 自己写一套编排组件**，参照 dashboard 的编排模式但独立实现。

编排模式（参照 dashboard.js）：
- 6 个 agent（vod/issue/coding/qa/sre/chat）按固定顺序排列
- 每个 agent 可选 skill（不绑引擎，见 2.3.3）
- 可启用/禁用 agent（vod/issue 不可禁用）
- next 链路按 agent 顺序自动生成（最后一个 handoff=complete, next=done）

### 1.5 后端 skill 查询接口（编排时用）

文件：`src/api/routes/scenes.py` 第 638-656 行

已有接口 `GET /api/scenes/skills/{agent_type}`：
```python
SELECT skill_name, display_name, scope, owner_id FROM skill
WHERE agent_type=? AND is_active=1
AND (scope='official' OR (scope='user' AND owner_id=?) OR (scope='repo' AND owner_id=?))
```

这个接口已经按 scope 过滤了——查 official + 当前用户的 user skill + 当前仓库的 repo skill。

**skill 可见范围设计**：
| 场景 scope | 可见 skill 范围 | skill 下拉 tab |
|-----------|----------------|---------------|
| 仓库场景(repo) | official + repo | 官方 / 仓库 |
| 个人场景(user) | official + repo + user | 官方 / 仓库 / 个人 |

后端 `GET /api/scenes/skills/{agent_type}` 已返回 official + user + repo 的 skill（含 scope 字段）。前端按场景 scope 决定可见范围，并在 skill 下拉内按类型 tab 筛选：
- 仓库场景：下拉内展示「官方」「仓库」两个 tab，分别筛 scope=official 和 scope=repo 的 skill
- 个人场景：下拉内展示「官方」「仓库」「个人」三个 tab，分别筛 scope=official、scope=repo、scope=user 的 skill

---

## 二、方案

### 2.1 总体架构

```
场景页（scene-market.js）
├── 顶部一级 Tab：场景 / 技能（已有，不变）
├── 场景页内二级子 Tab（新增）
│   ├── 场景市场(official) — 现有功能，保持不变
│   ├── 仓库场景(repo) — 新增：列表 + 创建 + 编辑 + 删除 + 导入 + 应用 + 体验
│   └── 个人场景(user) — 新增：列表 + 创建 + 编辑 + 删除 + 导入 + 应用 + 体验
├── 场景市场 Tab
│   ├── 精选场景横向滚动（不变）
│   ├── 全部场景（搜索+分类+排序+分页）（不变）
│   └── 场景详情页（不变）
├── 仓库场景/个人场景 Tab
│   ├── 场景卡片列表（调 GET /api/scenes/market?scope=xxx&repo=xxx）
│   ├── 创建场景按钮 → 场景编排页（scene-market.js 自己写的编排组件）
│   ├── 导入场景按钮 → JSON 文件导入
│   ├── 卡片操作：查看 / 编辑 / 删除 / 应用 / 体验
│   └── 名称重复校验
└── scene_sync ZIP 下载优化（后端独立改动）
```

### 2.2 改动清单

| # | 文件 | 改动类型 | 说明 |
|---|------|---------|------|
| 1 | `src/api/static/js/pages/scene-market.js` | 修改 | 新增二级子 Tab + 仓库/个人场景 CRUD + 导入 + 独立编排组件 |
| 2 | `src/api/db_setup.py` | 修改 | scene_market 表加 scope + repo_key 列 + UNIQUE 约束重建（方案 A）；新增 uploaded_files 表；新增 distributed_locks 表 |
| 3 | `src/api/routes/scenes.py` | 修改 | `GET /api/scenes/market` 加 scope 过滤；`GET /api/scenes/market/{id}` 返回加 nodes_raw + scope 字段；新增 scene_market CRUD（POST/PUT/DELETE）+ 图标上传；`GET /api/scenes/skills/{agent_type}` 加搜索+分页+scope过滤；新增名称重复校验接口 |
| 4 | `src/user/db.py` | 修改 | `list_scene_market` 加 scope 参数；新增 scene_market 的 CRUD 函数；新增 uploaded_files CRUD 函数 |
| 5 | `src/lock.py` | 新建 | 分布式锁封装：acquire_lock（MySQL/SQLite 双分支）/ release_lock / cleanup_expired_locks |
| 6 | `src/storage/upload_cleanup.py` | 新建 | 临时上传文件定时清理（每小时，分布式锁互斥，清理超过 24 小时的 temp 文件） |
| 7 | `src/api/main.py` | 修改 | 注册 upload_cleanup 定时任务 |
| 8 | `src/api/static/locales/zh/skills.json` | 修改 | 新增场景 scope i18n key |
| 9 | `src/api/static/locales/en/skills.json` | 修改 | 新增场景 scope i18n key |
| 10 | `src/api/static/index.html` | 修改 | scene-market.js 版本号 +1 |
| 11 | `src/storage/scene_sync.py` | 修改 | 逐文件 API 下载 → ZIP 归档下载 |
| 12 | `tests/test_scene_sync.py` | 修改 | 适配 ZIP 下载改动 |
| 13 | `tests/test_scenes_api.py` | 新增/修改 | 新增 scope 过滤、scene_market CRUD、名称重复校验测试 |

注：不改 `dashboard.js`——编排组件在 scene-market.js 内独立实现。

### 2.3 前端改动（scene-market.js）

#### 2.3.1 新增二级子 Tab

参照 skills.js 的 `_skillTab` 模式：

```javascript
let _sceneTab = 'official';  // official / repo / user

function _sceneScopeName(scope) {
  var names = {
    official: I18N.t('skills.scene_scope_official')||'场景市场',
    repo: I18N.t('skills.scene_scope_repo')||'仓库场景',
    user: I18N.t('skills.scene_scope_user')||'个人场景',
  };
  return names[scope] || scope;
}

function _switchSceneTab(tab) {
  _sceneTab = tab;
  _sceneMarketPage = 1;
  if (tab === 'official') {
    _loadSceneMarket();
  } else {
    _loadCustomScenes(tab);
  }
}
```

在 `_renderSceneMarketPage` 顶部加二级 Tab 栏（和 skills.js 的 seg-bar 结构一致）。

#### 2.3.2 仓库场景/个人场景列表

```javascript
function _loadCustomScenes(scope) {
  const repo = _currentRepoFullName();
  const params = ['scope=' + scope, 'sort=latest'];
  if (scope === 'repo' || scope === 'user') {
    if (repo) params.push('repo=' + encodeURIComponent(repo));
  }
  params.push('page=' + _sceneMarketPage);
  params.push('page_size=' + _sceneMarketPageSize);
  API.get('/scenes/market?' + params.join('&')).then(d => {
    _sceneMarketScenes = d.scenes || [];
    _sceneMarketTotal = d.total || 0;
    _renderCustomSceneList(scope);
  });
}
```

列表展示复用场景卡片样式（`_renderSceneCard`），但操作按钮不同：
- official 场景：底部应用 / 体验（白底轮廓按钮，hover 浅蓝）
- repo/user 场景：底部应用 / 体验（白底轮廓按钮）；悬停时右上角浮层显示编辑(✎) / 导出(⬇) / 删除(🗑) 图标按钮（半透明毛玻璃浮层，不遮挡卡片底部按钮）
- 卡片点击跳转详情页（`app.nav('scene-detail','{id}')`）
- 删除成功后跳回场景市场列表页（`app.nav('scene-market')`）
- 导出：调 `GET /api/scenes/market/{id}` 拿数据，生成 scene.json 文件下载（格式和 grape-skills 仓库一致）
- repo/user 列表也展示分类 tab（和 official 一样，支持分类过滤）

#### 2.3.3 创建/编辑场景（独立编排组件）

scene-market.js 自己写一套编排组件，不依赖 dashboard.js。

**编排页结构**（全页渲染到 pageContent，和详情页一致的布局）：

点「创建场景」→ 全屏渲染编排界面到 pageContent（不在 marketTabContent 子容器内），布局参考场景详情页：

1. **返回按钮**：← 返回场景市场（`app.nav('scene-market')`）
2. **顶部信息区**（垂直布局，苹果风格）：
   - 场景图标上传（72px 圆角，点击上传；旁边有 ⓘ 帮助符号，点击弹出格式说明气泡，移走自动消失）
   - 场景名称输入框（maxlength=50，实时字数计数 0/50，苹果风格圆角输入框 focus 蓝色边框）
   - 场景描述输入框（maxlength=200，实时字数计数 0/200，苹果风格圆角输入框 focus 蓝色边框）
   - 场景分类下拉选择（苹果风格圆角输入框）
3. **执行流程**：5 个 agent 卡片（vod/issue/coding/qa/sre，不含 chat），det-card 样式和详情页一致：
   - 彩色顶条 + 序号 + agent名称 + 启用/禁用按钮（vod/issue 不可禁用；coding/qa/sre 可禁用）
   - 启用/禁用按钮逻辑：当前可用显示 ✕（点击禁用），当前禁用显示 ✓（点击启用），可反复切换
   - **默认状态**：创建模式时 vod/issue 默认启用，coding/qa/sre 默认禁用（需手动 ✓ 启用）；编辑模式时只有 nodes_raw 里明确 `enabled=true` 的才启用，其余默认禁用
   - **保存校验**：所有启用的 agent 必须配置了 skill，未配置则提示"XX Agent 已启用但未配置技能"并阻止保存
   - skill 选择框（点击弹出下拉浮层，含类型 tab + 搜索 + 分页）
   - 选中 skill 后卡片显示 skill display_name + skill description（det-card-desc）
   - 引擎状态框（广播）
   - 卡片间用 → 箭头连接
4. **技能套件**：选中 skill 后实时显示，det-suite-card 网格布局，展示已选 skill 的 display_name + description + agent 标签（和详情页样式一致）
5. **样例项目模板**（放在最底部）：项目模板下拉浮层（和 skill 下拉一样的苹果风格 + 搜索）+ 选了模板后显示模板信息（图标/名称/描述/语言/星标/来源，和详情页样式一致）
6. **底部**：取消 + 保存按钮

agent 列表：vod/issue/coding/qa/sre（5 个，不含 chat——chat 是聊天会话用的，不是开发流程 agent，和 dashboard.js 的 agentOrder 一致）

nodes_json 里存 skill_id + skill_name（双存），详情接口优先用 skill_id 查 skill 表获取 display_name + description。

**skill 下拉浮层**（苹果风格）：
- 半透明毛玻璃遮罩（`backdrop-filter:blur(2px)`），点击遮罩区域关闭浮层
- 弹出框：毛玻璃背景 + 圆角 16px + 阴影，宽 340px
- 定位：以触发元素位置为准，左对齐到触发元素左边缘；底部空间 ≥300px 时向下展开，不够时向上展开；右边界溢出时左移
- max-height 自适应可用空间（最大 420，最小 300），内部列表区域可滚动
- 顶部类型 tab（官方/仓库/[个人]）+ ✕ 关闭按钮
- 搜索框（苹果风格圆角输入框，focus 时蓝色边框）
- skill 列表项：display_name + skill_name，hover 浅蓝背景
- 滚动到底部自动加载下一页（分页）
- 事件绑定用 data 属性 + addEventListener，不用内联 onclick 转义

**模板下拉浮层**（和 skill 下拉一样的苹果风格 + 搜索）：
- 同样的毛玻璃遮罩 + 圆角弹出框
- 标题栏 + ✕ 关闭按钮 + 搜索框
- 模板列表项：图标 + 名称 + 分类，hover 浅蓝背景
- 底部"不选择模板"选项

**不绑引擎（engine_runtime_ids）**：

编排页只做 skill 选择，不做引擎绑定。理由：
1. 引擎绑定是仓库运行时配置，不是场景模板属性——同一场景应用到不同仓库引擎配置可能不同
2. 编排时没有"当前仓库"上下文（尤其个人场景），无法列出可选引擎
3. scene_market 的 nodes_json 存场景模板，不存 engine_runtime_ids；官方场景 nodes_json 里也没有此字段
4. 引擎绑定在场景中心 dashboard 已有——场景应用到仓库后，用户在 dashboard 按需配置即可

nodes_json 里不存 engine_runtime_ids，应用场景后默认全广播，用户在 dashboard 里按需配置。

**项目模板选择（本期做）**：

编排页底部支持配置 recommended_template 字段，调 `GET /api/project-templates` 获取模板列表，下拉选择一个模板。选择后下方实时显示模板详细信息（和详情页样例项目模板区域一致）。配置后该场景支持「体验」功能。

**skill 可见范围与 tab 筛选**：

编排时调 `GET /api/scenes/skills/{agent_type}` 获取 skill 列表，接口返回 scope + display_name + description 字段。前端按场景 scope 决定可见的 skill 范围，并在 skill 下拉内按类型 tab 筛选：

| 场景 scope | 可见 skill 范围 | skill 下拉 tab |
|-----------|----------------|---------------|
| 仓库场景(repo) | official + repo | 官方 / 仓库 |
| 个人场景(user) | official + repo + user | 官方 / 仓库 / 个人 |

下拉交互：点开 skill 下拉 → 顶部类型 tab（官方/仓库/[个人]）→ 搜索框（名称模糊搜索）→ 当前 tab + 搜索词下的 skill 列表 → 选中。默认展示「官方」tab。选中后卡片显示 skill 名称 + 描述，技能套件区域同步更新。

**skill 搜索 + 分页**：

skill 下拉内支持名称模糊搜索 + 分页加载：

- 搜索框：输入关键词后按名称模糊匹配（调后端 `GET /api/scenes/skills/{agent_type}?q=xxx&scope=xxx&page=xxx&page_size=xxx`）
- 分页：每页 20 条，滚动到底部自动加载下一页（前端监听下拉列表 scroll 事件，触底时 page+1 追加）
- 搜索词变化时重置到第 1 页
- 切换 tab 时清空搜索词并重置到第 1 页

**编辑模式**：

点「编辑」→ 同样的编排界面，但预填已有节点数据（调 `GET /api/scenes/market/{id}` 获取 scene_market 详情，用返回的 `nodes_raw` 字段预填 _sceneEditorNodes）。

**nodes_raw 字段**：

现有 `GET /api/scenes/market/{id}` 返回结构不含原始 nodes_json（返回的是解析后的 skills_detail）。编辑模式需要原始 node 结构（id/tag/skill/mode/next/handoff/enabled）来预填编排组件。方案 A：在现有接口返回里加 `nodes_raw` 字段（json.loads(nodes_json) 后的原始数组），不影响现有详情页展示字段。

**保存**：

- 创建：`POST /api/scenes/market`，body 含 name/display_name/description/scope(repo/user)/nodes/icon/obs_icon_key/category_id/recommended_template（tags 留空）
- 编辑：`PUT /api/scenes/market/{id}`，body 含 name/display_name/description/nodes/icon/obs_icon_key/category_id/recommended_template（tags 留空）
- nodes 按 agent 顺序自动生成 next 链路（最后一个 handoff=complete, next=done）

```javascript
// 编排组件状态
let _sceneEditorNodes = [];  // 6 个 agent 的编排数据
let _sceneEditorMode = '';   // 'create' / 'edit'
let _sceneEditorScope = '';  // 'repo' / 'user'
let _sceneEditorSceneId = '';// 编辑模式下的 scene_market 主键 id

function _enterSceneEditor(mode, scope, sceneId) {
  _sceneEditorMode = mode;
  _sceneEditorScope = scope;
  _sceneEditorSceneId = sceneId || '';
  // 初始化 6 个 agent 节点
  _sceneEditorNodes = ['vod','issue','coding','qa','sre','chat'].map(at => ({
    id: at, tag: at, skill: '', mode: 'emit', next: 'done',
    handoff: 'a2a', enabled: true
  }));
  if (mode === 'edit') {
    // 加载已有场景数据预填（调 GET /api/scenes/market/{id} 获取 scene_market 详情）
    API.get('/scenes/market/' + sceneId).then(d => {
      // 用 d.nodes_raw 预填 _sceneEditorNodes
    });
  }
  _renderSceneEditor();
}
```

#### 2.3.4 导入场景

```javascript
function _showSceneImport(scope) {
  // 弹出文件选择框，选择 JSON 文件
  // 解析 JSON → 校验格式
  // scope 按当前 Tab 自动设定（不读 JSON 里的 scope）
  // 调 POST /api/scenes/market 创建，body 含 scope=当前Tab + nodes/icon/recommended_template 等
}
```

导入格式：scene.json（和 grape-skills 仓库的 scene.json 格式一致）。
导入时 scope 按当前 Tab 自动设定——在仓库场景 Tab 导入则 scope=repo，在个人场景 Tab 导入则 scope=user。

#### 2.3.5 名称重复校验

创建/编辑时实时校验：
```javascript
function _checkSceneName(name, scope, repo) {
  API.get('/scenes/check-name?name=' + encodeURIComponent(name) + '&scope=' + scope + '&repo=' + repo)
    .then(d => {
      if (d.exists) {
        // 显示提醒「当前名称重复请重新命名」
      }
    });
}
```

### 2.4 后端改动

#### 2.4.0 scene_market 表加 scope + repo_key 列 + UNIQUE 约束重建（方案 A）

scene_market 表已有 source/source_user_id/icon/obs_icon_key 等字段，但缺少 scope 和 repo_key 列：

```sql
ALTER TABLE scene_market ADD COLUMN scope VARCHAR(32) DEFAULT 'official';
ALTER TABLE scene_market ADD COLUMN repo_key VARCHAR(191) DEFAULT '';
```

- scope='official' — 官方场景（source='official'，repo_key 为空）
- scope='repo' — 仓库场景（source='user'，repo_key=当前仓库，该仓库所有用户可见）
- scope='user' — 个人场景（source='user'，repo_key=当前仓库，只有创建者本人在该仓库可见）

UNIQUE 约束从 `UNIQUE (name, source, source_user_id)` 改为 `UNIQUE (name, scope, source_user_id, repo_key)`（同 scope 同用户同仓库下名称唯一），天然实现名称重复校验。

项目为双 DB 后端（SQLite 本地 + MySQL 云 RDS，通过 mysql_compat.py 翻译方言），改 UNIQUE 约束按 DB 分别处理（方案 A）：

SQLite（表重建，项目已有先例 _migrate_scene_table_to_repo_model）：
1. 创建带新 UNIQUE 约束的临时新表
2. 从旧表复制存量数据（official 场景补 scope='official', repo_key=***
3. 删旧表，重命名新表

MySQL（在线 DDL，无需重建表）：
1. `ALTER TABLE scene_market DROP INDEX uk_name_source_user;`
2. `ALTER TABLE scene_market ADD UNIQUE INDEX uk_name_scope_user_repo (name, scope, source_user_id, repo_key);`

存量数据风险：scene_market 表当前只有 source='official' 的官方场景数据（scene_sync 同步进来），搬移时填 scope='official'、repo_key=*** 即可，不丢数据。

用户自定义场景写入 scene_market 表：
- 创建仓库场景：`INSERT INTO scene_market (..., source='user', scope='repo', repo_key=当前仓库, source_user_id=当前用户, icon, obs_icon_key, ...)`
- 创建个人场景：`INSERT INTO scene_market (..., source='user', scope='user', repo_key=当前仓库, source_user_id=当前用户, icon, obs_icon_key, ...)`

scene_market 表已有 created_by/updated_by/created_at/updated_at 字段，记录创建人更新人。

不改 devops_pipeline_scene 表。

#### 2.4.1 `GET /api/scenes/market` 加 scope 过滤

现有 `GET /api/scenes/market` 查 scene_market WHERE is_active=1（全量）。加 scope 参数：

```python
@router.get("/market")
def list_scenes_market(user: dict = Depends(get_current_user),
                       q: str = "", category_id: int = 0, sort: str = "latest",
                       scope: str = "",  # 新增：official / repo / user，空=全部
                       repo: str = "",   # 新增：scope=repo/user 时按 repo_key 过滤
                       featured_only: int = 0, limit: int = 0,
                       page: int = 1, page_size: int = 20):
    """场景市场列表：支持 scope 过滤"""
    # WHERE is_active=1 AND (scope 为空或 scope=参数值)
    # scope='official'：查官方场景（scope='official'）
    # scope='repo'：查当前仓库下所有用户的仓库场景（scope='repo' AND repo_key=to_repo_key(repo)）
    # scope='user'：查当前用户在当前仓库的个人场景（scope='user' AND repo_key=to_repo_key(repo) AND source_user_id=当前用户）
```

场景市场 Tab 调 scope=official，仓库场景 Tab 调 scope=repo，个人场景 Tab 调 scope=user。

返回结构每条 scene 新增 `scope` 字段（前端按 scope 区分卡片操作按钮：official 显示应用/体验/点赞，repo/user 显示查看/编辑/删除/应用/体验）。

#### 2.4.2 仓库场景/个人场景 CRUD（写 scene_market 表）+ apply/experience 复用

新增或修改 CRUD 接口，直接操作 scene_market 表：

```python
# 创建：POST /api/scenes/market
# body: name, display_name, description, scope(repo/user), nodes, icon, obs_icon_key, category_id, recommended_template
# INSERT INTO scene_market (..., source='user', scope=?, repo_key=当前仓库, source_user_id=当前用户, created_by=当前用户名, ...)
# UNIQUE (name, scope, source_user_id, repo_key) 约束 → 重复时返回 409

# 更新：PUT /api/scenes/market/{id}
# body: name, display_name, description, nodes, icon, obs_icon_key, category_id, recommended_template
# 校验：scope != 'official'（官方场景不可编辑）
# 校验：scope='user' 时只有创建者可编辑；scope='repo' 时仓库成员可编辑
# 更新 updated_by=当前用户名

# 删除：DELETE /api/scenes/market/{id}
# 软删除：UPDATE scene_market SET is_active=0 WHERE id=? AND scope!='official'
# 校验：scope='user' 时只有创建者可删除；scope='repo' 时仓库成员可删除

# 图标上传：POST /api/scenes/market/icon-upload（新建）
# 限制：格式 jpg/png/webp/gif/svg，大小 ≤ 2MB
# 流程（临时 key + uploaded_files 表，安全处理取消不删老图）：
#   1. 接收 UploadFile 图片 → 校验格式+大小
#   2. OBS 模式 → storage.save_bytes 存到临时 key（scene-icons/tmp-{uuid}.{ext}）
#      非 OBS 模式 → 写本地 static/uploads/scene-icons/tmp-{uuid}.{ext}
#   3. INSERT uploaded_files 行（scope='temp', file_key=临时key, file_type='scene_icon', created_by=当前用户）
#   4. 返回 {file_id, icon: URL/路径, obs_icon_key: 临时key}
#   5. 用户保存场景时 body 带 file_id + icon + obs_icon_key
#      后端：临时 key 重命名为正式 key（scene-icons/{scene_market_id}.{ext}）→ UPDATE uploaded_files scope='committed', ref_table='scene_market', ref_id=场景id
#      编辑场景重新上传时：删老正式 key 文件 + 旧 uploaded_files 行 → 新文件落定
#   6. 用户取消 → 临时 key 留在 OBS，uploaded_files 行 scope='temp'
#   7. 定时清理（挂在 sync_official_scenes 定时任务里）：
#      SELECT WHERE scope='temp' AND created_at < 24小时前 → 删 OBS 文件 + 删表行
# 参照 skills.py 的 OBS 分支逻辑（storage.save_bytes）
```

uploaded_files 表结构（新增）：
```sql
CREATE TABLE IF NOT EXISTS uploaded_files (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    file_key        VARCHAR(1024) NOT NULL,       -- OBS 对象 key / 本地路径
    original_name   VARCHAR(512) DEFAULT '',      -- 用户上传原始文件名
    file_size       BIGINT DEFAULT 0,             -- 文件大小（字节）
    mime_type       VARCHAR(128) DEFAULT '',      -- MIME 类型（如 image/png）
    file_type       VARCHAR(32) DEFAULT '',       -- 'scene_icon'（预留扩展）
    scope           VARCHAR(16) DEFAULT 'temp',   -- 'temp'（临时）/ 'committed'（已提交）
    ref_table       VARCHAR(64) DEFAULT '',       -- 关联表名（committed 时填，如 'scene_market'）
    ref_id          INTEGER DEFAULT 0,            -- 关联记录 ID（committed 时填）
    is_active       INTEGER DEFAULT 1,            -- 软删除标记（0=已删除）
    created_by      VARCHAR(64) DEFAULT '',
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now'))
)

CREATE INDEX IF NOT EXISTS idx_uploaded_files_scope ON uploaded_files(scope, is_active, created_at);
CREATE INDEX IF NOT EXISTS idx_uploaded_files_ref ON uploaded_files(ref_table, ref_id, is_active);
```

#### 2.4.2a 分布式锁表 + 封装（落地 docs/new-version3/knowledge-id-and-auth-design.md §2.6 设计）

集群部署下定时任务（upload_cleanup）需跨实例互斥。不采用 GET_LOCK（依赖连接、阻塞 db_lock），采用 distributed_locks 表方案（TTL + holder 校验，更可控）。

distributed_locks 表结构（新增）：
```sql
CREATE TABLE IF NOT EXISTS distributed_locks (
    lock_key    VARCHAR(128) NOT NULL,          -- 锁唯一 key（如 'upload_cleanup'）
    holder      VARCHAR(128) NOT NULL,          -- 锁持有者标识（hostname-随机hex）
    locked_at   BIGINT       NOT NULL DEFAULT 0, -- 获取锁时间戳（秒）
    expires_at  BIGINT       NOT NULL DEFAULT 0, -- 锁过期时间戳（秒），过期可被抢占
    update_at   BIGINT       NOT NULL DEFAULT 0, -- 最后更新时间戳（排查用）
    created_at  BIGINT       NOT NULL DEFAULT 0, -- 锁记录首次创建时间（排查用）
    PRIMARY KEY (lock_key)
)
```

设计要点：
- lock_key 主键唯一，同一把锁只能存在一行，主键索引命中性能高
- holder 持有者标识，只有持有者才能释放锁（DELETE WHERE holder=?）
- expires_at 过期时间戳，解决实例宕机死锁
- 秒级 BIGINT 时间戳，跨数据库兼容
- 释放锁用 DELETE（不是 UPDATE 置 0），行不存在=锁空闲，无模糊语义

src/lock.py 封装（新建）：

```python
# acquire_lock(lock_key, ttl) → bool
# 抢锁逻辑（原子 SQL）：
#   MySQL: INSERT ... ON DUPLICATE KEY UPDATE
#     holder=IF(expires_at < UNIX_TIMESTAMP(), VALUES(holder), holder), ...
#   SQLite: INSERT OR ABORT（主键冲突 → 查现有行 → 已过期则条件 UPDATE）
#   抢锁后回查 holder 确认是否抢到（不靠 affected_rows 判断）

# release_lock(lock_key)
#   DELETE FROM distributed_locks WHERE lock_key=? AND holder=?
#   只删自己的锁，不会误删别人的

# cleanup_expired_locks()
#   DELETE FROM distributed_locks WHERE expires_at < now AND holder != ?
#   后台定时调用（5 分钟一次），防表膨胀兜底
```

upload_cleanup.py 使用：
```python
from src.lock import acquire_lock, release_lock

def _cleanup_temp_files():
    acquired = acquire_lock("upload_cleanup", ttl=300)
    if not acquired:
        log.info("Upload cleanup: lock contention, skipped")
        return
    try:
        # 清理逻辑...
    finally:
        release_lock("upload_cleanup")
```

不采用 GET_LOCK 的理由（设计文档 §2.6.8）：
- GET_LOCK 放在 db_lock 块内，等待锁期间 db_lock 被持有，整个进程其他 DB 操作阻塞
- GET_LOCK 依赖 MySQL 连接保持，连接池切换/断线后锁释放行为不明确
- 分布式锁表方案更可控：TTL 超时、holder 校验、过期清理，不依赖连接

apply/experience 复用现有 market_id 接口，不新建：

现有 `POST /api/scenes/market/{market_id}/apply`（apply_market_scene，第 301 行）和 `POST /api/scenes/market/{market_id}/experience`（experience_market_scene，第 452 行）都通过 market_id 操作 scene_market 表，scope 无关——不管场景是 official / repo / user，只要有 market_id 就能 apply/experience。三个 Tab 的应用和体验按钮都调这两个接口。

现有 `POST /api/scenes` (create_scene) 写 devops_pipeline_scene 的逻辑保持不变——那是仓库应用场景的运行配置，和场景元信息分离。

`GET /api/scenes/market/{id}` 详情接口返回新增两个字段：
- `scope` — 场景可见范围（official/repo/user），前端详情页按 scope 隐藏 repo/user 不需要的区块（在用仓库列表、点赞、精选标记）
- `nodes_raw` — 原始 nodes_json 解析后的数组（json.loads(nodes_json)），编辑模式编排组件用此字段预填

#### 2.4.3 `GET /api/scenes/skills/{agent_type}` 加搜索 + 分页参数

现有接口（第 638-656 行）返回全量 skill，不支持搜索和分页。需扩展：

```python
@router.get("/skills/{agent_type}")
def list_skills_for_agent(
    agent_type: str,
    user: dict = Depends(get_current_user),
    q: str = "",           # 名称模糊搜索
    scope: str = "",       # 按 skill scope 过滤：official / repo / user
    page: int = 1,
    page_size: int = 20,
):
    """返回该 agent_type 可选的 skill 列表（支持搜索 + scope 过滤 + 分页）"""
    # WHERE agent_type=? AND is_active=1
    #   AND (scope='official' OR (scope='user' AND owner_id=?) OR (scope='repo' AND owner_id=?))
    #   AND (q 为空或 skill_name LIKE '%q%' or display_name LIKE '%q%')
    #   AND (scope 参数为空或 scope=参数值)
    # ORDER BY scope, skill_name
    # LIMIT page_size OFFSET (page-1)*page_size
    return {"skills": [...], "total": N}
```

#### 2.4.4 新增名称重复校验接口

```python
@router.get("/check-name")
def check_scene_name(name: str, scope: str, user: dict = Depends(get_current_user), repo: str = ""):
    """检查场景名称是否重复"""
    # 按 scope + name + repo_key 查 scene_market 表
    return {"exists": bool}
```

### 2.5 scene_sync ZIP 下载优化

详见 `docs/new-version3/scene-sync-zip-download-optimization.md`，此处不重复。

要点：
- 新增 `_download_repo_zip` 函数，URL: `https://raw.gitcode.com/{owner}/{repo}/archive/refs/heads/{branch}.zip`
- 删除 `_api_list_dir` / `_api_get_file` / `_download_dir_recursive`
- 下架基准从 `repo_scene_names` 回到 `synced_names`

---

## 三、关键设计决策

| 决策 | 选项 | 选择 | 理由 |
|------|------|------|------|
| Tab 结构 | 复用技能页三子Tab / 新建独立结构 | 复用技能页三子Tab | 用户明确要求，保持 UI 一致性 |
| 可视化编排 | 复用 dashboard 组件 / 自己写 | 自己写 | dashboard 编排函数绑定在 renderDashboard 闭包内，提取改造量比自己写还大；且和 dashboard 页面骨架耦合 |
| 与场景中心关系 | 合并 / 并存 | 并存 | 用户明确要求，功能不重叠 |
| 引擎绑定 | 编排时绑定 / 不绑 | 不绑 | 引擎绑定是仓库运行时配置不是场景模板属性；编排时无仓库上下文；dashboard 已有引擎配置 |
| 名称校验 | 前端校验 / 后端校验 | 前端实时校验 + 后端 UNIQUE 约束兜底 | 用户体验好，即时反馈 |
| UNIQUE 约束改法 | 表重建 / DROP+ADD INDEX / 代码查重 | 方案 A：按 DB 分别处理 | SQLite 表重建（有先例）；MySQL 在线 DDL（DROP INDEX + ADD UNIQUE INDEX） |
| 场景存储 | devops_pipeline_scene 表 / scene_market 表 | scene_market 表 | scene_market 已有 source/source_user_id/icon/obs_icon_key 等字段，加 scope + repo_key 列即可 |
| repo vs user 区别 | — | repo=仓库所有用户可见，user=创建者本人可见 | 仓库场景是仓库共享的，个人场景是私有的 |
| apply/experience | 新建 / 复用现有 market_id 接口 | 复用 | apply_market_scene/experience_market_scene 通过 market_id 操作 scene_market，scope 无关 |
| 图标上传 | 不做 / 新建接口 | 新建 POST /api/scenes/market/icon-upload | 用户自定义场景需要自定义图标，参照 skills.py OBS 分支逻辑 |
| 项目模板选择 | 留空后续加 / 本期做 | 本期做 | 调 GET /api/templates 拿模板列表下拉选择，无技术障碍 |
| 仓库场景可选 skill | official+repo / official+repo+user | official+repo | 仓库场景绑定到具体仓库，只用该仓库技能 |
| 个人场景可选 skill | official+repo / official+repo+user | official+repo+user | 个人场景编排时也可见个人技能 |
| scene_sync 下载方式 | 逐文件 API / ZIP 归档 | ZIP 归档 | 一次请求全量，简化逻辑 |
| 临时文件清理并发 | 不加锁 / GET_LOCK / 分布式锁表 | 分布式锁表（distributed_locks） | 集群部署需跨实例互斥；GET_LOCK 依赖连接、阻塞 db_lock；分布式锁表 TTL+holder 更可控（设计文档 knowledge-id-and-auth-design.md §2.6） |
| 版本号管理 | 用户输入 / 自动递增 | 自动递增（方案 A） | 用户会乱写；新建默认 1.0.0，编辑保存时后端做内容比对，只有真正变更才 patch+1，无变更不涨；版本号仅代表编辑轮次不是标准语义化版本 |
| 卡片操作按钮 | 全放底部 / 悬浮浮层 | 底部主操作 + 悬浮浮层 | 底部放应用/体验主操作不挤压；编辑/导出/删除放右上角悬浮浮层（图标按钮），悬停显示不遮挡卡片底部 |
| 导出功能 | 不做 / 下载 scene.json | 下载 scene.json | 用户可导出场景配置文件，格式和 grape-skills 仓库一致，支持导入 |

---

## 四、影响范围

| 维度 | 影响 |
|------|------|
| DB | scene_market 表加 scope + repo_key 列 + UNIQUE 约束重建（方案 A：SQLite 表重建 / MySQL DROP+ADD INDEX）；新增 uploaded_files 表；新增 distributed_locks 表（集群分布式锁） |
| 后端 API | `GET /api/scenes/market` 加 scope 过滤；`GET /api/scenes/market/{id}` 返回加 nodes_raw + scope 字段；新增 scene_market CRUD + 图标上传 + 名称校验；`GET /api/scenes/skills/{agent_type}` 加搜索+分页 |
| 前端 | scene-market.js 大改（二级子Tab + CRUD + 导入 + 独立编排组件 + 详情页按 scope 精简）；不改 dashboard.js |
| i18n | 新增场景 scope 相关 key |
| 配置 | 无变更 |
| Dockerfile | 无变更 |
| scene_sync | 逐文件 → ZIP 下载 |
| 分布式锁 | 新增 src/lock.py（acquire_lock/release_lock/cleanup_expired_locks），upload_cleanup 定时任务加锁防集群并发 |
| 测试 | scene_sync 测试适配 + 新增 scenes API 测试 |

---

## 五、实施计划

分 3 个提交（同分支）：

### 提交 1：scene_sync ZIP 下载优化
- 改 `src/storage/scene_sync.py`
- 改 `tests/test_scene_sync.py`
- 独立改动，不影响前端

### 提交 2：后端 scenes API 扩展
- 改 `src/api/db_setup.py`（scene_market 加 scope + repo_key 列）
- 改 `src/api/routes/scenes.py`（GET /api/scenes/market 加 scope 过滤；新增 scene_market CRUD + 图标上传 + 名称校验；skills 接口加搜索+分页）
- 改 `src/user/db.py`（list_scene_market 加 scope 参数；新增 scene_market CRUD 函数）
- 新增 `tests/test_scenes_api.py`
- 为提交 3 提供前端所需的 API

### 提交 3：前端场景页三子Tab
- 改 `src/api/static/js/pages/scene-market.js`（二级子Tab + CRUD + 导入 + 独立编排组件）
- 改 i18n 文件
- 改 `index.html` 版本号
- 不改 dashboard.js

---

## 六、验收标准（修订版 2026-09-08）

- [x] 场景页展示两个子Tab（场景市场 / 仓库场景）——移除个人场景
- [x] 仓库场景 Tab 支持创建场景
- [x] 仓库场景 Tab 支持查看场景详情
- [x] 仓库场景 Tab 支持编辑场景
- [x] 仓库场景 Tab 支持删除已创建场景（硬删除）
- [x] 支持从零可视化编排节点（scene-market.js 独立编排组件）
- [x] 场景市场/仓库场景均支持一键应用和体验
- [x] 场景名称重复时弹出提醒「场景 '{name}' 已存在」
- [x] 仓库场景 Tab 与场景中心(dashboard)并存，功能不重叠
- [x] 所有现有测试通过（37 passed）
- [x] scene_sync ZIP 下载优化测试通过
- [x] 精选场景加载竞态修复（异步加载完后重绘页面）
- [x] 仓库场景详情页不展示样例项目模板（仅 official 展示）
- [x] 仓库场景编排页不展示样例项目模板（移除 recommended_template 选择）
- [x] 技能选择使用 Select2 组件（搜索 + 分页 + tab 切换技能市场/仓库技能）
- [x] 编辑模式进入编排页时根据已选 skill 的 scope 自动切换 tab

### 移除的功能（2026-09-08 修订）

- [x] 移除个人场景（scope='user'）——前端 Tab/后端校验/DB 注释/i18n 全部清理
- [x] 移除导入功能——删除 POST /api/scenes/market/import 路由 + 前端导入弹窗 + 模板文件
- [x] 移除导出功能——删除 GET /api/scenes/market/{id}/export 路由 + 前端导出按钮 + 导出函数
- [x] 移除编排页样例项目模板选择——删除模板下拉 + 模板加载 + _saveScene 中 recommended_template 字段
- [x] 移除仓库场景"最热"排序按钮
- [x] 后端 list_skills_for_agent 修复：去掉 user scope 查询，repo scope 用 repo_key（从 repo 参数获取）匹配

---

## 七、确认点（已全部确认 ✓）

1. ✓ 仓库场景直接写入 scene_market 表（source='user', scope='repo', repo_key=当前仓库, source_user_id=当前用户），scene_market 加 scope + repo_key 列
2. ✓ ~~repo 和 user 都绑定仓库~~ → 移除个人场景，只保留 repo scope
3. ✓ scene_market 表已有 icon/obs_icon_key/created_by/updated_by/created_at/updated_at/recommended_template 字段，不用加其他列
4. ✓ 可视化编排由 scene-market.js 自己写独立编排组件，不复用 dashboard.js
5. ✓ 编排页不绑引擎（engine_runtime_ids）——nodes_json 不存 engine_runtime_ids，应用后默认全广播，dashboard 按需配置
6. ✓ ~~编排页支持配置项目模板~~ → 移除编排页模板选择（recommended_template），详情页仅 official 展示
7. ✓ 仓库场景编排时可选 skill 范围：official + repo（通过 Select2 tab 切换）
8. ✓ skill 下拉使用 Select2 组件（搜索 + 分页 + tab 切换），编辑模式根据已选 skill scope 自动选 tab
9. ✓ ~~导入场景~~ → 移除导入功能
10. ✓ ~~导出场景~~ → 移除导出功能
11. ✓ 名称重复校验：前端实时校验 + 后端 UNIQUE (name, scope, source_user_id, repo_key) 约束兜底
12. ✓ UNIQUE 约束改法：方案 A——SQLite 表重建 + MySQL DROP INDEX/ADD UNIQUE INDEX
13. ✓ apply/experience 复用现有 market_id 接口（apply_market_scene / experience_market_scene），不新建
14. ✓ 图标上传接口 POST /api/scenes/market/icon-upload 新建，参照 skills.py OBS 分支逻辑
15. ✓ create_scene 的 body.scope 仅做参数校验不写 DB（devops_pipeline_scene 的 scope 列已被 Issue-39 迁移删除）
16. ✓ scene_sync ZIP 下载优化作为本 Issue 的一部分，在同一分支提交
17. ✓ 分支从 weekly-02 拉取，MR 目标 weekly-02
18. ✓ 提交信息英文 + Conventional Commits + DCO 签署
19. ✓ 编排页顶部含分类选择（category_id），tags 暂不做（留空）
20. ✓ 编辑模式调 GET /api/scenes/market/{id}（按 scene_market 主键查详情），不调 GET /api/scenes/{scene_id}（那查的是 devops_pipeline_scene 运行配置）
21. ✓ GET /api/scenes/market/{id} 返回加 nodes_raw 字段（方案 A），编辑模式用此字段预填编排组件
22. ✓ repo 场景详情页按 scope 精简——隐藏在用仓库列表、点赞、精选标记区块（只展示节点链路+基本信息）
23. ✓ 图标上传限制：格式 jpg/png/webp/gif/svg，大小 ≤ 2MB
24. ✓ 图标上传用临时 key + uploaded_files 表方案——上传存临时 key（scope='temp'），保存时落定（重命名为正式 key，scope='committed'），取消不删老图
25. ✓ 新增 uploaded_files 表（file_key/file_type/scope/ref_table/ref_id/created_by/created_at），独立模块 src/storage/upload_cleanup.py 定时清理 scope='temp' 且超过 24 小时的孤儿文件
26. ✓ GET /api/scenes/market 列表返回每条 scene 新增 scope 字段（前端按 scope 区分卡片操作按钮）
27. ✓ GET /api/scenes/market/{id} 详情返回新增 scope + nodes_raw 两个字段（scope 用于详情页精简，nodes_raw 用于编辑模式预填）
28. ✓ 分布式锁表 distributed_locks 落地（表结构 + src/lock.py 封装：acquire_lock/release_lock/cleanup_expired_locks，MySQL ON DUPLICATE KEY UPDATE / SQLite INSERT OR ABORT 双分支）
29. ✓ upload_cleanup 定时任务用分布式锁防集群并发（lock_key='upload_cleanup', TTL=300s，抢不到锁跳过）
30. ✓ 临时文件清理任务独立模块 src/storage/upload_cleanup.py，不和 scene_sync 耦合
31. ✓ 版本号自动管理（方案 A）：新建默认 1.0.0，用户不可输入仅展示；编辑保存时后端做内容比对，只有真正变更才 patch+1（1.0.0→1.0.1→1.0.2），无变更不涨；后台脚本/批量修改不递增；复制克隆重置为 1.0.0。版本号仅代表编辑轮次，不是标准语义化版本
32. ✓ 卡片操作按钮：底部放应用/体验（白底轮廓 hover 浅蓝）；repo 卡片悬停时右上角浮层显示编辑(✎)/删除(🗑) 图标按钮（半透明毛玻璃浮层不遮挡底部按钮）——导出按钮已移除
33. ~~导出功能~~ → 已移除（GET /api/scenes/market/{id}/export 路由 + 前端导出按钮/函数全部删除）
34. ✓ 删除成功后跳回场景市场列表页（app.nav('scene-market')）
35. ✓ repo 列表也展示分类 tab（和 official 一样，支持分类过滤）
36. ✓ 详情页按钮统一白底轮廓（应用/体验/编辑/删除），hover 浅蓝；编辑/删除用 opacity:0.7 次要样式——导出按钮已移除
37. ✓ 详情页名称旁加分类标签（category_name）
38. ✓ 编排页顶部信息区苹果风格垂直布局：图标(72px)→名称(maxlength=50)→描述(maxlength=200)→分类；ⓘ帮助符号点击弹出气泡移走消失；输入框圆角 focus 蓝色边框
39. ✓ 加载性能优化：official 列表三个请求（精选/分类/列表）并行非阻塞，列表不等精选和分类
40. ✓ nodes_json 存 skill_id + skill_name 双存，详情接口优先用 skill_id 查 skill 表获取 display_name + description
41. ✓ 卡片悬停浮层（编辑/导出/删除）苹果风格：saturate(180%) blur(20px) 毛玻璃 + inset 0.5px 细边框 + 轻阴影；删除按钮苹果红 #ff3b30 hover 红色半透明
42. ✓ 卡片标题超长自动截断（overflow:hidden;text-overflow:ellipsis;white-space:nowrap）
43. ✓ 局部刷新：点击查询/排序/分类/分页只刷新 customSceneGrid/officialSceneGrid 区域，搜索栏和分类 tab 不重新渲染（DOM 元素不变）
44. ~~导出 ZIP 格式~~ → 已移除导出功能
45. ✓ 场景市场列表 min-height:600px 避免切 Tab 时页面高度跳变
46. ✓ customSceneList loading class 在数据加载成功后移除（之前残留导致布局错乱）
47. ~~导入弹出层~~ → 已移除导入功能
48. ~~导入接口~~ → 已移除（POST /api/scenes/market/import 路由 + 前端导入弹窗/函数全部删除）
49. ✓ scene_id 统一用完整 UUID（str(uuid.uuid4())）：apply_market_scene / experience_market_scene / create_scene 三处统一，不再用场景名称或短 UUID
50. ✓ apply_market_scene 回写 market_scene_id 修复：WHERE id=? 传 new_id（create_custom_scene 返回的 lastrowid int）而不是 scene_id（UUID 字符串），之前关联失败导致 applied check 查不到、不弹覆盖确认
51. ✓ MySQL UNIQUE 冲突报 Duplicate entry 不是 UNIQUE，错误检查同时匹配 UNIQUE 和 DUPLICATE，返回 409 而不是 500
52. ~~导入/导出错误信息~~ → 已移除导入导出功能
53. ~~模板 ZIP 包~~ → 已移除（/static/templates/ 目录删除）
54. ✓ 场景标识 name 字段：编排页加"场景标识"输入框（只允许小写英文+短横线，实时校验重复）；详情页标题旁显示 name 标签；保存时 name 和 display_name 分开提交
55. ✓ name 校验规则：official 场景 name 唯一；任何 scope 不能和 official 重名；repo 场景同仓库下 name 唯一、不同仓库可以同名
56. ✓ 编辑场景时 name 变更预校验（update_market_scene 路由层调 check_scene_name_exists）+ 数据库层 UNIQUE 兜底，返回 409 而不是 500
57. ~~导入弹出层改为只支持 ZIP~~ → 已移除导入功能
58. ✓ i18n key 清理：移除导入导出相关 22 个 key（export_scene/export_success/export_failed/import_tip/import_guide_*/import_choose_file/import_format_*/import_size_error/import_format_invalid/import_json_error/importing/confirm_import/download_template/import_scene/import_scene_tip/import_success/import_failed）；保留 scene_name_conflict/goto_repo_scenes/scene_name_available/template_search_placeholder/no_template_found（编排页仍用）
59. ✓ 删除场景改为硬删除（DELETE FROM），name 立即释放，UNIQUE 约束不变；路由层删除前先清理关联图标文件
60. ✓ check_scene_name_exists 去掉 scope=? 条件：同仓库下不分 scope，有重名就拦
61. ✓ create_market_scene（创建）/ update_market_scene（编辑）两个写入路径统一调 check_scene_name_exists 预校验，返回 409"场景标识已存在"——import_scene_zip 已移除
62. ✓ 场景删除改为硬删除方案：无回收站需求，删除即销毁，UNIQUE(name,scope,source_user_id,repo_key) 数据库强约束最安全
63. ✓ OBS 存储模式已启用（AK/SK/endpoint/bucket 配置在 .env，启动时 source .env 加载）：图标上传走 OBS，返回签名 URL；旧本地图标兼容不迁移
64. ~~导出文件名~~ → 已移除导出功能
65. ✓ 分类下拉框缓存命中时在 el.innerHTML=html 之后同步填充（之前在 innerHTML 之前访问 DOM 返回 null 导致分类只有默认 option）
66. ✓ VOD/ISSUE agent toggle：不存在时显示 ✓ 可启用配置回来，已启用时不显示 ✕ 不能禁用；其他 agent 自由 ✓/✕
67. ✓ 图标 onerror 处理：icon 文件不存在时显示 📷 不显示破图
68. ✓ _finalize_scene_icon 后清空 body.icon/body.obs_icon_key：避免 update_scene_market 用临时 URL 覆盖正式路径
69. ✓ create_custom_scene 的 lastrowid 被 _try_set_display_name 的 UPDATE 覆盖为 0：INSERT 后立即保存 new_id，后续操作不影响
70. ~~导出前端文件名~~ → 已移除导出功能
71. ✓ 删除确认改为苹果风格弹出层（毛玻璃遮罩+圆角卡片+红色删除按钮+删除中状态），替代原生 confirm()
72. ✓ 排序按钮局部刷新时高亮更新：_updateSortBtns 在 _renderOfficialSceneGrid/_renderCustomSceneGrid 里调用（仓库场景已移除"最热"按钮，仅保留"最新"）
73. ~~导入 ⓘ 气泡~~ → 已移除导入功能
74. ✓ 场景名称和场景标识统一限制 40 个字符：前端 maxlength=40 + 字数计数 0/40 + 保存校验；后端创建/导入/编辑三个接口统一校验返回 400
75. ✓ 重名提示文案改为"场景 '{name}' 已存在"（之前是"场景标识"）
76. ✓ apply 覆盖确认：create_custom_scene 返回 lastrowid=0 导致 market_scene_id 回写失败（WHERE id=0 rowcount=0），applied check 查不到不弹覆盖确认——已修复
77. ✓ _finalize_scene_icon 职责单一化：只做文件操作（重命名/上传 OBS/删临时/commit_uploaded_file），返回 (final_icon, final_obs_key) 给调用方；调用方负责数据库更新——避免 _finalize_scene_icon 和 update_scene_market 两次 UPDATE 互相覆盖
78. ✓ OBS icon key 统一用 scene-icons/{market_id}{ext}：icon-upload/导入/编辑/scene_sync 全部统一，market_id 全局唯一不会冲突
79. ✓ _resolve_scene_icon OBS 失败时不用过期签名 URL 兜底，改用分类 emoji
80. ✓ update_scene_market 字段更新条件：obs_icon_key/recommended_template 改成 truthy 判断（非空才更新），避免空字符串覆盖已有值
81. ~~README.md 逻辑统一（导出/导入/scene_sync）~~ → 导出/导入已移除，scene_sync 读 README.md 作为 market_description 保留
82. ✓ 卡片图标 onerror：图片加载失败时显示分类 emoji 兜底，不显示破图
83. ✓ 删除场景改为硬删除（DELETE FROM）+ 路由层先清理关联图标文件
84. ~~导入接口 icon 临时存储~~ → 已移除导入功能
85. ~~模板 ZIP README.md 改为 GUIDE.md~~ → 已移除模板文件
86. ✓ 场景页局部刷新 loading 动画：查询/排序/分类 tab/分页切换时，网格区域先显示 scn-spinner 旋转动画（蓝色圆环 0.8s 线性循环），API 返回后替换为内容——_loadSceneMarketList 和 _loadCustomScenes 两处统一处理；CSS .scn-loading/.scn-spinner/@keyframes scn-spin 新增
87. ✓ 技能页局部刷新 loading 动画：查询/查找/排序/分页切换时，技能列表区域先显示 spinner——_reloadSkills 已有 #all-skills 容器时局部刷新，不重建搜索栏/排序栏/Agent tab；新增 _renderSkillGrid 只更新技能列表+分页器+计数+Agent tab 高亮+排序按钮高亮，首次渲染仍走 _renderSkillPage 全量构建
88. ✓ 场景页分页器补页大小选择器：official 和 repo 分页器统一加 20/50/100 每页选择器
89. ✓ 应用/体验场景前 display_name 查重：apply_market_scene 和 experience_market_scene 在创建/覆盖 devops_pipeline_scene 前检查目标仓库是否已有同名场景（name=display_name AND repo_key AND is_active=1），存在返回 409；覆盖分支排除自己（id != existing_row_id）；前端 _doApplyScene/_doExperienceScene 捕获 409 弹苹果风格提示框（⚠️ + 冲突信息 + 关闭/去修改场景按钮），点击"去修改场景"跳转到 dash 页面；体验冲突返回 full_name，前端先刷新侧边栏仓库列表（API.get /repos/managed）再 switchRepo 切换到体验仓库
90. ✓ experience_market_scene 修复 host_repo 调用：host_repo 是同步函数（def），两处 await host_repo(...) 改为 host_repo(...)，之前 await 同步函数抛 TypeError 被 catch 静默跳过，导致仓库未托管但体验流程继续返回成功
91. ✓ _selectRepoAndNav 修复侧边栏同步：体验成功跳转和冲突跳转都用 API.get('/repos/managed') 先刷新侧边栏下拉（新仓库可能刚托管还没进列表），完成后再 app.switchRepo(fullName) 切换仓库+nav('dash')；switchRepo 内部同步设置 repoSelect.value 确保下拉选中项一致
92. ✓ 托管配置补全：configs/global-bot-token.json 缺失导致 host_repo 返回 503（全局 Bot Token 未配置），已创建配置文件（含 gitcode_api_token/bot_usernames/webhook_url 等字段）
93. ✓ 精选场景加载竞态修复：_loadSceneMarket 中精选场景异步加载完后，如果页面已渲染（officialSceneGrid 存在），触发 _renderSceneMarketPage 重绘精选区
94. ✓ 技能选择改用 Select2 组件：引入 jQuery 3.7.1 + Select2 4.1.0（vendor 目录），AJAX 模式搜索 + 分页 + tab 切换（技能市场/仓库技能）
95. ✓ Select2 苹果风格 CSS 覆盖：圆角 8px、边框 var(--b)、focus 蓝色边框 var(--a)、选项 hover rgba(0,113,227,.08)、optgroup 分组标题 var(--t3)
96. ✓ 编排页 skill select tab 切换：select 上方加技能市场/仓库技能 tab，切换时销毁并重新初始化 Select2（带 scope 参数），自动打开下拉
97. ✓ 编辑模式 tab 自动恢复：_loadAgentSkillOptions 根据 node.skill_scope 初始化 _skillSelectScope 并同步 tab 高亮
98. ✓ 后端 list_skills_for_agent 修复：去掉 user scope 查询（个人场景已移除），repo scope 用 repo_key（从 repo 参数获取，app_current_repo 是 placeholder 返回 None）匹配
99. ✓ 后端 skills_detail 加 scope 字段：SQL 查询加 scope 列，skills_detail.append 加 scope，前端编辑模式预填 node.skill_scope
100. ✓ 仓库场景编排页移除样例项目模板：删除模板下拉 HTML + _loadEditorMeta 模板加载 + _renderSceneEditor 模板预填 + _saveScene recommended_template 字段 + _sceneEditorTemplates 变量 + 4 个模板函数（_openTemplateDropdown/_renderTemplateDropdownList/_selectTemplate/_onEditorTemplateChange）
101. ✓ 仓库场景详情页不展示样例项目模板：if (s.recommended_template && isOfficial) 仅 official 展示
102. ✓ 个人技能去掉点赞功能：skills.js 卡片列表 scope='user' 时不显示点赞按钮和精选标记
103. ✓ 个人技能详情页去掉点赞和精选：renderSkillDetail 中 isUserDetail 时不显示精选标记、点赞按钮、精选信息行、点赞信息行
104. ✓ 个人技能列表页去掉最热按钮：_skillTab === 'user' 时不渲染 sort-hot 按钮
105. ✓ 场景详情页技能描述截断优化：det-card-desc 加 title 属性（悬停显示完整描述）+ CSS -webkit-line-clamp:3 最多 3 行超出省略号
106. ✓ 编排页技能描述同步优化：det-card-desc 加 title 属性 + CSS 截断样式
107. ✓ scene_sync 移除自动下架逻辑：同步只做新增+更新，不再自动 is_active=0 下架 official 场景；下架由运营管理台手动操作
# 场景同步优化：逐文件 API 下载 → ZIP 归档下载

> 日期：2026-09-07
> 关联设计：[issue-11-scene-skill-market-tech-design.md §8.6](./issue-11-scene-skill-market-tech-design.md)
> 影响文件：`src/storage/scene_sync.py`、`tests/test_scene_sync.py`
> 不影响：DB 表结构、configs、Dockerfile、API 接口、前端

---

## 一、背景

### 1.1 现状

`src/storage/scene_sync.py` 通过 GitCode API v5 逐文件下载场景：

1. `_api_list_dir` — 调 `/api/v5/repos/{owner}/{repo}/contents/{path}` 列目录
2. `_api_get_file` — 逐个文件下载（base64 解码）
3. `_download_dir_recursive` — 递归遍历目录，根目录只取 scene.json/README.md/icon.*，references/ 下全量下载

7 个场景 × 每个 2-3 个文件 = 20+ 次 HTTP 请求。

### 1.2 问题

| 问题 | 说明 |
|------|------|
| 请求数多 | 20+ 次 API 调用，每次都有网络开销和 429 限流风险 |
| 递归下载脆弱 | 单文件失败需 per-file try-catch 兜底，逻辑复杂 |
| 下架基准不可靠 | 需要 `repo_scene_names`（API 列目录结果）做下架基准，因为逐文件下载可能部分成功部分失败。这是上一轮修 bug 时加的补丁，本质是逐文件下载的中间态问题 |
| 分页防御冗余 | `_api_list_dir` 加了分页循环防御，但 GitCode contents 接口当前忽略分页参数全量返回，循环只跑一轮——多余代码 |

### 1.3 原设计文档的理由（已证伪）

原设计文档 §8.6（第 1133-1137 行）选择逐文件下载而非 ZIP 归档下载，理由：

> - GitCode 的 ZIP 归档 URL（`/-/archive/{ref}/{repo}-{ref}.zip`）返回 HTML 页面而非 ZIP 文件，无法直接下载

**实测结论**：`https://gitcode.com/{owner}/{repo}/-/archive/{ref}.zip` 确实返回 HTML（SPA 前端拦截，`x-rendered-by: proxy`）。但 GitCode 真实的 ZIP 下载域名是 `raw.gitcode.com`，URL 格式为：

```
https://raw.gitcode.com/{owner}/{repo}/archive/refs/heads/{branch}.zip
```

这个端点实测可用（公开仓库直接下载，私有仓库用 `Authorization: Bearer {token}` 认证），返回标准 ZIP 文件。

---

## 二、实测验证

### 2.1 测试环境

- 仓库：`grape-dev/grape-skills`（私有），分支 `main`
- token：`configs/scene-sync.json` 的 `gitcode_api_token`
- 工具：curl + Python httpx

### 2.2 测试结果

| URL | 认证方式 | 结果 |
|-----|---------|------|
| `gitcode.com/{owner}/{repo}/-/archive/{ref}.zip` | PRIVATE-TOKEN / Bearer / access_token / Basic auth / oauth2 URL 嵌入 | 全部返回 HTML（3576 字节 SPA 壳） |
| `api.gitcode.com/api/v5/repos/{owner}/{repo}/archive/{ref}.zip` | Bearer | 404 |
| `api.gitcode.com/api/v5/repos/{owner}/{repo}/repository/archive` | Bearer | 404 |
| `raw.gitcode.com/{owner}/{repo}/archive/refs/heads/{branch}.zip` | 无（公开仓库） | ✅ ZIP 文件（1.8MB） |
| `raw.gitcode.com/{owner}/{repo}/archive/refs/heads/{branch}.zip` | Authorization: Bearer {token} | ✅ ZIP 文件（637KB，私有仓库 grape-skills） |
| `raw.gitcode.com/{owner}/{repo}/archive/refs/heads/{branch}.zip` | access_token={token} 参数 | ✅ ZIP 文件（md5 与 Bearer 一致） |
| `raw.gitcode.com/{owner}/{repo}/archive/refs/heads/{branch}.zip` | oauth2:{token} URL 嵌入 | ❌ JSON 错误（不支持 Basic auth） |

### 2.3 发现过程

GitCode 前端是 Vue SPA，`/-/archive/` 路径被前端代理拦截返回 `index.html`。用 Playwright 渲染仓库页面，点击"下载zip"按钮，监听到浏览器实际发起的下载请求指向 `raw.gitcode.com` 域名。

### 2.4 ZIP 包内容

```
grape-skills-dev/              ← 顶层目录 {repo}-{branch}
├── .gitignore
├── README.md
├── skill-description.json
└── scenes/
    ├── agent-dev/
    │   ├── README.md
    │   └── scene.json
    ├── blog-publish/
    │   ├── README.md
    │   └── scene.json
    ├── cloud-native/
    │   ├── README.md
    │   └── scene.json
    ├── common-skill-dev/
    │   ├── README.md
    │   └── scene.json
    ├── default/
    │   ├── README.md
    │   └── scene.json
    ├── huawei-skill/
    │   ├── README.md
    │   └── scene.json
    └── iac/
        ├── README.md
        └── scene.json
```

注意：ZIP 包不含 `.git` 目录，是分支最新代码的文件快照。

---

## 三、方案

### 3.1 改动本质

当前：逐文件调 GitCode API v5 列目录 + 下载，7 个场景 20+ 次请求
改后：一次 HTTP 请求下载整个仓库分支 ZIP 包，zipfile 标准库解压后遍历 scenes/ 目录

### 3.2 新增函数

#### `_download_repo_zip(client, token) -> str`

```python
async def _download_repo_zip(client: httpx.AsyncClient, token: str) -> str:
    """下载仓库 ZIP 归档并解压到临时目录，返回解压后的本地根路径。

    URL: https://raw.gitcode.com/{owner}/{repo}/archive/refs/heads/{branch}.zip
    认证: Authorization: Bearer {token}
    ZIP 内顶层目录名: {repo}-{branch}
    """
```

- 下载 ZIP bytes → `zipfile.ZipFile(io.BytesIO(data))` 解压到临时目录
- 返回解压后的本地路径（`{tmp_dir}/{repo}-{branch}/`）
- 下载失败抛异常，由调用方 `sync_official_scenes` 的 try-catch 处理

### 3.3 删除函数

| 函数 | 行数 | 删除原因 |
|------|------|---------|
| `_api_list_dir` | 202-229 | 逐文件列目录，不再需要 |
| `_api_get_file` | 232-243 | 逐文件下载，不再需要 |
| `_download_dir_recursive` | 246-275 | 递归下载目录，不再需要 |
| `import base64` | 15 | 只被 `_api_get_file` 用 |

### 3.4 重写 `sync_official_scenes` 主流程

**删除**（第 324-355 行）：API 列目录 + 逐场景下载循环 + `repo_scene_names`

**替换为**：

```python
# 1. 下载 ZIP 归档并解压
try:
    repo_root = await _download_repo_zip(client, bot_token)
except Exception as e:
    log.warning(f"Scene sync: download repo zip failed: {e}")
    return

# 2. 定位 scenes/ 目录
scenes_dir = os.path.join(repo_root, "scenes")
if not os.path.isdir(scenes_dir):
    log.warning("Scene sync: scenes/ directory not found in zip")
    return
```

**不变**（第 363-525 行）：刷新 category 缓存 → 遍历场景目录 → 读 scene.json → 校验 → 入库 → 重建 scene_skill_ref

### 3.5 下架逻辑变化

| | 当前 | 改后 |
|---|------|------|
| 代码 | `db_names - repo_scene_names` | `db_names - synced_names` |
| 基准 | API 列目录结果 | 成功入库的场景集合 |
| 原因 | 逐文件下载可能部分成功部分失败，需要 API 列目录结果做兜底基准 | ZIP 包一次性全量下载，要么全部成功要么全部失败。下载失败时 sync_official_scenes 在下载阶段就 return，不会走到下架逻辑 |

`repo_scene_names` 变量删除。

### 3.6 模块文档字符串更新

```
1. 每 30 分钟从 grape-skills 仓库通过 GitCode API v5 逐文件下载 scenes/ 目录
```
→
```
1. 每 30 分钟从 grape-skills 仓库下载 ZIP 归档包并解压 scenes/ 目录
```

### 3.7 不改动

| 模块 | 说明 |
|------|------|
| `_load_config` / `_get_sync_token` | 配置和 token 获取逻辑不变 |
| `_compute_scene_hash` | hash 计算逻辑不变 |
| `_validate_scene_nodes` | nodes 校验逻辑不变 |
| `_check_skills_exist` | skill 预检不变 |
| `_parse_version` | 版本比较不变 |
| `_refresh_cat_cache` | category 缓存不变 |
| `_rebuild_scene_skill_ref` / `_rebuild_scene_skill_ref_by_name` | skill_ref 重建不变 |
| `_scene_sync_worker` / `start_scene_sync_scheduler` / `stop_scene_sync_scheduler` | 调度逻辑不变 |
| `configs/scene-sync.json` | token 和 repo 配置复用 |
| `Dockerfile` | 不依赖 git 命令，zipfile 是 Python 标准库 |
| DB 表结构 | 不变 |
| API 接口 / 前端 | 不变 |

---

## 四、测试改动

### 4.1 删除

| 测试类/函数 | 测试数 | 删除原因 |
|------------|--------|---------|
| `TestApiListDir` | 4 | `_api_list_dir` 不再存在 |
| `TestApiGetFile` | 2 | `_api_get_file` 不再存在 |
| `TestDownloadDirRecursive` | 5 | `_download_dir_recursive` 不再存在 |
| `test_download_failure_continues` | 1 | 逐场景下载失败场景不再适用 |
| `test_download_failure_does_not_delist` | 1 | `repo_scene_names` 补丁不再需要 |
| import 里的 `_api_list_dir` / `_api_get_file` / `_download_dir_recursive` | — | 清理 import |

### 4.2 新增

| 测试 | 验证点 |
|------|--------|
| `TestDownloadRepoZip.test_download_and_extract` | mock httpx 返回 ZIP bytes，验证解压后目录结构正确 |
| `TestDownloadRepoZip.test_auth_header` | 验证请求头包含 `Authorization: Bearer {token}` |
| `test_zip_download_failure_skips` | ZIP 下载失败时 `sync_official_scenes` 直接 return，不执行下架 |
| `test_full_sync_new_scene` | 改为 mock `_download_repo_zip`（替代原来的 mock `_download_dir_recursive`） |

### 4.3 保留不变

| 测试 | 数量 |
|------|------|
| `TestComputeSceneHash` | 4 |
| `TestValidateSceneNodes` | 5 |
| `TestCheckSkillsExist` | 2 |
| `TestParseVersion` | 1 |
| `test_no_token_skips` | 1 |
| `test_only_template_dirs_skips` | 1（调整 mock 路径） |
| `_rebuild_scene_skill_ref` 相关 | 不变 |

### 4.4 预计

- 当前：45 个测试
- 改后：~35 个测试（删 13 个，新增 4 个）

---

## 五、影响评估

| 维度 | 影响 |
|------|------|
| DB | 无变更 |
| API | 无变更 |
| 前端 | 无变更 |
| 配置 | 无变更（`configs/scene-sync.json` 复用） |
| Dockerfile | 无变更（zipfile 是标准库，不依赖 git） |
| 依赖 | 无新增（zipfile + io 都是 Python 标准库，httpx 已有） |
| 部署 | 无变化（改 Python 代码后重启 3000 端口即生效） |

---

## 六、确认点

1. ZIP 下载 URL：`https://raw.gitcode.com/{owner}/{repo}/archive/refs/heads/{branch}.zip`
2. 认证方式：`Authorization: Bearer {token}`（不用 access_token URL 参数，避免日志泄露）
3. ZIP 包内顶层目录名：`{repo}-{branch}`，scenes/ 在其下一级，`_download_repo_zip` 返回后由 `sync_official_scenes` 拼接 `os.path.join(repo_root, "scenes")`
4. 下架基准回到 `db_names - synced_names`，删除 `repo_scene_names`
5. 不改 Dockerfile，不依赖 git 命令

108. ✓ 技能同步移除手动接口：删除 POST /api/skills/sync-official 路由 + 前端调用；DB 元信息同步（INSERT/UPDATE from skill-description.json）合并到定时任务 sync_official_skills_to_obs 中，保证两边逻辑一致

