# Grape 事件调度模型重构设计 V2 — 引擎标签化 + 流水线化 + 大规模设备调度

> 基于 DeepSeek Harness 事件调度模式，融合 Grape 现有架构，面向 10 万+ 设备规模

## 一、需求总览

### 1.1 改进目标

| 维度 | 现状 | 目标 |
|------|------|------|
| 引擎选择 | 仓库×Agent 维度绑定（devops_agent_runtime_binding） | 引擎标签化（vod/issue/coding/qa/sre），10万+设备按标签自认领 |
| 任务创建 | 仓库级场景（managed_repos.dev_scene） | 任务级场景选择，默认用户最近使用的场景 |
| 流水线 | 硬编码 5 阶段（vod→issue→coding→qa→sre） | 可配置流水线（JSON），各节点指定标签+事件处理模式 |
| 事件调度 | 直接分派（direct/queue 二选一） | 4 种事件模式（emit/waterfall/serial/parallel），默认 emit |
| Pipeline 展示 | 固定 5 节点流程图 | 动态渲染配置的流水线节点，实时状态 |
| 规模 | 单仓库绑定少量引擎 | 10万+设备按标签自认领任务，基于健康/繁忙状态自适应 |

### 1.2 DSH 四种事件处理模式（参考）

```
emit      — 广播通知，所有监听器都被调用，无人可拦截（fire-and-forget）
waterfall — 瀑布式中间件链，每个监听器必须调 next() 才能继续，可拦截/修改
serial    — 串行执行，按顺序逐个调用，第一个 bail 的返回值生效
parallel  — 并行执行，所有监听器同时启动，等全部完成
```

---

## 二、引擎标签化设计

### 2.1 标签模型

引擎从"仓库×Agent 绑定"升级为"标签化自注册"：

```
现状：
  devops_agent_runtime_binding (repo_key, agent_type, runtime_id)
  → 仓库 A 的 coding Agent 绑定引擎 #33

目标：
  agent_runtimes.tags = ["coding", "qa"]  ← 引擎自声明标签
  → 引擎 #33 注册时声明自己能干 coding 和 qa
  → 任何需要 coding 的任务，引擎 #33 都可以认领
```

### 2.2 DB 改动

```sql
-- agent_runtimes 新增 tags 列（引擎自注册时可空，不强校验）
ALTER TABLE agent_runtimes ADD COLUMN tags TEXT DEFAULT '';
-- 格式：逗号分隔，如 "vod,issue" 或 "coding,qa,sre"
-- 引擎自注册时可以不填 tags（空字符串），后续用户在控制台手动配置
```

```sql
-- 新增引擎能力声明表（支持 10万+ 设备的细粒度能力）
-- 每行 = 一个引擎的一个标签能力（一对多）
CREATE TABLE IF NOT EXISTS engine_capabilities (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    runtime_id  INTEGER NOT NULL,          -- agent_runtimes.id
    tag         TEXT NOT NULL,             -- vod/issue/coding/qa/sre/chat
    max_concurrent INTEGER DEFAULT 1,      -- 该标签最大并发数
    priority    INTEGER DEFAULT 50,        -- 自声明优先级（小=高）
    enabled     INTEGER DEFAULT 1,
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now')),
    UNIQUE(runtime_id, tag)
);
```

### 2.3 引擎注册流程（自注册标签可为空，不强校验）

```
Worker 启动 → 注册引擎 → 可选声明标签

现状：
  worker 注册时仅上报引擎类型（hermes/opencode）
  服务端按 devops_agent_runtime_binding 分配任务

目标：
  worker 注册时上报引擎类型 + 可选标签
  引擎 #35000 注册：{name: "hermes", tags: ["coding", "qa"]}  ← 有标签
  引擎 #35001 注册：{name: "opencode", tags: []}               ← 无标签（空）
  引擎 #35002 注册：{name: "hermes"}                           ← 不传 tags（默认空）

  服务端：
  - tags 非空 → 写入 engine_capabilities 表
  - tags 为空 → agent_runtimes.tags = ''，不写 engine_capabilities
  - 用户后续在控制台 Agent Runtimes 页面为该引擎添加/修改/删除标签

  10万设备场景：
  - 引擎按标签自认领（类似 Kafka consumer group）
  - 服务端不分配，只维护任务队列
  - 引擎根据自身健康/繁忙状态决定是否 claim
  - 新设备上线自动加入标签组，开始认领
  - 设备下线后任务超时回收，其他设备认领
```

### 2.4 标签管理 API（控制台 CRUD）

```python
# src/api/routes/settings.py (新增端点)

@router.get("/agent-runtimes/{rid}/tags")
async def get_engine_tags(rid: int, user: dict = Depends(get_current_user)):
    """获取引擎的标签列表"""
    # 返回 engine_capabilities 中该 runtime_id 的所有标签行

@router.post("/agent-runtimes/{rid}/tags")
async def add_engine_tag(rid: int, body: EngineTagBody, user: dict = Depends(get_current_user)):
    """为引擎添加标签
    body: {tag: "coding", max_concurrent: 2, priority: 50}
    幂等：已有相同 tag 则更新 max_concurrent/priority
    """

@router.put("/agent-runtimes/{rid}/tags/{tag}")
async def update_engine_tag(rid: int, tag: str, body: EngineTagUpdateBody,
                            user: dict = Depends(get_current_user)):
    """修改引擎标签配置
    body: {max_concurrent: 3, priority: 10, enabled: 1}
    """

@router.delete("/agent-runtimes/{rid}/tags/{tag}")
async def delete_engine_tag(rid: int, tag: str, user: dict = Depends(get_current_user)):
    """删除引擎标签
    删除 engine_capabilities 中 (runtime_id, tag) 行
    不影响引擎本身运行，只是该引擎不再认领此标签的任务
    """
```

