# Agentic DevOps — 技术设计文档

> 基于 `system-workflow-overview.md` 和 `prototype.html` 的完全重构方案。

---

## 一、系统概述

Agentic DevOps 是一个基于事件驱动的多 Agent 协作 DevOps 平台。架构为 **Bot-Shell / Agent-Brain** 分离模式：

- **Bot（Shell）**：Python 编排层，负责 Webhook 接收、事件归一化、路由分发、Prompt 构建、Agent 执行、会话管理
- **Agent（Brain）**：CLI 子进程（opencode / codearts），接收完整文本 Prompt，自主执行 Skill、调用 API，输出 NDJSON 事件流

**与旧版的关键区别：** 本次重构完全移除旧的 Admin/User HTML 渲染面板（端口 80），统一为单一 SPA 前端 + REST JSON API 后端 + Webhook 事件引擎。

| 旧版 | 新版 |
|------|------|
| Python HTTP 服务端渲染 HTML | FastAPI REST JSON API |
| 两个面板（Admin + User）分离 | 单一 SPA 前端 |
| 凭据管理分散（/user/cred） | 统一 Settings API |
| VM 管理（/user/vm*） | 暂不提供（可按需扩展） |
| 端口 80 + 8080 双服务 | 单一 API 端口 + Webhook 端口 |

---

## 二、系统架构

```
┌──────────────────────────────────────────────────────────────┐
│                       外部系统                               │
│  GitCode (Webhook/OAuth/API)   Feishu (WS Chat)  Huawei Cloud│
└──────────────┬─────────────────────┬──────────────────┬──────┘
               │                     │                  │
┌──────────────▼─────────────────────▼──────────────────▼──────┐
│                       接入层                                 │
│                                                              │
│  :80   SPA 前端 (静态文件)                                    │
│  :3000 REST JSON API (FastAPI) — 认证、仓库、看板、Agent...   │
│  :8080 Webhook + A2A + Health (http.server)                  │
│  WS    飞书 IM 长连接 (lark-oapi)                             │
└──────────────┬───────────────────────────────────────────────┘
               │
┌──────────────▼───────────────────────────────────────────────┐
│                    事件处理管道                               │
│                                                              │
│  webhook_handler.py → event_processor.py                     │
│    ├─ 归一化 (VCS Adapter)                                   │
│    ├─ 反循环过滤                                              │
│    ├─ IM 通知分支                                             │
│    ├─ 逐仓库锁 → 路由解析 → 上下文预取                        │
│    └─ Prompt 构建 → Agent 执行 → 结果记录                     │
└──────────┬───────────────────────────────────────────────────┘
           │
┌──────────▼──────────┐  ┌─────────────────────────────────────┐
│  prompt_builder.py   │  │  agent/executor.py                  │
│  · 事件描述+历史     │  │  · Backend 抽象 (OpenCode/CodeArts) │
│  · Skill 指令注入    │  │  · NDJSON 流式解析                   │
│  · 角色定义+规则     │  │  · 超时控制 (120s)                   │
│  · A2A 协议+注册表   │  │  · 凭据环境注入                      │
└─────────────────────┘  └─────────────────────────────────────┘
           │
┌──────────▼───────────────────────────────────────────────────┐
│                       数据层                                  │
│  data/sessions.db (SQLite)                                    │
│  ┌─────────────────┬─────────────────┬──────────────────────┐ │
│  │ user_accounts   │ agent_sessions   │ feishu_bindings      │ │
│  │ managed_repos   │ events           │ repo_skill_mapping   │ │
│  │ user_vms        │ user_credentials│ oauth_states         │ │
│  └─────────────────┴─────────────────┴──────────────────────┘ │
│  skills/ (文件系统)   data/workspaces/   configs/ (JSON)      │
└──────────────────────────────────────────────────────────────┘
```

---

## 三、前端 SPA

### 3.1 技术选型

| 项目 | 选项 | 原因 |
|------|------|------|
| 框架 | **Vanilla JS** | 无框架依赖，原型可直接演进 |
| 构建 | 无构建 | 纯 HTML/CSS/JS，部署即生效 |
| 样式 | CSS 变量 | 12 个设计令牌，统一视觉 |
| 状态 | localStorage + 内存 | token、repo、配置缓存 |
| API  | fetch() | 原生 JS，无需额外库 |

### 3.2 页面树

