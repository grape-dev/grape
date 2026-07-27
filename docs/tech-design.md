# Agentic DevOps 完整技术设计文档

> 基于 `system-workflow-overview.md`、`prototype.html` 以及现有代码库的完整技术方案。

---

## 一、系统概述

Agentic DevOps 是一个基于事件驱动的多 Agent 协作 DevOps 平台。核心架构为 **Bot-Shell / Agent-Brain** 分离模式：

- **Bot（Shell）**：Python 编写的编排层，负责 Webhook 接收、事件归一化、路由分发、会话管理、环境注入、日志监控
- **Agent（Brain）**：CLI 子进程（opencode / codearts），接收完整文本 Prompt，自主读取 Skill 文件、调用 API、执行操作，输出 NDJSON 事件流

**一句话定位：** 从开发者声音收集到代码发布的 Loop Engineer 全流程自动化。

### 核心设计模式

| 模式 | 说明 |
|------|------|
| **Shell-Brain 分离** | Bot 只管 I/O，Agent 只管决策与执行，接口仅为文本 Prompt → NDJSON 流 |
| **事件归一化管道** | 平台特定 Webhook（GitCode）→ 归一化事件字典 → 下游统一处理 |
| **多级路由** | 用户自定义 Skill → 全局 Repo 覆盖 → 全局规则（三级降级） |
| **会话连续性** | Agent CLI 的 `--session <id>` 跨事件持久化，Bot 管理历史归档 |
| **多账户隔离** | Webhook Token → Account → 凭据注入 → 独立 Session/History |
| **A2A 链式协作** | Agent 通过 HTTP API 调用其他 Agent，深度限制（max 5） |
| **有界并发** | 两个线程池 + 逐仓库锁控制并发，防止竞态 |

---

## 二、系统架构

```
┌──────────────────────────────────────────────────────────────────┐
│                         外部系统                                  │
│  GitCode (Webhook/OAuth/API)    Feishu (WS Chat)    Huawei Cloud │
└──────────────┬───────────────────────┬──────────────────┬────────┘
               │                       │                  │
┌──────────────▼───────────────────────▼──────────────────▼────────┐
│                       HTTP / WS 入口层                           │
│                                                                  │
│  :80   admin/server.py     用户面板 / 管理员面板 / OAuth 回调     │
│  :8080 webhook_handler.py  Webhook / A2A / Health / Sessions     │
│  :3000 v2/api/main.py      REST JSON API (FastAPI)               │
│  WS    im/feishu.py        飞书消息长连接                         │
└──────────────┬───────────────────────────────────────────────────┘
               │
┌──────────────▼───────────────────────────────────────────────────┐
│                      事件处理管道 (Event Pipeline)                │
│                                                                  │
│  webhook_handler.py                                              │
│    → 验证 Token → 解析 Header → 异步提交                          │
│                                                                  │
│  event_processor.py                                              │
│    → 归一化 (VCS Adapter) → 反循环过滤 → IM 通知分支              │
│    → 逐仓库加锁 → 路由解析 → 上下文预取                           │
│    → Prompt 构建 → 环境注入 → Agent 执行 → 结果记录               │
└──────────┬──────────────────┬────────────────────────────────────┘
           │                  │
┌──────────▼────────┐  ┌─────▼─────────────────────────────────────┐
│   Prompt 构建器    │  │         Agent 执行层                       │
│                   │  │                                           │
│ prompt_builder.py │  │ agent/executor.py (子进程管理器)            │
│  · A2A 上下文注入 │  │  · Backend 抽象 (OpenCode/CodeArts)        │
│  · 事件描述+历史  │  │  · CLI 参数构建 + 环境注入                  │
│  · Skill 指令注入 │  │  · NDJSON 流式解析                          │
│  · 角色定义+规则  │  │  · 超时控制 (120s)                          │
│  · Agent 注册表   │  │  · 凭据注入 (user_credentials)              │
└───────────────────┘  └───────────────────────────────────────────┘
           │
┌──────────▼────────────────────────────────────────────────────────┐
│                         数据层                                    │
│                                                                  │
│  data/sessions.db    (SQLite)  skills/  (文件系统)                │
│  · user_accounts     · sessions  · feishu_bindings               │
│  · user_vms          · user_skills  · repo_skill_mapping         │
│  · managed_repos     · user_credentials  · oauth_states           │
│                                                                  │
│  data/workspaces/history/{account}/{repo}/*.json  (会话历史归档)   │
│  configs/event-routing.json  configs/agent-routing.json           │
└──────────────────────────────────────────────────────────────────┘
```

