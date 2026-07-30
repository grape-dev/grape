# Webhook 事件处理方案

## 1. 完整处理流程

```
GitCode 触发事件 (Push Hook / Issue Hook / Note Hook / Merge Request Hook...)
  │
  │  POST http://203.0.113.2:8080/webhook/gitcode
  │  Headers: X-Gitcode-Event, X-Gitcode-Token
  │  Body: JSON payload
  │
  ▼
┌─ 1. Webhook 接收（webhook.py:gitcode_webhook）── 端口 8080 ────┐
│                                                                  │
│  ├── (a) 读取 X-Gitcode-Event / X-Gitcode-Token / body           │
│  ├── (b) 解析 JSON → 提取 project.path_with_namespace            │
│  ├── (c) 查 managed_repos → get_webhook_token_by_repo(repo)     │
│  ├── (d) Token 对比验证（仅托管仓库需要验证）                     │
│  │    一致 → 继续                                                │
│  │    不一致 → 403 invalid webhook token                         │
│  └── (e) 传 repo_key_override → run_pipeline()                   │
│                                                                  │
└────────────────────────────────────┬─────────────────────────────┘
                                     │
                                     ▼
┌─ 2. 事件管道（event_pipeline.py:run_pipeline）──────────────────┐
│                                                                  │
│  Step 1 — 归一化                                                 │
│  ├── get_event_header(headers) → 大小写不敏感匹配                │
│  │   (修复：dict(request.headers) 转小写后匹配)                  │
│  ├── GitCodeBackend.normalize(raw, event_header)                 │
│  │   ├── 事件类型映射:                                           │
│  │   │   "Push Hook"           → push                           │
│  │   │   "Issue Hook"          → issue_opened / issue_updated   │
│  │   │   "Note Hook"           → issue_comment / note_hook_pr   │
│  │   │   "Merge Request Hook"  → pull_request_merged/opened     │
│  │   │   "tag_push"            → tag_created                    │
│  │   │   "release"             → release_created                │
│  │   │   "pipeline"            → build_start                    │
│  │   ├── 区分 Note Hook type:                                    │
│  │   │   noteable_type=="MergeRequest" → note_hook_pr           │
│  │   │   否则 → issue_comment                                    │
│  │   └── 返回 { type, repository, actor, payload }               │
│  │                                                               │
│  Step 2 — 反循环过滤                                             │
│  ├── resolve_actor() → 提取事件触发者                             │
│  ├── 评论事件 (issue_comment / note_hook_pr)                     │
│  │   且 actor 在 bot_usernames 中 → 跳过（不会死循环）            │
│  │                                                               │
│  Step 3 — 路由（routing.py:get_domain_agent）                    │
│  ├── SQL: SELECT agent_type FROM agent_type_config               │
│  │         WHERE order_num>0                                     │
│  │         AND subscribe_events LIKE '%"issue_comment"%'         │
│  ├── 返回 agent_type (如 "issue") 或 "" (无匹配则跳过)            │
│  │                                                               │
│  Step 4 — 查找 Agent 会话 + 回填默认                              │
│  ├── SQL: SELECT backend,skill FROM agent_sessions               │
│  │         WHERE repo_key=? AND agent_type=?                     │
│  ├── 找到 → 使用用户配置的 backend + skill                        │
│  ├── 没找到 → 读 agent-skill-mapping.json                        │
│  │           → scenes.default.mapping[agent_type]                │
│  │   ├── 有默认 skill → 使用 (backend="opencode")                │
│  │   └── 无默认 skill → 跳过（return, 不处理）                   │
│  │                                                               │
│  Step 5 — 提取 Issue ID                                          │
│  ├── 从 payload.issue.number / pull_request.number               │
│  │   或 object_attributes.number/iid 提取                       │
│  │                                                               │
│  Step 6 — 存储事件（event_store.py:append_event）                 │
│  ├── INSERT INTO events (repo_key, issue_id, event_type,         │
│  │    source, agent_type, title, payload, status='running')      │
│  └── 返回 seq                                                    │
│                                                                  │
│  Step 7 — 后台执行 Agent（新增）                                  │
│  ├── threading.Thread(target=_execute_agent, daemon=True)        │
│  └── 立即返回 HTTP 200，Agent 在线程中执行                        │
│                                                                  │
└────────────────────────────────────┬─────────────────────────────┘
                                     │
                                     ▼
┌─ 3. 返回 HTTP 200 ─────────────────────────────────────────────┐
│  {"status": "ok", "seq": 42, "event_type": "issue_comment"}     │
└─────────────────────────────────────────────────────────────────┘
                                     │
                          （后台线程继续）
                                     ▼
┌─ 4. Agent 执行（event_pipeline.py:_execute_agent）─── 后台线程 ─┐
│                                                                  │
│  ├── (a) 参数来源                                                │
│  │    ├── backend:   agent_sessions.backend 或 默认 "opencode"  │
│  │    ├── skill:     agent_sessions.skill 或 agent-skill-mapping │
│  │    │              → scenes.default.mapping[agent_type]        │
│  │    └── oc_session_id:  agent_sessions.oc_session_id           │
│  │                   （当前传 ""，未读此字段，Agent 无上下文记忆） │
│  │                                                              │
│  ├── (b) 创建工作目录:                                           │
│  │    data/workspaces/{repo}/{agent_type}-agent/                 │
│  │                                                              │
│  ├── (c) 构建 Prompt（prompt_builder.py:build_prompt）            │
│  │   ├── 事件描述（Issue/PR 标题、内容、评论等）                  │
│  │   ├── Issue 聚类摘要（events 表按 issue 聚类）                │
│  │   ├── Skill 指令注入（skills/{skill}/SKILL.md）               │
│  │   ├── 角色定义（build_role_definition）                        │
│  │   └── A2A 协议指令 + 可用 Agent 注册表                       │
│  │                                                              │
│  ├── (d) 获取 repo Bot Token                                     │
│  │    get_bot_token_by_repo(owner, repo)                        │
│  │                                                              │
│  ├── (e) 调用 stream_agent()（agent/executor.py）                 │
│  │   ├── 选择 Backend（opencode / codearts）                      │
│  │   ├── 注入环境变量:                                           │
│  │   │   GITCODE_API_TOKEN (repo Bot Token)                     │
│  │   │   REPO_FULL_NAME                                         │
│  │   │   DEVOPS_EVENT_TYPE                                      │
│  │   ├── spawn 子进程: opencode --format json --prompt ...       │
│  │   ├── NDJSON 流式解析:                                        │
│  │   │   step_start / text / reasoning / tool_use               │
│  │   │   step_finish / error / done                             │
│  │   └── 阻塞等待 Agent 完成                                     │
│  │                                                              │
│  └── (f) 更新事件状态                                            │
│       exit_code == 0 → update_event_status(seq, "completed")    │
│       exit_code != 0 → update_event_status(seq, "failed")       │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

## 2. 路由判定（单级）

### 唯一路由

`agent_type_config.subscribe_events`（仅 `order_num>0` 的 5 个 DevOps Agent）

```python
def get_domain_agent(event_type: str) -> str:
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT agent_type FROM agent_type_config "
            "WHERE order_num>0 AND subscribe_events LIKE ?",
            (f'%"{event_type}"%',)
        )
        row = c.fetchone()
        if row:
            return row[0]
    return ""
