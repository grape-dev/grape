# Grape 组织（Organization）特性技术方案

> 状态：技术方案（2026-10-06）｜目标分支：multitask
> 作者：songspace/吕军涛
> 关联：参考 Multica 工作区隔离理念，为 Grape 增加多组织/多租户能力

---

## 1. 背景与目标

### 1.1 背景

Grape 当前是一个面向个人开发者的 AI DevOps 平台，核心数据模型围绕 **用户（user_accounts）+ 仓库（managed_repos）** 两级结构组织：

- 用户登录后绑定 GitCode/GitHub 账号，拉取个人仓库列表（`user_repos`）
- 托管仓库（`managed_repos`）是全局表，以 `repo_key`（如 `gitcode:owner/repo`）为主键
- 事件路由、Agent 调度、Skill/Scene 配置全部以 `repo_key` 为隔离边界
- 凭据（`runtime_credentials`）通过 `scope` 字段（`user`/`repo`/`global`）区分归属

随着企业内部团队使用增多，出现以下诉求：

1. **团队级资源隔离**：一个企业内多个研发团队，各自管理仓库、Worker、Skill、Scene，互不可见
2. **成员协作**：同一团队的多个用户共享托管仓库、共享 Worker 引擎池、共享 Skill 库
3. **统一计费/配额**：按组织维度统计 Agent 任务用量、Worker 数量、Skill 调用次数
4. **权限分级**：组织管理员、团队管理员、普通成员三级权限模型

### 1.2 目标

| 目标 | 描述 |
|------|------|
| **G1 组织隔离** | 引入 `organization` 实体，所有业务数据（仓库、凭据、引擎、场景、技能、知识）按组织隔离 |
| **G2 成员模型** | 用户可加入多个组织，组织内有 owner/admin/member 三级角色 |
| **G3 向后兼容** | 现有个人用户零感知——自动创建「个人组织」，原有数据迁入个人组织，API 行为不变 |
| **G4 权限模型** | 组织级权限（管理成员/配置/只读）+ 仓库级权限（管理员/成员）双层叠加 |
| **G5 多组织切换** | 前端支持组织切换器，API 通过 `X-Org-ID` header 或 `org_slug` 标识当前组织上下文 |

### 1.3 非目标

- 不实现跨组织数据共享（组织间严格隔离）
- 不实现组织级 SSO/LDAP 集成（后续迭代）
- 不改变 Grape 的 Agent 类型（VOD/Issue/Coding/QA/SRE）和事件路由逻辑
- 不引入 PostgreSQL——继续使用 MySQL RDS + sqlite3 兼容层

---

## 2. Multica 工作区隔离理念学习总结

### 2.1 核心设计：Workspace 作为顶层隔离边界

Multica 是一个 AI 原生任务管理平台，其核心隔离理念是 **Workspace-first**：

```
User ──(member)──> Workspace ──(owns)──> Agent / Issue / Skill / Runtime / Comment / Inbox
```

**关键设计点（来自 `server/migrations/001_init.up.sql`）：**

```sql
-- 工作区表（顶层租户）
CREATE TABLE workspace (
    id UUID PRIMARY KEY,
    name TEXT NOT NULL,
    slug TEXT UNIQUE NOT NULL,        -- 人类可读标识，用于 URL
    description TEXT,
    settings JSONB NOT NULL DEFAULT '{}',
    context TEXT,                      -- 工作区级上下文（给 Agent 的全局指令）
    repos JSONB NOT NULL DEFAULT '[]', -- 工作区关联的仓库列表
    issue_prefix TEXT,                 -- Issue 编号前缀（如 ACME-1）
    avatar_url TEXT,
    attribution_fail_closed BOOLEAN DEFAULT FALSE,  -- 合规策略
    created_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ
);

-- 成员表（用户 ↔ 工作区 多对多）
CREATE TABLE member (
    id UUID PRIMARY KEY,
    workspace_id UUID NOT NULL REFERENCES workspace(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('owner', 'admin', 'member')),
    created_at TIMESTAMPTZ,
    UNIQUE(workspace_id, user_id)      -- 一个用户在一个工作区只有一条成员记录
);
```

### 2.2 隔离字段贯穿所有业务表

Multica 的 **每一张业务表都带 `workspace_id` 列**，作为租户隔离的物理字段：

| 表 | 隔离字段 | 说明 |
|----|---------|------|
| `agent` | `workspace_id` | Agent 属于工作区，`visibility` 区分 `workspace`/`private` |
| `issue` | `workspace_id` | Issue 属于工作区 |
| `issue_label` | `workspace_id` | 标签属于工作区 |
| `comment` | `workspace_id`（migration 025 补加） | 评论通过 issue 回溯到工作区 |
| `inbox_item` | `workspace_id` | 收件箱按工作区隔离 |
| `activity_log` | `workspace_id` | 活动日志按工作区隔离 |
| `agent_runtime` | `workspace_id` | 运行时实例属于工作区 |
| `runtime_profile` | `workspace_id` | 自定义运行时配置属于工作区 |
| `skill` | `workspace_id` | Skill 属于工作区 |
| `workspace_share_link` | `workspace_id` | 邀请链接属于工作区 |
| `workspace_invitation` | `workspace_id` | 邀请记录属于工作区 |

### 2.3 中间件层强制隔离

Multica 在 HTTP 中间件层实现工作区解析与成员校验（`server/internal/middleware/workspace.go`）：

```go
// 工作区标识解析优先级（单一真相源）：
// 1. task-token binding（X-Actor-Source == "task_token"）— 服务端设置，不可篡改
// 2. 中间件注入的 context（快速路径）
// 3. X-Workspace-Slug header → GetWorkspaceBySlug → UUID
// 4. ?workspace_slug query → GetWorkspaceBySlug → UUID
// 5. X-Workspace-ID header（CLI/daemon 兼容）
// 6. ?workspace_id query（CLI/daemon 兼容）

func RequireWorkspaceMember(queries *db.Queries) func(http.Handler) http.Handler {
    // 1. 解析 workspace_id
    // 2. 校验用户是该 workspace 的 member
    // 3. 注入 workspaceID + member 到 context
}

func RequireWorkspaceRole(queries *db.Queries, roles ...string) func(http.Handler) http.Handler {
    // 额外校验 member.role 是否在允许列表内
}
```

**路由注册（`server/cmd/server/router.go`）：**

```go
// 工作区管理路由（URL 内含 workspace_id）
r.Route("/api/workspaces", func(r chi.Router) {
    r.Get("/", h.ListWorkspaces)            // 列出我的工作区
    r.Post("/", h.CreateWorkspace)           // 创建工作区
    r.Route("/{id}", func(r chi.Router) {
        r.Group(func(r chi.Router) {
            r.Use(middleware.RequireWorkspaceMemberFromURL(queries, "id"))
            r.Get("/", h.GetWorkspace)
            r.Get("/members", h.ListMembersWithUser)
            // ...
        })
        r.Group(func(r chi.Router) {
            r.Use(middleware.RequireWorkspaceRoleFromURL(queries, "id", "owner", "admin"))
            r.Put("/", h.UpdateWorkspace)
            r.Post("/members", h.CreateInvitation)
            // ...
        })
    })
})

// 工作区级路由（所有业务 API 都走 RequireWorkspaceMember 中间件）
r.Group(func(r chi.Router) {
    r.Use(middleware.RequireWorkspaceMember(queries))
    r.Route("/api/issues", ...)
    r.Route("/api/agents", ...)
    r.Route("/api/skills", ...)
    // ...
})
```

### 2.4 成员缓存与权限分层

- **MembershipCache**（Redis）：缓存 `user_id → workspace_id` 的成员关系，TTL 5 分钟。**只缓存存在性，不缓存角色**——角色校验始终查 DB（`server/internal/auth/membership_cache.go`）
- **三层角色**：`owner`（创建者/删除工作区）/ `admin`（管理成员/配置）/ `member`（只读+操作）
- **Agent visibility**：`workspace`（工作区内可见）/ `private`（仅 owner 可见）
- **Runtime profile visibility**：同样 `workspace`/`private` 两级

### 2.5 邀请机制

- **邮箱邀请**（`workspace_invitation` 表）：admin 发起，邀请状态 `pending`/`accepted`/`declined`/`expired`
- **分享链接**（`workspace_share_link` 表）：生成一次性 code，支持过期时间和最大使用次数

### 2.6 Task-Token 绑定（防越权）

Multica 的 Agent 任务执行使用 `task_token`，该 token **在创建时绑定到特定 workspace**，执行期间不可切换：

```go
// task_token 绑定的工作区不可被客户端覆盖
if r.Header.Get("X-Actor-Source") == "task_token" {
    bound := r.Header.Get("X-Workspace-ID")
    if bound == "" || workspaceID != bound {
        writeError(w, http.StatusForbidden, "task token is bound to a different workspace")
        return
    }
}
```