```
Login Page
  ├─ GitCode OAuth (主按钮)
  ├─ 密码登录 (toggle 展开)
  └─ 管理员登录 (跳转 /admin/login)

App Shell (Sidebar + Topbar + Content)
  ├─ Repo Selection (全屏，首次登录无仓库)
  ├─ Dashboard (per-repo, 侧边栏下拉切换)
  ├─ Pipeline (活跃+历史, 节点/行点击→Event Detail)
  ├─ Agent Detail × 5 (VOD/Issue/Coding/QA/SRE)
  │   ├─ 指标卡 + Event History
  │   └─ [Configure] → Backend Engine + Skill 下拉
  ├─ Event Detail (上下文+日志 / 元信息+反馈)
  ├─ Chat Agent (对话+Agents状态+Backend/IM配置)
  ├─ Repositories (Hosted列表 + 可加入表格)
  ├─ DevOps Skills (按Agent分组的技能卡)
  ├─ General Settings (凭据/通知/Skills tabs)
  ├─ GitCode Repo (Bot Token + Webhook Bot)
  └─ Backend Agents (引擎健康检测)
```

### 3.3 路由机制

```
function nav(page, param, extra):
  1. 隐藏所有 .vw
  2. 执行 renderXxx(param)
  3. 显示目标 .vw
  4. 更新面包屑标题
  5. 高亮侧边栏匹配链接
  6. 特殊: 'rsel' 隐藏 Sidebar+Topbar (全屏独立)

数据流:
  renderDash(repo) → 静态假数据 (原型)
  → 生产: GET /api/dashboard/{owner}/{repo} → {metrics, agents, activity}
```

### 3.4 交互链路

```
Dashboard Activity 行 ──点击──→ Event Detail (agent, idx)
Pipeline 节点         ──点击──→ Event Detail (agent, 0)  
Pipeline 历史行       ──点击──→ 切换上方 Active 流水线状态
Agent Detail 事件     ──点击──→ Event Detail (agent, idx)
Sidebar Agents       ──点击──→ Agent Detail
Repo 卡片            ──点击──→ Dashboard (切换仓库)
```

---

## 四、后端 API（FastAPI :3000）

### 4.1 统一规范

```
Headers:    Authorization: Bearer <token>
Content:    application/json

响应格式:   {"code": 200, "data": {...}, "message": "ok"}
错误格式:   {"code": 401, "data": null, "message": "会话过期"}

分页参数:   ?page=1&per_page=20
搜索参数:   ?search=keyword
```

### 4.2 认证 `/api/auth/`

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/login` | 密码登录 → `{token, user}` |
| GET | `/login/gitcode` | OAuth 跳转 URL |
| GET | `/callback/gitcode` | OAuth 回调 → `{token, user}` |
| GET | `/profile` | 当前用户信息 + 托管仓库列表 |

### 4.3 仓库 `/api/repos/`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/member` | GitCode 成员仓库 (含角色/状态) |
| GET | `/managed` | 已托管仓库列表 |
| POST | `/join` | 加入仓库 `{full_name}` |
| POST | `/host` | 托管仓库 (创建 webhook) |
| POST | `/remove` | 移除仓库 |

### 4.4 Dashboard `/api/dashboard/`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/{owner}/{repo}` | 全量数据 `{repo, metrics, agents, activity}` |

### 4.5 Agent `/api/agents/`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/{name}/{owner}/{repo}` | Agent 详情 (指标/事件/Skill) |
| PUT | `/{name}/{owner}/{repo}` | 配置 Agent (Engine + Skill) |

### 4.6 Chat `/api/chat/`

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/send` | 发送消息 → Agent 回复 |
| GET | `/config` | Chat Agent 配置 |
| PUT | `/config` | 更新 Backend/IM 配置 |

### 4.7 Settings `/api/settings/`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET/PUT | `/credentials` | 凭据状态/更新 |
| GET/PUT | `/notifications` | 通知偏好 |
| GET/PUT | `/gitcode-repo/{owner}/{repo}` | 仓库级 GitCode 配置 |
| GET/PUT | `/backend-agents` | 引擎健康状态/配置 |

### 4.8 依赖注入 (`deps.py`)

```python
def get_current_user(authorization: str = Header(None)) -> dict:
    """Authorization: Bearer <token> → {id, username, token}"""
    token = authorization.replace("Bearer ", "").strip()
    uid = user_db.validate_token(token)
    if not uid: raise 401
    user = user_db.get_user(uid)
    return {"id": uid, "username": user["username"], "token": token}