### 2.5 标签管理 UI（Agent Runtimes 页面增强）

在现有 Agent Runtimes 页面的每个引擎卡片上增加标签管理区域：

```
┌─────────────────────────────────────────────────┐
│ 🟢 在线  hermes-worker-ecs-d27a  v0.20.5        │
│                                                  │
│ worker: worker-ecs-d27a    owner: songspace      │
│                                                  │
│ 标签:                                             │
│ ┌──────┐ ┌──────┐ ┌──────┐ ┌─────────┐         │
│ │ vod  │ │issue │ │coding│ │ qa      │         │
│ │并发:2│ │并发:1│ │并发:3│ │并发:1   │         │
│ │ ✏ 🗑 │ │ ✏ 🗑 │ │ ✏ 🗑 │ │ ✏    🗑 │         │
│ └──────┘ └──────┘ └──────┘ └─────────┘         │
│ + 添加标签 ▼                                      │
│   [vod] [issue] [coding] [qa] [sre] [chat]      │
│                                                  │
│ 无标签时显示：                                     │
│ 标签: 无（点击 + 添加标签为引擎指定用途）            │
└─────────────────────────────────────────────────┘
```

```javascript
// runtimes.js 改造 — renderEngines 中每个引擎卡片增加标签区域

function renderEngineTags(rt) {
  const tags = rt.tags || [];
  if (tags.length === 0) {
    return `<div style="margin-top:8px;font-size:12px">
      <span style="color:var(--t3)">标签:</span>
      <span style="color:var(--t3);font-style:italic">无</span>
      <button class="bt bt-o bt-s" onclick="showAddTagForm(${rt.id})">+ 添加标签</button>
    </div>`;
  }
  const tagChips = tags.map(t => `
    <span style="display:inline-flex;align-items:center;gap:4px;background:#e3edfd;color:#235ec7;padding:3px 10px;border-radius:6px;font-size:11px;font-weight:600">
      ${t.tag}
      <span style="font-weight:400;font-size:10px;color:var(--t3)">并发:${t.max_concurrent}</span>
      <span onclick="editEngineTag(${rt.id},'${t.tag}')" style="cursor:pointer;color:var(--a)">✏</span>
      <span onclick="deleteEngineTag(${rt.id},'${t.tag}')" style="cursor:pointer;color:var(--r)">🗑</span>
    </span>`).join(' ');
  return `<div style="margin-top:8px;font-size:12px">
    <span style="color:var(--t3)">标签:</span> ${tagChips}
    <button class="bt bt-o bt-s" onclick="showAddTagForm(${rt.id})" style="margin-left:4px">+</button>
  </div>
  <div id="addTagForm_${rt.id}" style="display:none;margin-top:6px"></div>`;
}

function showAddTagForm(rid) {
  const el = document.getElementById('addTagForm_' + rid);
  if (!el) return;
  el.style.display = '';
  el.innerHTML = `
    <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">
      <select id="newTag_${rid}" style="padding:4px 8px;border:1px solid var(--b);border-radius:4px;font-size:11px">
        <option value="vod">vod</option>
        <option value="issue">issue</option>
        <option value="coding">coding</option>
        <option value="qa">qa</option>
        <option value="sre">sre</option>
        <option value="chat">chat</option>
      </select>
      <input id="newTagConc_${rid}" type="number" value="1" min="1" max="20" style="width:60px;padding:4px;border:1px solid var(--b);border-radius:4px;font-size:11px" title="最大并发数">
      <input id="newTagPrio_${rid}" type="number" value="50" min="1" max="100" style="width:60px;padding:4px;border:1px solid var(--b);border-radius:4px;font-size:11px" title="优先级(小=高)">
      <button class="bt bt-p bt-s" onclick="addEngineTag(${rid})">添加</button>
      <button class="bt bt-o bt-s" onclick="document.getElementById('addTagForm_${rid}').style.display='none'">取消</button>
    </div>`;
}

function addEngineTag(rid) {
  const tag = document.getElementById('newTag_' + rid).value;
  const conc = parseInt(document.getElementById('newTagConc_' + rid).value) || 1;
  const prio = parseInt(document.getElementById('newTagPrio_' + rid).value) || 50;
  API.post('/settings/agent-runtimes/' + rid + '/tags', {tag, max_concurrent: conc, priority: prio})
    .then(() => { app.showToast('标签已添加'); refreshRuntimes(); })
    .catch(e => app.showToast('添加失败: ' + e.message));
}

function deleteEngineTag(rid, tag) {
  if (!confirm('确认删除标签 "' + tag + '"？该引擎将不再认领此标签的任务。')) return;
  API.del('/settings/agent-runtimes/' + rid + '/tags/' + tag)
    .then(() => { app.showToast('标签已删除'); refreshRuntimes(); })
    .catch(e => app.showToast('删除失败: ' + e.message));
}

function editEngineTag(rid, tag) {
  const conc = prompt('最大并发数（1-20）:', '1');
  if (conc === null) return;
  const prio = prompt('优先级（1-100，小=高）:', '50');
  if (prio === null) return;
  API.put('/settings/agent-runtimes/' + rid + '/tags/' + tag, {
    max_concurrent: parseInt(conc) || 1,
    priority: parseInt(prio) || 50
  }).then(() => { app.showToast('标签已更新'); refreshRuntimes(); })
    .catch(e => app.showToast('更新失败: ' + e.message));
}
```

### 2.4 设备 scope 三级隔离（全局/仓库/个人）