---

## 三、子系统详解

### 3.1 入口与服务编排 (`src/main.py`)

**职责：** 系统启动入口，编排所有服务的生命周期。

**启动流程：**
1. 加载配置（触发 `_load_accounts()`、`_load_routing_config()` 等）
2. 启动 Admin 面板 → daemon 线程，端口 80
3. 创建 Webhook 服务 → 主线程，端口 8080
4. 为每个配置的飞书账户启动 WS 连接 → daemon 线程
5. 若有 WS 连接，启动飞书用户同步 → daemon 线程
6. 主线程阻塞在 `server.serve_forever()`

**v2 入口 (`src/v2/run.py`)：** 同时启动 v2 FastAPI (3000) + 原有服务 (80/8080)。

```
端口规划:
  :3000  v2 REST API (FastAPI)     ← 新增，SPA 前端调用
  :80    Admin + User 面板 (legacy)
  :8080  Webhook / A2A / Health
  WS     飞书 IM 长连接
```

### 3.2 配置系统 (`src/config.py`)

**职责：** 集中式配置中心，所有设置从 SQLite 的 `user_accounts` 表加载。

**模块级变量：**
```
GITCODE_API_TOKEN, GITCODE_WEBHOOK_TOKEN, BOT_USERNAMES,
FEISHU_APP_ID, FEISHU_APP_SECRET, HUAWEICLOUD_SDK_AK/SK,
CODEARTS_CLI_AK/SK, ACCOUNTS (dict), EVENT_ROUTING, AGENT_ROUTING
```

**三级事件路由（get_domain_skill）：**
```
事件到达
  → 1. repo_skill_mapping 表（用户自定义，最高优先级）
    → 2. event-routing.json repo_routing（Repo 级覆盖）
      → 3. event-routing.json routing（全局默认路由）
        → 4. fallback_skill（空字符串 = 跳过事件）
```

**Agent 后端路由（get_agent_backend）：**
```
skill_name → agent-routing.json skill_backends → 默认 opencode
```

**配置文件：**

| 文件 | 用途 | 格式 |
|------|------|------|
| `configs/event-routing.json` | 事件类型 → Skill 映射 | JSON |
| `configs/agent-routing.json` | Skill → Backend 映射 | JSON |
| `configs/agent-description.json` | Agent 注册表（供 A2A 使用） | JSON |

### 3.3 Webhook 处理 (`src/webhook_handler.py`)

**职责：** 端口 8080 的 HTTP 处理器，接收外部事件和内部 A2A 调用。

**路由表：**

| 方法 | 路径 | 认证 | 用途 |
|------|------|------|------|
| POST | `/webhook/gitcode` | `X-GitCode-Token` | 接收 GitCode 事件推送 |
| POST | `/api/a2a` | 无（localhost） | Agent-to-Agent 调用 |
| GET | `/health` | 无 | 健康检查 |
| GET | `/api/sessions` | 无 | 列出所有 Session |
| GET | `/api/user-mapping` | 无 | 查询 VCS→IM 用户映射 |

**Webhook 处理流程：**
```
1. 验证 X-GitCode-Token → 403 或继续
2. 解析 Token → Account
3. 立即返回 200（异步处理）
4. _submit_event(process_event, body, headers, account) → 线程池
```

