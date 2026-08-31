# Engine 角色标签需求文档

## 1. 背景与现状

### 1.1 当前 engine 与 agent 的关系

Grape 工作流有 5 个 agent 角色：vod、issue、coding、qa、sre（外加 chat）。每个 agent 可以绑定一组 engine（通过 Backend Engines 下拉多选），任务执行时从绑定的 engine 集合中选一个健康的来执行。

当前 engine 与 agent 角色之间**没有标签级别的关联** — 任何 engine 都可以被任何 agent 角色选中。用户在 Agent Configuration 页面的 Backend Engines 下拉中看到所有 global/repo scope 的 engine，手动勾选。

### 1.2 当前 engine 选择流程

```
Agent Config 页面 → 用户勾选 Backend Engines → 保存到 devops_agent_runtime_binding 表
Task 入队 → pick_engine(repo_key, agent_type) → 从 binding 集合中选健康引擎
```

`query_engines()` 返回所有 global/repo scope 的 engine，前端全部展示给用户勾选。没有"这个 engine 适合哪些 agent 角色"的过滤。

### 1.3 现有 agent 角色定义（configs/agent-description.json）

| agent_type | 名称 | 图标 | 默认 backend |
|------------|------|------|-------------|
| vod | VOD Agent | 🔍 | opencode |
| issue | Issue Agent | 📋 | opencode |
| coding | Coding Agent | 💻 | opencode |
| qa | QA Agent | 🧪 | opencode |
| sre | SRE Agent | 🚀 | opencode |

### 1.4 agent_runtimes 表结构（现有）

| 列 | 类型 | 说明 |
|----|------|------|
| id | int | 主键 |
| credential_id | int | 凭据 ID |
| name | varchar(191) | 引擎名（opencode/codearts/hermes） |
| version | text | CLI 版本 |
| healthy | int | 健康标志 |
| status | text | online/offline |
| scope | varchar(191) | global/repo/user |
| local_skills | text | 本地 skill 列表 |
| pending_skill_scan | int | 扫描指令标记 |
| skill_scan_result | text | 扫描结果缓存 |
| ... | | |

**无角色标签列。**

## 2. 需求目标

给每个 engine 分配角色标签，标识该 engine 可以作为哪些 agent 角色参与工作流。标签共 6 个：

| 标签 | 值 | 说明 |
|------|-----|------|
| 全能 | `all` | 可作为所有 agent 角色（默认） |
| VOD | `vod` | 仅可作为 VOD Agent |
| Issue | `issue` | 仅可作为 Issue Agent |
| Coding | `coding` | 仅可作为 Coding Agent |
| QA | `qa` | 仅可以作为 QA Agent |
| SRE | `sre` | 仅可以作为 SRE Agent |

### 2.1 标签规则

1. **允许多选** — 一个 engine 可以同时勾选 vod + issue + coding 等
2. **全能与其他互斥**：
   - 勾选其他标签时，全能自动关闭
   - 其他标签全部取消时，允许空标签（engine 对整个流水线不可见，Backend Engines 下拉不展示，pick_engine 不选中）
3. **默认值** — engine 注册时标签为 `["all"]`
4. **允许空标签** — 用户可以取消所有标签（包括全能），此时该 engine 对所有 agent 角色不可见
5. **chat 不在标签中** — chat agent 不受标签限制（chat 使用本机直连引擎，不走工作流绑定）

### 2.2 标签对工作流的影响

- **Backend Engines 下拉过滤** — Agent Config 页面的 Backend Engines 下拉只展示包含该 agent_type 标签的 engine（或全能标签的 engine）
- **引擎池过滤** — `pick_engine()` 和 `query_engines()` 在为某 agent_type 选引擎时，排除不匹配标签的 engine
- **不影响已绑定集合** — 已有的 devops_agent_runtime_binding 不变；标签只影响"可选引擎列表"，不自动修改已绑定的集合

## 3. 改动逻辑

### 3.1 DB schema — agent_runtimes 增加角色标签列 — db_setup.py

```sql
agent_tags TEXT DEFAULT '["all"]'
```

存储 JSON 数组，如 `["all"]`、`["vod","issue"]`、`["coding","qa","sre"]`。

迁移：
```python
_ensure_column(c, "agent_runtimes", "agent_tags", "TEXT", "'[\"all\"]'")
```

### 3.2 服务端 — register 时设置默认标签 — runtime.py

