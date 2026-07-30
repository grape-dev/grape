# Webhook 事件处理方案

## 1. 完整处理流程

```
GitCode 触发事件（push / issue / PR / 评论等）
  │
  │  HTTP POST → http://203.0.113.2/webhook/gitcode
  │  Headers: X-GitCode-Event, X-GitCode-Token
  │
  ▼
┌─ 1. Webhook 接收 ─────────────────────────────────────────┐
│  POST /webhook/gitcode（webhook.py）                         │
│                                                             │
│  ├─ (a) 解析事件体获取 repo 名                                │
│  │    payload.project.path_with_namespace                    │
│  │    → "Jason7602/devops-bot"                               │
│  │    取不到则尝试 payload.repository.full_name              │
│  │                                                           │
│  ├─ (b) 用 repo 查 managed_repos 的预期 token               │
│  │    SELECT gitcode_webhook_token FROM managed_repos        │
│  │    WHERE full_name='owner/repo'                           │
│  │    → "预期 token"                                         │
│  │    若 managed_repos 中没有该 repo 的记录（未托管），      │
│  │    跳过 token 验证（仅记录日志）                           │
│  │                                                           │
│  ├─ (c) 与请求头 X-GitCode-Token 对比验证                   │
│  │    一致 → 继续处理                                        │
│  │    不一致 → 403                                           │
│  │                                                           │
│  └─ (d) 传 repo_key_override = "gitcode:owner/repo"         │
│       → run_pipeline()                                       │
└─────────────────────────────────────────────────────────────┘
  │
  ▼
┌─ 2. 事件管道 ─────────────────────────────────────────────┐
│  run_pipeline(body, headers, repo_key_override)              │
│  （event_pipeline.py）                                        │
│                                                             │
│  ├─ (a) 归一化 ───────────────────────────────────────── │
│  │    GitCodeBackend.normalize()                              │
│  │    → { type, repository, actor, payload, title }          │
│  │                                                           │
│  ├─ (b) 自循环检测 ───────────────────────────────────── │
│  │    actor in bot_usernames? → 跳过                         │
│  │                                                           │
│  ├─ (c) 路由判定 ─────────────────────────────────────── │
│  │    get_domain_agent(event_type) → routing.py              │
│  │    └─ 单级路由：agent_type_config.subscribe_events       │
│  │       SELECT agent_type FROM agent_type_config            │
│  │       WHERE order_num>0                                   │
│  │       AND subscribe_events LIKE '%event_type%'            │
│  │       → 返回 agent_type（如 "issue"）                     │
│  │                                                           │
│  ├─ (d) 查找 Agent 配置 ──────────────────────────────── │
│  │    get_agent_session(repo_key, agent_type)                │
│  │    → agent_sessions 表                                    │
│  │    → 返回 backend + skill                                 │
│  │                                                           │
│  ├─ (e) 提取 Issue ID ──────────────────────────────── │
│  │                                                           │
│  └─ (f) 存储事件 ────────────────────────────────────── │
│       append_event(status="running")                         │
│       → events 表（agent_type = 路由到的 skill）             │
└─────────────────────────────────────────────────────────────┘
  │
  ▼
┌─ 3. 返回 ──────────────────────────────────────────────────┐
│  {"status": "ok", "seq": 42, "event_type": "issue_opened"}   │
└─────────────────────────────────────────────────────────────┘
```

## 2. 路由判定（单级）

### 删除

- `repo_skill_mapping` 表的第一级路由（用户自定义映射）
- `event_route_config` 表的第二级路由（仓库覆盖）

### 保留并作为唯一路由

`agent_type_config.subscribe_events`（仅 `order_num>0` 的 5 个 DevOps Agent，不含 skill）