```

---

## 五、Webhook 处理 (`webhook_handler.py`)

**端口 8080，HTTP 请求处理器。**

| 方法 | 路径 | 认证 | 说明 |
|------|------|------|------|
| POST | `/webhook/gitcode` | `X-GitCode-Token` | 接收 GitCode 事件 |
| POST | `/api/a2a` | 无 (localhost) | Agent-to-Agent 调用 |
| GET | `/health` | 无 | 健康检查 |
| GET | `/api/sessions` | 无 | 列出所有 Session |

**Webhook 处理流程：**
```
1. 验证 X-GitCode-Token → 403 或继续
2. 解析账户 → resolve_account_by_webhook_token()
3. 立即返回 200 (异步处理)
4. _submit_event(process_event) → 有界线程池
```

**A2A 处理流程：**
```
1. 解析 JSON → {agent, payload}
2. 验证 agent 在 agent-description.json 注册
3. 检查 A2A 深度 (max 5)
4. process_a2a_event() → 同步执行
5. 返回 {"status":"ok"}
```

---

## 六、事件处理管道 (`event_processor.py`)

### 6.1 并发模型

```
BoundedSemaphore(20) → ThreadPoolExecutor(10) → Per-Repo Lock → process_event()
```

### 6.2 主流程

```
process_event(raw_body, headers, account):
  ├── 1. get_event_header() → 事件类型 + 来源
  ├── 2. normalize_event() → {source, type, repository, actor, payload}
  ├── 3. 反循环过滤: actor ∈ BOT_USERNAMES ∧ comment_type → skip
  ├── 4. IM通知分支: clone为 im_notify → 异步提交
  ├── 5. acquire per-repo lock(timeout=180s)
  └── 6. _process_event_inner()
```

### 6.3 核心处理

```
_process_event_inner(evt, repo_key, account):
  ├── 1. get_or_create_session() → history + agent_session_id
  ├── 2. A2A? → _override_agent, skip get_domain_skill()
  ├── 3. get_domain_skill() → skill name (3-tier routing)
  │       ├─ user repo_skill_mapping (优先级最高)
  │       ├─ event-routing.json repo_routing
  │       ├─ event-routing.json routing
  │       └─ fallback_skill (空=skip)
  ├── 4. get_agent_backend() → "opencode" | "codearts"
  ├── 5. _get_feishu_context() → 协作者、群成员、tenant_token
  ├── 6. build_prompt() → 完整 Prompt
  ├── 7. 注入环境变量 → REPO, ACCOUNT, ISSUE_NUMBER, HEAD_SHA, IS_A2A
  ├── 8. stream_agent() → subprocess NDJSON 流
  │       └─ yield: step_start, text, reasoning, tool_use, error, done
  └── 9. 记录 history → dump_to_file → update_session()
```

---

## 七、Prompt 构建系统 (`prompt_builder.py`)

### 7.1 Prompt 组成（事件处理 `build_prompt`）

```
1. A2A 上下文 (可选)          → "## 前序 Agent 处理摘要"
2. 事件描述                   → 类型、Issue/PR号、标题、正文、@mention检测
3. 历史概要                   → "该仓库 N 个历史事件"
4. 历史文件指南               → workspace 目录中的 history JSON 文件
5. Skill 指令                 → skills/{skill}/SKILL.md 全文
6. 飞书上下文 (im-notify)     → 协作者、群成员、Card API、名称映射
7. 角色定义                   → DevOps 助手、仅中文、代码权限限制
8. A2A 协议                   → Agent 注册表 + curl 模板 + context 规则
```

### 7.2 Prompt 组成（IM 对话 `build_chat_prompt`）

```
1. 用户身份解析 → Feishu名、GitCode Login、OpenID
2. 用户消息
3. Skill 能力摘要 → get_skill_capabilities()
4. 历史文件指南
5. Chat Skill 指令
6. 角色定义 → 对话助手规则
7. A2A 协议
```

### 7.3 配置注入

| 配置项 | 注入方式 |
|--------|---------|
| GitCode Token | 环境变量 `GITCODE_API_TOKEN` |
| CodeArts AK/SK | 环境变量 `CODEARTS_CLI_AK/SK` |
| 飞书 Token | Prompt 内联 `tenant_access_token` |
| 事件编号 | 环境变量 `ISSUE_NUMBER`、`PR_NUMBER` |
| A2A 标记 | 环境变量 `IS_A2A` |

---

## 八、Agent 执行层 (`src/agent/`)

### 8.1 Backend 抽象 (`base.py`)

```python
class AgentBackend:
    name: str              # "opencode" | "codearts"
    binary: str            # CLI 可执行文件
    prompt_via_stdin: bool # prompt 通过管道还是参数？
    build_args(prompt, session_id) → list[str]
    parse_output(line) → dict | None  # NDJSON → 统一事件
    get_env(account, user_id) → dict  # 环境变量
