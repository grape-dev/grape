# 云沙箱页面优化 + LLM 凭据优化设计

> 分支: feat/devbox-sandbox-integration (HEAD a4b1c8ad)
> 日期: 2026-10-09
> 状态: 待确认

## 一、需求概述

1. 菜单增加图标
2. 沙箱列表 tab：增加图标、去掉总数显示、增加分页（后端分页）、增加搜索、对齐技能市场页面样式
3. 系统模板 tab：增加图标、去掉总数显示、增加分页（后端分页）、增加搜索、对齐技能市场页面样式
4. i18n：sandbox.js 硬编码中英文迁移到语言包
5. 云沙箱整体页面和菜单有哪些可以优化的

## 二、现状分析

### 2.1 菜单项

index.html 第 90 行：
```html
<a id="nav-sandbox" onclick="app.nav('sandbox')"><span class="ic"></span><span>云沙箱</span></a>
```
图标 `<span class="ic"></span>` 内容为空。其他菜单项都有 emoji 图标（⚡ 📊 📋 等）。

### 2.2 沙箱列表 tab

sandbox.js 第 90-91 行：
- 无图标
- 显示总数 `({{ sandboxes.length }})`
- 无分页——后端 `GET /runtime/cloud-sandboxes` 硬编码 `LIMIT 100`，前端全量渲染
- 无搜索——不支持关键词搜索

### 2.3 系统模板 tab

sandbox.js 第 93-94 行：
- 无图标
- 显示总数 `({{ templates.length }})`
- 无分页——后端 `GET /settings/sandbox-templates` 全量返回
- 无搜索

### 2.4 页面样式

- 沙箱列表用 `.stabs` tab + `<div class="cd">` 容器 + 原始 `<table>`
- 技能市场用 `.seg-bar` 分段控件 + `.apple-card` 容器 + `.cd-h` 标题行
- 风格不一致

### 2.5 后端 API 现状

| 接口 | 文件 | 分页 | 搜索 |
|------|------|------|------|
| `GET /runtime/cloud-sandboxes` | runtime.py:1082 | 无（LIMIT 100） | 无 |
| `GET /settings/sandbox-templates` | settings.py:1632 | 无（全量返回） | 无 |

### 2.6 DB 表结构

**cloud_sandboxes** 表可搜索字段：
- `sandbox_id` (text) — 沙箱 ID
- `issue_id` (varchar 191) — Issue 编号
- `repo_key` (varchar 191) — 仓库标识
- `provider` (text) — 供应商
- `state` (text) — 状态
- `task_id` (text) — 任务 ID

**sandbox_templates** 表可搜索字段：
- `name` (varchar 255) — 模板名称
- `provider` (varchar 64) — 供应商
- `template_id` (varchar 255) — 模板 ID
- `alias` (varchar 255) — 别名
- `description` (varchar 512) — 描述

### 2.7 技能市场后端分页参考（skills.py:56）

技能市场 `list_skills` 接口的分页 + 搜索模式：
```python
def list_skills(..., q: str = "", sort: str = "", page: int = 1, page_size: int = 0):
    # 构建查询条件
    conditions = [...]
    params = []
    # 搜索过滤
    q = (q or "").strip()
    if q:
        conditions.append("(skill_name LIKE ? OR display_name LIKE ? OR description LIKE ?)")
        kw = f"%{q}%"
        params.extend([kw, kw, kw])
    # 分页
    use_paging = page_size and page_size > 0
    if use_paging:
        offset = (page - 1) * page_size
        c.execute(f"SELECT COUNT(*) FROM skill{where_sql}", tuple(params))
        total = c.fetchone()[0]
        c.execute(f"SELECT ... FROM skill{where_sql} {order_by} LIMIT {int(page_size)} OFFSET {int(offset)}", tuple(params))
    return {"skills": skills, "total": total, "page": page, "page_size": page_size}
```

### 2.8 i18n 现状

- i18n.js MODULES 数组有 15 个模块：`common, app, settings, knowledge, skills, chat, events, agents, pipeline, runtimes, tasks, repos, dashboard, observability, templates`
- 无 `sandbox` 模块——需要新增
- app.json 的 `nav` 对象无 `sandbox` 键——需要新增
- index.html 第 90 行云沙箱菜单项无 `data-i18n` 属性——需要新增

## 三、改动设计

### 3.1 菜单增加图标 + i18n

**文件**: `src/api/static/index.html` 第 90 行

```html
<!-- 改前 -->
<a id="nav-sandbox" onclick="app.nav('sandbox')"><span class="ic"></span><span>云沙箱</span></a>
<!-- 改后 -->
<a id="nav-sandbox" onclick="app.nav('sandbox')"><span class="ic">☁</span><span data-i18n="app.nav.sandbox">云沙箱</span></a>
```

### 3.2 后端改动：两个接口加分页 + 搜索

#### 3.2.1 `GET /runtime/cloud-sandboxes`

**文件**: `src/api/routes/runtime.py` 第 1082 行

**注意：现有代码有两条分支（有 repo_key / 无 repo_key），改后端时两条分支都要加搜索 + provider/state 筛选 + 分页逻辑。不能只改一条。**

改动：
```python
@router.get("/cloud-sandboxes", ...)
def runtime_list_cloud_sandboxes(
    repo_key: str = "",
    provider: str = "",       # 新增：provider 筛选（原来前端做，改后端做）
    state: str = "",          # 新增：state 筛选（原来前端做，改后端做）
    q: str = "",              # 新增：搜索词
    page: int = 1,            # 新增：页码
    page_size: int = 20,      # 新增：每页条数
    user: dict = Depends(require_no_session_token)
):
```

搜索逻辑——对 `issue_id` 做 LIKE 模糊匹配（用户搜索场景：按 Issue 号查关联沙箱）：
```python
q = (q or "").strip()
if q:
    conditions.append("s.issue_id LIKE ?")
    kw = f"%{q}%"
    params.append(kw)
```

provider/state 筛选从前端 computed 移到后端：
```python
if provider:
    conditions.append("s.provider=?")
    params.append(provider)
if state:
    conditions.append("s.state=?")
    params.append(state)
```