`runtime_register()` 中 upsert agent_runtimes 时：
- 新建行（INSERT）：`agent_tags` 默认 `'["all"]'`
- 已有行（UPDATE）：**不覆盖** agent_tags（保留用户已配置的标签）

```python
# INSERT 时带 agent_tags 默认值
c.execute(
    "INSERT INTO agent_runtimes (..., agent_tags) VALUES (..., ?)",
    (..., '["all"]'))

# UPDATE 不碰 agent_tags（保留用户配置）
c.execute("UPDATE agent_runtimes SET version=?, ..., local_skills=? WHERE id=?",
          (..., local_skills_json, rid))  # agent_tags 不在 UPDATE 列中
```

### 3.3 服务端 — 新增标签管理 API — runtime.py

#### `GET /api/runtime/runtimes/{runtime_id}/tags`

返回当前 engine 的标签列表。

```python
@router.get("/runtimes/{runtime_id}/tags")
async def get_runtime_tags(runtime_id: int, user: dict = Depends(get_current_user)):
    # 查 agent_tags
    ...
    return {"tags": json.loads(tags_json)}
```

#### `PUT /api/runtime/runtimes/{runtime_id}/tags`

设置 engine 的标签。前端传入标签数组，服务端应用互斥规则后写入。

```python
@router.put("/runtimes/{runtime_id}/tags")
async def set_runtime_tags(runtime_id: int, body: dict, user: dict = Depends(get_current_user)):
    tags = body.get("tags") or []
    # 应用互斥规则
    tags = _apply_tag_rules(tags)
    # 写入 DB
    ...
    return {"tags": tags}
```

#### 互斥规则函数：`_apply_tag_rules(tags: list[str]) -> list[str]`

```python
def _apply_tag_rules(tags: list[str]) -> list[str]:
    """应用标签互斥规则。
    - 全能与其他互斥：有其他标签时去掉全能。
    - 5 个角色全选时自动替换为全能。
    - 允许空标签（不自动回全能）。
    """
    AGENT_TAGS = {"vod", "issue", "coding", "qa", "sre"}
    tag_set = set(tags)
    has_all = "all" in tag_set
    has_others = tag_set & AGENT_TAGS

    if has_all and has_others:
        # 互斥：去掉全能，保留其他
        tag_set.discard("all")
    elif has_others and len(has_others) == len(AGENT_TAGS):
        # 5 个角色全选 → 替换为全能
        tag_set = {"all"}
    # 允许空标签：不自动回全能

    return sorted(tag_set)
```

### 3.4 服务端 — engine 选择过滤 — engine_select.py

#### `query_engines()` 增加可选参数 `agent_type`

```python
def query_engines(repo_key: str, scope: str, agent_type: str = "") -> list[dict]:
    """返回候选引擎。agent_type 非空时，只返回标签匹配的引擎。"""
    ...  # 现有查询逻辑
    # 查询 SQL 增加 agent_tags 列
    # 过滤：agent_type 非空时，只保留 tags 包含 agent_type 或 "all" 的引擎
    for r in rows:
        tags = json.loads(r_tags or '["all"]')
        if agent_type and agent_type not in tags and "all" not in tags:
            continue  # 标签不匹配，排除
        out.append({...})
    return out
```

#### `pick_engine()` 传递 agent_type 到 `query_engines()`

`pick_engine()` 现有逻辑调用 `get_binding()` 取已绑定集合，不走 `query_engines()`。标签过滤应用在绑定集合层：

```python
def pick_engine(repo_key: str, agent_type: str, fallback_runtime_id: int = 0) -> int | None:
    ...
    members = get_binding(repo_key, agent_type)
    # 过滤：排除标签不匹配的成员
    members = _filter_by_tags(members, agent_type)
    ...
```

新增函数：

```python
def _filter_by_tags(members: list[dict], agent_type: str) -> list[dict]:
    """过滤绑定集合，只保留标签匹配 agent_type 的引擎。"""
    if not agent_type:
        return members
    db, lock = _db()
    result = []
    for m in members:
        rid = m.get("runtime_id")
        if not rid:
            continue
        with lock:
            c = db.cursor()
            c.execute("SELECT agent_tags FROM agent_runtimes WHERE id=?", (rid,))
            row = c.fetchone()
        tags = json.loads(row[0] or '["all"]') if row else ["all"]
        if agent_type in tags or "all" in tags:
            result.append(m)
    return result
```

#### `resolve_default_engine()` 传递 agent_type 到 `query_engines()`