```

### 8.2 统一 NDJSON 事件

```json
{"type":"step_start","sessionID":"..."}
{"type":"text","text":"...","tokens":{"input":123,"output":45}}
{"type":"reasoning","text":"..."}
{"type":"tool_use","tool":"bash","input":{"command":"..."}}
{"type":"step_finish"}
{"type":"error","message":"..."}
```

### 8.3 OpenCode Backend

```
binary: "opencode"
prompt: CLI 参数
args:   ["run","--format","json","--session",session_id,prompt]
env:    DEVOPS_SKILLS_DIR, DEVOPS_EVENT_TYPE, GITCODE_API_TOKEN
```

### 8.4 CodeArts Backend

```
binary: "codearts"
prompt: stdin 管道
args:   ["run","--format","json","--session",session_id]
env:    GITCODE_API_TOKEN, CODEARTS_CLI_AK, CODEARTS_CLI_SK
```

### 8.5 子进程执行器 (`executor.py`)

```
stream_agent(prompt, cwd, session_id, backend, account, user_id, env):
  ├── 1. 查找 Backend
  ├── 2. 构建环境变量 → backend.get_env() + user_credentials + env_extra
  ├── 3. 构建 CLI 参数 → backend.build_args()
  ├── 4. subprocess.Popen → 逐行读 stdout
  ├── 5. backend.parse_output(line) → yield 事件
  ├── 6. 等待退出或超时 120s kill
  └── 7. yield done → {exit_code, text, oc_session_id, errors, stderr}
```

---

## 九、VCS 适配器 (`src/vcs/`)

### 9.1 统一事件格式

```json
{
  "source": "gitcode",
  "type": "issue_opened",
  "repository": "owner/repo",
  "branch": "main",
  "actor": "username",
  "payload": { /* 原始数据 */ }
}
```

### 9.2 GitCode 归一化映射

| GitCode Webhook | Action | 统一事件 |
|----------------|--------|---------|
| Issue Hook | open | `issue_opened` |
| Issue Hook | update | `issue_updated` |
| Note Hook (Issue) | — | `issue_comment` |
| Note Hook (MR) | — | `note_hook_pr` |
| Merge Request Hook | open | `pull_request_opened` |
| Merge Request Hook | merge | `pull_request_merged` |
| Push Hook | — | `push` |

### 9.3 扩展性

新增平台 (GitHub/GitLab/Gitee) 只需实现 `VcsBackend` 子类并注册到 `_BACKENDS`，下游零改动。

---

## 十、飞书 IM 集成 (`src/im/`)

### 10.1 WebSocket 连接

```
lark-oapi ws.Client → im.message.receive_v1 事件
  ├─ 多账户: _ws_clients[account_name]
  ├─ 线程池: 5 workers + 5 semaphore
  └─ 回调: handler.process_chat()
```

### 10.2 Chat 处理流程

```
process_chat(parsed):
  ├─ 1. 能力查询 → 直接回复能力列表
  ├─ 2. Slash 命令 → /bind /unbind /status /autodev /notify /clean /help
  ├─ 3. resolve_repo() → repo_key (4级: 显式>正则>DB>空)
  ├─ 4. acquire repo lock
  └─ 5. _process_chat_inner()
       ├─ 解析身份 (Feishu↔GitCode 映射)
       ├─ build_chat_prompt()
       ├─ add_reaction("OK")
       ├─ stream_agent()
       └─ reply_card()