设备注册时继承凭据的 scope（与现有 runtime_credentials.scope 一致），决定能认领哪些任务：

```
scope=global  → 可认领所有仓库的任务
scope=repo    → 只认领 owner_id 对应仓库的任务
scope=user    → 只认领 owner_name 对应用户创建的任务

10万+设备 = 全局设备 + 仓库设备 + 个人设备的总和，各范围设备各自只认领自身 scope 内的任务。
```

**engine_capabilities 表增加 scope 透传：**

引擎注册时，engine_capabilities 行继承 agent_runtimes 的 scope/owner_id/owner_name（通过 runtime_id 关联 runtime_credentials，无需冗余存储）。claim 时通过 JOIN 过滤。

**claim_by_tag 增加 scope 过滤：**

```python
# src/runtime/service.py (改造)

def claim_by_tag(tag: str, runtime_id: int) -> dict | None:
    """按标签认领任务 + scope 隔离

    scope=global → 可认领所有仓库的 queued 任务
    scope=repo   → 只认领 owner_id 对应仓库的 queued 任务
    scope=user   → 只认领该用户创建的 queued 任务（通过 agent_tasks.user_id 匹配）
    """
    with db_lock:
        c = db.cursor()

        # 1. 校验标签能力 + 获取引擎 scope 信息
        c.execute("""
            SELECT ec.tag, rc.scope, rc.owner_id, rc.owner_name
            FROM engine_capabilities ec
            JOIN agent_runtimes ar ON ar.id = ec.runtime_id
            LEFT JOIN runtime_credentials rc ON rc.id = ar.credential_id
            WHERE ec.runtime_id=? AND ec.tag=? AND ec.enabled=1
        """, (runtime_id, tag))
        row = c.fetchone()
        if not row:
            return None  # 无此标签能力

        scope = row[1] or "global"
        owner_id = row[2] or ""
        owner_name = row[3] or ""

        # 2. 构建任务查询（加 scope 过滤）
        if scope == "repo":
            # 仓库级：只认领该仓库的任务
            sql = """
                SELECT t.id FROM agent_tasks t
                WHERE t.pool_tag=? AND t.status='queued'
                  AND t.repo_key=?
                  AND NOT EXISTS (
                    SELECT 1 FROM agent_tasks a2
                    WHERE a2.status IN ('dispatched','running')
                      AND (
                        (t.issue_session_id != '' AND a2.issue_session_id = t.issue_session_id)
                        OR (t.issue_session_id = '' AND a2.agent_session_id = t.agent_session_id)
                      )
                  )
                ORDER BY t.created_at ASC LIMIT 1
            """
            c.execute(sql, (tag, owner_id))

        elif scope == "user":
            # 个人级：只认领该用户创建的任务
            # owner_name 对应 user_accounts.username → user_id
            c.execute("SELECT id FROM user_accounts WHERE username=?", (owner_name,))
            uid_row = c.fetchone()
            uid = uid_row[0] if uid_row else 0
            sql = """
                SELECT t.id FROM agent_tasks t
                WHERE t.pool_tag=? AND t.status='queued'
                  AND t.user_id=?
                  AND NOT EXISTS (
                    SELECT 1 FROM agent_tasks a2
                    WHERE a2.status IN ('dispatched','running')
                      AND (
                        (t.issue_session_id != '' AND a2.issue_session_id = t.issue_session_id)
                        OR (t.issue_session_id = '' AND a2.agent_session_id = t.agent_session_id)
                      )
                  )
                ORDER BY t.created_at ASC LIMIT 1
            """
            c.execute(sql, (tag, uid))

        else:
            # 全局级：可认领所有任务
            sql = """
                SELECT id FROM agent_tasks
                WHERE pool_tag=? AND status='queued'
                  AND NOT EXISTS (
                    SELECT 1 FROM agent_tasks a2
                    WHERE a2.status IN ('dispatched','running')
                      AND (
                        (agent_tasks.issue_session_id != '' AND a2.issue_session_id = agent_tasks.issue_session_id)
                        OR (agent_tasks.issue_session_id = '' AND a2.agent_session_id = agent_tasks.agent_session_id)
                      )
                  )
                ORDER BY created_at ASC LIMIT 1
            """
            c.execute(sql, (tag,))

        task_row = c.fetchone()
        # ... 后续与现有 claim 逻辑一致：标记 dispatched + 返回 task
```

**scope 隔离示意：**

```
任务队列（pool_tag=coding）:
  task_001 (repo=hello-blog,   user_id=1)
  task_002 (repo=shelltest,    user_id=1)
  task_003 (repo=hello-blog,   user_id=2)

引擎 A (scope=global,  tags=[coding])  → 可认领 task_001/002/003（全部）
引擎 B (scope=repo,    owner=hello-blog, tags=[coding]) → 只认领 task_001/003（该仓库）
引擎 C (scope=user,    owner=songspace,  tags=[coding]) → 只认领 task_001/002（该用户的）

引擎 A claim → 领走 task_001
引擎 B claim → 领走 task_003（task_001 已被 A 领走，task_002 不是该仓库）
引擎 C claim → 领走 task_002（task_001 已被 A 领走，task_003 不是该用户）
```

**scope 优先级（claim 排序优化）：**

任务入队时，服务端不指定 runtime_id，但可以在 claim 排序时让更精确 scope 的引擎优先认领：

```
claim 排序：repo 引擎 > user 引擎 > global 引擎
（精确 scope 优先认领，global 作为兜底）
```

这保证了：仓库有专属设备时优先用专属设备，全局设备只在专属设备空闲时才兜底。

### 2.5 agent_tasks 表改动