```python
def resolve_default_engine(repo_key: str, agent_type: str = "") -> int | None:
    ...
    for scope in cfg.get("scopes") or ["repo", "global", "local"]:
        cand = query_engines(repo_key, scope, agent_type=agent_type)  # 传入 agent_type
        ...
```

### 3.5 服务端 — API 返回标签 — settings.py

`get_agent_runtimes()` 查询增加 `agent_tags` 列，返回给前端：

```python
# devices 查询
c.execute("SELECT id, name, version, healthy, installed, status, last_seen_at, local_skills, skill_scan_result, agent_tags FROM agent_runtimes WHERE credential_id=?", (cid,))
# 构建 runtimes
runtimes = [
    {...,
     "local_skills": ...,
     "skill_scan_result": ...,
     "agent_tags": json.loads(x[9] or '["all"]') if x[9] else ["all"]}
    for x in c.fetchall()
]
```

user_db.get_agent_runtimes 也同步增加 `agent_tags` 列。

本机直连行也加 `agent_tags`。

### 3.6 前端 — Engine 详情页标签展示与编辑 — runtimes.js

#### Engine 详情页增加角色标签模块

在 `openEngineDetail()` 弹窗中，"本地 Skill 列表"模块上方，增加"角色标签"模块：

```javascript
// 在 openEngineDetail 中，engine 信息区下方、skill 列表上方
const tags = rt.agent_tags || ['all'];
const tagColors = {all: '#6366f1', vod: '#ec4899', issue: '#ef4444', coding: '#3b82f6', qa: '#a855f7', sre: '#f59e0b'};
const tagLabels = {all: '全能', vod: 'VOD', issue: 'Issue', coding: 'Coding', qa: 'QA', sre: 'SRE'};
const allOptions = ['all', 'vod', 'issue', 'coding', 'qa', 'sre'];

// 标签 checkbox 列表（可编辑）
const tagCheckboxes = allOptions.map(t => {
    const checked = tags.includes(t) ? 'checked' : '';
    return `<label style="display:flex;align-items:center;gap:8px;padding:6px 10px;border-radius:6px;cursor:pointer;font-size:12px;user-select:none">
        <input type="checkbox" id="engineTagCb_${t}" value="${t}" ${checked} onchange="onEngineTagChange('${t}')"
               style="width:14px;height:14px;accent-color:${tagColors[t]}">
        <span style="font-size:10px;padding:2px 8px;border-radius:999px;color:#fff;background:${tagColors[t]}">${tagLabels[t]}</span>
        <span style="color:var(--t3);font-size:11px">${t === 'all' ? '可作为所有角色' : '仅 ' + tagLabels[t] + ' 角色'}</span>
    </label>`;
}).join('');

// 插入到详情弹窗中
html += `<div style="border-top:1px solid var(--b);padding-top:16px;margin-bottom:16px">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">
        <span style="font-weight:600;font-size:14px">角色标签</span>
        <button class="bt bt-p" style="font-size:11px;padding:4px 10px;border-radius:4px" onclick="saveEngineTags(${runtimeId})">保存标签</button>
    </div>
    <div id="engineTagList" style="display:flex;flex-wrap:wrap;gap:4px">${tagCheckboxes}</div>
</div>`;
```

#### 互斥规则前端实现

```javascript
function onEngineTagChange(tag) {
    const allCb = document.getElementById('engineTagCb_all');
    const agentCbs = ['vod','issue','coding','qa','sre'].map(t => document.getElementById('engineTagCb_'+t));
    const checkedAgents = agentCbs.filter(cb => cb.checked);

    if (tag === 'all') {
        // 勾选全能 → 取消其他
        if (allCb.checked) {
            agentCbs.forEach(cb => cb.checked = false);
        }
    } else {
        // 勾选其他 → 取消全能
        if (allCb.checked) {
            allCb.checked = false;
        }
        // 5 个全选 → 替换为全能
        if (checkedAgents.length === 5) {
            agentCbs.forEach(cb => cb.checked = false);
            allCb.checked = true;
        }
        // 全部取消 → 允许空标签（不自动回全能）
    }
}
```

#### 保存标签