**A2A 处理流程：**
```
1. 解析 JSON body → {agent, payload}
2. 验证 agent 在 agent-description.json 中注册
3. 检查 A2A 深度（max 5）
4. process_a2a_event(data) → 同步执行
5. 返回 {"status":"ok"}
```

### 3.4 事件处理管道 (`src/event_processor.py`)

**职责：** 核心事件处理逻辑，归一化 → 过滤 → 路由 → Prompt → Agent → 记录。

**线程模型：**
- `ThreadPoolExecutor(max_workers=10)`
- `BoundedSemaphore(20)`（最多排队 20 个任务）
- 每仓库一个 `threading.Lock`（串行化同仓库事件）

**主流程 (process_event)：**
```
1. get_event_header() → 事件类型 + 来源
2. normalize_event() → 归一化字典 {source, type, repository, actor, payload}
3. 反循环过滤：BOT_USERNAMES 中的 actor 的 comment 类事件 → skip
4. IM 通知分支：clone 为 type="im_notify" → 提交到线程池（异步）
5. 逐仓库加锁（timeout 180s）
6. _process_event_inner()
```

**核心处理 (_process_event_inner)：**
```
1. 创建 Workspace 目录
2. get_or_create_session() → 历史 + Agent Session ID
3. A2A 覆盖？ → 直接使用 override_agent
4. get_domain_skill() → Skill 名称
5. get_agent_backend() → Backend 名称
6. _get_feishu_context() → 协作者、群成员、Token
7. build_prompt() → 完整 Prompt 文本
8. 注入环境变量 → REPO_FULL_NAME, ACCOUNT, ISSUE_NUMBER, HEAD_SHA, IS_A2A
9. stream_agent() → 子进程 NDJSON 流
10. 记录历史 → dump_history_to_file → update_session()
```

### 3.5 Prompt 构建系统 (`src/prompt_builder.py`)

**职责：** 构建 Agent 接收的完整文本 Prompt。这是 Bot 和 Agent 之间的唯一接口。

**Prompt 组成（build_prompt 用于事件处理）：**
```
1. A2A 上下文（若有）→ "## 前序 Agent 处理摘要"
2. 事件描述 → 类型、Issue/PR 号、标题、正文
3. 历史概要 → "该仓库已记录 N 个历史事件"
4. 历史文件指南 → workspace 中可读取的 history JSON 文件列表
5. Skill 指令 → skills/{skill}/SKILL.md 的完整内容
6. 飞书上下文（仅 im-notify） → 协作者列表、群成员、通知 API 指令
7. 角色定义 → DevOps 操作助手规则（仅中文、限制定非实现 Skill 写代码）
8. A2A 协议 → Agent 注册表 + curl 调用模板 + context 构建规则
```

**Prompt 组成（build_chat_prompt 用于 IM 对话）：**
```
1. 用户身份解析 → Feishu 名、GitCode Login、OpenID
2. 用户消息
3. 技能能力摘要 → get_skill_capabilities()
4. 历史文件指南
5. Chat Skill 指令
6. 角色定义 → 对话助手规则
7. A2A 协议
```

### 3.6 会话管理 (`src/session_manager.py`)

**职责：** SQLite 持久化 + 文件系统历史归档。

**数据库表：**

| 表 | 主键 | 用途 |
|----|------|------|
| `sessions` | `repo_key` (UNIQUE) | 按仓库存储 Session ID + 事件历史 (JSON) |
| `feishu_bindings` | `chat_id` | 飞书群 ↔ 仓库绑定 + 功能开关 |
| `feishu_users` | `repo_key` + `gitcode_user` | VCS 用户 ↔ 飞书用户映射缓存 |

**Session 字段：**
```
id, repo_key, source, repository, status, event_history (JSON),
opencode_session_id, codearts_session_id, backend, account,
created_at, last_active_at
```

