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
| POST | `/logout` | 清除 session token |

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

### 4.5 Pipeline `/api/pipeline/`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/{owner}/{repo}` | 流水线数据 `{active, history}`，active 含每个 Agent 阶段的事件详情 |

### 4.6 Events `/api/events/`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/{event_seq}` | 单条事件详情 `{context, execution_log, metadata}` |
| GET | `/{owner}/{repo}` | 仓库事件列表 `{events}` |

### 4.7 Agent `/api/agents/`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/{name}/{owner}/{repo}` | Agent 详情 (指标/事件/Skill) |
| PUT | `/{name}/{owner}/{repo}` | 配置 Agent (Engine + Skill) |

> 所有 DevOps Agents (VOD/Issue/Coding/QA/SRE) 使用 **DevOps Bot Token** 执行 GitCode 操作。当用户未配置自定义 Bot Token 时，自动回退使用系统默认 Bot Token（由管理员在管理员系统中配置）。Chat Agent 使用用户的 **User Token** 代表用户操作。

### 4.8 Chat `/api/chat/`

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/send` | 发送消息 → Agent 回复 |
| GET | `/config` | Chat Agent 配置 |
| PUT | `/config` | 更新 Backend/IM 配置 |
| POST | `/feedback` | 提交事件反馈（→ A2A 推给 VOD Agent） |

> 飞书 App ID/Secret 支持「Scan QR to auto-configure」快捷接入（见 `docs/feishu-agent-auto-config.md`）。

### 4.9 Settings `/api/settings/`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET/PUT | `/credentials` | 凭据状态/更新 |
| GET/PUT | `/notifications` | 通知偏好 |
| GET/PUT | `/gitcode-repo/{owner}/{repo}` | 仓库级 GitCode 配置 |
| GET/PUT | `/backend-agents` | 引擎健康状态/配置 |

### 4.11 Skills `/api/skills/`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | 按 Agent 分类的技能列表 `?agent=vod&search=关键字` |
| POST | `/upload` | 上传自定义 Skill (zip/base64) |
| DELETE | `/{skill_name}` | 删除自定义 Skill |

### 4.12 Search `/api/search/`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | 全局搜索 `?q=keyword&type=event|repo|agent` |

> 注：仓库选择页的「＋ 新增仓库」卡片点击跳转到 `https://gitcode.com/create`（前端行为，无需 API）。  
> 注：Topbar 的「☁ 云开发环境」按钮链接到华为云 CodeArts（前端行为，无需 API）。

### 4.13 Token 三层体系

DevOps Bot 使用三种不同的 GitCode Token，按用途严格隔离：

| Token 类型 | 用途 | 权限范围 | 存储字段 | 配置入口 |
|-----------|------|---------|---------|---------|
| **DevOps Bot Token** | DevOps Agents 执行 GitCode 操作的全局凭证 | `read_repo, write_repo, manage_webhook` | `user_accounts.gitcode_api_token`（Bot 账户） | GitCode Repo 页面 |
| **User Token** | Chat Agent 代表用户操作的个人凭证 | `read_user`（OAuth 自动获取） | `user_accounts.gitcode_api_token`（普通用户） | General Settings → Credentials（OAuth 自动填充）|
| **Webhook Token** | GitCode 推送事件时的验签凭证 | 仅用于验签，无 API 调用能力 | `user_accounts.gitcode_webhook_token` | GitCode Repo 页面 |

**回退机制：** 当管理员未为仓库配置 Bot Token 时，DevOps Agents 自动使用系统默认 Token（管理员在 GitCode Repo 页面的 `default` 条目下预配）。若默认 Token 也未配置，则 Agents 报错提示「请管理员配置 DevOps Bot Token」，不执行 GitCode 操作。

**调用关系：**