```sql
-- agent_tasks 新增 pool_tag 列（替代 pool_agent_type）
ALTER TABLE agent_tasks ADD COLUMN pool_tag TEXT DEFAULT '';
-- 任务入队时写入标签（如 "coding"），引擎按标签 claim
```

---

## 三、任务级场景选择

### 3.1 用户创建任务时选择场景

```
用户创建 Issue/任务 → VOD 澄清 → 判定场景 → 写入 issue_state.scene_id

默认场景：用户最近使用的开发场景（user_accounts.last_scene）
```

```sql
-- user_accounts 新增 last_scene 列
ALTER TABLE user_accounts ADD COLUMN last_scene TEXT DEFAULT 'default';
```

### 3.2 场景选择 API

```python
# POST /api/tasks/create
{
    "repo_key": "songspace/hello-blog",
    "title": "创建华为云VPC",
    "description": "...",
    "scene_id": "iac"           # 用户选择，缺省读 user_accounts.last_scene
}

# 响应
{
    "task_id": "t_xxx",
    "scene_id": "iac",
    "pipeline": {               # 返回该场景的流水线配置
        "nodes": [
            {"id": "vod", "tag": "vod", "skill": "iac-vod", "mode": "emit"},
            {"id": "issue", "tag": "issue", "skill": "iac-issue", "mode": "emit"},
            ...
        ]
    }
}
```

---

## 四、流水线配置设计

### 4.1 配置格式（替代 agent-skill-mapping.json）

```json
// configs/pipelines.json
{
  "scenes": {
    "default": {
      "name": "通用开发",
      "nodes": [
        {
          "id": "vod",
          "tag": "vod",
          "skill": "vod",
          "mode": "emit",
          "next": "issue",
          "handoff": "a2a"
        },
        {
          "id": "issue",
          "tag": "issue",
          "skill": "issue-advance",
          "mode": "emit",
          "next": "coding",
          "handoff": "a2a"
        },
        {
          "id": "coding",
          "tag": "coding",
          "skill": "implementation",
          "mode": "emit",
          "next": "qa",
          "handoff": "a2a"
        },
        {
          "id": "qa",
          "tag": "qa",
          "skill": "pr",
          "mode": "emit",
          "next": "sre",
          "handoff": "a2a"
        },
        {
          "id": "sre",
          "tag": "sre",
          "skill": "cloud-deploy",
          "mode": "emit",
          "next": "done",
          "handoff": "complete"
        }
      ]
    },
    "iac": {
      "name": "IaC开发",
      "nodes": [
        {
          "id": "vod",
          "tag": "vod",
          "skill": "iac-vod",
          "mode": "emit",
          "next": "issue",
          "handoff": "a2a"
        },
        {
          "id": "issue",
          "tag": "issue",
          "skill": "iac-issue",
          "mode": "waterfall",
          "next": "coding",
          "handoff": "a2a",
          "listeners": [
            {"tag": "sre", "skill": "iac-sre", "mode": "parallel", "condition": "need_deploy_check"}
          ]
        },
        {
          "id": "coding",
          "tag": "coding",
          "skill": "iac-coding",
          "mode": "emit",
          "next": "qa",
          "handoff": "a2a"
        },
        {
          "id": "qa",
          "tag": "qa",
          "skill": "iac-qa",
          "mode": "emit",
          "next": "sre",
          "handoff": "a2a"
        },
        {
          "id": "sre",
          "tag": "sre",
          "skill": "iac-sre",
          "mode": "emit",
          "next": "done",
          "handoff": "complete"
        }
      ]
    },
    "skill-dev": {
      "name": "华为云Skill开发",
      "nodes": [
        {
          "id": "vod",
          "tag": "vod",
          "skill": "vod",
          "mode": "emit",
          "next": "issue",
          "handoff": "a2a"
        },
        {
          "id": "issue",
          "tag": "issue",
          "skill": "skill-issue",
          "mode": "emit",
          "next": "coding",
          "handoff": "a2a"
        },
        {
          "id": "coding",
          "tag": "coding",
          "skill": "skill-creator",
          "mode": "emit",
          "next": "qa",
          "handoff": "a2a"
        },
        {
          "id": "qa",
          "tag": "qa",
          "skill": "skill-tester",
          "mode": "emit",
          "next": "sre",
          "handoff": "a2a"
        },
        {
          "id": "sre",
          "tag": "sre",
          "skill": "skill-publish",
          "mode": "emit",
          "next": "done",
          "handoff": "complete"
        }
      ]
    },
    "blog-publish": {
      "name": "博客发布",
      "nodes": [
        {
          "id": "vod",
          "tag": "vod",
          "skill": "blog-vod",
          "mode": "emit",
          "next": "issue",
          "handoff": "a2a"
        },
        {
          "id": "issue",
          "tag": "issue",
          "skill": "blog-issue",
          "mode": "emit",
          "next": "coding",
          "handoff": "a2a"
        },
        {
          "id": "coding",
          "tag": "coding",
          "skill": "ai-blog-generation",
          "mode": "emit",
          "next": "sre",
          "handoff": "a2a",
          "skip": ["qa"]
        },
        {
          "id": "sre",
          "tag": "sre",
          "skill": "blog-publish",
          "mode": "emit",
          "next": "done",
          "handoff": "complete"
        }
      ]
    }
  }
}
```