```javascript
function saveEngineTags(runtimeId) {
    const tags = [];
    ['all','vod','issue','coding','qa','sre'].forEach(t => {
        const cb = document.getElementById('engineTagCb_'+t);
        if (cb && cb.checked) tags.push(t);
    });
    API.put('/runtime/runtimes/' + runtimeId + '/tags', {tags}).then(d => {
        // 更新本地数据
        const rt = __runtimeData.runtimes.find(r => r.id === runtimeId);
        if (rt) rt.agent_tags = d.tags;
        app.showToast('标签已保存');
    }).catch(e => app.showToast('保存失败: ' + e.message));
}
```

#### Engines/Workers 视图卡片标签缩略展示

每个 engine 卡片/链接后面跟标签缩略（如 `[全能]` 或 `[VOD][Coding]` 或无标签时显示 `[未分配]`）：

```javascript
const tagStr = (rt.agent_tags || []).length === 0 ? '<span style="color:var(--t3);font-size:10px">未分配</span>'
    : (rt.agent_tags || ['all']).map(t => tagLabels[t]||t).join('/');
```

### 3.7 前端 — Backend Engines 下拉过滤 — agents.js

Agent Config 页面的 Backend Engines 下拉加载引擎列表时，按当前 agent_type 过滤：

```javascript
// 现有：加载所有 global/repo 引擎
const ok = runtimes.filter(r => r.scope === 'repo' || r.scope === 'global');

// 改为：只展示标签匹配当前 agent_type 的引擎
const agentType = backendBox.dataset.agent || '';  // 当前配置的 agent 类型
const ok = runtimes.filter(r => {
    if (r.scope !== 'repo' && r.scope !== 'global') return false;
    const tags = r.agent_tags || ['all'];
    return tags.includes('all') || tags.includes(agentType);
});
```

每个引擎选项后面也展示标签信息：

```javascript
const tagStr = (r.agent_tags || ['all']).map(t => tagLabels[t]||t).join('/');
// 在 cb 函数的描述行末尾追加
`${online ? ' ✓' : ' ✗'} · ${tagStr}`
```

## 4. 改动文件清单

| # | 文件 | 改动类型 | 改动内容 |
|---|------|---------|---------|
| 1 | src/api/db_setup.py | DDL + 迁移 | agent_runtimes 增加 `agent_tags TEXT DEFAULT '["all"]'` 列 |
| 2 | src/api/routes/runtime.py | 新增 API + 逻辑 | register 不覆盖 agent_tags；新增 GET/PUT tags 端点；`_apply_tag_rules()` 互斥函数 |
| 3 | src/runtime/engine_select.py | 过滤逻辑 | `query_engines()` 增加 agent_type 过滤；`pick_engine()` 增加 `_filter_by_tags()`；`resolve_default_engine()` 传 agent_type |
| 4 | src/api/routes/settings.py | 查询扩展 | `get_agent_runtimes()` 查询选 agent_tags 列并返回 |
| 5 | src/user/db.py | 查询扩展 | `get_agent_runtimes()` 查询选 agent_tags 列并返回 |
| 6 | src/api/static/js/pages/runtimes.js | 展示 + 编辑 | Engines/Workers 卡片展示标签 badges；标签编辑弹窗 + 互斥逻辑；保存标签 |
| 7 | src/api/static/js/pages/agents.js | 下拉过滤 | Backend Engines 下拉按 agent_type 过滤；引擎选项展示标签 |

## 5. 改动后完整数据流

### 5.1 Engine 注册

```
Worker register → 服务端 upsert agent_runtimes
  → 新建行：agent_tags = '["all"]'（默认全能）
  → 已有行：agent_tags 不变（保留用户配置）
```

### 5.2 标签编辑

```
用户在 Engines/Workers 页点击 engine 标签
  → 打开标签编辑弹窗（6 个 checkbox）
  → 用户勾选/取消标签
    → 前端实时应用互斥规则
  → 点保存
    → PUT /api/runtime/runtimes/{id}/tags {tags: [...]}
    → 服务端 _apply_tag_rules() 二次校验
    → 写入 DB agent_tags
    → 返回最终 tags
  → 前端更新本地数据 + 刷新视图
```

### 5.3 Backend Engines 下拉过滤

```
用户打开 Agent Config → Backend Engines 下拉
  → GET /settings/agent-runtimes（返回含 agent_tags）
  → 前端按当前 agent_type 过滤：
    只展示 tags 包含 agent_type 或 "all" 的引擎
  → 用户勾选引擎 → 保存到 binding（不变）
```

### 5.4 引擎选择（pick_engine）