**核心操作：**
- `get_or_create_session()` → 返回 session + history + agent_session_id。失败/错误状态的 session 自动重置为 idle
- `update_session()` → 更新 history（cap 100）+ backend 特定 session_id
- `dump_history_to_file()` → 工作区文件归档（`data/workspaces/history/{account}/{repo}/`）
- `get_history_file_list()` → 列出可读的历史文件路径注入 Prompt

**设计意图：**
Agent CLI 的 `--session <id>` 参数使 Agent 能跨事件保持上下文。Bot 将 Agent Session ID 持久化在 SQLite 中，每次事件复用同一 Session，Agent 自动引用历史对话。当执行失败时自动重置为 idle。

### 3.7 Agent 执行层 (`src/agent/`)

**Backend 抽象 (`base.py`)：**
```
class AgentBackend:
    name: str            # "opencode" | "codearts"
    binary: str          # CLI 可执行文件名
    prompt_via_stdin: bool
    build_args(prompt, session_id) → list[str]
    parse_output(line) → dict | None  # NDJSON 行 → 统一事件字典
    get_env(account, user_id) → dict  # 环境变量
```

**NDJSON 统一事件格式：**
```json
{"type": "step_start", "sessionID": "..."}
{"type": "text", "text": "...", "tokens": {"input": 123, "output": 45}}
{"type": "reasoning", "text": "..."}
{"type": "tool_use", "tool": "bash", "input": {"command": "..."}}
{"type": "step_finish"}
{"type": "error", "message": "..."}
```

**OpenCode Backend：**
- binary = `opencode`
- prompt 通过 CLI 位置参数传入（不通过 stdin）
- `build_args`: `["run", "--format", "json", "--session", session_id, prompt]`

**CodeArts Backend：**
- binary = `codearts`
- prompt 通过 stdin 管道传入
- `build_args`: `["run", "--format", "json", "--session", session_id]`
- 注入 `CODEARTS_CLI_AK/SK` 环境变量

**子进程执行器 (`executor.py`)：**
```
stream_agent(prompt, cwd, session_id, backend_name, account, user_id, env_extra):
  1. 查找 Backend
  2. 构建环境变量（backend.get_env + user_credentials + env_extra）
  3. 构建 CLI 参数
  4. 启动子进程（subprocess.Popen）
  5. 逐行读取 stdout → backend.parse_output(line) → yield 事件
  6. 收集完整输出（text, errors, stderr）
  7. 进程退出 → yield done 事件 {exit_code, text, oc_session_id, errors, stderr}
  8. 超时 120s 强制 kill
```

### 3.8 VCS 适配器 (`src/vcs/`)

**职责：** 将 GitCode 的 Webhook 格式归一化为统一的内部事件字典。

**统一事件格式：**
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

**归一化映射（GitCode → 统一类型）：**
```
"Issue Hook" + action=open       → "issue_opened"
"Issue Hook" + action=update     → "issue_updated"
"Note Hook" + noteable_type=Issue → "issue_comment"
"Note Hook" + noteable_type=MergeRequest → "note_hook_pr"
"Merge Request Hook" + action=open → "pull_request_opened"
"Merge Request Hook" + action=merge → "pull_request_merged"
"Push Hook"                       → "push"
```

**设计意图：** 新增 Git 平台时只需实现 `VcsBackend` 子类并注册到 `_BACKENDS`，下游所有代码无需修改。

### 3.9 飞书 IM 集成 (`src/im/`)

**WebSocket 连接 (`feishu.py`)：**
- 使用 `lark-oapi` SDK 的 `ws.Client`
- 长期持有长连接，自动重连
- 多账户支持：`_ws_clients[account_name]`
- 消息处理线程池：5 workers + 5 semaphore

**消息处理流程：**
```
1. WS 收到 im.message.receive_v1 事件
2. 提取消息文本（支持 text 和 post 类型）
3. 去除 @mention
4. 提交到线程池
5. handler.process_chat(parsed)
```