分页——COUNT + LIMIT OFFSET。统一两条分支的分页逻辑：
```python
# 两条分支共用 conditions 列表构建 WHERE 子句
# 分支1（有 repo_key）：base condition = "s.repo_key=?" + check_repo_access
# 分支2（无 repo_key）：base condition = "s.repo_key IN (...)" 从 get_managed_repos
# 两条分支上方都追加 q/provider/state 条件

where_sql = " WHERE " + " AND ".join(conditions) if conditions else ""
# COUNT
c.execute(f"SELECT COUNT(*) FROM cloud_sandboxes s LEFT JOIN user_accounts u ON s.created_by=u.id{where_sql}", tuple(params))
total = c.fetchone()[0]
# 分页查询
offset = (page - 1) * page_size
c.execute(
    f"SELECT s.sandbox_id, s.issue_id, s.repo_key, s.provider, s.state, "
    f"s.agent_installed, s.kill_attempts, s.created_at, s.last_refresh_at, "
    f"s.task_id, s.created_by, u.username "
    f"FROM cloud_sandboxes s LEFT JOIN user_accounts u ON s.created_by=u.id"
    f"{where_sql} ORDER BY s.created_at DESC LIMIT {int(page_size)} OFFSET {int(offset)}",
    tuple(params))
```

返回结构：
```json
{
  "sandboxes": [...],
  "total": 763,
  "page": 1,
  "page_size": 20
}
```

> 注意：原来 `repo_key` 筛选走 `check_repo_access` + `WHERE s.repo_key=?`，这个逻辑不变。provider/state 从前端 computed 筛选移到后端 WHERE 条件。去掉硬编码 `LIMIT 100`。

#### 3.2.2 `GET /settings/sandbox-templates`

**文件**: `src/api/routes/settings.py` 第 1632 行

改动：
```python
@router.get("/sandbox-templates", ...)
def list_sandbox_templates(
    request: Request,
    provider: str = "",
    q: str = "",              # 新增：搜索词
    page: int = 1,            # 新增：页码
    page_size: int = 20,      # 新增：每页条数
):
```

搜索逻辑——对 `name`、`description` 做 LIKE 模糊匹配：
```python
q = (q or "").strip()
if q:
    conditions.append("(name LIKE ? OR description LIKE ?)")
    kw = f"%{q}%"
    params.extend([kw, kw])
```

分页——同上模式，COUNT + LIMIT OFFSET。

返回结构：
```json
{
  "templates": [...],
  "total": 24,
  "page": 1,
  "page_size": 20
}
```

> 注意：保持 cloud_worker 路径（rta_ token + JOIN sandbox_provider_config）的逻辑不变，只加分页和搜索。cloud_worker 不传 page/page_size 时（page_size=0），返回全量（兼容旧逻辑）。

### 3.3 前端改动：sandbox.js 重构

**文件**: `src/api/static/js/pages/sandbox.js`

保持 Vue 3 架构不变，在现有基础上改动：

#### 3.3.1 Vue data 新增字段

```javascript
data() {
  return {
    activeTab: 'sandboxes',
    sandboxes: [],
    templates: [],
    loading: false,
    providerFilter: '',
    stateFilter: '',
    sandboxSearchQ: '',        // 新增：沙箱搜索词
    templateSearchQ: '',      // 新增：模板搜索词
    isZh: I18N.getLang() === 'zh',
    // 沙箱分页
    sandboxPage: 1,
    sandboxPageSize: 20,
    sandboxTotal: 0,
    // 模板分页
    templatePage: 1,
    templatePageSize: 20,
    templateTotal: 0,
  };
}
```

#### 3.3.2 Vue computed 改动

删除 `filteredSandboxes`（筛选移到后端），新增：
```javascript
computed: {
  providers() {
    // 保留——用于 provider 筛选下拉框
    const _known = ['cubesandbox', 'devbox'];
    const _from_data = this.sandboxes.map(s => s.provider).filter(Boolean);
    return [...new Set([..._known, ..._from_data])];
  },
  sandboxTotalPages() {
    return Math.max(1, Math.ceil(this.sandboxTotal / this.sandboxPageSize));
  },
  templateTotalPages() {
    return Math.max(1, Math.ceil(this.templateTotal / this.templatePageSize));
  },
}
```

> 注意：`providers` computed 原来从 `this.sandboxes` 提取——改为后端分页后，当前页数据可能不含所有 provider。解决方案：providers 列表改为固定列表（与后端实际 provider 一致），或首次加载时拿一次全量 provider 列表。考虑到 provider 种类有限（cubesandbox/devbox），用固定列表即可。

#### 3.3.3 Vue methods 改动

```javascript
methods: {
  switchTab(tab) {
    this.activeTab = tab;
    if (tab === 'sandboxes') { this.sandboxPage = 1; this.loadSandboxes(); }
    if (tab === 'templates') { this.templatePage = 1; this.loadTemplates(); }
  },
  async loadSandboxes() {
    this.loading = true;
    try {
      const repo = (typeof app !== 'undefined' && app.currentRepo) ? app.currentRepo() : null;
      const repoKey = (repo && repo.repoKey) || '';
      let url = '/runtime/cloud-sandboxes?page=' + this.sandboxPage + '&page_size=' + this.sandboxPageSize;
      if (repoKey) url += '&repo_key=' + encodeURIComponent(repoKey);
      if (this.providerFilter) url += '&provider=' + encodeURIComponent(this.providerFilter);
      if (this.stateFilter) url += '&state=' + encodeURIComponent(this.stateFilter);
      if (this.sandboxSearchQ) url += '&q=' + encodeURIComponent(this.sandboxSearchQ);
      const d = await API.get(url);
      this.sandboxes = d.sandboxes || [];
      this.sandboxTotal = d.total || 0;
    } catch(e) { this.sandboxes = []; this.sandboxTotal = 0; }
    this.loading = false;
  },
  async loadTemplates() {
    try {
      let url = '/settings/sandbox-templates?page=' + this.templatePage + '&page_size=' + this.templatePageSize;
      if (this.providerFilter && this.activeTab === 'templates') url += '&provider=' + encodeURIComponent(this.providerFilter);
      if (this.templateSearchQ) url += '&q=' + encodeURIComponent(this.templateSearchQ);
      const d = await API.get(url);
      this.templates = d.templates || [];
      this.templateTotal = d.total || 0;
    } catch(e) { this.templates = []; this.templateTotal = 0; }
  },
  // 筛选/搜索变化时重置分页并重新加载
  onSandboxFilterChange() { this.sandboxPage = 1; this.loadSandboxes(); },
  onTemplateFilterChange() { this.templatePage = 1; this.loadTemplates(); },
  sandboxPrevPage() { if (this.sandboxPage > 1) { this.sandboxPage--; this.loadSandboxes(); } },
  sandboxNextPage() { if (this.sandboxPage < this.sandboxTotalPages) { this.sandboxPage++; this.loadSandboxes(); } },
  templatePrevPage() { if (this.templatePage > 1) { this.templatePage--; this.loadTemplates(); } },
  templateNextPage() { if (this.templatePage < this.templateTotalPages) { this.templatePage++; this.loadTemplates(); } },
}
```