```
Task 入队 → pick_engine(repo_key, agent_type)
  → get_binding() 取已绑定集合
  → _filter_by_tags(members, agent_type)
    → 查每个 member 的 agent_tags
    → 只保留 tags 包含 agent_type 或 "all" 的
  → 从过滤后的集合中选健康引擎
```

### 5.5 默认引擎选择（resolve_default_engine）

```
托管默认选择 → resolve_default_engine(repo_key, agent_type)
  → query_engines(repo_key, scope, agent_type=agent_type)
    → 查询时带 agent_tags 列
    → 过滤：tags 不含 agent_type 且不含 "all" → 排除
  → 从过滤后候选中选引擎
```

## 6. 测试用例设计

### 6.1 互斥规则测试 — test_tag_rules.py

| 用例 ID | 用例名 | 输入 tags | 预期输出 |
|---------|--------|----------|---------|
| T001 | 默认全能 | `[]` | `["all"]` |
| T002 | 只有全能 | `["all"]` | `["all"]` |
| T003 | 全能 + 其他 → 去掉全能 | `["all", "vod"]` | `["vod"]` |
| T004 | 多个角色标签 | `["vod", "coding", "sre"]` | `["coding", "sre", "vod"]` |
| T005 | 5 个角色全选 → 替换为全能 | `["vod","issue","coding","qa","sre"]` | `["all"]` |
| T006 | 4 个角色 + 全能 → 去掉全能 | `["all","vod","issue","coding","qa"]` | `["coding","issue","qa","vod"]` |
| T007 | 空标签允许 | `[]` | `[]` |

### 6.2 DB 迁移测试

| 用例 ID | 用例名 | 前置条件 | 预期结果 |
|---------|--------|---------|---------|
| T010 | 全新库建表含 agent_tags | 空 DB | agent_runtimes 表含 agent_tags 列 |
| T011 | 老库迁移加列 | 无 agent_tags 列 | 列被添加；已有行默认 `'["all"]'` |

### 6.3 服务端 API 测试 — test_runtime_tags.py

| 用例 ID | 用例名 | 操作 | 预期结果 |
|---------|--------|------|---------|
| T020 | GET tags | GET /runtimes/{id}/tags | 返回当前标签列表 |
| T021 | PUT tags 设置角色标签 | PUT /runtimes/{id}/tags {tags:["vod","coding"]} | DB agent_tags = `'["coding","vod"]'`；返回相同 |
| T022 | PUT tags 全能+其他互斥 | PUT {tags:["all","vod"]} | DB = `'["vod"]'`（全能被去掉） |
| T023 | PUT tags 5个全选→全能 | PUT {tags:["vod","issue","coding","qa","sre"]} | DB = `'["all"]'` |
| T024 | PUT tags 空列表允许 | PUT {tags:[]} | DB = `'[]'`；engine 对流水线不可见 |
| T025 | register 新建行默认全能 | POST /register | 新行 agent_tags = `'["all"]'` |
| T026 | register 已有行不覆盖标签 | 已有行 tags=["vod"]，POST /register | agent_tags 保持 `["vod"]` 不变 |

### 6.4 engine_select 过滤测试 — test_engine_select_tags.py

| 用例 ID | 用例名 | 前置条件 | 操作 | 预期结果 |
|---------|--------|---------|------|---------|
| T030 | query_engines 按 agent_type 过滤 | engine A tags=["vod"], engine B tags=["coding"], engine C tags=["all"] | query_engines(repo, "global", agent_type="vod") | 返回 A 和 C，不返回 B |
| T031 | query_engines 无 agent_type 不过滤 | 同上 | query_engines(repo, "global") | 返回 A, B, C 全部 |
| T032 | pick_engine 过滤不匹配标签 | binding=[A(vod), B(coding)], agent_type="vod" | pick_engine(repo, "vod") | 只选 A，不选 B |
| T033 | pick_engine 全能标签匹配 | binding=[C(all)], agent_type="qa" | pick_engine(repo, "qa") | 选 C（全能匹配所有角色） |
| T034 | pick_engine 全部不匹配 | binding=[A(vod)], agent_type="sre" | pick_engine(repo, "sre") | 返回 None（engine_unavailable） |
| T035 | resolve_default_engine 过滤 | A(vod), B(coding), C(all), agent_type="sre" | resolve_default_engine(repo, "sre") | 只选 C |

### 6.5 前端手动验证