```

### 10.3 Slash 命令

| 命令 | 功能 |
|------|------|
| `/bind owner/repo` | 绑定群聊到仓库 |
| `/unbind` | 解绑 |
| `/status` | 显示当前绑定和开关 |
| `/autodev on/off` | 自动实现开关 |
| `/notify on/off` | 通知开关 |
| `/clean` | 清理 Agent Session |
| `/help` | 帮助 |

---

## 十一、事件与 Agent 会话管理 (`session_manager.py`)

### 11.1 核心概念

旧模型中，"Session" 表示"一个仓库一个会话"。但实际使用中，**同一个仓库下的不同 Agent（VOD、Coding、SRE）即使都使用 opencode，也应该拥有各自独立的 Agent 上下文**，而事件历史是它们的**共享资源**。

因此从「会话管理」更名为「**事件管理与 Agent 会话绑定**」：

| 概念 | 范围 | 存储 |
|------|------|------|
| **Event History** | 按仓库共享 | 所有 Agent 可见，按 issue 关联 |
| **Agent Session** | 按 (repo_key, agent_type) 独立 | 每种 Agent 独享自己的 Backend 上下文 |

### 11.2 为什么不能共享 Session

```
仓库: owner/repo
  ├─ VOD Agent (opencode)   → Session: sess_vod_001
  │   上下文: 正在分析 47 个 signals，已创建 Issue #345
  │
  ├─ Coding Agent (codearts) → Session: sess_code_001
  │   上下文: 正在实现 Issue #342，已提交 PR #1278
  │
  └─ SRE Agent (opencode)    → Session: sess_sre_001
      上下文: 正在监控 v2.3.1 部署，Incident INC-023 已恢复
```

如果三者共享同一个 opencode session：VOD 的分析上下文会污染 Coding 的代码生成上下文，SRE 的监控信息会干扰 Issue 管理。**每个 Agent 需要的上下文完全不同，必须隔离。**

但它们共享**同一份事件历史**：当 Coding Agent 需要了解"Issue #342 是怎么产生的"，它可以查阅 VOD Agent 创建 Issue 的那条事件记录。

### 11.3 数据模型

```
┌─ event_history (仓库级共享) ──────────────────────────────┐
│                                                            │
│  SELECT * FROM events WHERE repo_key = 'owner/repo'        │
│  ORDER BY seq DESC LIMIT 100                                │
│                                                            │
│  ┌────┬──────────┬──────────┬────────────────┬──────────┐ │
│  │seq │ agent    │ event    │ response       │ issue    │ │
│  ├────┼──────────┼──────────┼────────────────┼──────────┤ │
│  │ 47 │ vod      │ Signal 47│ → created #345 │ #345     │ │
│  │ 46 │ issue    │ #345 bug │ → assigned     │ #345     │ │
│  │ 45 │ coding   │ PR #1278 │ → QA triggered │ #342     │ │
│  │ 44 │ sre      │ Deploy   │ → monitoring   │ #338     │ │
│  └────┴──────────┴──────────┴────────────────┴──────────┘ │
│                                                            │
│  Prompt 注入: "该仓库有 47 条事件，最近: VOD→#345,          │
│              Coding→#342, SRE→#338"                        │
└────────────────────────────────────────────────────────────┘

┌─ agent_sessions (按 Agent 类型独立) ────────────────────────┐
│                                                             │
│  INSERT/UPDATE agent_sessions                               │
│    SET backend_session_id = ?, backend = ?, status = ?       │
│    WHERE repo_key = ? AND agent_type = ?                     │
│                                                             │
│  ┌──────────┬──────────┬──────────────┬───────────────────┐ │
│  │repo_key  │agent_type│backend       │backend_session_id │ │
│  ├──────────┼──────────┼──────────────┼───────────────────┤ │
│  │owner/rp  │ vod      │ opencode     │ sess_vod_abc      │ │
│  │owner/rp  │ issue    │ opencode     │ sess_issue_abc    │ │
│  │owner/rp  │ coding   │ codearts     │ sess_code_xyz     │ │
│  │owner/rp  │ qa       │ opencode     │ sess_qa_abc       │ │
│  │owner/rp  │ sre      │ opencode     │ sess_sre_abc      │ │
│  └──────────┴──────────┴──────────────┴───────────────────┘ │
│                                                             │
│  每个 (repo_key, agent_type) 一行                             │
│  VOD 的 opencode session ≠ SRE 的 opencode session           │
└─────────────────────────────────────────────────────────────┘
```

### 11.4 核心操作

```python
def get_or_create_agent_session(repo_key: str, agent_type: str,
                                 backend: str, account: str) -> dict:
    """获取 Agent Session。不存在则创建，返回 backend_session_id"""
    row = SELECT * FROM agent_sessions
          WHERE repo_key = ? AND agent_type = ?
    if row:
        # 该 Agent 已存在会话 → 返回现有 session_id
        return {"agent_session_id": row.backend_session_id, "status": row.status}
    else:
        # 首次执行该 Agent → 创建空会话
        INSERT INTO agent_sessions (repo_key, agent_type, backend, account, status)
        VALUES (?, ?, ?, ?, 'idle')
        return {"agent_session_id": None, "status": "idle"}