**Chat 处理 (`handler.py`)：**
```
1. 能力查询 → 直接回复能力列表
2. Slash 命令检测 → /bind, /unbind, /status, /autodev, /notify, /clean, /help
3. 仓库解析 → 4 级：显式绑定 > owner/repo 正则 > DB 绑定 > 空
4. 加锁 → _process_chat_inner()
   ├── 解析发送者身份（Feishu↔GitCode 映射）
   ├── build_chat_prompt()
   ├── 添加 "OK" reaction
   ├── stream_agent() → 生成回复
   └── reply_card() 回复消息
```

### 3.10 用户与 Admin 管理 (`src/admin/server.py` + `src/user/db.py`)

**Admin 面板 (端口 80) — 关键路由：**

| 方法 | 路径 | 认证 | 说明 |
|------|------|------|------|
| GET | `/admin/` | Token | 管理员面板（账户管理） |
| POST | `/admin/login` | 无 | 管理员登录 |
| POST | `/admin/save` | Token | 保存账户配置 |
| GET | `/admin/delete` | Token | 删除账户 |
| POST | `/user/cred` | Token | 用户保存凭据 |
| GET | `/auth/gitcode/login` | 无 | OAuth 登录入口 |
| GET | `/auth/gitcode/callback` | 无 | OAuth 回调处理 |

**用户数据库 (`user/db.py`) — 核心表：**

| 表 | 说明 |
|----|------|
| `user_accounts` | 合并后的统一用户表（登录 + 凭据 + 权限） |
| `user_credentials` | OAuth 凭据存储 |
| `user_vms` / `user_vm_repos` | 云主机 + 部署映射 |
| `user_skills` / `repo_skill_mapping` | 自定义技能 + 事件绑定 |
| `managed_repos` | 托管的仓库（webhook） |
| `oauth_states` | OAuth CSRF 令牌 |

### 3.11 v2 API 层 (`src/v2/api/`)

**职责：** 端口 3000 的 FastAPI REST JSON API，为 SPA 前端提供数据。

**技术选型：** FastAPI + Uvicorn（异步支持、自动 OpenAPI 文档、类型校验）

**路由模块：**

| 模块 | Prefix | 核心端点 |
|------|--------|---------|
| `auth.py` | `/api/v2/auth` | OAuth 登录、密码登录、回调、用户信息 |
| `repos.py` | `/api/v2/repos` | 成员仓库、托管列表、Join/Host |
| `dashboard.py` | `/api/v2/dashboard` | 仓库看板全量数据 |
| `agents.py` | `/api/v2/agents` | Agent 详情（指标、事件、Skill） |
| `chat.py` | `/api/v2/chat` | 对话、Backend/IM 配置 |
| `settings.py` | `/api/v2/settings` | 凭据、通知、GitCode Repo 配置 |

**认证机制：** `Authorization: Bearer <token>` header → `deps.py:get_current_user()`

**统一响应格式：** `{"code": 200, "data": {...}, "message": "ok"}`

### 3.12 前端 SPA (`docs/ui-demo/prototype.html`)

**架构：** 纯 HTML/CSS/JS 单页应用，无框架依赖。

**页面树：**
```
Login Overlay
  ├─ GitCode OAuth (主按钮)
  ├─ 密码登录 (toggle 展开)
  └─ 管理员登录 (跳转 /admin/)

App (sidebar + topbar + content)
  ├─ Repo Selection (全屏，无 sidebar)
  ├─ Dashboard (按仓库)
  ├─ Pipeline (活跃 + 历史)
  ├─ Agent Detail × 5 (VOD/Issue/Coding/QA/SRE)
  ├─ Event Detail (上下文 + 日志 + 反馈)
  ├─ Chat Agent (对话 + Config)
  ├─ Repositories (Hosted + 可加入)
  ├─ General Settings (凭据/通知/Skill)
  ├─ GitCode Repo (Bot Token + Webhook)
  ├─ DevOps Skills (按 Agent 分类)
  └─ Backend Agents (引擎健康检测)
```