### 4.2 节点字段说明

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | string | 是 | 节点唯一标识（如 "vod"） |
| `tag` | string | 是 | 引擎标签（对应 engine_capabilities.tag） |
| `skill` | string | 是 | 该节点使用的 Skill 名称 |
| `mode` | string | 是 | 事件处理模式：emit/waterfall/serial/parallel |
| `next` | string | 是 | 完成后流转到的下一个节点 ID（"done" 表示结束） |
| `handoff` | string | 是 | 交办方式：a2a（Agent间交办）/ complete（直接完成） |
| `skip` | list | 否 | 跳过的节点 ID 列表（如 blog-publish 跳过 qa） |
| `listeners` | list | 否 | 并行监听器（用于 waterfall/parallel 模式） |
| `condition` | string | 否 | 触发条件（listeners 用） |

### 4.3 四种事件处理模式在流水线中的应用

```python
# src/api/pipeline_executor.py (新增)

class PipelineExecutor:
    """流水线执行器 — 按配置的 mode 调度各节点"""

    async def execute_node(self, node: PipelineNode, task: TaskContext):
        mode = node.mode or "emit"

        if mode == "emit":
            # 广播：通知所有有此标签的引擎，第一个 claim 的执行
            # 适合：默认模式，单 Agent 处理一个节点
            await self._emit_dispatch(node, task)

        elif mode == "waterfall":
            # 瀑布：中间件链，每个监听器可拦截/修改/批准
            # 适合：需要审批/检查的节点（如 Issue Agent 需 SRE 预检）
            proceed = await self._waterfall_dispatch(node, task)
            if not proceed:
                return  # 被拦截

        elif mode == "serial":
            # 串行：多个 Agent 按顺序处理同一节点
            # 适合：需要多步骤处理的节点（如 VOD 澄清 → Issue 分类 → Spec 生成）
            for agent_config in node.listeners or [node]:
                await self._dispatch_one(agent_config, task)

        elif mode == "parallel":
            # 并行：多个 Agent 同时处理，等全部完成
            # 适合：并行审查（如 QA + 安全扫描 + 性能测试 同时进行）
            await asyncio.gather(*[
                self._dispatch_one(listener, task)
                for listener in (node.listeners or [node])
            ])

    async def _emit_dispatch(self, node, task):
        """emit 模式：任务入队到标签队列，引擎自认领"""
        # 1. 任务写入 agent_tasks，pool_tag = node.tag
        enqueue_task(
            pool_tag=node.tag,       # 引擎按标签 claim
            skill=node.skill,
            event_seq=task.event_seq,
            # ...
        )
        # 2. 通知所有有此标签的引擎（SSE/websocket）
        await event_bus.emit(GrapeEvent(
            type="pipeline/node/queued",
            payload={"tag": node.tag, "node_id": node.id, "task_id": task.id}
        ))
        # 3. 引擎自行 claim_by_tag(node.tag, self_runtime_id)

    async def _waterfall_dispatch(self, node, task):
        """waterfall 模式：中间件链"""
        for listener in (node.listeners or []):
            result = await self._dispatch_and_wait(listener, task)
            if result.get("block"):
                return False  # 被拦截
        await self._emit_dispatch(node, task)  # 主 Agent 执行
        return True

    async def _parallel_dispatch(self, node, task):
        """parallel 模式：并行执行 + 等全部完成"""
        listeners = node.listeners or [node]
        results = await asyncio.gather(*[
            self._dispatch_and_wait(l, task) for l in listeners
        ], return_exceptions=True)
        # 所有结果汇总
        return results
```

### 4.4 流水线动态呈现

Pipeline 页面从固定 5 节点改为动态渲染配置的节点：

```javascript
// pipeline.js (改造)

function renderPipelineFlow(data) {
  const pipeline = data.pipeline;  // 从 API 获取流水线配置
  const nodes = pipeline.nodes || [];
  const statuses = data.stages || [];

  // 动态生成流程图节点（不再硬编码 5 个）
  let flowHtml = '<div class="af">';
  nodes.forEach((node, i) => {
    const status = statuses.find(s => s.node_id === node.id);
    const sc = status ? status.status_code : 0;  // 0=pending, 1=running, 2=done, 3=failed
    const mode = node.mode || 'emit';
    const modeIcon = {emit: '→', waterfall: '◇', serial: '↻', parallel: '⇄'}[mode] || '→';

    flowHtml += `<div class="nd" onclick="app.nav('${node.tag}')">
      <div class="nh">
        <span class="n" style="background:${node.color || 'var(--a)'}"></span>
        <h4>${node.id}</h4>
        <span class="ts t3">${modeIcon} ${mode}</span>
      </div>
      <div class="nds">${node.skill}</div>
      <div class="ns">
        <span class="d" style="background:${dotColor(sc)}"></span>
        ${statusText(sc)}
      </div>
    </div>`;
  });
  flowHtml += '</div>';
  return flowHtml;
}
```

---

## 五、10万+设备调度机制

### 5.1 标签化任务队列

```
任务入队时不再指定 runtime_id，而是指定 pool_tag：

  enqueue_task(pool_tag="coding", skill="implementation", ...)

引擎认领时按标签匹配：

  claim_by_tag("coding", runtime_id=35000)

服务端维护按标签分组的任务队列：

  vod 队列:    [task_001, task_002, ...]
  issue 队列:  [task_003, task_004, ...]
  coding 队列: [task_005, task_006, ...]
  qa 队列:     [task_007, ...]
  sre 队列:    [task_008, ...]

10万台设备的引擎按自身标签订阅队列：

  设备 A (tags=coding,qa, max_concurrent=2):
    → claim_by_tag("coding") → 领取 task_005
    → claim_by_tag("qa")     → 领取 task_007
    → 同时执行 2 个任务

  设备 B (tags=coding, max_concurrent=1):
    → claim_by_tag("coding") → 领取 task_006（task_005 已被 A 领走）
```

### 5.2 自适应负载均衡