### 2.7 对 Grape 的启发

| Multica 理念 | Grape 适用性 |
|-------------|-------------|
| Workspace 作为顶层隔离 | ✅ 引入 `organization` 作为 Grape 的顶层租户 |
| 所有业务表带 `workspace_id` | ✅ 核心表加 `org_id` 列 |
| 中间件强制解析+校验 | ✅ 新增 `require_org_member` 依赖 |
| 三级角色 owner/admin/member | ✅ 直接复用 |
| slug 用于 URL | ✅ 组织 slug 用于前端路由 |
| MembershipCache（Redis） | ⚠️ Grape 无 Redis，先用内存缓存或直接查 DB |
| Task-Token 绑定 | ✅ Grape 的 `session_api_tokens` 已有 `repo_key` 绑定，扩展加 `org_id` |
| 邀请机制 | ✅ 后续迭代，一期可先用「直接添加成员」 |

---

## 3. Grape 现有架构分析

### 3.1 用户与仓库模型

**用户表（`src/user/db.py`）：**

```sql
CREATE TABLE user_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL DEFAULT '',
    gitcode_id TEXT DEFAULT '',
    gitcode_api_token TEXT DEFAULT '',
    github_id TEXT DEFAULT '',
    github_api_token TEXT DEFAULT '',
    nickname TEXT DEFAULT '',
    feishu_app_id TEXT DEFAULT '',       -- 个人飞书 Bot
    feishu_app_secret TEXT DEFAULT '',
    huawei_cloud_ak TEXT DEFAULT '',     -- 华为云凭据
    huawei_cloud_sk TEXT DEFAULT '',
    gitcode_publish_repo TEXT DEFAULT '',
    token_version INTEGER DEFAULT 0,     -- JWT 撤销版本号
    created_by INTEGER DEFAULT 0,
    created_at TEXT,
    last_active_at TEXT
);
```

**托管仓库表（`src/user/db.py`）：**

```sql
CREATE TABLE managed_repos (
    repo_key TEXT PRIMARY KEY,           -- gitcode:owner/repo（全局唯一键）
    repo_owner TEXT NOT NULL,
    repo_name TEXT NOT NULL,
    full_name TEXT NOT NULL,             -- owner/repo
    webhook_id INTEGER DEFAULT 0,
    dev_scene TEXT DEFAULT '',           -- 当前开发场景 scene_id
    host_status TEXT DEFAULT '',         -- 托管状态
    bot_gitcode_token TEXT DEFAULT '',   -- Bot token（仓库级）
    gitcode_webhook_token TEXT DEFAULT '',
    bot_username TEXT DEFAULT '',
    created_at TEXT
);
```

**用户仓库缓存表（`src/user/db.py`）：**

```sql
CREATE TABLE user_repos (
    user_id INTEGER NOT NULL,
    repo_key TEXT NOT NULL,
    owner TEXT DEFAULT '',
    name TEXT DEFAULT '',
    role TEXT DEFAULT '',                -- Admin/Member
    is_admin INTEGER DEFAULT 0,
    is_join INTEGER DEFAULT 0,           -- 是否已「加入」
    created_at TEXT,
    synced_at TEXT,
    PRIMARY KEY (user_id, repo_key)
);
```

**现状问题：**
- `managed_repos` 是全局表，无 `user_id` 或 `org_id` 列——所有用户的托管仓库在同一个表中
- 仓库托管后，任何登录用户都能看到所有托管仓库（通过 `/api/repos/managed`）
- 没有团队/组织概念，无法实现「团队 A 的仓库对团队 B 不可见」

### 3.2 现有 Scope 机制

Grape 在 `runtime_credentials` 和 `skill`/`backend_agents` 表中使用 `scope` 字段：

**runtime_credentials（`src/api/db_setup.py`）：**

```sql
CREATE TABLE runtime_credentials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT NOT NULL,            -- sha256(rta_xxx)
    name TEXT NOT NULL UNIQUE,           -- 凭据名=worker 名
    device_name TEXT DEFAULT '',
    scope TEXT DEFAULT 'user',           -- user(个人)/repo(仓库)/global(全局)
    owner_id TEXT DEFAULT '',            -- repo scope 时存 repo 全名；个人/全局为 ''
    owner_name TEXT DEFAULT '',          -- 归属用户 user_name
    repo_key TEXT DEFAULT NULL,          -- scope=user 时关联项目
    status TEXT DEFAULT 'active',
    last_seen_at TEXT,
    last_ip TEXT,
    created_at TEXT,
    UNIQUE (token_hash)
);
```

**skill 表：**

```sql
CREATE TABLE skill (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_name TEXT NOT NULL,
    display_name TEXT DEFAULT '',
    description TEXT DEFAULT '',
    agent_type TEXT DEFAULT '',          -- vod/issue/coding/qa/sre/chat
    skill_path TEXT DEFAULT '',
    file_path VARCHAR(512) DEFAULT '',
    scope TEXT DEFAULT 'official',       -- official/user/repo
    owner_id TEXT DEFAULT '',            -- scope=user 时存 user_id；scope=repo 时存 repo_key
    created_by VARCHAR(64) DEFAULT '',
    is_active INTEGER DEFAULT 1,
    sub_scene TEXT DEFAULT '',
    category_id INTEGER DEFAULT 0,
    UNIQUE (skill_name, scope, owner_id)
);
```

**Scope 语义对照：**

| 表 | scope 值 | owner_id 含义 | 可见性 |
|----|---------|-------------|--------|
| `runtime_credentials` | `global` | `''` | 所有用户可见 |
| `runtime_credentials` | `user` | `''` + `repo_key` | 个人仓库级 |
| `runtime_credentials` | `repo` | `repo_full_name` | 仓库级 |
| `skill` | `official` | `''` | 所有用户可见 |
| `skill` | `user` | `user_id` | 仅本人 |
| `skill` | `repo` | `repo_key` | 仓库成员 |
| `backend_agents` | `global` | `''` | 所有用户 |
| `backend_agents` | `user`/`repo` | 对应 id | 个人/仓库级 |

**现状问题：**
- Scope 是「字段值」而非「外键关系」，缺乏结构化约束
- `global` scope 的资源对所有用户可见，无法实现组织级隔离
- 不同表的 scope 语义不完全一致（runtime_credentials 的 `user` 和 skill 的 `user` 含义不同）

### 3.3 事件路由与 Agent 调度

**事件表（`src/api/db_setup.py`）：**

```sql
CREATE TABLE events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_key TEXT NOT NULL,              -- 事件隔离键
    event_type TEXT NOT NULL,
    source TEXT DEFAULT 'gitcode',
    issue_id TEXT DEFAULT '',
    title TEXT, summary TEXT, actor TEXT,
    payload TEXT DEFAULT '{}',
    agent_type TEXT DEFAULT '',          -- 路由到的 Agent
    backend TEXT DEFAULT '',
    skill TEXT DEFAULT '',
    status TEXT DEFAULT 'pending',
    entity_type TEXT DEFAULT '',
    created_at TEXT, started_at TEXT, completed_at TEXT
);
```

**Agent 任务队列（`src/api/db_setup.py`）：**

```sql
CREATE TABLE agent_tasks (
    id TEXT PRIMARY KEY,
    agent_session_id TEXT NOT NULL,      -- → devops_agent_session.id
    runtime_id INTEGER NOT NULL,         -- 确定路由 → agent_runtimes.id
    event_seq INTEGER,                   -- → events.seq
    issue_id TEXT DEFAULT '',
    repo_key TEXT DEFAULT '',
    agent_type TEXT DEFAULT '',
    skill TEXT DEFAULT '',
    user_id INTEGER DEFAULT 0,           -- 触发用户
    repo_gitcode_token TEXT DEFAULT '',
    user_gitcode_token TEXT DEFAULT '',
    env_extra MEDIUMTEXT DEFAULT '{}',
    skill_files MEDIUMTEXT,
    status TEXT DEFAULT 'queued',
    ...
);
```

**Agent 会话表（`src/api/db_setup.py`）：**

```sql
CREATE TABLE devops_agent_session (
    id TEXT PRIMARY KEY,
    repo_key TEXT NOT NULL,              -- 会话隔离键
    agent_type TEXT NOT NULL,            -- vod/issue/coding/qa/sre
    backend_id INTEGER DEFAULT 0,
    skill TEXT DEFAULT '',
    backend_session_id TEXT DEFAULT '',
    status TEXT DEFAULT 'idle',
    UNIQUE(repo_key, agent_type)         -- 每仓库每 Agent 类型一个配置会话
);
```