**路由机制：** `nav(page, param, extra)` → 隐藏/显示视图 → 渲染数据 → 高亮侧边栏

**路由跳转映射：**
```
Agent Detail ← Sidebar DevOps Agents 点击
Event Detail ← Agent Detail 事件行点击 / Dashboard Activity 点击 / Pipeline 节点点击
Chat         ← Sidebar Account 点击
Settings     ← Sidebar Settings 点击
Repos        ← "+ 新增仓库托管" 点击
```

**生产演进：** 原型 HTML → 提取为独立 SPA → 替换假数据为 API fetch() → 状态管理 → 错误处理

---

## 四、数据流

### 4.1 完整 Webhook 事件流

```
GitCode Webhook (POST /webhook/gitcode)
  │
  ▼
webhook_handler.py: Handler.do_POST()
  ├── 验证 X-GitCode-Token → 403 或继续
  ├── resolve_account_by_webhook_token(token) → account
  ├── 立即返回 200（异步处理）
  └── _submit_event(process_event, body, headers, account)
       │
       ▼
event_processor.py: process_event()
  ├── get_event_header() → event_type, source
  ├── normalize_event() → {source, type, repository, actor, payload}
  ├── 反循环过滤：skip if actor in BOT_USERNAMES and comment-type
  ├── IM 通知分支：clone as im_notify → submit parallel task
  ├── Acquire per-repo lock
  │
  ▼
_process_event_inner()
  ├── get_or_create_session() → history + agent_session_id
  ├── get_domain_skill() → skill name (3-tier routing)
  ├── get_agent_backend() → backend (opencode/codearts)
  ├── build_prompt() → full text prompt
  ├── Set env: REPO_FULL_NAME, ACCOUNT, ISSUE_NUMBER, HEAD_SHA, IS_A2A
  ├── stream_agent() → subprocess NDJSON streaming
  │   └── yield events: step_start, text, reasoning, tool_use, error, done
  └── Record history → dump_to_file → update_session()
```

### 4.2 飞书 Chat 交互流

```
飞书用户发送消息 → Feishu WS 事件
  │
  ▼
im/feishu.py: _handle_message()
  ├── 提取消息文本（支持 text + post 类型）
  ├── 去除 @mention
  └── 提交到线程池 → handler.process_chat()
       │
       ▼
im/handler.py: process_chat()
  ├── 能力查询 → 直接回复
  ├── Slash 命令 → /bind /unbind /status /autodev /notify /clean /help
  ├── resolve_repo() → repo_key
  └── _process_chat_inner()
       ├── 解析发送者身份
       ├── build_chat_prompt()
       ├── add_reaction("OK")
       ├── stream_agent()
       └── reply_card() 回复
```

### 4.3 A2A 链式协作流

```
Agent A 执行完毕 → curl POST /api/a2a {agent: "implementation", payload: {...}}
  │
  ▼
webhook_handler.py: do_POST()
  ├── 验证 agent 在 agent-description.json 中注册
  ├── 检查 A2A 深度 (max 5)
  └── process_a2a_event()
       └── _process_event_inner() with _override_agent + _a2a_context
```

### 4.4 前端登录流

```
用户打开 SPA → 显示登录页
  │
  ├─ GitCode OAuth:
  │   1. GET /api/v2/auth/login/gitcode → {redirect_url}
  │   2. 浏览器跳转 GitCode 授权页
  │   3. 用户授权 → 302 回到 /api/v2/auth/callback/gitcode
  │   4. 服务端：交换 code → access_token → 创建/绑定用户 → 返回 {token, user}
  │   5. SPA 存储 token 到 localStorage
  │   6. 调用 /api/v2/auth/profile → {managed_repos}
  │   7. 有仓库 → 直接进入 Dashboard
  │   8. 无仓库 → 展示仓库选择页
  │
  └─ 密码登录:
      1. POST /api/v2/auth/login {username, password}
      2. 返回 {token} → 同 OAuth 后续流程
```