```
DevOps Bot Token（系统全局）
  ├── VOD Agent    → 创建 Issue、读取事件、分析信号
  ├── Issue Agent   → 打标签、指派、A2A 触发 Coding
  ├── Coding Agent  → 创建/提交 PR、代码实现
  ├── QA Agent      → PR 评论、代码审查标记
  └── SRE Agent     → 变更日志、部署状态更新
  └── 回退规则: 若 repo 级未配置 → 使用管理员系统配置的默认 Token

User Token（用户个人）
  └── Chat Agent    → 代表用户发起操作：提交 Issue、审批流程、查询状态

Webhook Token
  └── GitCode Webhook 验签（端口 8080）
```

**安全原则：**
- Bot Token 是系统级凭证，管理员配置，**不随用户变化**。所有 DevOps Agents 自动享有仓库的读写权限
- 若未配置自定义 Bot Token，自动回退使用**系统默认 Bot Token**（由管理员在管理员系统中配置），确保零配置即可运行
- User Token 是用户级凭证，OAuth 登录自动获取，**代表用户身份**。Chat Agent 用此 Token 在 GitCode 上以用户名义操作
- Webhook Token 仅用于验证 Webhook 来源，无 API 调用权限

### 4.10 依赖注入 (`deps.py`)

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

**Token 注入规则：**

| Agent 类型 | 使用的 Token | 来源 |
|-----------|-------------|------|
| VOD / Issue / Coding / QA / SRE | **DevOps Bot Token** | `user_accounts.gitcode_api_token`（Bot 账户） |
| Chat Agent | **User Token**（OAuth 个人凭证） | `user_accounts.gitcode_api_token`（当前用户） |

`stream_agent` 根据 `account` 参数决定注入哪个 Token：DevOps Agents 由 `event_processor` 调用时传入系统账户；若系统账户未配置 Token，自动回退使用 `default` 账户的 Token；Chat Agent 由 `handler` 调用时传入用户账户。

### 8.6 Backend 发现与健康检测

**前端 Backend Agents 页面** 需要展示引擎的安装状态和运行状态。后端通过系统级检测实现：

**发现机制：**

```python
def discover_backends() -> list[dict]:
    for be in REGISTERED_BACKENDS:
        for p in be["paths"]:
            found = shutil.which(p)
            if found:
                result["installed"], result["path"] = True, found
                break
```

| 引擎 | 查找方式 | 示例路径 |
|------|---------|---------|
| OpenCode | `which opencode` | `/usr/local/bin/opencode` |
| CodeArts | `which codearts` | `/usr/local/bin/codearts` |
| Hermes | `which hermes` | `/usr/local/bin/hermes` |
| Cursor | `which cursor-agent` | `/usr/local/bin/cursor-agent` |

**健康检测：**

```python
def check_health(binary: str) -> dict:
    # 1. --version 获取版本
    p = subprocess.run([binary, "--version"], ...)
    if p.rc == 0: result["version"] = p.stdout.strip()

    # 2. 最小功能验证
    p = subprocess.run([binary, "run", "--format", "json",
                        "Respond with exactly 'OK'"], timeout=10)
    result["running"] = (p.rc == 0 and "OK" in p.stdout)
```

**CodeArts AK/SK 验证：**

```python
def validate_codearts_auth(ak: str, sk: str) -> bool:
    env = {"CODEARTS_CLI_AK": ak, "CODEARTS_CLI_SK": sk}
    p = subprocess.run(["codearts", "run", "--format", "json",
                        "Respond with exactly 'AUTH_OK'"],
                       env=env, ..., timeout=15)
    return p.rc == 0 and "AUTH_OK" in p.stdout
```

**API 端点：**

```
GET  /api/settings/backend-agents
  → [{name, label, installed, path, version, running, requires_auth}]

POST /api/settings/backend-agents/refresh
  → 强制重新 discover + check_health

PUT  /api/settings/backend-agents/codearts
  → {ak, sk} → 实时验证 AK/SK 有效性
```

**缓存：** 检测结果缓存 5 分钟，手动刷新 (Refresh All / Test Connection) 强制更新。

---

## 十、VCS 适配器 (`src/vcs/`)

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

## 十一、飞书 IM 集成 (`src/im/`)

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