```

### 三种路由优先级

```
事件到达 → 1. agent_sessions 表（用户 UI 配置的 backend + skill）
         → 2. agent-skill-mapping.json default 场景（首次接入自动创建）
         → 3. 跳过（无匹配且无默认映射）
```

## 3. Agent 会话创建时机

| 时机 | 方式 | 说明 |
|------|------|------|
| 加入/托管仓库 | `ensure_default_agent_sessions()` | 自动创建 5 个 Agent 行，skill 来自 default 场景 |
| Dashboard 场景选择器 | `POST /api/scenes/apply` | 手动选择场景，刷新所有 Agent 的 skill |
| Agent 配置页 | `PUT /api/agents/{name}/{owner}/{repo}` | 手动配置单个 Agent 的 backend + skill |
| 解除托管 | `remove_agent_sessions()` | 删除仓库所有 agent_sessions |

## 4. _execute_agent() 参数来源

_execute_agent() 的参数从 pipeline 的 Step 4（查找 Agent 会话 + 回填默认）决定：

| 参数 | 来源 | 说明 |
|------|------|------|
| `backend` | `agent_sessions.backend` 或 `"opencode"`（默认映射固定写死） | Agent 引擎（opencode / codearts），仅 UI 可修改 |
| `skill_name` | `agent_sessions.skill` 或 `agent-skill-mapping.json` 的 default 场景映射 | 决定注入哪个 SKILL.md |
| `oc_session_id` | `agent_sessions.oc_session_id`（当前传 `""`） | opencode 对话记忆，**当前未启用**。如果读取此字段传给 `stream_agent()` 并在执行完毕后更新回 `agent_sessions`，Agent 可延续上下文 |

**当前缺口：** `session_id` 传空，Agent 对同一个 repo 的多次事件没有上下文连贯性。需要补充：
1. `_execute_agent()` 开始前从 `agent_sessions` 读 `oc_session_id`
2. 传给 `stream_agent(session_id=oc_session_id)`
3. 执行完毕后从 done event 提取新的 `oc_session_id` → `UPDATE agent_sessions`

## 5. 关键修复记录

| 问题 | 根因 | 修复 |
|------|------|------|
| Note Hook 不处理 | `subscribe_events` 全为 `[]`，seed 因 `count>0` 跳过 | `_migrate_tables()` 新增回填：JSON→`UPDATE WHERE subscribe_events='[]'` |
| JSON 解析报错 | `agent-description.json` 含 `\UXXXXXXXX` 逃逸（Python 3.12+ 拒绝） | 替换为真实 UTF-8 emoji 字符 |
| raw_type=unknown | `dict(request.headers)` 转小写，`X-GitCode-Event` 匹配不到 | `get_event_header()` 改为大小写不敏感匹配 |
| Agent 不执行 | pipeline 只存事件，未调用 `stream_agent()` | 新增 `_execute_agent()` 后台线程执行 |

## 6. Token 验证

```python
def get_webhook_token_by_repo(full_name: str) -> str:
    with db_lock:
        c = db.cursor()
        c.execute("SELECT gitcode_webhook_token FROM managed_repos WHERE full_name=? LIMIT 1", (full_name,))
        row = c.fetchone()
        return row[0] if row else ""