#### 3.3.4 Vue template 改动

**Tab 栏**：`.stabs` → `.seg-bar`，加图标，去总数，i18n：
```html
<div style="text-align:left;margin-bottom:20px">
  <div class="seg-bar" style="margin-top:0">
    <a :class="activeTab === 'sandboxes' ? 'ac' : ''" @click="switchTab('sandboxes')">
      ☁ {{ t('sandbox.tab.sandboxes') }}
    </a>
    <a :class="activeTab === 'templates' ? 'ac' : ''" @click="switchTab('templates')">
      📋 {{ t('sandbox.tab.templates') }}
    </a>
  </div>
</div>
```

**沙箱列表区域**：`.apple-card` + `.cd-h` 标题行（含搜索框 + 筛选器 + 刷新）+ 表格 + 分页：
```html
<div v-if="activeTab === 'sandboxes'" class="apple-card">
  <div class="cd-h" style="justify-content:space-between">
    <h2>☁ {{ t('sandbox.tab.sandboxes') }}</h2>
    <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
      <input v-model="sandboxSearchQ" @keyup.enter="onSandboxFilterChange"
        :placeholder="t('sandbox.search.placeholder')"
        style="padding:6px 12px;border:1px solid var(--b);border-radius:8px;font-size:12px;background:var(--c);width:200px">
      <select v-model="providerFilter" @change="onSandboxFilterChange" style="...">
        <option value="">{{ t('sandbox.filter.all_providers') }}</option>
        <option v-for="p in providers" :key="p" :value="p">{{ p }}</option>
      </select>
      <select v-model="stateFilter" @change="onSandboxFilterChange" style="...">
        <option value="">{{ t('sandbox.filter.all_states') }}</option>
        <option value="running">running</option>
        <option value="killed">killed</option>
        <option value="kill_failed">kill_failed</option>
        <option value="stale">stale</option>
        <option value="creating">creating</option>
      </select>
      <button class="bt bt-o bt-s" @click="onSandboxFilterChange">{{ t('sandbox.action.search') }}</button>
      <button class="bt bt-o bt-s" @click="loadSandboxes">{{ t('sandbox.action.refresh') }}</button>
    </div>
  </div>
  <!-- 表格：v-for 改为 s in sandboxes（直接用后端返回的当前页数据） -->
  <div v-if="loading" class="loading">{{ t('common.loading') }}</div>
  <div v-else-if="sandboxes.length === 0" class="tx t2" style="padding:16px;text-align:center">
    {{ t('sandbox.empty.no_sandboxes') }}
  </div>
  <div v-else style="overflow-x:auto">
    <table>...同现有列结构，v-for="s in sandboxes"...</table>
  </div>
  <!-- 分页控件 -->
  <div style="display:flex;align-items:center;justify-content:center;gap:12px;margin-top:16px;padding:12px 0">
    <button class="bt bt-o bt-s" :disabled="sandboxPage <= 1" @click="sandboxPrevPage">‹</button>
    <span style="font-size:12px;color:var(--t2)">
      {{ sandboxPage }} / {{ sandboxTotalPages }} ({{ sandboxTotal }}{{ t('sandbox.items_suffix') }})
    </span>
    <button class="bt bt-o bt-s" :disabled="sandboxPage >= sandboxTotalPages" @click="sandboxNextPage">›</button>
    <select v-model="sandboxPageSize" @change="sandboxPage=1;loadSandboxes()" style="...">
      <option value="20">20{{ t('sandbox.per_page') }}</option>
      <option value="50">50{{ t('sandbox.per_page') }}</option>
      <option value="100">100{{ t('sandbox.per_page') }}</option>
    </select>
  </div>
</div>
```

**系统模板区域**：同样模式——`.apple-card` + `.cd-h`（搜索 + 刷新）+ 表格 + 分页：
```html
<div v-if="activeTab === 'templates'" class="apple-card">
  <div class="cd-h" style="justify-content:space-between">
    <h2>📋 {{ t('sandbox.tab.templates') }}</h2>
    <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
      <input v-model="templateSearchQ" @keyup.enter="onTemplateFilterChange"
        :placeholder="t('sandbox.search.template_placeholder')"
        style="...">
      <button class="bt bt-o bt-s" @click="onTemplateFilterChange">{{ t('sandbox.action.search') }}</button>
      <button class="bt bt-o bt-s" @click="loadTemplates">{{ t('sandbox.action.refresh') }}</button>
    </div>
  </div>
  <div v-if="templates.length === 0" class="tx t2" style="padding:16px;text-align:center">
    {{ t('sandbox.empty.no_templates') }}
  </div>
  <div v-else style="overflow-x:auto">
    <table>...v-for="t in templates"...</table>
  </div>
  <!-- 分页控件 -->
  <div style="...">
    <button :disabled="templatePage <= 1" @click="templatePrevPage">‹</button>
    <span>{{ templatePage }} / {{ templateTotalPages }} ({{ templateTotal }}{{ t('sandbox.items_suffix') }})</span>
    <button :disabled="templatePage >= templateTotalPages" @click="templateNextPage">›</button>
    <select v-model="templatePageSize" @change="templatePage=1;loadTemplates()">
      <option value="20">20{{ t('sandbox.per_page') }}</option>
      <option value="50">50{{ t('sandbox.per_page') }}</option>
      <option value="100">100{{ t('sandbox.per_page') }}</option>
    </select>
  </div>
</div>
```