**路由逻辑（`src/api/routing.py`）：**
- 事件按 `issue_state` 阶段矩阵路由到 Agent（VOD → Issue → Coding → QA → SRE）
- 非 Issue 事件用静态映射兜底（`vod_signal` → vod, `build_failure` → sre 等）
- **所有路由以 `repo_key` 为隔离键**——事件、会话、任务都按仓库关联

**现状问题：**
- 事件和任务以 `repo_key` 隔离，而 `repo_key` 是全局的——无法区分「团队 A 的仓库」和「团队 B 的仓库」
- `agent_tasks.user_id` 记录触发用户，但不用于隔离——任何用户都能查看所有任务

### 3.4 Skill/Scene 的作用域

**场景表（`src/api/db_setup.py`）：**

```sql
CREATE TABLE devops_pipeline_scene (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scene_id TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    display_name TEXT DEFAULT '',
    repo_key TEXT NOT NULL DEFAULT '',    -- 场景只与仓库关联（issue-39 方案 B）
    nodes_json TEXT DEFAULT '[]',         -- 流水线节点配置
    onboarding TEXT DEFAULT 'repo-onboarding',
    is_active INTEGER DEFAULT 1,
    scene_type TEXT DEFAULT 'standard',   -- standard | pr_subscription
    UNIQUE (scene_id, repo_key)
);
```

**场景市场表（`src/api/db_setup.py`）：**

```sql
CREATE TABLE scene_market (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    display_name TEXT DEFAULT '',
    version TEXT DEFAULT '1.0.0',
    source TEXT DEFAULT 'official',       -- official/repo
    source_user_id INTEGER DEFAULT 0,
    scope VARCHAR(32) DEFAULT 'official', -- official/repo
    repo_key VARCHAR(191) DEFAULT '',
    UNIQUE (name, scope, source_user_id, repo_key)
);
```

**现状：**
- 场景（`devops_pipeline_scene`）已经迁移到 repo 模型（`UNIQUE(scene_id, repo_key)`），与仓库强绑定
- 场景市场（`scene_market`）有 `scope` 字段区分官方/仓库级
- 知识库（`agent_shared_knowledge`）有 `scope` 字段：`baseline`/`project`/`issue`/`issue_agent`/`member`

### 3.5 认证与 API 模型

**认证流程（`src/api/deps.py`）：**

```python
def get_current_user_or_api_key(request: Request) -> dict:
    # 三通道取值：Bearer / PRIVATE-TOKEN / access_token
    # 按前缀分流：
    # - tk_    → 会话级临时 token（session_api_tokens，绑定 repo_key + agent_type）
    # - grape_ → 用户级 API Key（api_tokens，绑定单用户）
    # - 其他   → JWT access token
    # 返回 {id, username, token, token_type, auth_type, ...}
```

**API 路由注册（`src/api/main.py`）：**

```python
app.include_router(auth.router)       # /api/auth
app.include_router(repos.router)      # /api/repos
app.include_router(agents.router)     # /api/agents
app.include_router(events.router)     # /api/events
app.include_router(chat.router)       # /api/chat
app.include_router(skills.router)     # /api/skills
app.include_router(scenes.router)     # /api/scenes
app.include_router(runtime.router)    # /api/runtime
app.include_router(tasks.router)      # /api/tasks
app.include_router(issues.router)     # /api/issues
# ... 共 22 个路由模块
```

**现状问题：**
- 所有 API 以 `/api/{resource}` 为前缀，无组织级路由
- 认证只解析用户身份，不解析组织上下文
- 仓库级鉴权依赖 `user_repos` 表的 `is_admin` 字段，无组织级权限

---

## 4. 组织特性设计方案

### 4.1 整体架构

```
User ──(org_member)──> Organization ──(owns)──> Managed Repos
                                         ├──> Runtime Credentials (Workers)
                                         ├──> Agent Runtimes (Engines)
                                         ├──> Skills (org scope)
                                         ├──> Scenes (org scope)
                                         ├──> Knowledge Base
                                         ├──> Events / Agent Tasks
                                         └──> Chat Conversations
```

**隔离层级：**

```
Organization (组织)
  └── Managed Repos (托管仓库，repo_key 全局唯一但归属组织)
       └── Events (事件，按 repo_key 关联)
       └── Agent Sessions (配置会话，按 repo_key)
       └── Issue State (Issue 生命周期)
       └── Scenes (开发场景)
  └── Runtime Credentials (Worker 凭据，org_id 隔离)
       └── Agent Runtimes (引擎实例，继承 credential 的 org_id)
  └── Skills (技能，org scope)
  └── Knowledge (知识库，org_id 隔离)
```

### 4.2 数据模型设计

#### 4.2.1 新增表：`organization`

```sql
CREATE TABLE IF NOT EXISTS organization (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            VARCHAR(128) NOT NULL,
    slug            VARCHAR(64) NOT NULL UNIQUE,       -- URL 友好标识（如 my-team）
    description     TEXT DEFAULT '',
    avatar_url      VARCHAR(512) DEFAULT '',
    owner_id        INTEGER NOT NULL,                   -- 创建者 user_id
    is_personal     INTEGER DEFAULT 0,                  -- 1=个人组织（自动创建，不可删除）
    settings        TEXT DEFAULT '{}',                  -- JSON：组织级配置
    max_members     INTEGER DEFAULT 50,                 -- 成员上限
    max_workers     INTEGER DEFAULT 10,                 -- Worker 凭据上限
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now'))
);
```

**MySQL DDL（生产环境）：**

```sql
CREATE TABLE IF NOT EXISTS organization (
    id              BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    name            VARCHAR(128) NOT NULL COMMENT '组织名称',
    slug            VARCHAR(64) NOT NULL COMMENT 'URL标识',
    description     TEXT COMMENT '组织描述',
    avatar_url      VARCHAR(512) DEFAULT '' COMMENT '组织头像',
    owner_id        BIGINT UNSIGNED NOT NULL COMMENT '创建者user_id',
    is_personal     TINYINT DEFAULT 0 COMMENT '1=个人组织(自动创建)',
    settings        TEXT COMMENT 'JSON组织级配置',
    max_members     INT DEFAULT 50 COMMENT '成员上限',
    max_workers     INT DEFAULT 10 COMMENT 'Worker凭据上限',
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_slug (slug)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='组织表';
```

#### 4.2.2 新增表：`org_member`

```sql
CREATE TABLE IF NOT EXISTS org_member (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id          INTEGER NOT NULL,
    user_id         INTEGER NOT NULL,
    role            TEXT NOT NULL DEFAULT 'member',    -- owner / admin / member
    invited_by      INTEGER DEFAULT 0,
    joined_at       TEXT DEFAULT (datetime('now')),
    UNIQUE (org_id, user_id)
);
```

**MySQL DDL：**

```sql
CREATE TABLE IF NOT EXISTS org_member (
    id              BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    org_id          BIGINT UNSIGNED NOT NULL,
    user_id         BIGINT UNSIGNED NOT NULL,
    role            VARCHAR(16) NOT NULL DEFAULT 'member' COMMENT 'owner/admin/member',
    invited_by      BIGINT UNSIGNED DEFAULT 0,
    joined_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_org_user (org_id, user_id),
    KEY idx_user_orgs (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='组织成员表';
```

#### 4.2.3 新增表：`org_invitation`（二期，一期可省略）

```sql
CREATE TABLE IF NOT EXISTS org_invitation (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id          INTEGER NOT NULL,
    inviter_id      INTEGER NOT NULL,
    invitee_email   TEXT DEFAULT '',
    invitee_user_id INTEGER DEFAULT 0,
    role            TEXT NOT NULL DEFAULT 'member',
    status          TEXT DEFAULT 'pending',            -- pending/accepted/declined/expired
    expires_at      TEXT,
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now'))
);
```

#### 4.2.4 核心表变更：新增 `org_id` 列

以下表新增 `org_id INTEGER DEFAULT 0` 列（`0` 表示「全局/未归属」，兼容现有数据）：