| 用例 ID | 用例名 | 操作 | 预期结果 |
|---------|--------|------|---------|
| T040 | Engines 卡片展示标签 | 打开 Engines 页 | 每个 engine 卡片显示标签 badges |
| T041 | engine 详情页显示角色标签模块 | 点击 engine 卡片打开详情 | 详情弹窗中"本地 Skill 列表"上方有"角色标签"模块，6 个 checkbox |
| T042 | 勾选角色标签时全能自动关闭 | 全能已勾选 → 勾选 VOD | 全能 checkbox 自动取消 |
| T043 | 取消所有标签允许空标签 | 只有 VOD 勾选 → 取消 VOD | 所有 checkbox 都取消，不自动回全能 |
| T044 | 5个角色全选替换为全能 | 逐个勾选 5 个角色 | 第 5 个勾选时其他取消、全能点亮 |
| T045 | 保存标签 | 点保存标签按钮 | toast 提示已保存；engine 卡片标签缩略更新 |
| T046 | 空标签时卡片显示未分配 | 取消所有标签并保存 | engine 卡片/链接显示"未分配" |
| T047 | Backend Engines 下拉过滤 | engine A tags=["vod"]，打开 coding agent config | 下拉不显示 engine A |
| T048 | Backend Engines 下拉显示全能 | engine C tags=["all"]，打开任意 agent config | 下拉显示 engine C |
| T049 | 空标签 engine 下拉不显示 | engine D tags=[]，打开任意 agent config | 下拉不显示 engine D |

### 6.6 端到端集成测试

| 用例 ID | 用例名 | 步骤 | 预期结果 |
|---------|--------|------|---------|
| T050 | 标签限制工作流 | 1. engine A 设 tags=["vod"]<br>2. coding agent config 的 Backend Engines 下拉不显示 A<br>3. 手动绑定 A 到 coding（API）<br>4. 触发 coding task | pick_engine 过滤掉 A（tags 不含 coding），返回 None |
| T051 | 全能标签不限制 | 1. engine C tags=["all"]<br>2. 任意 agent config 的下拉显示 C<br>3. 绑定 C<br>4. 触发 task | pick_engine 选中 C |
| T052 | 注册后默认全能 | 1. 新 worker 注册<br>2. 查看 engine 标签 | 标签为 ["all"] |
| T053 | 重注册不覆盖标签 | 1. engine tags=["vod"]<br>2. worker 重注册<br>3. 查看标签 | 标签仍为 ["vod"] |
| T054 | 空标签 engine 完全不可见 | 1. engine D tags=[]<br>2. 打开任意 agent config | 下拉不显示 D；pick_engine 不选 D |
| T055 | 空标签后恢复全能 | 1. engine D tags=[]<br>2. 详情页勾选全能并保存<br>3. 打开 agent config | 下拉恢复显示 D |

## 7. 向后兼容性

| 场景 | 兼容方案 |
|------|---------|
| 老库无 agent_tags 列 | 迁移加列，已有行默认 `'["all"]'`（全能，不限制任何角色） |
| 老 Worker 注册 | register 不传 agent_tags → 新建行默认 `["all"]`，已有行不覆盖 |
| 已有 binding 集合 | 标签只影响"可选列表"和 pick_engine 过滤；已绑定的集合不变（用户可在 binding 中保留被标签排除的引擎，但 pick_engine 不会选它） |
| 前端老版本 | `rt.agent_tags` 为 undefined 时前端默认 `["all"]`，不过滤 |
| chat agent | chat 不在标签范围内，resolve_local_engine() 不传 agent_type，不受标签影响 |

## 8. 边界与风险

| 风险 | 影响 | 缓解 |
|------|------|------|
| 已绑定引擎被标签排除 | pick_engine 过滤后集合可能变空 → engine_unavailable | 提示用户在 Agent Config 中重新绑定标签匹配的引擎；UI 上 Backend Engines 下拉标注不匹配的已绑定引擎 |
| 标签与 binding 冲突 | 用户先绑定引擎再改标签 → binding 中可能有标签不匹配的引擎 | pick_engine 运行时过滤，不自动修改 binding；UI 提示用户检查 |
| 全能标签语义 | "全能" = 可作为所有角色，不是"优先" | UI 文案明确说明"可作为所有角色" |
| 标签持久化 | worker 重注册不能覆盖用户配置的标签 | register UPDATE 不碰 agent_tags 列 |
| 本机直连引擎 | credential_id=0 的引擎也需支持标签 | 迁移加列覆盖所有行；settings.py 返回 agent_tags；前端编辑同样适用 |