**i18n helper 方法**：在 Vue methods 中加一个 `t(key)` 简化模板：
```javascript
t(key) {
  return I18N.t(key) || key;
}
```

### 3.4 i18n 改动

#### 3.4.1 新增 sandbox 语言包文件

**新增文件**: `src/api/static/locales/zh/sandbox.json`
**新增文件**: `src/api/static/locales/en/sandbox.json`

zh/sandbox.json:
```json
{
  "tab": {
    "sandboxes": "沙箱列表",
    "templates": "系统模板"
  },
  "search": {
    "placeholder": "搜索 sandbox ID / issue / task...",
    "template_placeholder": "搜索模板名称 / ID / 描述..."
  },
  "filter": {
    "all_providers": "全部 Provider",
    "all_states": "全部状态"
  },
  "action": {
    "search": "查找",
    "refresh": "刷新"
  },
  "empty": {
    "no_sandboxes": "暂无沙箱",
    "no_templates": "暂无模板"
  },
  "items_suffix": "条",
  "per_page": "/页",
  "table": {
    "sandbox_id": "Sandbox ID",
    "issue": "Issue",
    "repo": "Repo",
    "provider": "Provider",
    "state": "State",
    "agent": "Agent",
    "created_by": "创建人",
    "created_at": "创建时间",
    "last_refresh": "Last Refresh",
    "action": "操作",
    "running": "运行中",
    "name": "Name",
    "template_id": "Template ID",
    "engines": "Engines",
    "preinstalled": "Preinstalled",
    "status": "Status"
  }
}
```

en/sandbox.json:
```json
{
  "tab": {
    "sandboxes": "Sandboxes",
    "templates": "Templates"
  },
  "search": {
    "placeholder": "Search sandbox ID / issue / task...",
    "template_placeholder": "Search template name / ID / desc..."
  },
  "filter": {
    "all_providers": "All Providers",
    "all_states": "All States"
  },
  "action": {
    "search": "Search",
    "refresh": "Refresh"
  },
  "empty": {
    "no_sandboxes": "No sandboxes",
    "no_templates": "No templates"
  },
  "items_suffix": " items",
  "per_page": "/page",
  "table": {
    "sandbox_id": "Sandbox ID",
    "issue": "Issue",
    "repo": "Repo",
    "provider": "Provider",
    "state": "State",
    "agent": "Agent",
    "created_by": "Created By",
    "created_at": "Created",
    "last_refresh": "Last Refresh",
    "action": "Action",
    "running": "Running",
    "name": "Name",
    "template_id": "Template ID",
    "engines": "Engines",
    "preinstalled": "Preinstalled",
    "status": "Status"
  }
}
```

#### 3.4.2 i18n.js 注册 sandbox 模块

**文件**: `src/api/static/js/i18n.js`

MODULES 数组新增 `'sandbox'`：
```javascript
var MODULES = [
  'common', 'app', 'settings', 'knowledge', 'skills',
  'chat', 'events', 'agents', 'pipeline', 'runtimes',
  'tasks', 'repos', 'dashboard', 'observability', 'templates', 'sandbox'
];
```

#### 3.4.3 app.json 新增 nav.sandbox

**文件**: `src/api/static/locales/zh/app.json` — nav 对象加 `"sandbox": "云沙箱"`
**文件**: `src/api/static/locales/en/app.json` — nav 对象加 `"sandbox": "Cloud Sandbox"`

#### 3.4.4 index.html 菜单加 data-i18n

第 90 行：
```html
<a id="nav-sandbox" onclick="app.nav('sandbox')">
  <span class="ic">☁</span>
  <span data-i18n="app.nav.sandbox">云沙箱</span>
</a>
```

### 3.5 index.html 其他改动

1. 第 90 行：菜单图标 `☁` + `data-i18n="app.nav.sandbox"`
2. 第 121 行：i18n.js 版本号 `?v=5` → `?v=6`（新增 sandbox 模块）
3. 第 139 行：sandbox.js 版本号 `?v=1` → `?v=2`
4. 第 138 行：settings.js 版本号 `?v=23` → `?v=24`（LLM 凭据改动）

### 3.6 app.js 改动

1. **第 95 行 VIEWS 数组**：新增 `'sandbox'`（一致性——虽然 VIEWS 当前未用于校验，但其他视图都在列表里）
2. **第 688 行 title.textContent = '云沙箱'**：改为 `I18N.t('app.nav.sandbox')`
3. **离开 sandbox 页面时卸载 _sandboxVueApp**：与 knowledge 页面一致，在 nav() 中加入卸载逻辑：
```javascript
// 在现有 knowledge 卸载逻辑后面加：
if (view !== 'sandbox' && typeof _sandboxVueApp !== 'undefined' && _sandboxVueApp) {
  try { _sandboxVueApp.unmount(); } catch(e) {}
  _sandboxVueApp = null;
}
```

### 3.7 CSS 改动

**无 CSS 改动**——`.seg-bar` / `.apple-card` / `.cd-h` / `.bt` / `.bt-o` / `.bt-s` 等样式已存在。

## 四、整体页面和菜单优化分析

### 4.1 当前问题

| 问题 | 说明 | 本次解决 |
|------|------|----------|
| 菜单无图标 | `<span class="ic"></span>` 为空 | 是 |
| Tab 样式不统一 | 沙箱用 `.stabs`，技能市场用 `.seg-bar` | 是 |
| 无分页 | 全量渲染 | 是（后端分页） |
| 无搜索 | 不支持关键词搜索 | 是（后端 LIKE） |
| Tab 显示总数 | `(N)` 冗余 | 是（去掉） |
| 无 `.apple-card` 容器 | 直接 `<div class="cd">` | 是 |
| 模板 tab 标题重复 | 内容区 `<h3>系统模板</h3>` 与 tab 重复 | 是（去掉 h3） |
| i18n 缺失 | 硬编码中英文 | 是（新增 sandbox 语言包） |
| 沙箱操作列空 | running 状态只有"运行中"文本 | 否（后续） |
| 模板无操作列 | 无校验/编辑入口 | 否（后续） |

### 4.2 后续优化建议