| 表 | 新增列 | 说明 | 迁移策略 |
|----|--------|------|---------|
| `managed_repos` | `org_id INTEGER DEFAULT 0` | 仓库归属组织 | 现有仓库 `org_id=0`，按托管用户迁入其个人组织 |
| `runtime_credentials` | `org_id INTEGER DEFAULT 0` | Worker 凭据归属组织 | 现有 `scope=user` 凭据迁入用户个人组织 |
| `agent_runtimes` | `org_id INTEGER DEFAULT 0` | 引擎实例归属组织（继承 credential） | 跟随 credential 迁移 |
| `skill` | `org_id INTEGER DEFAULT 0` | 技能归属组织 | `scope=official` 保持 `org_id=0`；`scope=user/repo` 迁入个人组织 |
| `devops_pipeline_scene` | `org_id INTEGER DEFAULT 0` | 场景归属组织 | 跟随 repo_key 迁移 |
| `scene_market` | `org_id INTEGER DEFAULT 0` | 场景市场归属 | `scope=official` 保持 0；`scope=repo` 迁入 |
| `agent_shared_knowledge` | `org_id INTEGER DEFAULT 0` | 知识归属组织 | 跟随 repo_key 迁移 |
| `events` | `org_id INTEGER DEFAULT 0` | 事件归属组织 | 跟随 repo_key（新事件写入时填充） |
| `agent_tasks` | `org_id INTEGER DEFAULT 0` | 任务归属组织 | 跟随 repo_key |
| `devops_agent_session` | `org_id INTEGER DEFAULT 0` | 配置会话归属组织 | 跟随 repo_key |
| `chat_conversations` | `org_id INTEGER DEFAULT 0` | 对话归属组织 | 跟随 user_id |
| `issue` | `org_id INTEGER DEFAULT 0` | Issue 卡片归属组织 | 跟随 repo_key |

**DDL 示例（`managed_repos` 加列）：**

```sql
-- SQLite（开发环境）
ALTER TABLE managed_repos ADD COLUMN org_id INTEGER DEFAULT 0;

-- MySQL（生产环境）
ALTER TABLE managed_repos ADD COLUMN org_id BIGINT UNSIGNED DEFAULT 0 COMMENT '归属组织ID';
ALTER TABLE managed_repos ADD INDEX idx_org (org_id);
```

**`runtime_credentials` 的 scope 扩展：**

```sql
-- 原 scope 值：user / repo / global
-- 新增 org scope，表示组织级共享 Worker
-- scope=org 时，owner_id 存 org_id
ALTER TABLE runtime_credentials ADD COLUMN org_id INTEGER DEFAULT 0;
```

#### 4.2.5 `skill` 表 scope 扩展

```sql
-- 原 scope 值：official / user / repo
-- 新增 org scope，表示组织级共享技能
-- scope=org 时，org_id 字段存组织 ID
ALTER TABLE skill ADD COLUMN org_id INTEGER DEFAULT 0;

-- 唯一键扩展（兼容现有数据）：
-- 原：UNIQUE (skill_name, scope, owner_id)
-- 新：UNIQUE (skill_name, scope, owner_id, org_id)
```

### 4.3 API 端点设计

#### 4.3.1 组织管理 API

新增路由模块 `src/api/routes/orgs.py`，前缀 `/api/orgs`：

| 方法 | 路径 | 说明 | 权限 |
|------|------|------|------|
| `GET` | `/api/orgs` | 列出当前用户加入的组织 | 任意登录用户 |
| `POST` | `/api/orgs` | 创建组织 | 任意登录用户 |
| `GET` | `/api/orgs/{org_id}` | 获取组织详情 | 组织成员 |
| `PUT` | `/api/orgs/{org_id}` | 更新组织信息 | owner/admin |
| `DELETE` | `/api/orgs/{org_id}` | 删除组织（个人组织不可删） | owner |
| `GET` | `/api/orgs/{org_id}/members` | 列出组织成员 | 组织成员 |
| `POST` | `/api/orgs/{org_id}/members` | 添加成员（直接添加） | owner/admin |
| `PATCH` | `/api/orgs/{org_id}/members/{user_id}` | 修改成员角色 | owner |
| `DELETE` | `/api/orgs/{org_id}/members/{user_id}` | 移除成员 | owner/admin（不能移除 owner） |
| `POST` | `/api/orgs/{org_id}/leave` | 离开组织 | 任意成员（owner 不可离开） |

**请求/响应示例：**

```python
# POST /api/orgs
class CreateOrgBody(BaseModel):
    name: str
    slug: str
    description: str = ""

# 响应
{
    "id": 1,
    "name": "我的团队",
    "slug": "my-team",
    "description": "",
    "is_personal": false,
    "role": "owner",        # 当前用户在该组织的角色
    "member_count": 1,
    "created_at": "..."
}

# GET /api/orgs/{org_id}/members
[
    {
        "user_id": 1,
        "username": "songspace",
        "nickname": "吕军涛",
        "role": "owner",
        "joined_at": "..."
    }
]
```

#### 4.3.2 组织上下文传递

**方式一：HTTP Header（推荐）**

```
X-Org-ID: 1
# 或
X-Org-Slug: my-team
```

**方式二：Query 参数（兼容）**

```
GET /api/repos/managed?org_id=1
GET /api/repos/managed?org_slug=my-team
```

**新增依赖函数（`src/api/deps.py`）：**

```python
def resolve_org_context(request: Request, user: dict) -> int:
    """解析当前请求的组织上下文，返回 org_id。

    优先级：
    1. X-Org-ID header
    2. X-Org-Slug header → 查 organization 表
    3. ?org_id query
    4. ?org_slug query → 查 organization 表
    5. 未指定 → 返回用户的个人组织 ID（兼容现有行为）

    校验：用户必须是该组织的成员（org_member 表），否则 403。
    """
    org_id = 0
    # 1. X-Org-ID
    org_id_str = request.headers.get("X-Org-ID", "")
    if org_id_str:
        org_id = int(org_id_str)
    # 2. X-Org-Slug
    elif request.headers.get("X-Org-Slug", ""):
        slug = request.headers.get("X-Org-Slug")
        org_id = _get_org_id_by_slug(slug)
    # 3/4. query 参数
    elif request.query_params.get("org_id"):
        org_id = int(request.query_params.get("org_id"))
    elif request.query_params.get("org_slug"):
        org_id = _get_org_id_by_slug(request.query_params.get("org_slug"))

    if org_id == 0:
        # 未指定组织 → 使用用户个人组织（兼容）
        org_id = _get_personal_org_id(user["id"])
    else:
        # 校验成员关系
        if not _is_org_member(user["id"], org_id):
            raise HTTPException(403, "不是该组织的成员")
    return org_id


def require_org_member(request: Request, user: dict = Depends(get_current_user_or_api_key)) -> dict:
    """组织成员校验依赖。返回 {**user, "org_id": int, "org_role": str}。"""
    org_id = resolve_org_context(request, user)
    role = _get_org_role(user["id"], org_id)
    return {**user, "org_id": org_id, "org_role": role}


def require_org_admin(user: dict = Depends(require_org_member)) -> dict:
    """组织管理员校验。"""
    if user["org_role"] not in ("owner", "admin"):
        raise HTTPException(403, "需要组织管理员权限")
    return user
```

#### 4.3.3 现有 API 改造

**`/api/repos/managed` — 托管仓库列表**

```python
@router.get("/managed")
def list_managed_repos(user: dict = Depends(require_org_member)):
    org_id = user["org_id"]
    # 原查询：SELECT * FROM managed_repos
    # 新查询：SELECT * FROM managed_repos WHERE org_id=?
    with db_lock:
        c = db.cursor()
        c.execute("SELECT * FROM managed_repos WHERE org_id=?", (org_id,))
        return [_row_to_dict(r) for r in c.fetchall()]
```

**`/api/repos/member` — 成员仓库列表**

```python
@router.get("/member")
def list_member_repos(user: dict = Depends(require_org_member)):
    org_id = user["org_id"]
    # 过滤本组织内已托管的仓库
    # 1. 拉取用户 VCS 仓库列表（不变）
    # 2. 标记 in_managed 时只查本组织
    managed = {r["repo_key"] for r in get_managed_repos_by_org(org_id)}
    # ...
```

**`/api/skills` — 技能列表**

```python
@router.get("")
def list_skills(user: dict = Depends(require_org_member), ...):
    org_id = user["org_id"]
    # 原查询条件：is_active=1 AND (scope='official' OR scope='user' AND owner_id=? OR scope='repo' AND owner_id=?)
    # 新查询条件：is_active=1 AND (
    #     scope='official'                           -- 官方技能（全局）
    #     OR (scope='org' AND org_id=?)             -- 本组织技能
    #     OR (scope='user' AND owner_id=? AND org_id=?)  -- 本人在本组织的个人技能
    #     OR (scope='repo' AND owner_id IN (...本组织仓库...))
    # )
```

**`/api/runtime/credentials` — Worker 凭据列表（新增端点）**

```python
@router.get("/credentials")
def list_credentials(user: dict = Depends(require_org_member)):
    org_id = user["org_id"]
    # 查询本组织的凭据
    c.execute("SELECT * FROM runtime_credentials WHERE org_id=? ORDER BY created_at DESC", (org_id,))
```

**`/api/scenes` — 场景列表**

```python
@router.get("")
def list_scenes(user: dict = Depends(require_org_member), repo: str = ""):
    org_id = user["org_id"]
    # 场景已按 repo_key 隔离，repo_key 归属 org_id
    # 查询时加 org_id 过滤
    c.execute("SELECT * FROM devops_pipeline_scene WHERE org_id=? AND repo_key=?", (org_id, repo_key))
```