def update_agent_session(repo_key: str, agent_type: str, backend: str,
                          backend_session_id: str, status: str):
    """Agent 执行完毕后，更新 Agent 的 backend session_id"""
    UPDATE agent_sessions
    SET backend_session_id = ?, backend = ?, status = ?,
        updated_at = datetime('now')
    WHERE repo_key = ? AND agent_type = ?


def append_event_history(repo_key: str, event: dict):
    """追加事件到共享历史（所有 Agent 可见）"""
    INSERT INTO events (repo_key, agent_type, issue_id,
                        event_type, event, response,
                        duration_sec, exit_code)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    # 保持总容量 ≤ 100，超出自动裁剪


def get_event_history(repo_key: str, limit: int = 100) -> list[dict]:
    """获取仓库共享事件历史"""
    return SELECT * FROM events WHERE repo_key = ?
           ORDER BY seq DESC LIMIT ?
```

### 11.5 事件与 Issue 关联

每条事件记录绑定到具体的 Issue ID，Prompt 构建时据此生成结构化上下文：

```json
{
  "seq": 47,
  "repo_key": "owner/repo",
  "agent_type": "vod",
  "issue_id": 345,
  "event_type": "issue_opened",
  "event": "Signal #47: CI pipeline too slow",
  "response": "Created Issue #345: Optimize CI pipeline",
  "duration_sec": 12.5,
  "exit_code": 0,
  "created_at": "2026-07-26T12:05:00"
}
```

Prompt 构建时按 Issue 聚合并注入：

```
"该仓库 47 条事件:
  Issue #345: VOD→创建, Issue→分类bug, Coding→PR #1278, QA→审查中
  Issue #342: VOD→跳过, Issue→enhancement, Coding→已完成
  ..."
```

### 11.6 端到端数据流

```
Webhook 事件到达 → _process_event_inner()
  │
  ├─ 1. 路由解析: get_domain_skill() → agent_type = "vod" (或 issue/coding/..)
  ├─ 2. Backend 选择: get_agent_backend() → backend = "opencode"
  │
  ├─ 3. 获取 Agent Session:
  │     get_or_create_agent_session(repo_key, "vod", "opencode")
  │     → {agent_session_id: "sess_vod_abc", status: "idle"}
  │     注意: 返回的是 VOD Agent 专属的 session，与仓库内其他 Agent 隔离
  │
  ├─ 4. 获取共享历史:
  │     get_event_history(repo_key)
  │     → [...]  所有 Agent 的事件记录，按 issue 关联
  │
  ├─ 5. build_prompt(history, agent_session_id)
  │     注入: "该仓库 47 条事件，涉及 Issue #345 #342 #338"
  │
  ├─ 6. stream_agent(prompt, session_id="sess_vod_abc")
  │     启动: opencode run --session sess_vod_abc
  │     Agent 只加载 VOD 的历史上下文，不影响 Coding/SRE
  │
  ├─ 7. append_event_history(repo_key, {
  │       agent_type: "vod", issue_id: 345,
  │       event: "Signal #47...", response: "Created #345",
  │       duration: 12.5, exit_code: 0
  │    })
  │
  └─ 8. update_agent_session(repo_key, "vod", "opencode",
         "sess_vod_abc", "idle")
       → UPDATE agent_sessions SET backend_session_id='sess_vod_abc'
```

### 11.7 每日事件汇总（VOD Agent 消费）

VOD Agent 定时读取共享事件历史，聚类分析共性问题：

```
每天 08:00:
  ├─ get_event_history(repo_key, limit=200)
  ├─ VOD Agent 分析最近事件:
  │   "本周共 47 条事件，其中: 
  │    CI flakiness 相关 12 条，API timeout 相关 8 条，
  │    code quality 相关 5 条"
  └─ 生成 VOD Signal → 创建 Issue → Issue Agent 处理
```

## 十二、技能系统（Skills）

### 12.1 存储结构

```
skills/
├── vod/              # VOD Agent（待开发）
├── issue/            # Issue Agent
│   ├── SKILL.md
│   └── references/   # 辅助文档
├── implementation/   # Coding Agent
├── pr/               # QA Agent
├── changelog-gen/    # SRE Agent
├── cloud-deploy/     # SRE Agent
├── deploy-guard/     # SRE Agent
├── build-debug/      # SRE Agent
├── incident-triage/  # SRE Agent
├── chat/             # Chat Agent
├── im-notify/        # Chat Agent (通知)
└── custom/           # 用户自定义
    └── {user_id}/
        └── {skill_name}/
            ├── SKILL.md
            └── references/