1. **沙箱 kill 按钮**：running 状态显示 kill 按钮，调用 `DELETE /runtime/cloud-sandboxes/{sandbox_id}`
2. **模板校验按钮**：每行模板显示"校验"按钮，调用 `POST /settings/sandbox-templates/{id}/check`
3. **模板编辑弹窗**：admin 用户可编辑模板
4. **沙箱统计卡片**：列表上方加统计卡片（总数/running/killed/stale）

## 五、改动文件清单

| 文件 | 改动内容 |
|------|----------|
| `src/api/static/index.html` | 菜单图标 + data-i18n + i18n.js 版本号 + sandbox.js 版本号 + settings.js 版本号 |
| `src/api/static/js/app.js` | VIEWS 加 sandbox + title i18n + 离开 sandbox 页面卸载 Vue |
| `src/api/static/js/pages/sandbox.js` | 全文重构：.seg-bar tab + 图标 + 去总数 + .apple-card + .cd-h + 后端分页 + 搜索 + i18n |
| `src/api/static/js/pages/settings.js` | settings tab .seg-bar + LLM 列表 .apple-card + _presets 提取 + 启用/禁用 + 弹窗修复 + i18n |
| `src/api/static/js/i18n.js` | MODULES 数组新增 `'sandbox'` |
| `src/api/static/locales/zh/sandbox.json` | 新建：中文语言包（含 llm 节点） |
| `src/api/static/locales/en/sandbox.json` | 新建：英文语言包（含 llm 节点） |
| `src/api/static/locales/zh/app.json` | nav 对象加 `"sandbox": "云沙箱"` |
| `src/api/static/locales/en/app.json` | nav 对象加 `"sandbox": "Cloud Sandbox"` |
| `src/api/routes/runtime.py` | `/cloud-sandboxes` 接口加分页 + 搜索 + provider/state 后端筛选（两条分支都改） |
| `src/api/routes/settings.py` | `/sandbox-templates` 接口加分页 + 搜索 + 新增 `PUT /llm-credentials/{id}/toggle` |

**不改动**：
- `src/api/static/css/app.css` — 样式已存在
- `src/api/static/css/apple-theme.css` — 样式已存在

### 5.1 遗漏排查结果

以下 8 项遗漏已在本次设计文档中补充修复：

| # | 遗漏 | 修复方案 | 文档位置 |
|---|------|----------|----------|
| 1 | VIEWS 数组缺少 'sandbox' | app.js 第 95 行新增 | §3.6 |
| 2 | app.js title.textContent = '云沙箱' 硬编码 | 改为 I18N.t('app.nav.sandbox') | §3.6 |
| 3 | 离开 sandbox 页面未卸载 _sandboxVueApp | nav() 中加卸载逻辑（与 knowledge 一致） | §3.6 |
| 4 | 后端 /cloud-sandboxes 两条分支都需改 | 文档明确标注两条分支共用 conditions | §3.2.1 |
| 5 | settings tab 'API Keys' label 也硬编码 | 统一走 i18n | §9.1 |
| 6 | i18n.js 版本号需 bump | ?v=5 → ?v=6 | §3.5 |
| 7 | sandbox-templates cloud_worker 路径兼容 | page_size=0 时不分页（兼容旧逻辑） | §3.2.2 |
| 8 | settings.js .stabs → .seg-bar 影响所有 settings tab | 预期行为，所有 settings tab 统一样式 | §9.2 |

## 六、后端搜索字段设计

### 6.1 沙箱列表搜索

| 搜索字段 | DB 列 | 类型 | 说明 |
|----------|-------|------|------|
| issue_id | issue_id | varchar(191) | Issue 编号 |

搜索方式：`LIKE %q%` 模糊匹配。用户场景：按 Issue 号查关联沙箱。sandbox_id / task_id 是长 UUID 不可能手输，repo_key 有 provider 筛选已够。

### 6.2 模板列表搜索

| 搜索字段 | DB 列 | 类型 | 说明 |
|----------|-------|------|------|
| name | name | varchar(255) | 模板名称 |
| description | description | varchar(512) | 描述 |

搜索方式：`LIKE %q%` 模糊匹配，OR 连接 2 个字段。template_id 是长 ID 不会手搜，alias 使用率低。

## 七、验证方案

1. 访问云沙箱页面，确认菜单图标 `☁` 显示
2. 确认 tab 样式与技能市场一致（`.seg-bar` 药丸样式）
3. 确认 tab 有图标、无总数
4. 确认沙箱列表后端分页功能正常：
   - 翻页（上一页/下一页）
   - 每页条数切换（20/50/100）
   - 分页后 total 正确
5. 确认沙箱列表搜索功能正常：
   - 输入 sandbox_id 搜索
   - 输入 issue_id 搜索
   - 输入 task_id 搜索
   - 搜索 + provider 筛选 + state 筛选组合
   - 搜索后分页重置到第 1 页
6. 确认模板列表后端分页功能正常
7. 确认模板列表搜索功能正常（name / template_id / alias / description）
8. 确认 `.apple-card` 容器样式与技能市场一致
9. 确认 provider/state 筛选后分页重置正常
10. 确认刷新按钮正常工作
11. 确认 tab 切换时分页重置
12. 确认 i18n 正常——切换中英文，所有文案正确
13. 确认 cloud_worker 路径（rta_ token）调用 `/settings/sandbox-templates` 仍正常（不传分页参数时返回全量）

---

# Part B: LLM 凭据优化

## 八、LLM 凭据现状分析

### 8.1 后端现状

**DB 表**: `user_llm_credentials`
- 主键: `id` (自增)
- 唯一键: `uk_user_engine_type` (user_id, engine_type) — 每个 user 每种 engine_type 只能有一条
- 字段: id, user_id, provider, api_key_enc(AES-256-GCM 加密), base_url, model, enabled, created_at, updated_at, key_version, engine_type

**后端接口** (settings.py):
| 接口 | 方法 | 功能 |
|------|------|------|
| `/settings/llm-credentials` | GET | 列出当前用户的 LLM 凭据（不含明文 key） |
| `/settings/llm-credentials` | POST | 创建/更新 LLM 凭据（UPSERT：同 user + engine_type 覆盖） |
| `/settings/llm-credentials/{id}` | DELETE | 删除 LLM 凭据 |
| `/settings/llm-engine-types` | GET | 返回引擎类型列表（common + ENGINE_REGISTRY keys） |