#### 4.3.4 Webhook 事件入库时填充 org_id

```python
# src/api/event_store.py - append_event 函数
def append_event(repo_key: str, event_type: str, ...):
    # 解析 repo_key → org_id
    org_id = _get_org_id_by_repo_key(repo_key)  # 查 managed_repos.org_id
    c.execute(
        "INSERT INTO events (repo_key, event_type, ..., org_id) VALUES (?, ?, ..., ?)",
        (repo_key, event_type, ..., org_id)
    )
```

#### 4.3.5 Worker 注册时绑定 org_id

```python
# src/api/routes/runtime.py - register 端点
@router.post("/register")
def register(body: RuntimeRegBody, credential_id: int = Depends(_require_credential)):
    # 凭据已带 org_id（创建时指定）
    c.execute("SELECT org_id FROM runtime_credentials WHERE id=?", (credential_id,))
    org_id = c.fetchone()[0]
    # 注册 agent_runtimes 时填充 org_id
    for rt in body.runtimes:
        c.execute(
            "INSERT OR REPLACE INTO agent_runtimes (credential_id, name, ..., org_id) VALUES (..., ?)",
            (..., org_id)
        )
```

### 4.4 权限模型

#### 4.4.1 组织级权限

| 角色 | 权限 |
|------|------|
| `owner` | 删除组织、转让所有权、管理成员（添加/移除/改角色）、管理所有资源、配置组织设置 |
| `admin` | 管理成员（添加/移除）、管理组织资源（仓库/Worker/Skill/Scene）、查看组织所有数据 |
| `member` | 查看组织内仓库/Worker/Skill/Scene、操作已授权仓库的 Agent、创建个人技能 |

#### 4.4.2 仓库级权限（叠加在组织权限之上）

仓库级权限沿用现有 `user_repos.is_admin` 机制：

- 仓库管理员（VCS owner/admin）：可托管/移除仓库、配置场景、管理 Bot
- 仓库成员：可查看仓库数据、触发 Agent、参与对话

**鉴权流程：**

```
请求 → JWT 认证 → 组织解析（X-Org-ID） → 组织成员校验 → 仓库级权限校验
         ↓              ↓                      ↓                    ↓
    get_current_user  resolve_org_context   org_member 表      user_repos.is_admin
```

#### 4.4.3 Worker 凭据的 org 隔离

- Worker 凭据（`runtime_credentials`）创建时指定 `org_id`
- `rta_` token 认证时（`_auth_credential`），从凭据读取 `org_id`，注入到后续任务
- Worker 注册的引擎实例（`agent_runtimes`）继承凭据的 `org_id`
- 任务调度时，只从相同 `org_id` 的引擎池中选取

### 4.5 隔离机制

#### 4.5.1 查询层隔离

**所有业务查询加 `org_id` 过滤条件。** 通过统一的查询 helper 实现：

```python
# src/api/org_context.py（新模块）

def org_filter(org_id: int, table_alias: str = "") -> tuple[str, list]:
    """返回 (WHERE 片段, 参数列表)，用于追加到查询条件。"""
    prefix = f"{table_alias}." if table_alias else ""
    return (f"{prefix}org_id=?", [org_id])
```

**改造模式（以 `managed_repos` 查询为例）：**

```python
# 改造前
c.execute("SELECT * FROM managed_repos")

# 改造后
c.execute("SELECT * FROM managed_repos WHERE org_id=?", (org_id,))
```

#### 4.5.2 写入层隔离

**所有数据写入时填充 `org_id`：**

```python
# 托管仓库时
def add_managed_repo(repo_key, ..., org_id):
    c.execute("INSERT INTO managed_repos (repo_key, ..., org_id) VALUES (..., ?)", (..., org_id))

# 创建凭据时
def create_credential(name, ..., org_id):
    c.execute("INSERT INTO runtime_credentials (name, ..., org_id) VALUES (..., ?)", (..., org_id))

# 入库事件时
def append_event(repo_key, ...):
    org_id = _get_org_id_by_repo_key(repo_key)
    c.execute("INSERT INTO events (..., org_id) VALUES (..., ?)", (..., org_id))
```

#### 4.5.3 Session Token 的 org 绑定

扩展现有 `session_api_tokens`（`tk_` 前缀 token），增加 `org_id` 字段：

```sql
ALTER TABLE session_api_tokens ADD COLUMN org_id INTEGER DEFAULT 0;
```

```python
# 创建 session token 时绑定 org_id
def create_session_token(user_id, repo_key, agent_type, org_id):
    # token 创建时记录 org_id
    # 验证时校验 org_id 匹配当前请求的组织上下文
```

#### 4.5.4 官方资源（official scope）不受组织隔离

- `scope='official'` 的 Skill / Scene Market 资源对所有组织可见
- 这些资源的 `org_id=0`，查询时不加组织过滤
- 写入官方资源需要平台管理员权限（非组织管理员）

### 4.6 前端 UI 变更

#### 4.6.1 组织切换器

**位置：** 顶部导航栏左侧（当前 Grape 仓库名称选择器旁）

**交互：**
- 点击展开组织列表（当前用户加入的所有组织）
- 显示组织名称、角色、成员数
- 切换组织后，所有页面数据按新组织刷新
- 个人组织标记「个人」徽章

**实现（`src/api/static/js/app.js`）：**

```javascript
// 组织切换器组件
const OrgSwitcher = {
    template: `
        <div class="org-switcher" @click="toggleDropdown">
            <span class="org-name">{{ currentOrg?.name || '个人' }}</span>
            <span class="org-role" v-if="currentOrg">{{ currentOrg.role }}</span>
            <div class="org-dropdown" v-if="showDropdown">
                <div v-for="org in orgs" @click="switchOrg(org)" class="org-item">
                    {{ org.name }}
                    <span v-if="org.is_personal" class="badge">个人</span>
                    <span class="role">{{ org.role }}</span>
                </div>
                <div class="org-item create" @click="showCreateOrg">
                    + 创建组织
                </div>
            </div>
        </div>
    `,
    data() {
        return { orgs: [], currentOrg: null, showDropdown: false }
    },
    async mounted() {
        const resp = await api.get('/api/orgs');
        this.orgs = resp.data;
        // 从 localStorage 读取上次选择的组织
        const savedOrgId = localStorage.getItem('current_org_id');
        this.currentOrg = this.orgs.find(o => o.id == savedOrgId) || this.orgs[0];
        if (this.currentOrg) {
            api.defaults.headers['X-Org-ID'] = this.currentOrg.id;
        }
    },
    methods: {
        switchOrg(org) {
            this.currentOrg = org;
            localStorage.setItem('current_org_id', org.id);
            api.defaults.headers['X-Org-ID'] = org.id;
            // 触发全局数据刷新
            window.location.reload();
        }
    }
};
```

#### 4.6.2 组织管理页面

**新增页面：** `组织设置`（在用户菜单中）

**子页面：**
- 组织信息（名称、描述、头像、slug）
- 成员管理（成员列表、添加成员、修改角色、移除成员）
- 资源概览（仓库数、Worker 数、Skill 数、本月任务数）

#### 4.6.3 仓库列表页改造

- 仓库列表只显示当前组织内的托管仓库
- 「加入并托管」按钮仅在当前为非个人组织时显示组织选择
- 个人组织下显示用户个人 VCS 仓库

#### 4.6.4 Worker 管理页改造

- Worker 列表只显示当前组织的凭据和引擎
- 创建 Worker 凭据时自动绑定当前组织

#### 4.6.5 API 请求拦截器

```javascript
// 全局 axios 拦截器：每个请求自动带 X-Org-ID header
api.interceptors.request.use(config => {
    const orgId = localStorage.getItem('current_org_id');
    if (orgId) {
        config.headers['X-Org-ID'] = orgId;
    }
    return config;
});
```

---

## 5. 迁移路径

### 5.1 迁移策略：个人组织自动创建

**核心原则：** 每个现有用户自动获得一个「个人组织」（`is_personal=1`），其现有数据迁入个人组织。用户无感知。

### 5.2 迁移步骤

#### Step 1: 创建 organization / org_member 表

```python
# src/api/db_setup.py 新增 DDL
_ORGANIZATION_DDL = """
CREATE TABLE IF NOT EXISTS organization (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name VARCHAR(128) NOT NULL,
    slug VARCHAR(64) NOT NULL UNIQUE,
    description TEXT DEFAULT '',
    avatar_url VARCHAR(512) DEFAULT '',
    owner_id INTEGER NOT NULL,
    is_personal INTEGER DEFAULT 0,
    settings TEXT DEFAULT '{}',
    max_members INTEGER DEFAULT 50,
    max_workers INTEGER DEFAULT 10,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
)
"""

_ORG_MEMBER_DDL = """
CREATE TABLE IF NOT EXISTS org_member (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    role TEXT NOT NULL DEFAULT 'member',
    invited_by INTEGER DEFAULT 0,
    joined_at TEXT DEFAULT (datetime('now')),
    UNIQUE (org_id, user_id)
)
"""
```