```

### 12.2 路由优先级

```
事件到达
  → 1. repo_skill_mapping 表 (用户自定义, 最高优先级)
  → 2. event-routing.json repo_routing (仓库覆盖)
  → 3. event-routing.json routing (全局规则)
  → 4. fallback (空 = skip)
```

### 12.3 自定义 Skill 校验规则

| 规则 | 说明 |
|------|------|
| **目录结构** | 必须包含 `SKILL.md`，可选 `references/` |
| **YAML FrontMatter** | `name`(必填, kebab-case)、`version`(语义化)、`description`(≤200字) |
| **禁止内容** | 不得包含 .py/.sh/.jar 等可执行文件 |
| **文件大小** | 总计 ≤ 1MB |
| **命名冲突** | 不能与系统预制 Skill 同名 |

---

## 十三、数据库设计

### 13.1 核心表 `user_accounts`

```sql
CREATE TABLE user_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL DEFAULT '',
    gitcode_id TEXT DEFAULT '',
    -- GitCode 凭据
    gitcode_api_token TEXT DEFAULT '',
    gitcode_webhook_token TEXT DEFAULT '',
    gitcode_oauth_client_id TEXT DEFAULT '',
    gitcode_oauth_client_secret TEXT DEFAULT '',
    gitcode_oauth_redirect_uri TEXT DEFAULT '',
    -- 飞书凭据
    feishu_app_id TEXT DEFAULT '',
    feishu_app_secret TEXT DEFAULT '',
    -- 华为云凭据
    huawei_cloud_ak TEXT DEFAULT '',
    huawei_cloud_sk TEXT DEFAULT '',
    -- CodeArts 凭据
    CODEARTS_CLI_AK TEXT DEFAULT '',
    CODEARTS_CLI_SK TEXT DEFAULT '',
    -- 配置
    password TEXT DEFAULT '',
    bot_usernames TEXT DEFAULT '',
    webhook_url TEXT DEFAULT '',
    created_by INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);
```

### 13.2 关联表

| 表 | 用途 | 关键字段 |
|----|------|---------|
| `events` | 仓库级共享事件历史 | `repo_key`, `agent_type`, `issue_id`, `event`, `response`, `seq` |
| `agent_sessions` | Agent 独立会话 | `repo_key`, `agent_type`, `backend`, `backend_session_id`, `status` |
| `managed_repos` | 托管仓库 | `user_id`, `full_name`, `webhook_id`, `is_admin` |
| `user_skills` | 自定义技能索引 | `user_id`, `skill_name` |
| `repo_skill_mapping` | 仓库×事件×技能 | `user_id`, `repo_key`, `event_type`, `skill_name` |
| `user_credentials` | OAuth Token | `user_id`, `provider`, `config_json` |
| `feishu_bindings` | 飞书绑定 | `chat_id`, `repo_key`, `auto_impl_enabled`, `notify_enabled` |
| `feishu_users` | VCS↔IM 映射 | `repo_key`, `gitcode_user`, `feishu_open_id` |
| `user_vms` | 云主机 (待重新启用) | `user_id`, `host`, `ssh_user`, `ssh_password` |

### 13.3 前端状态 (不持久化到 DB)

| 数据 | 存储 | 生命周期 |
|------|------|---------|
| `auth_token` | localStorage | OAuth/密码登录后设置 |
| `current_repo` | 内存 / sessionStorage | 切换仓库时更新 |
| 通知偏好 | API → DB | 持久化 |
| Agent 配置 | API → DB | 持久化 |

---

## 十四、并发模型

| 组件 | 并发控制 | 说明 |
|------|---------|------|
| Webhook 处理 | Semaphore(20) + 10 workers | 有界队列，防止过载 |
| 同仓库事件 | threading.Lock (180s) | 串行，防止 Session 竞态 |
| IM 通知分支 | 异步提交，不获锁 | 避免死锁 |
| Chat 处理 | Semaphore(5) + 5 workers | 防止 IM 过载 |
| A2A 调用 | 同步执行，深度≤5 | 防止无限递归 |
| SQLite | db_lock (threading.Lock) | 全局序列化写入 |

---

## 十五、安全模型

| 措施 | 实现 |
|------|------|
| **Webhook 认证** | `X-GitCode-Token` → resolve_account_by_webhook_token() |
| **用户会话** | SHA256 Token，24h 过期，内存+DB 双重验证 |
| **OAuth CSRF** | 随机 state → 内存 dict pop → 一次性使用 |
| **A2A 隔离** | 仅 localhost:8080 |
| **反循环** | `BOT_USERNAMES` 列表 → 跳过 Bot 自身 comment |
| **凭据隔离** | 每用户独立 Token/AK/SK |
| **Chat Agent 隔离** | 每用户独立 Session |
| **Skill 安全** | 上传禁止可执行文件 + 大小限制 |

---

## 十六、配置体系

### 16.1 路由配置

| 文件 | 格式 | 用途 |
|------|------|------|
| `configs/event-routing.json` | JSON | 事件类型 → Skill 映射 |
| `configs/agent-routing.json` | JSON | Skill → Backend 映射 |
| `configs/agent-description.json` | JSON | Agent 注册表 (A2A 用) |

### 16.2 事件路由示例 (`event-routing.json`)

```json
{
  "routing": {
    "issue_opened": "issue",
    "issue_comment": "issue",
    "pull_request_opened": "pr",
    "pull_request_updated": "pr",
    "pull_request_merged": "cloud-deploy",
    "build_failure": "build-debug",
    "deploy_start": "deploy-guard",
    "release_created": "changelog-gen",
    "im_notify": "im-notify"
  },
  "repo_routing": {
    "owner/skill-repo": {
      "pull_request_opened": "skill-pr"
    }
  },
  "fallback_skill": ""
}
```

---

## 十七、部署架构

### 17.1 服务器

```
OS: Ubuntu 22.04+ / CentOS 7+
Python: 3.11+
依赖: fastapi, uvicorn, paramiko, lark-oapi, pyyaml
Agent: opencode / codearts CLI (单独安装)
```

### 17.2 systemd

```ini
[Unit]
Description=Agentic DevOps
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/devops-bot
ExecStart=/usr/bin/python3 -m src.main
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