```

## 7. 数据表关系

```sql
-- agent_type_config: Agent 类型定义 + 订阅事件
agent_type_config (
    agent_type TEXT PRIMARY KEY,        -- "issue", "coding" ...
    display_name TEXT NOT NULL,
    order_num INTEGER DEFAULT 0,        -- >0 的为 DevOps Agent
    subscribe_events TEXT DEFAULT '[]',  -- JSON 数组 ["issue_opened","issue_comment"]
    ...
)

-- agent_sessions: 每个 repo 下每个 Agent 的运行时配置
agent_sessions (
    id TEXT PRIMARY KEY,                -- 自生成 UUID
    repo_key TEXT NOT NULL,             -- "owner/repo"
    agent_type TEXT NOT NULL,           -- "issue", "coding" ...
    backend TEXT DEFAULT 'opencode',    -- Agent 引擎
    skill TEXT DEFAULT '',              -- 绑定的 Skill 名称
    oc_session_id TEXT DEFAULT '',      -- opencode 会话 ID（用于上下文记忆）
    repo_gitcode_token TEXT DEFAULT '', -- repo 级 Bot Token
    status TEXT DEFAULT 'idle',         -- idle / running
    created_at / last_active_at,
    UNIQUE(repo_key, agent_type)        -- 每个 repo 每种 Agent 只有一行
)

-- events: 事件记录（pipeline 存储 + Agent 执行结果）
events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_key TEXT NOT NULL,
    event_type TEXT NOT NULL,
    agent_type TEXT DEFAULT '',
    status TEXT DEFAULT 'pending',      -- running / completed / failed
    ...
)

-- managed_repos: 仓库托管信息 + token 验证
managed_repos (full_name, gitcode_webhook_token, bot_gitcode_token)

-- agent-skill-mapping.json（配置文件）：场景 → Agent → Skill 默认映射
-- agent-description.json（配置文件）：Agent 类型 + subscribe_events
```

## 8. 日志关键节点示例

```
Webhook: received event=Note Hook, body_size=3134, token=Cz9ZX1kU...
Webhook: extracted repo=Jason7602/huaweicloud-skills
Webhook: expected_token=found
Webhook: token OK, repo=..., event=Note Hook
Pipeline: start, raw_type=Note Hook, source=gitcode, repo=...
Pipeline: actor=Jason7602, event_type=issue_comment
Pipeline: comment from Jason7602, not a bot (['devops-bot','hwc-dev-bot'])
Route: issue_comment -> issue
Pipeline: using default skill for issue=issue
Pipeline: issue_id=#2, title=issue_comment
Event stored: seq=2, repo=gitcode:..., type=issue_comment, agent=issue, status=running
Pipeline: started agent execution for seq=2
Exec agent: type=issue, backend=opencode, skill=issue, seq=2
Exec agent: spawning opencode for seq=2
  ▶ step: analyzing issue...
  💬 response...
Agent done: exit=0, text_len=120
Exec agent: seq=2 -> completed
```

## 9. 涉及端口

| 端口 | 进程 | CMD | 用途 |
|------|------|-----|------|
| 80 | `uvicorn main:app` | `python -m src.api.run` (子进程1) | SPA + Dashboard + API + Feishu WS |
| 8080 | `uvicorn run_webhook:app` | `python -m src.api.run` (子进程2) | 仅 webhook/gitcode + A2A 端点 |