#### Step 2: 为现有表添加 org_id 列

```python
def _migrate_add_org_id():
    """为所有核心表添加 org_id 列（幂等）。"""
    tables_to_migrate = [
        "managed_repos",
        "runtime_credentials",
        "agent_runtimes",
        "skill",
        "devops_pipeline_scene",
        "scene_market",
        "agent_shared_knowledge",
        "events",
        "agent_tasks",
        "devops_agent_session",
        "chat_conversations",
        "issue",
    ]
    with db_lock:
        c = db.cursor()
        for table in tables_to_migrate:
            _ensure_column(c, table, "org_id", "INTEGER DEFAULT 0")
```

#### Step 3: 为每个用户创建个人组织

```python
def _migrate_create_personal_orgs():
    """为每个现有用户创建个人组织。"""
    with db_lock:
        c = db.cursor()
        users = c.execute("SELECT id, username FROM user_accounts").fetchall()
        for uid, username in users:
            # 检查是否已有个人组织
            existing = c.execute(
                "SELECT id FROM organization WHERE owner_id=? AND is_personal=1", (uid,)
            ).fetchone()
            if existing:
                continue
            # 创建个人组织
            slug = f"personal-{uid}"  # 确保唯一
            c.execute(
                "INSERT INTO organization (name, slug, owner_id, is_personal) VALUES (?, ?, ?, 1)",
                (f"{username}的个人空间", slug, uid)
            )
            org_id = c.lastrowid
            # 添加为 owner
            c.execute(
                "INSERT INTO org_member (org_id, user_id, role) VALUES (?, ?, 'owner')",
                (org_id, uid)
            )
        db.commit()
```

#### Step 4: 迁移现有数据到个人组织

```python
def _migrate_data_to_personal_orgs():
    """将现有数据迁入对应用户的个人组织。"""
    with db_lock:
        c = db.cursor()

        # 4.1 managed_repos：按 user_repos.is_admin 或托管者关联
        # 对于每个 managed_repos，找到第一个 is_admin=1 的用户，迁入其个人组织
        repos = c.execute("SELECT repo_key FROM managed_repos WHERE org_id=0 OR org_id IS NULL").fetchall()
        for (repo_key,) in repos:
            # 查找该仓库的管理员用户
            admin = c.execute(
                "SELECT user_id FROM user_repos WHERE repo_key=? AND is_admin=1 LIMIT 1",
                (repo_key,)
            ).fetchone()
            if admin:
                uid = admin[0]
                org = c.execute(
                    "SELECT id FROM organization WHERE owner_id=? AND is_personal=1", (uid,)
                ).fetchone()
                if org:
                    c.execute("UPDATE managed_repos SET org_id=? WHERE repo_key=?", (org[0], repo_key))

        # 4.2 runtime_credentials：scope=user 的迁入 owner 的个人组织
        creds = c.execute(
            "SELECT id, owner_name FROM runtime_credentials WHERE org_id=0 OR org_id IS NULL"
        ).fetchall()
        for cid, owner_name in creds:
            if not owner_name:
                continue
            user = c.execute("SELECT id FROM user_accounts WHERE username=?", (owner_name,)).fetchone()
            if user:
                uid = user[0]
                org = c.execute(
                    "SELECT id FROM organization WHERE owner_id=? AND is_personal=1", (uid,)
                ).fetchone()
                if org:
                    c.execute("UPDATE runtime_credentials SET org_id=? WHERE id=?", (org[0], cid))
                    # 同步更新 agent_runtimes
                    c.execute("UPDATE agent_runtimes SET org_id=? WHERE credential_id=?", (org[0], cid))

        # 4.3 skill：scope=user 的迁入 owner 的个人组织
        skills = c.execute(
            "SELECT id, owner_id FROM skill WHERE scope='user' AND (org_id=0 OR org_id IS NULL)"
        ).fetchall()
        for sid, owner_id in skills:
            try:
                uid = int(owner_id)
            except (ValueError, TypeError):
                continue
            org = c.execute(
                "SELECT id FROM organization WHERE owner_id=? AND is_personal=1", (uid,)
            ).fetchone()
            if org:
                c.execute("UPDATE skill SET org_id=? WHERE id=?", (org[0], sid))

        # 4.4 skill：scope=repo 的跟随 repo_key 迁移
        repo_skills = c.execute(
            "SELECT id, owner_id FROM skill WHERE scope='repo' AND (org_id=0 OR org_id IS NULL)"
        ).fetchall()
        for sid, owner_id in repo_skills:
            repo_org = c.execute(
                "SELECT org_id FROM managed_repos WHERE repo_key=?", (owner_id,)
            ).fetchone()
            if repo_org and repo_org[0]:
                c.execute("UPDATE skill SET org_id=? WHERE id=?", (repo_org[0], sid))

        # 4.5 devops_pipeline_scene / agent_shared_knowledge / events / agent_tasks：
        #     跟随 repo_key 的 org_id
        for table in ["devops_pipeline_scene", "agent_shared_knowledge", "events", "agent_tasks",
                       "devops_agent_session", "issue"]:
            c.execute(f"""
                UPDATE {table} SET org_id = (
                    SELECT m.org_id FROM managed_repos m
                    WHERE m.repo_key = {table}.repo_key AND m.org_id > 0
                )
                WHERE (org_id = 0 OR org_id IS NULL)
                  AND repo_key IN (SELECT repo_key FROM managed_repos WHERE org_id > 0)
            """)

        db.commit()
```

#### Step 5: 新用户注册时自动创建个人组织

```python
# src/user/db.py - create_user 函数改造
def create_user(username: str, password: str, created_by: int = 0) -> dict | None:
    # ... 原有创建逻辑 ...
    uid = c.lastrowid

    # 自动创建个人组织
    slug = f"personal-{uid}"
    c.execute(
        "INSERT INTO organization (name, slug, owner_id, is_personal) VALUES (?, ?, ?, 1)",
        (f"{username}的个人空间", slug, uid)
    )
    org_id = c.lastrowid
    c.execute(
        "INSERT INTO org_member (org_id, user_id, role) VALUES (?, ?, 'owner')",
        (org_id, uid)
    )
    db.commit()
    return {"id": uid, "username": username, "personal_org_id": org_id}
```

### 5.3 向后兼容保证

| 场景 | 兼容策略 |
|------|---------|
| 现有用户登录 | 自动有个人组织，`X-Org-ID` 未传时默认使用个人组织 |
| 现有 API 调用 | 未传 `X-Org-ID` 时，`resolve_org_context` 回退到个人组织 |
| 现有 Worker 注册 | `rta_` token 认证后读取 credential 的 `org_id`，自动注入 |
| 现有 Webhook 事件 | 入库时查 `managed_repos.org_id` 填充 |
| `scope='official'` 资源 | `org_id=0`，所有组织可见，查询不加 org 过滤 |
| `scope='global'` 凭据 | 保持 `org_id=0`，所有组织可见（后续可限制为平台级） |

---

## 6. 风险评估

### 6.1 数据迁移风险

| 风险 | 等级 | 缓解措施 |
|------|------|---------|
| 迁移过程中数据不一致 | 高 | 迁移脚本幂等设计，可重复执行；迁移期间服务只读 |
| managed_repos 无对应管理员用户 | 中 | 这类仓库 `org_id` 保持 0，标记为「未归属」，由管理员手动分配 |
| runtime_credentials 的 owner_name 不匹配 | 中 | 无法匹配的凭据保持 `org_id=0`，视为全局凭据 |
| agent_runtimes 与 credential org_id 不一致 | 中 | 迁移后校验脚本：`SELECT ar.org_id, rc.org_id FROM agent_runtimes ar JOIN runtime_credentials rc ... WHERE ar.org_id != rc.org_id` |

### 6.2 性能风险

| 风险 | 等级 | 缓解措施 |
|------|------|---------|
| 所有查询加 `org_id=?` 条件导致索引失效 | 中 | 为 `org_id` 列创建索引；复合索引 `(org_id, repo_key)` |
| 组织成员校验增加 DB 查询 | 中 | 内存缓存 `org_member` 关系（TTL 5 分钟，类似 Multica MembershipCache）；改密码/退出组织时清除缓存 |
| 大组织（100+ 仓库）查询变慢 | 低 | 分页查询；`org_id + created_at` 复合索引 |

### 6.3 兼容性风险