```python
# 引擎注册时声明能力 + 状态
class EngineRegistration(BaseModel):
    runtime_id: int
    name: str              # hermes / opencode
    tags: list[str]        # ["coding", "qa"]
    max_concurrent: int    # 最大并发数
    healthy: bool
    device_name: str
    # 引擎自声明的繁忙度（0-100，0=空闲，100=满载）
    load_factor: int = 0

# claim 时的负载感知（服务端侧）
def claim_by_tag(tag: str, runtime_id: int) -> dict | None:
    """引擎 claim 时，服务端可以返回 None 表示"建议稍后再来"

    10万设备场景（全局+仓库+个人总和）：服务端不主动分配，只在 claim 时做健康+scope 校验。
    如果引擎负载过高（busy_slots >= max_concurrent），claim 返回 None，
    引擎会在下一轮 poll 时再试。
    """
    # 1. 校验标签能力 + 并发槽
    c.execute(
        "SELECT max_concurrent, enabled FROM engine_capabilities WHERE runtime_id=? AND tag=?",
        (runtime_id, tag)
    )
    cap = c.fetchone()
    if not cap or not cap[1]:
        return None  # 无此标签能力

    # 2. 检查该引擎当前并发数
    c.execute(
        "SELECT COUNT(*) FROM agent_tasks WHERE runtime_id=? AND status IN ('dispatched','running')",
        (runtime_id,)
    )
    current = c.fetchone()[0]
    if current >= cap[0]:  # max_concurrent
        return None  # 已满载，建议稍后再来

    # 3. 从标签队列取队首
    # ... (与 2.4 相同)
```

### 5.3 设备健康与故障转移

```
设备故障转移流程：

1. 设备 A (runtime_id=35000) 认领 task_005，状态 dispatched
2. 设备 A 心跳超时（15s 无心跳）→ agent_runtimes.status = 'offline'
3. 服务端回收循环检测：dispatched 超时（300s 未 start）→ 回收入队
4. task_005 重新变 queued，pool_tag="coding"
5. 设备 B (tags=coding) 下一轮 poll → claim_by_tag("coding") → 认领 task_005
6. 任务在新设备上重新执行

10万设备规模下的优化：
- 心跳合并：不是每个引擎独立心跳，而是 worker 进程级心跳（一个 worker 管多个引擎）
- 回收批量：不是逐个回收，而是批量扫描超时任务
- 队列分片：按标签分队列，避免 10万设备同时 claim 一个队列的锁竞争
```

---

## 六、兼容性设计

### 6.1 双模式运行

```python
# 环境变量开关
GRAPE_PIPELINE_MODE = "legacy"  # legacy / plugin

# legacy 模式：完全使用现有 event_pipeline.py 逻辑
# plugin 模式：使用流水线配置 + 标签化调度
```

### 6.2 兼容映射

| 现有机制 | 新机制 | 兼容方式 |
|---------|--------|---------|
| `devops_agent_runtime_binding` | `engine_capabilities` | binding 表保留，plugin 模式优先读 capabilities |
| `claim(runtime_id)` | `claim_by_tag(tag, runtime_id)` | legacy 模式用 claim，plugin 模式用 claim_by_tag |
| `issue_state` 阶段机 | 流水线节点状态 | issue_state 保留，plugin 模式额外维护 pipeline_state |
| `agent-skill-mapping.json` | `pipelines.json` | 旧文件保留，plugin 模式读新文件 |
| `pick_engine` | 标签队列自认领 | legacy 模式用 pick_engine，plugin 模式引擎主动 claim |

### 6.3 渐进迁移

```
Phase 1: 引擎标签化（不改变现有调度）
  ├── agent_runtimes 加 tags 列
  ├── engine_capabilities 表创建
  ├── worker 注册时上报标签
  └── 现有 binding/claim 逻辑不变

Phase 2: 流水线配置化（不改变现有执行）
  ├── pipelines.json 配置文件
  ├── Pipeline 页面动态渲染
  ├── issue_state.scene_id 任务级场景
  └── 现有 agent-skill-mapping.json 仍作为 fallback

Phase 3: 标签化任务队列（双模式并行）
  ├── agent_tasks 加 pool_tag 列
  ├── claim_by_tag 实现
  ├── GRAPE_PIPELINE_MODE 开关
  └── 两种模式可 A/B 对比

Phase 4: 事件模式支持（emit/waterfall/serial/parallel）
  ├── PipelineExecutor 实现 4 种模式
  ├── waterfall/serial/parallel 用于特定节点
  └── emit 为默认模式

Phase 5: 大规模优化
  ├── 队列分片
  ├── 心跳合并
  ├── 批量回收
  └── 移除 legacy 模式
```

---

## 七、改动清单

### 7.1 DB 改动

| 表 | 改动 | Phase |
|------|------|-------|
| `agent_runtimes` | 加 `tags TEXT DEFAULT ''` 列 | 1 |
| `engine_capabilities` | 新建表 | 1 |
| `agent_tasks` | 加 `pool_tag TEXT DEFAULT ''` 列 | 3 |
| `user_accounts` | 加 `last_scene TEXT DEFAULT 'default'` 列 | 2 |
| `issue_state` | 加 `scene_id TEXT DEFAULT ''` 列（已有） | 2 |
| `pipeline_state` | 新建表（流水线节点状态） | 2 |

### 7.2 新增文件

| 文件 | 说明 | Phase |
|------|------|-------|
| `configs/pipelines.json` | 流水线配置 | 2 |
| `src/api/pipeline_executor.py` | 流水线执行器（4 种事件模式） | 4 |
| `src/api/pipeline_config.py` | 流水线配置加载/校验 | 2 |
| `src/api/event_bus.py` | 事件总线 | 3 |
| `src/api/engine_tags.py` | 标签管理 | 1 |