```python
def get_domain_agent(event_type: str) -> str:
    """单级路由：从 agent_type_config.subscribe_events 查找匹配的 Agent"""
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

### 仓库覆盖规则的替代方案

当前 `huaweicloud-skills` 的 PR 事件需要路由到 `skill-pr` 而非 `pr`。处理方式：

1. `skill-pr` 的 `subscribe_events` 中不声明 `pull_request_*`
2. Agent 配置页面提供一个 **「接管事件类型」** 的设置
3. 或将来在 `agent_sessions` 表加一个 `override_events` 字段，存储该 Agent 覆盖处理的额外事件类型

### 实施步骤

| # | 文件 | 改动 |
|---|------|------|
| 1 | `src/api/routing.py` | 删除 level 1/2，只保留 `agent_type_config.subscribe_events` 查询（仅 `order_num>0`） |
| 2 | `src/api/event_pipeline.py` | 路由后通过 `agent_sessions` 查找 `backend` 和 `skill` |
| 3 | `configs/skill-description.json` | 去掉 `subscribe_events`（skill 不需要） |

### 保留的表

- `agent_type_config` — `subscribe_events` 字段（Agent 订阅事件）
- `event_route_config` — 保留数据，代码不再读它
- `repo_skill_mapping` — 保留数据，代码不再读它
- `agent_sessions` — 路由后查 backend/skill

## 3. 事件 → Agent → Session 的完整链路

```
webhook 事件
  → event_type = "issue_opened"
  → get_domain_agent("issue_opened")
      → agent_type_config.subscribe_events → "issue"
  → get_agent_session("owner/repo", "issue")
      → agent_sessions.repo_key + agent_type
      → return { backend: "opencode", skill: "issue-management" }
  → append_event(agent_type="issue", ...)
  → events 表
```

## 4. Agent 执行（当前状态）

事件存入 `events` 表后，Agent 的执行尚未自动化。当前状态：

| 方式 | 状态 |
|------|------|
| 定时调度器（scheduler.py） | 代码已写，未自动启动 |
| 用户手动触发 | ✅ 网页 Agent 页面可点击执行 |
| A2A 链式调用 | ✅ 协议已定义 |

后续需要将 scheduler 集成到 `main.py` 的启动流程中。

## 5. Token 验证函数

### 新增 `get_webhook_token_by_repo()`

```python
def get_webhook_token_by_repo(full_name: str) -> str:
    """根据 full_name（owner/repo）查找 repo 的预期 webhook token"""
    with db_lock:
        c = db.cursor()
        c.execute("SELECT gitcode_webhook_token FROM managed_repos WHERE full_name=? LIMIT 1", (full_name,))
        row = c.fetchone()
        return row[0] if row else ""
```

### 删除

- ~~`resolve_repo_by_webhook_token()`~~ — 不再使用

### Webhook 验证伪代码

```python
@router.post("/webhook/gitcode")
async def gitcode_webhook(request):
    body = await request.body()
    token = request.headers.get("X-GitCode-Token", "")
    payload = json.loads(body)

    # 1. 从事件体提取 repo
    repo = payload.get("project", {}).get("path_with_namespace", "")
    if not repo:
        repo = payload.get("repository", {}).get("full_name", "")

    # 2. 用 repo 查预期 token
    expected = get_webhook_token_by_repo(repo)

    # 3. 对比验证（仅托管仓库需要验证）
    if expected and token != expected:
        return 403

    # 4. 处理事件
    result = run_pipeline(body, headers, repo_key_override=f"gitcode:{repo}")
    return {"status": "ok", "seq": result["seq"]}
```

## 6. 数据表关系

```sql
-- agent_type_config: 记录 Agent 类型及其订阅的事件
agent_type_config (agent_type, subscribe_events)

-- agent_sessions: 记录每个 repo 下每个 Agent 的运行时配置
agent_sessions (repo_key, agent_type, backend, skill)

-- events: 事件记录
events (seq, repo_key, event_type, agent_type, status)

-- managed_repos: 仓库托管信息 + webhook 验证
managed_repos (full_name, gitcode_webhook_token, bot_gitcode_token)

-- event_route_config: 保留（不再代码读取）
-- repo_skill_mapping: 保留（不再代码读取）
```