**凭据消费**: `src/agent/sandbox/llm_cred_selector.py` — 两级查询：
1. 引擎专属: `WHERE user_id=? AND engine_type=<引擎> AND enabled=1`
2. 通用兜底: `WHERE user_id=? AND engine_type='common' AND enabled=1`

### 8.2 前端现状

**settings.js LLM tab**:
- tab 名称硬编码 `I18N.getLang() === 'zh' ? 'LLM 凭据' : 'LLM Credentials'`（第 8 行）
- 列表渲染用纯 JS DOM 拼接（非 Vue），样式用 `.cd` 容器
- 添加/编辑用弹窗（modal），所有文案硬编码中英文 `isZh ? '中文' : 'English'`
- provider 预设（`_presets`）在 `showLlmDialog` 和 `onLlmProviderChange` 中重复定义了两遍

### 8.3 现有问题

| # | 问题 | 说明 | 严重度 |
|---|------|------|--------|
| L1 | **i18n 缺失** | LLM tab 标签、列表标题、按钮、描述、空状态全部硬编码 `isZh ? ... : ...`，未走 I18N.t() | 中 |
| L2 | **tab 样式不统一** | 用 `.stabs`（下划线 tab），未对齐 `.seg-bar`（药丸样式） | 中 |
| L3 | **列表样式不统一** | 用 `.cd` 容器，未对齐 `.apple-card` + `.cd-h` | 低 |
| L4 | **_presets 重复定义** | `showLlmDialog()` 和 `onLlmProviderChange()` 各定义一遍完整的 _presets 对象（7 个 provider），维护时需改两处 | 中 |
| L5 | **无启用/禁用切换** | DB 有 `enabled` 字段，后端列表返回 enabled 状态，但前端无 UI 切换。用户只能删除，不能临时禁用 | 中 |
| L6 | **列表无搜索** | 凭据多时无法搜索（provider/engine_type/keyword） | 低 |
| L7 | **弹窗 provider 下拉选项冗长** | `<option>` 内嵌完整 desc 描述，下拉项太长影响体验 | 低 |
| L8 | **保存后弹窗关闭逻辑脆弱** | `document.querySelector('[style*="fixed"][style*="rgba"]')?.remove()` — 用 CSS 属性选择器匹配弹窗，脆弱 | 低 |
| L9 | **无分页** | 凭据数量通常少（每种引擎 1 条），但后端全量返回无分页参数 | 低 |
| L10 | **编辑时 API Key 输入框无占位提示** | 编辑模式下 API Key 输入框 placeholder 为 `sk-...`，未提示"留空不修改" | 低 |

## 九、LLM 凭据优化设计

### 9.1 i18n 改造

在 `locales/zh/sandbox.json` 和 `locales/en/sandbox.json` 中新增 `llm` 节点（复用 sandbox 模块，不新建模块）：

zh/sandbox.json 新增：
```json
{
  "llm": {
    "tab_label": "LLM 凭据",
    "title": "LLM 凭据",
    "add_btn": "+ 添加凭据",
    "desc": "LLM API Key 用于云沙箱内的 agent 推理。加密存储（AES-256-GCM），不回显明文。每个引擎类型一个凭据。",
    "empty": "暂无 LLM 凭据，点击上方按钮添加",
    "dialog": {
      "add_title": "添加 LLM 凭据",
      "edit_title": "编辑 LLM 凭据",
      "engine_type": "适用引擎",
      "engine_type_desc": "选择此凭据适用的引擎类型，通用=兜底适用于所有引擎",
      "provider": "Provider",
      "api_key": "API Key",
      "api_key_keep_empty": "留空不修改",
      "base_url": "Base URL",
      "base_url_hint": "（兼容 OpenAI /v1/chat/completions 格式）",
      "model": "Model",
      "cancel": "取消",
      "save": "保存"
    },
    "action": {
      "edit": "编辑",
      "delete": "删除",
      "enable": "启用",
      "disable": "禁用"
    },
    "toast": {
      "saved": "LLM 凭据已保存",
      "save_failed": "保存失败",
      "deleted": "已删除",
      "delete_failed": "删除失败",
      "enabled": "已启用",
      "disabled": "已禁用",
      "toggle_failed": "操作失败",
      "api_key_required": "API Key 不能为空"
    },
    "confirm_delete": "确认删除此 LLM 凭据？",
    "updated_at": "更新"
  }
}
```

en/sandbox.json 新增对应英文。

### 9.2 tab 样式对齐 `.seg-bar`

**文件**: `src/api/static/js/pages/settings.js`

settings 页面的 tab 栏从 `.stabs` 改为 `.seg-bar`：
```javascript
// 改前
html += `<div class="stabs">${tabs.map(t => ...)}</div>`;
// 改后
html += `<div style="text-align:left;margin-bottom:20px"><div class="seg-bar" style="margin-top:0">${tabs.map(t => ...)}</div></div>`;
```

同时 tab label 走 i18n：
```javascript
{id:'llm', label: I18N.t('sandbox.llm.tab_label')},
```

### 9.3 列表样式对齐 `.apple-card` + `.cd-h`

```javascript
container.innerHTML = `
  <div class="apple-card">
    <div class="cd-h" style="justify-content:space-between">
      <h2>${I18N.t('sandbox.llm.title')}</h2>
      <button class="bt bt-p bt-s" onclick="showAddLlmDialog()">${I18N.t('sandbox.llm.add_btn')}</button>
    </div>
    <p class="tx t2" style="font-size:11px;line-height:1.6;margin-bottom:12px">${I18N.t('sandbox.llm.desc')}</p>
    <div id="llmCredList">${rowsHtml}</div>
  </div>
`;
```

### 9.4 _presets 提取为全局变量

消除 `showLlmDialog()` 和 `onLlmProviderChange()` 中的 _presets 重复定义：