## 十二、事件与 Agent 会话管理 (`session_manager.py`)

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

### 11.8 用户反馈处理流程 (Event Feedback → VOD Agent)

Event Detail 页面提供反馈表单，用户可对任意事件的执行日志提出建议或问题。反馈通过 A2A 协议发给 VOD Agent 分析处理。

**数据流：**

```
┌─ Event Detail 页面 ──────────────────────────────────┐
│  左侧: 事件上下文 + Agent 执行日志                      │
│  右侧: 反馈表单 (Title + Description + Submit)        │
└────────────────────┬────────────────────────────────┘
                     │ POST /api/chat/feedback
                     │ {event_seq: 47, title:"...", description:"..."}
                     ▼
┌─ API Server ─────────────────────────────────────────┐
│  1. 查询 events 表获取事件完整数据                      │
│     {seq, agent_type, issue_id, event, response,      │
│      execution_log, context}                          │
│  2. 构造 A2A 载荷:                                    │
│     {agent: "vod",                                    │
│      payload: {                                       │
│        type: "event_feedback",                        │
│        event_context: {                               │
│          agent: "coding",                             │
│          issue_id: 342,                               │
│          event: "PR #1278 submitted",                 │
│          execution_log: "[12:05:01] Agent started...",│
│        },                                            │
│        feedback: {                                    │
│          title: "PR review 太慢",                     │
│          description: "QA Agent 审查 PR #1278 用了30m",│
│          submitted_by: "zhangwei"                     │
│        }                                             │
│      }}                                              │
│  3. POST /api/a2a → 触发 VOD Agent                   │
└────────────────────┬────────────────────────────────┘
                     │ A2A 事件: agent="vod"
                     ▼
┌─ VOD Agent ──────────────────────────────────────────┐
│  Prompt 注入:                                         │
│    "## 用户反馈事件                                    │
│     原始事件: Coding Agent 提交 PR #1278               │
│     执行日志: [12:05:01] Agent started...              │
│     用户反馈: PR review 太慢 - QA 审查 PR #1278 用了 30m│
│                                                       │
│     请分析这个反馈，判断:                                │
│     1. 是否需要创建 Issue 跟踪？                        │
│     2. 是否属于已知的共性问题？                          │
│     3. 给出处理建议"                                   │
│                                                       │
│  VOD Agent 决策:                                       │
│  ├─ 需要处理 → 创建 Issue #346 "QA 审查时效过长"        │
│  │               → A2A 触发 Issue Agent               │
│  ├─ 已知问题 → 聚类到 "test flakiness" signal          │
│  │               → 更新 signal count                  │
│  └─ 无需处理 → 标记为 resolved，记录到 events          │
└───────────────────────────────────────────────────────┘
```

**关键设计点：**

| 环节 | 实现 |
|------|------|
| **前端提交** | Event Detail 右侧反馈表单 → `POST /api/chat/feedback` |
| **事件关联** | 通过 `event_seq` 从 `events` 表获取完整上下文 + 执行日志 |
| **A2A 路由** | 固定路由到 `vod` Agent（所有反馈归口 VOD 分析） |
| **载荷结构** | `event_context` (原事件数据) + `feedback` (用户输入) |
| **VOD 处理** | 自主决策：创建 Issue / 聚合分析 / 忽略 |
| **结果记录** | `append_event_history(agent_type="vod", event_type="feedback")` |

**与普通 A2A 的区别：**

```
普通 A2A (Issue→Coding):
  Issue Agent 处理完 → 触发 Coding Agent
  context = "Issue #342 需要实现..."

反馈 A2A (Event→VOD):
  用户审阅事件日志 → 提交反馈 → 触发 VOD Agent
  context = "事件 #47 的完整上下文 + 用户反馈内容"
  VOD 需要同时分析: 原始事件是否有问题 + 用户反馈是否合理
```

## 十三、技能系统（Skills）

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

## 十四、数据库设计

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

## 十五、并发模型