### 17.3 端口

| 端口 | 服务 | 协议 | 对外 |
|------|------|------|------|
| 3000 | REST JSON API (FastAPI) | HTTP | 推荐 |
| 80 | SPA 前端 (静态文件或反向代理) | HTTP | 可选 |
| 8080 | Webhook/A2A/Health | HTTP | 需要 |
| WS | 飞书 IM | WebSocket | 需要 |

### 17.4 目录结构

```
/root/devops-bot/
├── src/
│   ├── main.py               # 入口 (启动所有服务)
│   ├── api/                   # FastAPI REST API
│   │   ├── main.py            # FastAPI app
│   │   ├── deps.py            # Token 验证依赖
│   │   └── routes/            # 路由模块
│   ├── config.py              # 配置中心
│   ├── event_processor.py     # 事件处理管道
│   ├── webhook_handler.py     # HTTP Webhook
│   ├── session_manager.py     # 会话管理
│   ├── prompt_builder.py      # Prompt 构建
│   ├── agent/                 # Agent 执行
│   ├── vcs/                   # VCS 适配器
│   ├── im/                    # 飞书集成
│   └── user/db.py             # 用户数据库
├── docs/                      # 设计文档
├── skills/                    # Agent 技能
├── configs/                   # 路由配置
└── data/                      # DB + workspaces
```

---

## 十八、实施路线

### Phase 1：API 基础

- [x] FastAPI 框架搭建 + 路由注册
- [x] 认证路由 (OAuth + 密码)
- [x] 仓库路由 (列表/加入/托管)
- [x] Dashboard/Agent/Chat/Settings 路由
- [x] 统一响应格式

### Phase 2：前端 SPA

- [ ] 原型 HTML → 独立 SPA 模块
- [ ] 假数据 → API fetch()
- [ ] 状态管理 (token, repo)
- [ ] 路由守卫
- [ ] 加载态 + 错误态

### Phase 3：后端补全

- [ ] Dashboard 指标 ↔ 真实统计
- [ ] Pipeline ↔ Session 历史
- [ ] Agent 详情 ↔ 事件日志
- [ ] Chat ↔ opencode CLI
- [ ] Backend Agents ↔ 引擎检测

### Phase 4：体验优化

- [ ] Chat SSE 流式回复
- [ ] Dashboard 自动刷新
- [ ] 移动端响应式
- [ ] 性能优化