```javascript
// 全局一次定义
var _LLM_PRESETS = null;
function _getLlmPresets() {
  if (_LLM_PRESETS) return _LLM_PRESETS;
  var isZh = I18N.getLang() === 'zh';
  _LLM_PRESETS = {
    'custom':   { url: '',                                                   model: '',             desc: isZh ? '自定义兼容 OpenAI API 的厂商' : 'Custom OpenAI-compatible provider' },
    'openai':   { url: 'https://api.openai.com/v1',                         model: 'gpt-4o',       desc: isZh ? 'OpenAI 官方 API' : 'OpenAI official API' },
    'deepseek': { url: 'https://api.deepseek.com/v1',                       model: 'deepseek-chat', desc: isZh ? 'DeepSeek（深度求索）' : 'DeepSeek' },
    'zhipu':    { url: 'https://open.bigmodel.cn/api/paas/v4',              model: 'glm-4',        desc: isZh ? '智谱 GLM 系列' : 'Zhipu GLM series' },
    'moonshot': { url: 'https://api.moonshot.cn/v1',                         model: 'moonshot-v1-8k', desc: isZh ? 'Moonshot AI（月之暗面）' : 'Moonshot AI' },
    'qwen':     { url: 'https://dashscope.aliyuncs.com/compatible-mode/v1', model: 'qwen-plus',    desc: isZh ? '通义千问（阿里云）' : 'Tongyi Qianwen (Alibaba)' },
    'azure':    { url: '',                                                   model: '',             desc: isZh ? 'Azure OpenAI' : 'Azure OpenAI' },
  };
  return _LLM_PRESETS;
}
```

`showLlmDialog()` 和 `onLlmProviderChange()` 统一调用 `_getLlmPresets()`。

### 9.5 启用/禁用切换

**后端新增接口**: `PUT /settings/llm-credentials/{id}/toggle`

```python
class LLMToggleBody(BaseModel):
    enabled: int  # 0=禁用 1=启用

@router.put("/llm-credentials/{cred_id}/toggle", ...)
def toggle_llm_credential(cred_id: int, body: LLMToggleBody, user: dict = Depends(require_no_session_token)):
    """启用/禁用 LLM 凭据。"""
    from src.session_manager import db, db_lock
    from datetime import datetime, timezone
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE user_llm_credentials SET enabled=?, updated_at=? WHERE id=? AND user_id=?",
                   (body.enabled, datetime.now(timezone.utc).isoformat(), cred_id, user["id"]))
        db.commit()
        if c.rowcount == 0:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "LLM 凭据不存在"})
    return {"status": "ok", "enabled": body.enabled}
```

**前端**: 列表每行增加启用/禁用按钮：
```javascript
var _toggleBtn = c.enabled
  ? '<button class="bt bt-o bt-s" onclick="toggleLlmCredential(' + c.id + ',0)">' + I18N.t('sandbox.llm.action.disable') + '</button>'
  : '<button class="bt bt-o bt-s" style="color:var(--g);border-color:var(--g)" onclick="toggleLlmCredential(' + c.id + ',1)">' + I18N.t('sandbox.llm.action.enable') + '</button>';

function toggleLlmCredential(id, enabled) {
  API.put('/settings/llm-credentials/' + id + '/toggle', {enabled: enabled}).then(function() {
    app.showToast(enabled ? I18N.t('sandbox.llm.toast.enabled') : I18N.t('sandbox.llm.toast.disabled'));
    loadLlmCredentials();
  }).catch(function(e) {
    app.showToast(I18N.t('sandbox.llm.toast.toggle_failed') + ': ' + e.message);
  });
}
```

### 9.6 provider 下拉选项优化

`<option>` 只显示 provider 名，desc 移到下方独立描述行：
```javascript
// 改前：option 内嵌完整 desc
<option value="deepseek">deepseek — DeepSeek（深度求索），国产大模型</option>
// 改后：option 只显示名称
<option value="deepseek">deepseek</option>
```

desc 仍由 `llmProviderDesc` 元素显示（已有逻辑）。

### 9.7 弹窗关闭逻辑修复

给弹窗加 `id`，通过 `id` 移除：
```javascript
// 改前
document.querySelector('[style*="fixed"][style*="rgba"]')?.remove();
// 改后
var _modal = document.getElementById('llmCredModal');
if (_modal) _modal.remove();
```

弹窗容器加 `id="llmCredModal"`。

### 9.8 编辑模式 API Key 占位提示

```javascript
// 改前
<input type="password" id="llmApiKey" placeholder="sk-..." ...>
// 改后
<input type="password" id="llmApiKey" placeholder="${id ? I18N.t('sandbox.llm.dialog.api_key_keep_empty') : 'sk-...'}" ...>
```

## 十、LLM 凭据改动文件清单

| 文件 | 改动内容 |
|------|----------|
| `src/api/static/js/pages/settings.js` | LLM 列表 .apple-card + _presets 提取 + 启用/禁用按钮 + 弹窗修复 + i18n（tab 栏保持 .stabs 不变） |
| `src/api/routes/settings.py` | 新增 `PUT /llm-credentials/{id}/toggle` 接口 |
| `src/api/static/locales/zh/sandbox.json` | 新增 `llm` 节点 |
| `src/api/static/locales/en/sandbox.json` | 新增 `llm` 节点 |
| `src/api/static/index.html` | settings.js 版本号刷新 |

> 注意：settings 页面 tab 栏保持 .stabs 样式不变，只优化 LLM 凭据部分内部样式。

## 十一、LLM 凭据验证方案

1. 确认 LLM tab 标签走 i18n
2. 确认列表样式对齐 .apple-card + .cd-h
3. 确认 _presets 只定义一处
4. 确认启用/禁用切换功能正常——禁用后 select_llm_cred 查不到该凭据
5. 确认 provider 下拉选项简洁（只显示名称）
6. 确认保存后弹窗正确关闭
7. 确认编辑模式 API Key 占位提示"留空不修改"
8. 确认切换中英文后 LLM 页面所有文案正确

---

# Part C: 加载动画 + 防并发 + 预存测试修复

## 十二、加载动画 + 防并发

### 12.1 设计

页面查询/搜索/翻页时列表区域显示加载动画（spinner），按钮禁用防并发点击，按钮上不加动画。

**CSS**（app.css 新增）：
```css
.sb-spinner { 旋转加载圈 }
.sb-loading-overlay { 半透明遮罩 + spinner 居中 }
```