| 组件 | 并发控制 | 说明 |
|------|---------|------|
| Webhook 处理 | Semaphore(20) + 10 workers | 有界队列，防止过载 |
| 同仓库事件 | threading.Lock (180s) | 串行，防止 Session 竞态 |
| IM 通知分支 | 异步提交，不获锁 | 避免死锁 |
| Chat 处理 | Semaphore(5) + 5 workers | 防止 IM 过载 |
| A2A 调用 | 同步执行，深度≤5 | 防止无限递归 |
| SQLite | db_lock (threading.Lock) | 全局序列化写入 |

---

## 十六、安全模型

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

## 十七、配置体系

### 17.1 现状与问题

当前路由和 Agent 配置分散在 JSON 文件 + DB 表中，存在管理分裂：

| 数据 | 当前位置 | 问题 |
|------|---------|------|
| 全局事件→Skill | `configs/event-routing.json` | 文件更新需重启服务 |
| Skill→Backend | `configs/agent-routing.json` | 同上，无前端管理界面 |
| Agent 注册表 | `configs/agent-description.json` | A2A 依赖，需手动编辑 |
| 用户自定义映射 | `repo_skill_mapping` 表 | ✅ 已入 DB，前端可管理 |

### 17.2 推荐方案：全部统一入 DB

| JSON 文件 | 新 DB 表 | 用途 |
|----------|---------|------|
| `event-routing.json` → | `event_route_config` | 全局事件类型→技能映射 |
| `agent-routing.json` → | 合并到 `agent_type_config` | 每种 Agent 类型的默认 Backend |
| `agent-description.json` → | `agent_registry` | Agent 注册（名称、描述、触发词、工具） |

**新表 DDL：**

```sql
CREATE TABLE event_route_config (
    event_type TEXT NOT NULL,        -- "issue_opened", "pull_request_opened" 等
    skill_name TEXT NOT NULL,         -- "issue", "pr", "coding" 等
    repo_key TEXT DEFAULT '',         -- 空 = 全局, 非空 = 仓库级覆盖
    priority INTEGER DEFAULT 0,       -- 优先级 (数字越大越高)
    created_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (event_type, repo_key)
);

CREATE TABLE agent_type_config (
    agent_type TEXT PRIMARY KEY,      -- "vod", "issue", "coding", "qa", "sre"
    default_backend TEXT NOT NULL,    -- "opencode" | "codearts" | "hermes"
    description TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE agent_registry (
    name TEXT PRIMARY KEY,            -- "issue", "implementation", "pr"...
    description TEXT NOT NULL,
    triggers TEXT DEFAULT '',         -- JSON 数组: ["issue_opened","issue_comment"]
    tools TEXT DEFAULT '',            -- JSON 数组: 可用工具列表
    created_at TEXT DEFAULT (datetime('now'))
);
```

**迁移策略（bootstrap）：**

```python
def bootstrap_routing_config():
    """首次启动/首次迁移: 将 JSON 种子数据导入 DB"""
    with db_lock:
        cnt = SELECT COUNT(*) FROM event_route_config
    if cnt > 0: return  # 已迁移

    # 加载 event-routing.json → event_route_config
    data = json.load(open("configs/event-routing.json"))
    for event_type, skill in data["routing"].items():
        INSERT INTO event_route_config (event_type, skill_name, priority)
        VALUES (?, ?, 0)

    for repo_key, routes in data.get("repo_routing", {}).items():
        for event_type, skill in routes.items():
            INSERT ... VALUES (event_type, skill, repo_key, 10)

    # 加载 agent-routing.json → agent_type_config
    data = json.load(open("configs/agent-routing.json"))
    default = data["default_backend"]
    for skill, backend in data.get("skill_backends", {}).items():
        INSERT INTO agent_type_config (agent_type, default_backend)
        VALUES (?, ?) ON CONFLICT IGNORE

    # 加载 agent-description.json → agent_registry
    for agent in json.load(open("configs/agent-description.json")).get("agents", []):
        INSERT INTO agent_registry (name, description, triggers, tools)
        VALUES (?, ?, ?, ?)
```