### 7.3 修改文件

| 文件 | 改动 | Phase |
|------|------|-------|
| `src/runtime/service.py` | 新增 `claim_by_tag` + `enqueue_by_tag` | 3 |
| `src/runtime/agent.py` | worker 注册时上报标签 + claim_by_tag | 1 |
| `src/api/event_pipeline.py` | 双模式开关 + 流水线执行入口 | 3 |
| `src/api/routes/pipeline.py` | 返回流水线配置 + 动态节点状态 | 2 |
| `src/api/routes/tasks.py` | 任务创建时选择场景 | 2 |
| `src/api/static/js/pages/pipeline.js` | 动态渲染流水线节点 | 2 |
| `src/api/db_setup.py` | 新表/列 DDL + 迁移 | 1 |
| `src/api/routes/settings.py` | 引擎标签配置 UI | 1 |

---

## 八、验收用例设计

### 8.1 引擎标签化验收

| 用例 ID | 场景 | 预期 | 验证方法 |
|---------|------|------|---------|
| TC-TAG-01 | 引擎注册时声明标签 | engine_capabilities 表写入 (runtime_id, tag) 行 | 注册后查表确认 |
| TC-TAG-02 | 引擎声明多个标签 | 一个引擎可同时有 vod+issue 标签 | 查表确认多行 |
| TC-TAG-03 | 引擎注册时不传标签 | agent_runtimes.tags 为空，engine_capabilities 无行 | 注册后查表确认 |
| TC-TAG-04 | 引擎按标签 claim 任务 | coding 标签的引擎只认领 pool_tag=coding 的任务 | 入队 coding 任务，只有 coding 引擎领取 |
| TC-TAG-05 | 无标签引擎不能 claim | 未声明 coding 标签的引擎 claim 返回 None | 调用 claim_by_tag 验证 |
| TC-TAG-06 | 引擎并发限制 | max_concurrent=2 的引擎同时最多执行 2 个任务 | 连续 claim 第 3 次返回 None |
| TC-TAG-07 | 控制台查看引擎标签 | Agent Runtimes 页面引擎卡片显示标签列表 | UI 验证 |
| TC-TAG-08 | 控制台添加标签 | 点击 + 添加标签，选择 tag/并发/优先级，保存成功 | UI 操作 + 查表确认 |
| TC-TAG-09 | 控制台修改标签 | 点击 ✏ 修改并发数/优先级，保存成功 | UI 操作 + 查表确认 |
| TC-TAG-10 | 控制台删除标签 | 点击 🗑 删除标签，引擎不再认领此标签任务 | UI 操作 + claim 验证 |
| TC-TAG-11 | 无标签引擎显示"无" | 引擎卡片显示"标签: 无（点击 + 添加标签...）" | UI 验证 |
| TC-TAG-12 | 标签删除后引擎仍在线 | 删除标签不影响引擎 heartbeat 和其他标签的任务认领 | 删除后观察引擎状态 |

### 8.2 设备 scope 隔离验收

| 用例 ID | 场景 | 预期 | 验证方法 |
|---------|------|------|---------|
| TC-SCOPE-01 | 全局设备认领所有任务 | scope=global 引擎可认领任意仓库的 queued 任务 | 入队不同仓库任务，全局引擎都能领取 |
| TC-SCOPE-02 | 仓库设备只认领本仓库任务 | scope=repo 引擎只认领 owner_id 对应仓库的任务 | 入队其他仓库任务，仓库引擎 claim 返回 None |
| TC-SCOPE-03 | 个人设备只认领本人任务 | scope=user 引擎只认领该用户创建的任务 | 入队其他用户任务，个人引擎 claim 返回 None |
| TC-SCOPE-04 | 仓库设备认领非本仓库返回 None | scope=repo(owner=hello-blog) claim shelltest 仓库任务 → None | 调用 claim_by_tag 验证 |
| TC-SCOPE-05 | 个人设备认领非本人返回 None | scope=user(owner=songspace) claim user_id=2 的任务 → None | 调用 claim_by_tag 验证 |
| TC-SCOPE-06 | scope 优先级排序 | repo 引擎优先于 global 引擎认领同仓库任务 | 同时 claim，repo 引擎先领到 |
| TC-SCOPE-07 | 全局设备兜底 | 无 repo/user 设备时，global 设备认领所有任务 | 移除 repo 设备，global 设备接管 |
| TC-SCOPE-08 | scope 继承自凭据 | 引擎 scope = runtime_credentials.scope，注册时自动继承 | 注册后查 agent_runtimes.scope 确认 |
| TC-SCOPE-09 | 仓库设备配置标签后立即生效 | 为 repo 引擎添加 coding 标签后，立即开始认领该仓库 coding 任务 | 控制台添加标签后观察 claim |
| TC-SCOPE-10 | 多 scope 混合场景 | global + repo + user 设备同时在线，各自只认领 scope 内任务 | 混合部署验证 |

### 8.3 任务级场景验收

| 用例 ID | 场景 | 预期 | 验证方法 |
|---------|------|------|---------|
| TC-SCENE-01 | 创建任务时选择场景 | issue_state.scene_id 写入选择的场景 | 查表确认 |
| TC-SCENE-02 | 不选场景默认用户最近场景 | 读 user_accounts.last_scene | 不传 scene_id 验证 |
| TC-SCENE-03 | 场景使用后更新 last_scene | 执行后 user_accounts.last_scene 更新 | 执行前后对比 |
| TC-SCENE-04 | 不同场景走不同流水线 | iac 场景使用 iac-coding skill，default 使用 implementation | 执行时检查 skill |