---

## 五、数据库设计

### 5.1 核心表

```sql
-- 统一用户账户表（合并 users + bot_accounts）
CREATE TABLE user_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL DEFAULT '',
    gitcode_id TEXT DEFAULT '',
    -- 凭据字段
    gitcode_api_token TEXT DEFAULT '',
    gitcode_webhook_token TEXT DEFAULT '',
    gitcode_oauth_client_id TEXT DEFAULT '',
    gitcode_oauth_client_secret TEXT DEFAULT '',
    gitcode_oauth_redirect_uri TEXT DEFAULT '',
    feishu_app_id TEXT DEFAULT '',
    feishu_app_secret TEXT DEFAULT '',
    huawei_cloud_ak TEXT DEFAULT '',
    huawei_cloud_sk TEXT DEFAULT '',
    CODEARTS_CLI_AK TEXT DEFAULT '',
    CODEARTS_CLI_SK TEXT DEFAULT '',
    -- 配置字段
    password TEXT DEFAULT '',          -- 明文密码（admin 管理用）
    bot_usernames TEXT DEFAULT '',     -- JSON 数组，Bot 用户名列表
    webhook_url TEXT DEFAULT '',
    created_by INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);
```

### 5.2 关联表

| 表 | 用途 | 外键 |
|----|------|------|
| `managed_repos` | 托管仓库（webhook 状态） | `user_id` → `user_accounts.id` |
| `user_vms` | 云主机 SSH 配置 | `user_id` → `user_accounts.id` |
| `user_vm_repos` | VM ↔ Repo 部署映射 | `vm_id` → `user_vms.id` |
| `user_skills` | 用户自定义技能索引 | `user_id` → `user_accounts.id` |
| `repo_skill_mapping` | 仓库 × 事件 × Skill 绑定 | `user_id` → `user_accounts.id` |
| `user_credentials` | OAuth 凭据（per provider） | `user_id` → `user_accounts.id` |
| `oauth_states` | OAuth CSRF 防伪令牌 | —（内存存储，不依赖 SQLite） |
| `sessions` | 按仓库的 Agent Session | `repo_key` (UNIQUE) |
| `feishu_bindings` | 飞书群 ↔ 仓库绑定 | `chat_id` (PK) |
| `feishu_users` | VCS ↔ 飞书用户映射 | `repo_key` + `gitcode_user` |

---

## 六、并发模型

```
                        ┌─────────────┐
                        │   Webhooks  │
                        │   (HTTP)    │
                        └──────┬──────┘
                               │
                    ┌──────────▼──────────┐
                    │  BoundedSemaphore    │
                    │  (max 20 queued)     │
                    └──────────┬──────────┘
                               │
                    ┌──────────▼──────────┐
                    │  ThreadPoolExecutor │
                    │  (max 10 workers)   │
                    └──────────┬──────────┘
                               │
                    ┌──────────▼──────────┐
                    │  Per-Repo Lock      │
                    │  threading.Lock     │
                    │  (串行化同仓库事件)  │
                    └──────────┬──────────┘
                               │
                    ┌──────────▼──────────┐
                    │  process_event()    │
                    │  (event processor)  │
                    └─────────────────────┘
```

| 组件 | 并发控制 | 说明 |
|------|---------|------|
| Webhook 处理 | Semaphore(20) + 10 workers | 防止过载，超出排队丢弃 |
| 同仓库事件 | threading.Lock(timeout=180s) | 串行执行，防止 Session 竞态 |
| IM 通知分支 | 异步提交，不获取 repo lock | 避免 IM 分支死锁 |
| 飞书消息 | Semaphore(5) + 5 workers | 防止 WS 消息过载 |
| A2A 调用 | 同步执行 | 深度限制 5 层 |
| SQLite 操作 | db_lock (threading.Lock) | 全局序列化 DB 写入 |