### 17.3 调整后的路由查询

```python
# config.py — 从 DB 查询，替换 JSON 文件读取

def get_domain_skill(evt_type: str, repo_key: str = "", user_id: int = 0) -> str:
    # 1. 用户自定义 (最高优先级) — DB 已支持
    if user_id and repo_key:
        skill = get_skill_for_event(user_id, repo_key, evt_type)
        if skill: return skill

    # 2. 全局仓库覆盖 — 从 event_route_config 查
    row = SELECT skill_name FROM event_route_config
          WHERE event_type = ? AND repo_key = ? AND priority > 0
    if row: return row.skill_name

    # 3. 全局默认
    row = SELECT skill_name FROM event_route_config
          WHERE event_type = ? AND repo_key = ''
    return row.skill_name if row else ""

def get_agent_backend(skill_name: str) -> str:
    row = SELECT default_backend FROM agent_type_config
          WHERE agent_type = ?
    return row.default_backend if row else "opencode"
```

### 17.4 管理 API

```
GET    /api/settings/event-routes        → 所有路由规则(全局+仓库覆盖)
PUT    /api/settings/event-routes        → 批量更新路由 {event_type, skill, repo_key?}

GET    /api/settings/agent-backends      → 所有 Agent 类型的 Backend 绑定
PUT    /api/settings/agent-backends/{type} → 更新单类型绑定

GET    /api/settings/agent-registry      → Agent 注册表
```

### 17.5 JSON 文件的角色变化

```
旧: JSON 文件 = 运行时 Truth (每次读取)
新: JSON 文件 = Bootstrap 种子 (仅首次迁移使用)

启动流程:
  1. 检查 DB 是否有数据 → 有则跳过
  2. DB 无数据 → 读取 JSON → INSERT INTO DB
  3. 运行时: config.get_domain_skill() → SELECT FROM DB

前端修改路由后:
  PUT /api/settings/event-routes → UPDATE DB → 即时生效 (无需重启)
```

---

## 十八、部署架构

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
WorkingDirectory=/opt/grape
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
/opt/grape/
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
├── configs/                   # 种子数据 (首次启动导入 DB，非运行时 truth)
└── data/                      # DB + workspaces
```

---

## 二十、开放兼容设计

### 20.1 Backend Agent 插件模型

当前支持 OpenCode 和 CodeArts，架构设计为可插拔的后端 Agent 引擎模型，新增引擎只需实现 `AgentBackend` 接口：

```python
class AgentBackend:
    name: str              # 引擎标识: "opencode" | "codearts" | "openclaw" | "hermes" | "cursor"
    binary: str            # CLI 可执行文件名
    prompt_via_stdin: bool # prompt 传递方式
    build_args(prompt, session_id) → list[str]  # 参数构建
    parse_output(line) → dict | None            # 输出解析
    get_env(account, user_id) → dict            # 环境变量
```

**新增引擎步骤：**

```
1. 创建 src/agent/hermes.py → class HermesBackend(AgentBackend)
2. 在 _BACKENDS 字典注册: {"hermes": HermesBackend()}
3. 在 discover_backends() 的 REGISTERED_BACKENDS 添加路径
4. 重启服务 → Backend Agents 页面自动检测新引擎
```

**支持的引擎及扩展计划：**

| 引擎 | 状态 | 新增成本 |
|------|------|---------|
| OpenCode | ✅ 已支持 | — |
| CodeArts | ✅ 已支持 | — |
| OpenClaw | 可扩展 | 实现 AgentBackend 子类 (~20 行) |
| Hermes | 可扩展 | 同上 |
| Cursor | 可扩展 | 同上 |
| 其他 CLI Agent | 可扩展 | 同上 |
| HTTP API Agent | 可扩展 | 需新增 HTTPBackend 基类 (~50 行) |

### 20.2 IM 通道插件模型

当前 IM 层只实现了飞书（lark-oapi），架构预留了多通道扩展点：

```python
class IMChannel:
    channel: str          # "feishu" | "wecom" | "dingtalk"
    start_connection()    # 建立长连接
    handle_message(event) # 消息处理
    send_reply(msg, text) # 回复消息