| 风险 | 等级 | 缓解措施 |
|------|------|---------|
| 旧版前端不传 `X-Org-ID` | 高 | 后端默认回退到个人组织，行为与改造前一致 |
| 第三方集成使用 API Key（`grape_`）无组织上下文 | 中 | API Key 绑定用户，`resolve_org_context` 回退到用户的个人组织 |
| Session Token（`tk_`）绑定的 repo 跨组织 | 低 | Session Token 创建时记录 `org_id`，校验时与请求的 org_id 比对 |
| MySQL DDL 迁移（ALTER TABLE ADD COLUMN）锁表 | 中 | MySQL 8.0 的 `ADD COLUMN ... DEFAULT 0` 是 INSTANT 操作，不锁表 |

### 6.4 安全风险

| 风险 | 等级 | 缓解措施 |
|------|------|---------|
| 用户通过伪造 `X-Org-ID` 访问其他组织数据 | 高 | `resolve_org_context` 强制校验 `org_member` 成员关系 |
| Worker 凭据被用于其他组织的任务 | 高 | `rta_` token 认证时绑定 `org_id`，任务调度只匹配同 `org_id` 的引擎 |
| 跨组织仓库数据泄露 | 高 | 所有查询强制 `org_id` 过滤；代码审查检查无 `org_id` 条件的查询 |
| 个人组织被误删 | 中 | `is_personal=1` 的组织禁止删除（API 层和 DB 层双重校验） |

### 6.5 功能回归风险

| 风险 | 等级 | 缓解措施 |
|------|------|---------|
| 事件路由逻辑受影响 | 中 | 事件路由以 `repo_key` 为键不变，`org_id` 仅用于可见性过滤 |
| Agent 调度受影响 | 中 | 引擎池选择逻辑加 `org_id` 过滤，但路由优先级不变 |
| Skill 交付受影响 | 低 | Skill 查询加 `org_id` 过滤，但交付逻辑（OBS 存储 + zip 打包）不变 |
| Chat 会话受影响 | 低 | `chat_conversations` 加 `org_id`，对话隔离更严格，不影响现有逻辑 |

---

## 7. 实施计划

### 7.1 分阶段实施

| 阶段 | 内容 | 工作量 |
|------|------|--------|
| **P1** | DDL 建表 + org_id 列添加 + 个人组织自动创建 + 迁移脚本 | 2 天 |
| **P2** | `src/api/routes/orgs.py` 组织管理 API + `resolve_org_context` 依赖 | 2 天 |
| **P3** | 现有 API 改造（repos/skills/scenes/runtime 加 org_id 过滤） | 3 天 |
| **P4** | 前端组织切换器 + 组织管理页 + API 拦截器 | 2 天 |
| **P5** | 事件入库填充 org_id + Worker 注册绑定 org_id + Session Token 扩展 | 1 天 |
| **P6** | 测试 + 回归验证 + 迁移脚本验证 | 2 天 |

### 7.2 验证用例

#### 7.2.1 数据迁移验证

| 编号 | 用例 | 验证步骤 | 预期结果 | 状态 |
|------|------|----------|----------|------|
| M-01 | organization 表创建 | `SELECT count(*) FROM organization` | 返回 > 0（每个用户一个个人组织） | ✅ |
| M-02 | org_member 表创建 | `SELECT count(*) FROM org_member` | 返回 > 0（每个用户 owner 记录） | ✅ |
| M-03 | org_id 列存在 | 对 12 张核心表执行 `PRAGMA table_info(表名)` | 每张表都包含 `org_id` 列 | ✅ |
| M-04 | 个人组织自动创建 | `SELECT count(*) FROM organization WHERE is_personal=1` == `SELECT count(*) FROM user_accounts` | 两者相等 | ✅ |
| M-05 | managed_repos 迁移 | `SELECT count(*) FROM managed_repos WHERE org_id > 0` | > 0（现有仓库已归属个人组织） | ✅ |
| M-06 | runtime_credentials 迁移 | `SELECT count(*) FROM runtime_credentials WHERE org_id > 0 AND scope='user'` | scope=user 的凭据已迁移 | ✅ |
| M-07 | skill 迁移 | `SELECT count(*) FROM skill WHERE org_id > 0 AND scope IN ('user','repo')` | user/repo scope 的技能已迁移 | ✅ |
| M-08 | events 回填 | `SELECT count(*) FROM events WHERE org_id > 0` | > 0（跟随 repo_key 回填） | ✅ |
| M-09 | 迁移幂等性 | 二次执行 `_migrate_create_personal_orgs()` + `_migrate_data_to_personal_orgs()` | 不报错，不产生重复数据 | ✅ |
| M-10 | 新用户自动创建个人组织 | 调用 `create_user('testuser', 'pass')` 后查 `organization WHERE owner_id=<新uid> AND is_personal=1` | 存在一条记录 | ✅ |

#### 7.2.2 组织管理 API 验证

| 编号 | 用例 | 验证步骤 | 预期结果 | 状态 |
|------|------|----------|----------|------|
| A-01 | 列出用户组织 | `GET /api/orgs`（已登录） | 返回数组，含个人组织（is_personal=true, role=owner） | ✅ |
| A-02 | 创建组织 | `POST /api/orgs {"name":"测试团队","slug":"test-team"}` | 返回 200 + 组织详情，role=owner | ✅ |
| A-03 | 创建组织 slug 唯一 | 重复创建 slug=test-team | 返回 409 | ✅ |
| A-04 | 获取组织详情 | `GET /api/orgs/{org_id}` | 返回组织信息 + 当前用户角色 | ✅ |
| A-05 | 更新组织 | `PUT /api/orgs/{org_id} {"name":"新名称"}`（owner/admin） | 返回 `{"ok": true}` | ✅ |
| A-06 | 更新组织权限 | member 角色调用 `PUT /api/orgs/{org_id}` | 返回 403 | ✅ |
| A-07 | 删除组织 | `DELETE /api/orgs/{org_id}`（owner，非个人组织） | 返回 `{"ok": true}` | ✅ |
| A-08 | 个人组织不可删 | `DELETE /api/orgs/{personal_org_id}` | 返回 400 | ✅ |
| A-09 | 列出成员 | `GET /api/orgs/{org_id}/members` | 返回成员数组（含 username/nickname/role） | ✅ |
| A-10 | 添加成员 | `POST /api/orgs/{org_id}/members {"user_id":N,"role":"member"}` | 返回 `{"ok": true}` | ✅ |
| A-11 | 添加不存在用户 | `POST /api/orgs/{org_id}/members {"user_id":99999}` | 返回 404 | ✅ |
| A-12 | 重复添加成员 | 对已加入的成员再次 `POST /api/orgs/{org_id}/members` | 返回 409 | ✅ |
| A-13 | 修改成员角色 | `PATCH /api/orgs/{org_id}/members/{user_id} {"role":"admin"}`（owner） | 返回 `{"ok": true}` | ✅ |
| A-14 | 非 owner 修改角色 | admin 调用 `PATCH` 修改角色 | 返回 403 | ✅ |
| A-15 | 移除成员 | `DELETE /api/orgs/{org_id}/members/{user_id}`（admin） | 返回 `{"ok": true}` | ✅ |
| A-16 | 移除 owner | `DELETE /api/orgs/{org_id}/members/{owner_user_id}` | 返回 400（owner 不可移除） | ✅ |
| A-17 | 离开组织 | `POST /api/orgs/{org_id}/leave`（member） | 返回 `{"ok": true}` | ✅ |
| A-18 | owner 离开组织 | `POST /api/orgs/{org_id}/leave`（owner） | 返回 400 | ✅ |
| A-19 | 非成员访问组织 | 非组织成员调用 `GET /api/orgs/{org_id}` | 返回 403 | ✅ |
| A-20 | 未登录访问 | 无 token 调用 `GET /api/orgs` | 返回 401 | ✅ |

#### 7.2.3 组织上下文解析验证

| 编号 | 用例 | 验证步骤 | 预期结果 | 状态 |
|------|------|----------|----------|------|
| C-01 | X-Org-ID header | 请求带 `X-Org-ID: <org_id>`（成员） | resolve_org_context 返回该 org_id | ✅ |
| C-02 | X-Org-Slug header | 请求带 `X-Org-Slug: personal-1` | resolve_org_context 返回对应 org_id | ✅ |
| C-03 | ?org_id query | 请求带 `?org_id=<org_id>` | resolve_org_context 返回该 org_id | ✅ |
| C-04 | ?org_slug query | 请求带 `?org_slug=personal-1` | resolve_org_context 返回对应 org_id | ✅ |
| C-05 | 未指定组织 | 不传任何 org 标识 | 回退到用户个人组织 ID（兼容现有行为） | ✅ |
| C-06 | 非成员指定组织 | 用户传 `X-Org-ID` 指定非成员组织 | 返回 403 | ✅ |
| C-07 | 前端 X-Org-ID 注入 | 前端 localStorage 设置 current_org_id 后请求 | 每个请求 header 自动携带 X-Org-ID | ✅ |