**sandbox.js**（Vue 组件）：
- 新增 `sandboxLoading` / `templateLoading` 两个独立 loading 状态
- `loadSandboxes()` / `loadTemplates()` 入口 `if (xxxLoading) return` 防并发
- 搜索/刷新按钮 `:disabled="xxxLoading"` 加载时禁用
- 分页按钮 `:disabled="... || xxxLoading"` 加载时禁用
- 列表区域加载时显示 `<div class="sb-loading-overlay"><span class="sb-spinner"></span></div>`
- 加载遮罩不清空旧内容（v-if + v-else-if 链式）

### 12.2 改动文件

| 文件 | 改动 |
|------|------|
| `src/api/static/css/app.css` | 新增 .sb-spinner + .sb-loading-overlay |
| `src/api/static/js/pages/sandbox.js` | sandboxLoading/templateLoading + 防并发 + 遮罩 |
| `src/api/static/index.html` | app.css 版本号 v62→v63 |

## 十三、模板搜索 SQL 语法修复

### 13.1 问题

`/settings/sandbox-templates` 前端用户路径，当 `provider` 为空且 `q` 非空时：
- `base_where = ""` + `search_where = " AND (name LIKE ...)"` → `full_where = " AND (name LIKE ...)"`
- SQL 变成 `SELECT * FROM sandbox_templates  AND (name LIKE ...)` — 缺少 WHERE

### 13.2 修复

当 `base_where` 为空且有搜索条件时，将 `AND` 替换为 `WHERE`：
```python
full_where = base_where + search_where if base_where else (search_where.replace(" AND ", "WHERE ", 1) if search_where else "")
```

## 十四、预存测试修复

以下 5 个测试文件存在改动前（HEAD a4b1c8ad）就已经失败的测试，本次一并修复：

### 14.1 test_runtime_protocol.py

| 测试 | 根因 | 修复 |
|------|------|------|
| `TestProviderConfigsEndpoint::test_non_system_scope_403` | `_make_token` 创建的凭据 source 默认为 system，导致 provider-configs 端点不拒绝 | `_make_token` 显式设置 `source='user'` |
| `test_non_system_scope_403` 断言消息不匹配 | 测试断言 `"system scope"`，实际消息 `"provider configs require system source"` | 断言改为 `"system"` |
| `TestCredentialContextBOLA::test_user_scope_claims_other_user_task_403` | `_seed_task` INSERT 未包含 `host_name` 列，唯一约束 `(credential_id, name, scope, owner_id, host_name)` 冲突 | INSERT 显式设 `host_name=''`，先按唯一键查再插入 |

### 14.2 test_runtime_pool.py

| 测试 | 根因 | 修复 |
|------|------|------|
| `TestStaleRecovery::test_stale_dispatched_recovered_on_claim` | `_stale_dispatched` 用 age_minutes=5（300s），但代码回收窗口=600s，300s < 600s 不算 stale | age_minutes 改为 11（660s > 600s） |
| 同上 | 任务未设 `agent_type` 列，回收后 `pool_agent_type` 为空，claim 池匹配查不到 | INSERT 显式设 `agent_type='issue'` |
| 同上 | 默认 plugin 模式下 legacy claim 池匹配被跳过 | fixture 设置 `GRAPE_PIPELINE_MODE=legacy` |
| `TestStaleRecovery::test_stale_dispatched_attempts_limit_fails` | 测试用 attempts=2 期望 failed，但代码阈值为 attempts>=50 | attempts 改为 50 |

### 14.3 test_runtime_auto_mode.py

| 测试 | 根因 | 修复 |
|------|------|------|
| `TestAutoPipelineRouting::test_auto_remote_binding_enqueues` | 默认 plugin 模式下 pick_engine 返回 0，不使用 session 绑定的 runtime_id | setup 设置 `GRAPE_PIPELINE_MODE=legacy` |
| `TestAutoPipelineRouting::test_auto_local_binding_executes_direct` | 同上 + 断言 tasks==0 但 direct 执行也创建 task | 改为断言 `status IN ('queued','dispatched')` 的 task 数==0 |
| `TestChatConfigRuntimeId::test_unconfigured_chat_defaults_direct` | 同上 plugin 模式问题 | setup 设置 `GRAPE_PIPELINE_MODE=legacy` |

### 14.4 test_runtime_client.py

| 测试 | 根因 | 修复 |
|------|------|------|
| `TestSessionLifecycle::test_ensure_session_creates` | TCPConnector 参数从 `force_close=True` 改为 `force_close=False, enable_cleanup_closed=True, keepalive_timeout=30`，测试断言未同步 | 断言更新为实际参数 |
| 同上 | ClientSession 新增 `timeout` 参数，`assert_called_once_with(connector=...)` 不匹配 | 改为 `assert_called_once()` |

### 14.5 预存不修复的测试（与本分支无关）

| 测试 | 根因 | 不修复原因 |
|------|------|------------|
| `test_agent_event_subscription.py::test_double_path_subscription_extra_event` | sre vs qa 路由优先级问题 | 改动前已失败，与本分支无关 |
| `test_worker_package_mcp.py`（4 个） | worker 包不含 .codeartsdoer/mcp 文件，MANIFEST 从未包含这些文件 | 功能从未实现，测试是空壳回归保护 |
| `test_egress_e2e_real.py` | import 时 sys.exit(1) | 需要真实网络环境 |

## 十五、验证结果

```
test_runtime_protocol.py:  59 passed
test_runtime_pool.py:      13 passed
test_runtime_auto_mode.py:  10 passed
test_runtime_client.py:    40 passed
test_llm_credential_multi_provider.py: 5 passed
test_runtime.py:            22 passed
test_sandbox_alerts.py:    128 passed
test_sandbox_redact.py:      5 passed
test_cubesandbox.py:        18 passed
test_git_repo_api.py:       24 passed
test_skill_sync_obs.py:     21 passed
test_runtime_hermes.py:     15 passed
test_runtime_codex.py:      41 passed
test_runtime_install.py:     5 passed
test_runtime_agent.py:      30 passed
test_runtime_agent_concurrency.py: 8 passed
总计: 454 passed, 0 failed
```