```

**当前 CHAT AGENT 配置页已预留：**

| 通道 | 配置字段 | 状态 |
|------|---------|------|
| Feishu | App ID + App Secret | ✅ 已实现 |
| WeCom (企业微信) | Webhook URL + Token | 待扩展 |
| DingTalk (钉钉) | App Key + App Secret | 待扩展 |
| Custom IM | Webhook URL | 待扩展 |

**扩展步骤（以 WeCom 为例）：**

```
1. 创建 src/im/wecom.py → class WecomChannel(IMChannel)
2. 注册到 IM_CHANNELS: {"wecom": WecomChannel()}
3. 在 user_accounts 表新增 wecom_webhook_url 字段
4. Chat Agent Config 页新增 WeCom 配置表单
```

### 20.3 VCS 平台插件模型

当前 VCS 层通过适配器模式实现 GitCode 支持，通过统一事件格式屏蔽平台差异：

```python
class VcsBackend:
    name: str
    normalize(raw, event_header) → dict  # 归一化
    detect_event_header(headers) → str   # 事件头识别
```

**统一事件格式（平台无关）：**

```json
{"source": "github|gitcode|gitee|gitlab",
 "type": "issue_opened",
 "repository": "owner/repo",
 "actor": "username", "payload": {...}}
```

**扩展平台步骤（以 GitHub 为例）：**

```
1. 创建 src/vcs/github.py → class GitHubBackend(VcsBackend)
2. 实现 normalize() + detect_event_header()
3. 注册到 _BACKENDS: {"github": GitHubBackend()}
4. 新增 Webhook 路由: POST /webhook/github
5. 在 managed_repos 表通过 source 字段区分平台
```

### 20.4 OAuth 联邦登录扩展

当前 OAuth 基于 GitCode，认证路由设计为可扩展模式：

```
/api/auth/login/gitcode   → GitCode OAuth
/api/auth/login/github    → GitHub OAuth (可扩展)
/api/auth/login/gitee     → Gitee OAuth (可扩展)
```

**扩展步骤：**

```
1. 在 user_accounts 表新增 {platform}_oauth_client_id/secret 字段
2. 新增路由: GET /api/auth/login/{platform}
3. 新增回调: GET /api/auth/callback/{platform}
4. 前端登录页新增平台按钮
```

### 20.5 数据库可扩展性

`user_accounts` 表采用宽表设计，新增凭据字段只需 ALTER TABLE：

```sql
-- 扩展示例: 新增 GitHub OAuth 支持
ALTER TABLE user_accounts ADD COLUMN github_oauth_client_id TEXT DEFAULT '';
ALTER TABLE user_accounts ADD COLUMN github_oauth_client_secret TEXT DEFAULT '';

-- 扩展微信 IM 支持
ALTER TABLE user_accounts ADD COLUMN wecom_webhook_url TEXT DEFAULT '';
ALTER TABLE user_accounts ADD COLUMN wecom_webhook_token TEXT DEFAULT '';
```

### 20.6 Agent 类型扩展

5 种 DevOps Agent（VOD/Issue/Coding/QA/SRE）通过 `agent_type_config` 表和 Prompt Builder 的 Agent 注册表驱动，新增 Agent 类型无需修改事件管道代码：

```sql
INSERT INTO agent_type_config (agent_type, default_backend, description)
VALUES ('security', 'opencode', 'Security scanning agent');

INSERT INTO agent_registry (name, description, triggers, tools)
VALUES ('security-check', 'Security vulnerability scanning and reporting',
        '["deploy_start","pull_request_opened"]', '["sast","dependency_check"]');
```

**扩展步骤：**

```
1. INSERT INTO agent_type_config + agent_registry
2. 创建 skills/security-check/SKILL.md
3. 在 event_route_config 添加触发路由
4. 前端侧边栏 + Agent 详情页自动扩展
```