---

## 七、安全模型

| 措施 | 实现 |
|------|------|
| **Webhook 认证** | `X-GitCode-Token` header → resolve_account_by_webhook_token() |
| **用户认证** | SHA256 Token（24h 过期）→ validate_token() |
| **Admin 认证** | 独立 Token dict（24h 过期）→ _valid_token() |
| **OAuth CSRF** | 随机 state → 内存 dict pop → 一次性使用 |
| **A2A 隔离** | 仅 localhost:8080 → 不对外暴露 |
| **反循环** | BOT_USERNAMES 列表 → 跳过 Bot 自身 comment 事件 |
| **凭据隔离** | 每用户独立 Token/AK/SK → Agent 环境注入 |
| **Chat Agent 隔离** | 每用户独立实例 → 不共享 Agent Session |
| **Skill 安全** | 上传校验（禁止可执行文件/脚本、最大 1MB） |

---

## 八、部署架构

### 8.1 服务器环境

```
OS: Ubuntu 22.04 / CentOS 7+
Python: 3.11+
依赖: fastapi, uvicorn, paramiko, lark-oapi, pyyaml
```

### 8.2 目录结构

```
/root/devops-bot/
├── src/
│   ├── main.py                 # 原有入口
│   ├── v2/
│   │   ├── run.py              # v2 入口（启动所有服务）
│   │   ├── api/main.py         # FastAPI app
│   │   ├── api/deps.py         # Token 验证依赖
│   │   ├── api/routes/         # 6 个路由模块
│   │   └── web/                # 前端 SPA
│   ├── config.py               # 配置中心
│   ├── event_processor.py      # 事件处理管道
│   ├── webhook_handler.py      # HTTP Webhook
│   ├── session_manager.py      # 会话管理
│   ├── prompt_builder.py       # Prompt 构建
│   ├── agent/                  # Agent 执行
│   ├── vcs/                    # VCS 适配器
│   ├── im/                     # 飞书 IM
│   ├── admin/                  # Admin 面板
│   └── user/                   # 用户 DB
├── docs/                       # 设计文档
├── skills/                     # Agent 技能
├── configs/                    # 路由配置
└── data/                       # DB + workspaces
```

### 8.3 systemd 服务

```ini
[Unit]
Description=Agentic DevOps
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/devops-bot
ExecStart=/usr/bin/python3 -m src.v2.run
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

### 8.4 端口规划

| 端口 | 服务 | 协议 | 对外 |
|------|------|------|------|
| 3000 | v2 REST API (FastAPI) | HTTP/JSON | 可选 |
| 80 | Admin/User 面板 (legacy) | HTTP/HTML | 需要 |
| 8080 | Webhook/A2A/Health | HTTP | 需要 |
| WS | 飞书 IM 长连接 | WebSocket | 需要 |

---

## 九、实施路线

### Phase 1：API 基础 ✅
- [x] FastAPI 框架搭建 + 6 组路由
- [x] 认证（OAuth + 密码）
- [x] 仓库/看板/Agent/Chat/Settings API
- [x] 统一响应格式 + 错误处理

### Phase 2：前端 SPA
- [ ] 从原型 HTML 提取为独立模块
- [ ] 替换假数据 → API fetch()
- [ ] 状态管理（token, current_repo）
- [ ] 路由守卫（未登录 → 登录页）
- [ ] 加载态 + 错误态

### Phase 3：后端补全
- [ ] Dashboard 指标对接真实统计
- [ ] Pipeline 数据对接 Session 历史
- [ ] Agent 详情对接真实事件日志
- [ ] Chat 对话对接 opencode CLI
- [ ] Backend Agents 引擎健康检测
- [ ] 通知系统 + 偏好持久化

### Phase 4：体验优化
- [ ] Chat SSE 流式回复
- [ ] WebSocket Dashboard 实时推送
- [ ] 移动端响应式
- [ ] 性能优化（缓存、分页、懒加载）