#### 7.2.4 数据隔离验证

| 编号 | 用例 | 验证步骤 | 预期结果 | 状态 |
|------|------|----------|----------|------|
| I-01 | 仓库列表隔离 | 用户 A 在组织 A 仓库列表中看不到组织 B 的仓库 | managed_repos 查询加 org_id 过滤 | ⬜ |
| I-02 | 事件隔离 | 组织 A 的事件查询不返回组织 B 的事件 | events 查询加 org_id 过滤 | ⬜ |
| I-03 | Worker 凭据隔离 | 组织 A 的凭据列表不含组织 B 的凭据 | runtime_credentials 查询加 org_id 过滤 | ⬜ |
| I-04 | 引擎隔离 | 组织 A 的引擎列表不含组织 B 的引擎 | agent_runtimes 查询加 org_id 过滤 | ⬜ |
| I-05 | Skill 隔离 | 组织 A 的技能列表不含组织 B 的 user/repo scope 技能 | skill 查询加 org_id 过滤 | ⬜ |
| I-06 | 官方资源不受隔离 | scope='official' 的 Skill 在任何组织中可见 | org_id=0 不加组织过滤 | ⬜ |
| I-07 | 事件入库填充 | Webhook 触发新事件后，events.org_id 正确填充 | 查 managed_repos.org_id 并写入 | ✅ |
| I-08 | 创建仓库绑定 org | 托管新仓库时 managed_repos.org_id 填充当前组织 | 写入时带 org_id | ⬜ |
| I-09 | 跨组织伪造 X-Org-ID | 用户伪造其他组织的 X-Org-ID | 403（resolve_org_context 校验成员关系） | ✅ |

#### 7.2.5 前端 UI 验证

| 编号 | 用例 | 验证步骤 | 预期结果 | 状态 |
|------|------|----------|----------|------|
| U-01 | 侧边栏入口 | 登录后查看侧边栏 | 设置组中有「组织管理」入口 | ✅ |
| U-02 | 组织列表页 | 点击「组织管理」 | 显示用户加入的组织列表（含个人组织徽章） | ✅ |
| U-03 | 创建组织弹窗 | 点击「+ 创建组织」 | 弹出创建表单（名称/slug/描述） | ✅ |
| U-04 | 组织详情页 | 点击组织卡片 | 显示组织信息 + 成员列表 + 切换按钮 | ✅ |
| U-05 | 添加成员弹窗 | owner/admin 点击「+ 添加成员」 | 弹出添加表单（用户 ID + 角色选择） | ✅ |
| U-06 | 移除成员确认 | owner 点击「移除」 | 弹出确认弹窗 | ✅ |
| U-07 | 切换组织 | 点击「切换到此组织」 | localStorage 写入 current_org_id，页面刷新 | ✅ |
| U-08 | API 拦截器 | 切换组织后所有请求 | 请求 header 自动携带 X-Org-ID | ✅ |
| U-09 | 无仓库可访问 | 未选择仓库时点击组织管理 | 正常显示（在 no-repo allowlist 中） | ✅ |
| U-10 | 未登录路由 | 未登录时访问组织管理 | 重定向到登录页 | ✅ |

#### 7.2.6 向后兼容验证

| 编号 | 用例 | 验证步骤 | 预期结果 | 状态 |
|------|------|----------|----------|------|
| B-01 | 现有用户零感知 | 现有用户登录，不传 X-Org-ID | 所有 API 行为与改造前一致（回退到个人组织） | ✅ |
| B-02 | 现有 Worker 注册 | Worker 用 rta_ token 注册 | 正常注册（credential.org_id 已迁移） | ⬜ |
| B-03 | 现有 Webhook 事件 | GitCode push Issue 事件 | 事件正常入库，org_id 从 managed_repos 填充 | ✅ |
| B-04 | scope=official 资源 | 查询 Skill 列表 | 官方技能对所有组织可见 | ⬜ |
| B-05 | scope=global 凭据 | 查询凭据列表 | global 凭据对所有组织可见（org_id=0） | ⬜ |
| B-06 | 现有测试用例 | 运行全部已有测试 | 全部通过（无回归） | ✅ |

#### 7.2.7 安全验证

| 编号 | 用例 | 验证步骤 | 预期结果 | 状态 |
|------|------|----------|----------|------|
| S-01 | 伪造 X-Org-ID | 用户传非成员组织的 X-Org-ID | 403 拒绝 | ✅ |
| S-02 | 个人组织防删 | 尝试删除 is_personal=1 的组织 | 400 拒绝 | ✅ |
| S-03 | owner 防移除 | 尝试移除 owner 成员 | 400 拒绝 | ✅ |
| S-04 | tk_ token 限制 | 会话 token 调用组织管理 API | 403（require_no_session_token） | ✅ |
| S-05 | 成员校验 | 所有组织 API 均校验 org_member 成员关系 | 非成员 403 | ✅ |

#### 7.2.8 验证状态说明

| 标记 | 含义 |
|------|------|
| ✅ | 已实现并验证通过 |
| ⬜ | 已设计但尚未实现（P3 现有 API 改造阶段） |
| ❌ | 验证失败（需修复） |

---

## 附录 A：受影响文件清单

### 新增文件

| 文件 | 说明 |
|------|------|
| `src/api/routes/orgs.py` | 组织管理路由 |
| `src/api/org_context.py` | 组织上下文解析与缓存 |
| `src/user/org_db.py` | 组织数据访问层 |

### 修改文件

| 文件 | 改动 |
|------|------|
| `src/api/db_setup.py` | 新增 organization/org_member DDL + org_id 列迁移 |
| `src/api/deps.py` | 新增 `resolve_org_context`/`require_org_member`/`require_org_admin` |
| `src/api/main.py` | 注册 orgs router |
| `src/user/db.py` | `create_user` 自动创建个人组织 |
| `src/api/routes/repos.py` | 查询加 `org_id` 过滤 |
| `src/api/routes/skills.py` | 查询加 `org_id` 过滤 |
| `src/api/routes/scenes.py` | 查询加 `org_id` 过滤 |
| `src/api/routes/runtime.py` | Worker 注册时填充 `org_id` |
| `src/api/routes/agents.py` | 查询加 `org_id` 过滤 |
| `src/api/routes/events.py` | 查询加 `org_id` 过滤 |
| `src/api/routes/tasks.py` | 查询加 `org_id` 过滤 |
| `src/api/routes/chat.py` | 查询加 `org_id` 过滤 |
| `src/api/routes/knowledge.py` | 查询加 `org_id` 过滤 |
| `src/api/event_store.py` | `append_event` 填充 `org_id` |
| `src/api/event_pipeline.py` | 事件处理时传递 `org_id` |
| `src/api/session_store.py` | 会话查询加 `org_id` 过滤 |
| `src/api/static/js/app.js` | 组织切换器组件 |
| `src/api/static/js/api.js` | 请求拦截器添加 `X-Org-ID` |

## 附录 B：Multica 隔离设计引用索引

| Multica 文件 | 内容 | 对应 Grape 设计 |
|-------------|------|----------------|
| `server/migrations/001_init.up.sql` | workspace + member 初始 DDL | §4.2.1/4.2.2 organization + org_member |
| `server/migrations/025_comment_workspace_id.up.sql` | 补加 workspace_id 列 | §4.2.4 核心表加 org_id |
| `server/migrations/032_runtime_owner.up.sql` | runtime 加 owner_id | §4.2.4 agent_runtimes 加 org_id |
| `server/migrations/188_workspace_attribution_fail_closed.up.sql` | 合规策略字段 | §4.2.1 organization.settings |
| `server/internal/middleware/workspace.go` | 中间件解析+校验 | §4.3.2 resolve_org_context |
| `server/internal/auth/membership_cache.go` | Redis 成员缓存 | §4.3.2 内存缓存（后续可引入 Redis） |
| `server/internal/handler/workspace.go` | Workspace CRUD handler | §4.3.1 orgs.py |
| `server/cmd/server/router.go` | 路由注册与中间件挂载 | §4.3.2 现有 API 改造 |
| `server/pkg/db/queries/workspace.sql` | Workspace 查询 | §4.3.1 组织查询 |
| `server/pkg/db/queries/member.sql` | Member 查询 | §4.3.1 成员查询 |
| `server/pkg/db/queries/skill.sql` | Skill 按 workspace 查询 | §4.3.3 skill 查询加 org_id |

---

> 本方案基于 Multica `server/` 目录的实际代码和 Grape `/home/grape/grape-new-version2`（分支 multitask）的实际架构编写。所有引用的表名、字段名、API 路径均来自实际代码。