### 8.4 流水线配置验收

| 用例 ID | 场景 | 预期 | 验证方法 |
|---------|------|------|---------|
| TC-PIPE-01 | 默认流水线 5 节点 | Pipeline 页面显示 5 个节点 | UI 截图 |
| TC-PIPE-02 | blog-publish 跳过 qa | 流水线节点无 qa | 检查配置 + UI |
| TC-PIPE-03 | 自定义节点顺序 | nodes 按 JSON 配置顺序渲染 | UI 验证 |
| TC-PIPE-04 | 节点状态实时更新 | running→completed 时节点颜色变化 | SSE 推送验证 |
| TC-PIPE-05 | 流水线配置热加载 | 修改 pipelines.json 后不需重启 | 修改后创建新任务验证 |
| TC-PIPE-06 | pipeline_state 表记录节点状态 | 每个节点的 status/started_at/completed_at | 查表确认 |

### 8.5 事件处理模式验收

| 用例 ID | 场景 | 预期 | 验证方法 |
|---------|------|------|---------|
| TC-MODE-01 | emit 模式（默认） | 任务入队，第一个 claim 的引擎执行 | 单任务单引擎验证 |
| TC-MODE-02 | waterfall 模式 | 中间件链逐个调用，可拦截 | listener 返回 block=True，主 Agent 不执行 |
| TC-MODE-03 | serial 模式 | 多个 Agent 按顺序处理 | listeners 逐个执行，有先后顺序 |
| TC-MODE-04 | parallel 模式 | 多个 Agent 同时执行，等全部完成 | asyncio.gather 验证并行 |
| TC-MODE-05 | waterfall 拦截后不继续 | 被拦截的节点不执行主 Agent | 检查事件日志 |

### 8.6 大规模设备验收

| 用例 ID | 场景 | 预期 | 验证方法 |
|---------|------|------|---------|
| TC-SCALE-01 | 100 个引擎同标签 claim | 任务被均匀分配，无死锁 | 模拟 100 引擎并发 claim |
| TC-SCALE-02 | 引擎离线后任务转移 | 超时任务被其他引擎认领 | kill 引擎，观察任务回收+重新认领 |
| TC-SCALE-03 | 新引擎上线自动认领 | 新注册的引擎立即开始 claim | 注册新引擎后观察 |
| TC-SCALE-04 | 并发限制生效 | max_concurrent=1 的引擎串行执行 | 连续 claim 验证 |
| TC-SCALE-05 | 心跳超时回收 | 15s 无心跳→offline，300s dispatched 超时→回收入队 | 停止心跳观察 |

### 8.7 兼容性验收

| 用例 ID | 场景 | 预期 | 验证方法 |
|---------|------|------|---------|
| TC-COMPAT-01 | legacy 模式完全兼容 | GRAPE_PIPELINE_MODE=legacy 时行为不变 | 现有测试全通过 |
| TC-COMPAT-02 | 双模式切换 | 切换开关后立即生效 | 环境变量切换 |
| TC-COMPAT-03 | 现有 binding 不丢失 | 升级后 binding 表数据保留 | 查表确认 |
| TC-COMPAT-04 | 现有 webhook 不受影响 | webhook 事件正常处理 | 触发 webhook 验证 |
| TC-COMPAT-05 | 现有 A2A 协议兼容 | A2A 交办正常推进流水线 | Agent 执行后 A2A 验证 |

---

## 九、配置样例汇总

### 9.1 完整 pipelines.json 配置样例

见 §四「流水线配置设计」中的完整 JSON，包含 default/iac/skill-dev/blog-publish 四个场景。

### 9.2 引擎标签注册样例

```json
// POST /api/runtime/register（worker 注册时）
{
  "device_name": "worker-ecs-d27a",
  "runtimes": [
    {
      "name": "hermes",
      "version": "v0.20.5",
      "healthy": true,
      "installed": true,
      "tags": ["vod", "issue", "coding", "qa", "sre"],
      "max_concurrent": 3
    },
    {
      "name": "opencode",
      "version": "1.18.18",
      "healthy": true,
      "installed": true,
      "tags": ["coding", "qa"],
      "max_concurrent": 2
    }
  ]
}
```

### 9.3 任务创建样例

```json
// POST /api/tasks/create
{
  "repo_key": "songspace/hello-blog",
  "title": "创建华为云VPC和子网",
  "description": "我需要准备IaC基础设施...",
  "scene_id": "iac"
}

// 响应
{
  "task_id": "t_abc123",
  "scene_id": "iac",
  "pipeline": {
    "name": "IaC开发",
    "nodes": [
      {"id": "vod", "tag": "vod", "skill": "iac-vod", "mode": "emit"},
      {"id": "issue", "tag": "issue", "skill": "iac-issue", "mode": "waterfall"},
      {"id": "coding", "tag": "coding", "skill": "iac-coding", "mode": "emit"},
      {"id": "qa", "tag": "qa", "skill": "iac-qa", "mode": "emit"},
      {"id": "sre", "tag": "sre", "skill": "iac-sre", "mode": "emit"}
    ]
  }
}
```

### 9.4 引擎 claim 样例

```json
// POST /api/runtime/claim-by-tag
{
  "tag": "coding",
  "runtime_id": 35000
}

// 响应（有任务）
{
  "task": {
    "id": "t_abc123",
    "pool_tag": "coding",
    "skill": "iac-coding",
    "prompt": "...",
    "cwd": "/data/workspaces/.../issues/1/coding"
  }
}

// 响应（无任务或已满载）
{
  "task": null
}
```
