# 系统数据表全景与关系梳理（new-version2）

> 数据文件：`data/sessions.db`（SQLite 单库，WAL 模式）
> 采集时间：2026-08-08（new-version2 迁移后，基于实际 PRAGMA 结构）
> 前置：`docs/new-version/database-tables-overview.md`（v3.1 基线）；本文在其基础上反映 M0–M4 全部变更
> 说明：表中列名以**运行时实际结构**为准（DDL 建表后由 `db_setup._migrate_tables()` 迁移改名）

---

## 〇、new-version2 变更摘要（对比 v3.1 基线）

| 变更 | 说明 | 里程碑 |
|------|------|--------|
| `backend_agents` → `agent_runtimes` | 表重命名，数据搬迁；新增 `credential_id/provider/binary/status/last_seen_at` 列 | M0 |
| `backend_id` → `runtime_id` | `devops_agent_session` / `chat_agent_session` | M0 |
| `backend_session_id` → `runtime_session_id` | `devops_agent_session` / `chat_agent_session` / `chat_conversations` / `vod_conversations` | M0 |
| `default_backend_id` → `default_runtime_id` | `agent_type` | M0 |
| `backend` → `runtime` | `chat_agent_session`（TEXT）/ `events`（TEXT） | M0 |
| 新增 `agent_tasks` | 任务队列（确定路由 + 事件档案快照 + 技能交付字段） | M1 |
| 新增 `runtime_credentials` | RuntimeAgent 独立凭据（`rta_` token，D2） | M3 |
| `meta` 新增 `execution_mode` 键 | 执行模式开关 auto/direct/queue_remote（`queue_local` 已废除） | M2 |
| `agent_type` / `devops_agent_session` 新增 `enable` 列 | Agent 启用开关（全局默认 + 仓库级覆盖，sre 默认关） | 后续小版本 |
| SQLite WAL + busy_timeout | 跨进程写安全（`session_manager.py` PRAGMA） | M0 |

> 术语：v3.1 的 `backend_*` 全部更名为 `runtime_*`（除 VCS 层 `VcsBackend/GitCodeBackend` 等，不迁移）。

---

## 一、表总览

系统现有 **25 张业务表 + 若干历史遗留/系统表**。

### 按领域分组

| 领域 | 表 | 说明 |
|------|----|------|
| **用户体系** | `user_accounts`, `user_credentials`, `user_vms`, `user_vm_repos`, `user_repos`, `oauth_states` | 用户、凭据、云主机、仓库缓存、OAuth |
| **仓库托管** | `managed_repos` | 用户-仓库关联 + 仓库级配置 |
| **Agent Runtime 执行** | `agent_runtimes`, `runtime_credentials`, `agent_tasks` | 引擎注册/健康态、凭据、任务队列（**new-version2 新增/重构**） |
| **Agent 配置** | `devops_agent_session`, `agent_type`, `skill`, `event_route_config` | Agent 会话、类型、技能、事件路由 |
| **事件流水线** | `events`, `event_skipped`, `meta` | 事件审计、跳过审计、元数据 |
| **IM / 飞书** | `global_im_bot_config`, `repo_im_group_binding`, `unified_user_mapping`, `vod_conversations` | 全局 bot、群绑定、用户映射、VOD 会话 |
| **聊天** | `chat_conversations`, `chat_messages`, `chat_agent_session` | Chat 对话、消息、配置+会话 |
| **任务中心** | `tasks`, `bot_seen_messages` | 用户任务、bot 已读消息去重 |
| **系统/历史遗留** | `sqlite_sequence`, `users`, `bot_accounts`, `pipeline_instances`, `system_config`, `user_sessions`, `user_managed_repos` | SQLite 自增序列 + 旧表/迁移源 |

---

## 二、各表结构

### 2.1 用户体系

#### `user_accounts` — 用户主表
```sql
id INTEGER PRIMARY KEY AUTOINCREMENT,
username TEXT UNIQUE NOT NULL,        -- 真实用户 / GitCode bot 账号 / 仓库账户
password_hash TEXT NOT NULL DEFAULT '',
gitcode_id TEXT DEFAULT '',           -- GitCode OAuth 用户 ID
gitcode_api_token TEXT DEFAULT '',    -- GitCode API token
nickname TEXT DEFAULT '',             -- GitCode 昵称
feishu_app_id TEXT DEFAULT '',
feishu_app_secret TEXT DEFAULT '',
huawei_cloud_ak TEXT DEFAULT '',
huawei_cloud_sk TEXT DEFAULT '',
CODEARTS_CLI_AK TEXT DEFAULT '',
CODEARTS_CLI_SK TEXT DEFAULT '',
password TEXT DEFAULT '',
created_by INTEGER DEFAULT 0,
created_at TEXT DEFAULT (datetime('now'))
```

#### `user_credentials` — 用户第三方凭据
```sql
user_id INTEGER NOT NULL,
provider TEXT NOT NULL,               -- 'gitcode','feishu','huawei','codearts'
config_json TEXT NOT NULL,
updated_at TEXT DEFAULT (datetime('now')),
PRIMARY KEY (user_id, provider)
```

#### `user_vms` — 云主机
```sql
id INTEGER PRIMARY KEY AUTOINCREMENT,
user_id INTEGER NOT NULL,
name TEXT NOT NULL, host TEXT NOT NULL,
port INTEGER DEFAULT 22, ssh_user TEXT NOT NULL,
ssh_password TEXT NOT NULL, status TEXT DEFAULT '',
created_at TEXT DEFAULT (datetime('now')),
UNIQUE (user_id, name)
```

#### `user_vm_repos` — 云主机关联仓库
```sql
id INTEGER PRIMARY KEY AUTOINCREMENT,
vm_id INTEGER NOT NULL,               -- → user_vms.id
repo_key TEXT NOT NULL, deploy_dir TEXT DEFAULT '',
service_name TEXT DEFAULT '', branch TEXT DEFAULT 'master',
UNIQUE (vm_id, repo_key)
```

#### `user_repos` — 用户仓库缓存（OAuth 拉取）
```sql
user_id INTEGER NOT NULL,
repo_key TEXT NOT NULL,               -- 实际主键含平台前缀
owner TEXT DEFAULT '', name TEXT DEFAULT '',
description TEXT DEFAULT '', role TEXT DEFAULT '',
is_admin INTEGER DEFAULT 0, is_join INTEGER DEFAULT 0,
synced_at TEXT DEFAULT (datetime('now')),
PRIMARY KEY (user_id, repo_key)
```
> 与 v3.1 基线差异：主键/列从 `full_name` → `repo_key`，新增 `is_join`。

#### `oauth_states` — OAuth 授权状态
```sql
state TEXT PRIMARY KEY, provider TEXT NOT NULL,
data_json TEXT DEFAULT '{}', expires REAL NOT NULL
```

### 2.2 仓库托管

#### `managed_repos` — 用户-仓库关联 + 仓库级配置
```sql
repo_key TEXT PRIMARY KEY,            -- 实际主键（含平台前缀，如 gitcode:owner/repo）
repo_owner TEXT NOT NULL,
repo_name TEXT NOT NULL,
full_name TEXT NOT NULL,              -- owner/repo
webhook_id INTEGER DEFAULT 0,         -- repo 级 GitCode webhook 配置 ID
dev_scene TEXT DEFAULT 'default',     -- 开发场景
bot_gitcode_token TEXT DEFAULT '',    -- repo 级 DevOps Bot token（实际操作身份）
gitcode_webhook_token TEXT DEFAULT '',-- repo 级 webhook 验证 token
bot_username TEXT DEFAULT '',         -- repo 级 bot 用户名（self-comment 检测）
created_at TEXT DEFAULT (datetime('now')),
is_admin INTEGER DEFAULT 0
```
> 与 v3.1 基线差异：主键由 `(user_id, repo_owner, repo_name)` 改为 `repo_key`（去 user 维度，仓库级配置唯一）。

### 2.3 Agent Runtime 执行（new-version2 核心）

#### `agent_runtimes` — 引擎注册表（原 `backend_agents`，M0 更名+扩列）
```sql
id INTEGER PRIMARY KEY AUTOINCREMENT,
credential_id INTEGER DEFAULT 0,      -- ★ 绑定 runtime_credentials.id（凭据→设备可追溯）
name TEXT NOT NULL,                   -- 引擎名（小写：opencode/codearts/cursor/hermes）
display_name TEXT DEFAULT '',
provider TEXT DEFAULT '',             -- ★ 引擎家族（对标 multica protocol_family）
binary TEXT DEFAULT '',               -- ★ CLI 可执行名（shutil.which 目标）
installed INTEGER DEFAULT 0,
cli_path TEXT DEFAULT '',
version TEXT DEFAULT '',
healthy INTEGER DEFAULT 0,
pip_package TEXT DEFAULT '',
status TEXT DEFAULT 'offline',        -- ★ online/offline（边端心跳态；本地引擎由 installed/healthy 派生）
last_seen_at TEXT DEFAULT NULL,       -- ★ 心跳时间（边端上报）
scope TEXT DEFAULT 'global',          -- global/repo/user
owner_id TEXT DEFAULT '',
updated_at TEXT DEFAULT (datetime('now')),
UNIQUE (name, scope, owner_id)
```
> **健康态双语义**（见 `concurrency-design.md` / `settings.get_agent_runtimes`）：
> - `credential_id>0`（远端边端）：`status+last_seen_at` 派生四态（online/recently_lost/offline/about_to_gc）
> - `credential_id=0`（本地引擎，迁移而来）：由 `installed/healthy` 派生（就绪=online，未装=offline）

#### `runtime_credentials` — RuntimeAgent 独立凭据（M3，D2）
```sql
id INTEGER PRIMARY KEY AUTOINCREMENT,
token_hash TEXT NOT NULL,             -- sha256(rta_xxx)，不存明文
name TEXT NOT NULL,                   -- 管理端可读名（如 "CI-Worker-01"）
device_name TEXT DEFAULT '',          -- 首次注册时回填
scope TEXT DEFAULT 'global',
owner_id TEXT DEFAULT '',
status TEXT DEFAULT 'active',         -- active/revoked
last_seen_at TEXT DEFAULT NULL,
created_at TEXT DEFAULT (datetime('now')),
UNIQUE (token_hash)
```
> 生成流程：管理端 `POST /api/settings/runtime-credentials` → 返回 `rta_` token（仅展示一次明文）→ 存 sha256。吊销 = `status='revoked'`。
> 认证：`/api/runtime/*` 仅凭据通过（D2 凭据隔离，user token 拒收）。

#### `agent_tasks` — 任务队列（M1，确定路由 + 事件档案快照）
```sql
id                TEXT PRIMARY KEY,       -- 短 ID（t_ + uuid）
agent_session_id  TEXT NOT NULL,          -- 外键 → devops_agent_session.id（唯一关联字段）
runtime_id        INTEGER NOT NULL,       -- 确定路由 → agent_runtimes.id（从 devops_agent_session.runtime_id 拷贝）
event_seq         INTEGER,                -- 关联 events 表 seq
issue_id          TEXT DEFAULT '',        -- 关联 event.json pipeline 段
repo_key          TEXT DEFAULT '',
agent_type        TEXT DEFAULT '',
skill             TEXT DEFAULT '',
prompt            TEXT NOT NULL,
cwd               TEXT DEFAULT '',
runtime_session_id TEXT DEFAULT '',       -- 会话级共享续接指针（从 devops_agent_session 拷贝，稳定）
event_type        TEXT DEFAULT '',
account           TEXT DEFAULT '',
user_id           INTEGER DEFAULT 0,
repo_gitcode_token TEXT DEFAULT '',
env_extra         TEXT DEFAULT '{}',      -- JSON（含服务端已注入的账号/用户凭据）
skill_files       TEXT DEFAULT '{}',      -- ★ JSON: {skill_name: {rel_path: content}}，远端交付技能包
server_url        TEXT DEFAULT '',        -- ★ A2A 外呼地址，worker 替换 {{SERVER_URL}}
status            TEXT DEFAULT 'queued',  -- queued/dispatched/running/completed/failed/cancelled
exit_code         INTEGER DEFAULT NULL,
result_text       TEXT DEFAULT '',
errors            TEXT DEFAULT '',        -- JSON array
stderr            TEXT DEFAULT '',
usage_json        TEXT DEFAULT '{}',
started_at        TEXT DEFAULT NULL,
completed_at      TEXT DEFAULT NULL,
created_at        TEXT DEFAULT (datetime('now'))
```
> 索引：`idx_agent_tasks_status(status)` / `idx_agent_tasks_runtime(runtime_id)` / `idx_agent_tasks_session(agent_session_id)` / `idx_agent_tasks_event(event_seq)`
> 并发：claim 原子抢单（`UPDATE ... WHERE status='queued' RETURNING`）+ 串行槽 D9（同 session 活跃 task 过滤），见 `concurrency-design.md`。

### 2.4 Agent 配置

#### `devops_agent_session` — DevOps Agent 运行时配置
```sql
id TEXT PRIMARY KEY,                   -- UUID
repo_key TEXT NOT NULL,                -- owner/repo（含平台前缀）
agent_type TEXT NOT NULL,              -- vod/issue/coding/qa/sre（repo 级）
runtime_id INTEGER DEFAULT 0,          -- ★ 引用 agent_runtimes.id（原 backend_id）
enable INTEGER DEFAULT 1,             -- ★ 仓库级启用开关（用户 Dashboard 切换；创建时继承 agent_type.enable）
skill TEXT DEFAULT '',                 -- skill_name（冗余保留，供调试/回退）
skill_id INTEGER DEFAULT 0,            -- 引用 skill.id
runtime_session_id TEXT DEFAULT '',    -- ★ 通用 runtime 会话（原 backend_session_id）
repo_gitcode_token TEXT DEFAULT '',    -- repo 级 bot token
status TEXT DEFAULT 'idle',
im_provider TEXT DEFAULT '',           -- feishu/wecom/dingtalk
im_app_id TEXT DEFAULT '',
im_app_secret TEXT DEFAULT '',
created_at TEXT DEFAULT (datetime('now')),
last_active_at TEXT DEFAULT (datetime('now')),
UNIQUE(repo_key, agent_type)
```
> 确定路由链路：`devops_agent_session.runtime_id` → `agent_runtimes.id`；任务入队时拷贝到 `agent_tasks.runtime_id`（D3）。

#### `agent_type` — Agent 类型定义表
```sql
agent_type TEXT PRIMARY KEY,        -- vod/issue/coding/qa/sre
display_name TEXT NOT NULL, description TEXT DEFAULT '',
default_runtime_id INTEGER DEFAULT 0, -- ★ 引用 agent_runtimes.id（原 default_backend_id）
enable INTEGER DEFAULT 1,           -- ★ Agent 启用开关（全局默认；sre 默认 0）
icon TEXT DEFAULT '',
order_num INTEGER DEFAULT 0,        -- >0 为 DevOps Agent（事件路由依据）
subscribe_events TEXT DEFAULT '[]', -- JSON 数组，事件路由（v2.0 降级为接收白名单+非 Issue 事件兜底+前端展示；主路由改由 issue_state 阶段矩阵，见 vod-skill-upgrade-plan.md §3.0.4）
created_at TEXT DEFAULT (datetime('now'))
```

#### `skill` — 通用技能表（官方/仓库/个人）
```sql
id INTEGER PRIMARY KEY AUTOINCREMENT,
skill_name TEXT NOT NULL,
display_name TEXT DEFAULT '',
description TEXT DEFAULT '',
agent_type TEXT DEFAULT '',          -- vod/issue/coding/qa/sre/chat
skill_path TEXT DEFAULT '',          -- SKILL.md 相对路径
scope TEXT DEFAULT 'official',       -- official/repo/user
owner_id TEXT DEFAULT '',            -- repo→repo_key；user→user_id
is_active INTEGER DEFAULT 1,
created_at / updated_at,
UNIQUE (skill_name, scope, owner_id)
```
> 远端交付：入队时 `resolve_skill_id`/`get_skill_path` 打包技能目录到 `agent_tasks.skill_files`（见 `skill-delivery-remote.md`）。

#### `event_route_config` — 事件路由配置
```sql
id INTEGER PRIMARY KEY AUTOINCREMENT,
repo_key TEXT DEFAULT '', event_type TEXT NOT NULL,
skill TEXT NOT NULL, priority INTEGER DEFAULT 0,
created_at TEXT DEFAULT (datetime('now'))
```

### 2.5 事件流水线

#### `events` — 事件审计
```sql
seq INTEGER PRIMARY KEY AUTOINCREMENT,
repo_key TEXT NOT NULL, event_type TEXT NOT NULL,
source TEXT DEFAULT 'gitcode', issue_id TEXT DEFAULT '',
title TEXT DEFAULT '', summary TEXT DEFAULT '',
actor TEXT DEFAULT '', payload TEXT DEFAULT '{}',
agent_type TEXT DEFAULT '',
runtime TEXT DEFAULT '',               -- ★ 执行引擎名快照（原 backend，审计用，不改 id）
skill TEXT DEFAULT '',
status TEXT DEFAULT 'pending',        -- pending/running/completed/failed
exit_code INTEGER DEFAULT NULL, duration REAL DEFAULT NULL,
agent_logs TEXT DEFAULT '[]',
result TEXT DEFAULT '',               -- 解析后的 Agent 结论文本
created_at TEXT DEFAULT (datetime('now')),
started_at TEXT DEFAULT '', completed_at TEXT DEFAULT ''
```
> 结构化产出：complete 时 `parse_agent_output_json` → `finalize_event_archive`（event.json）+ `update_event_complete`（本表 result）。event.json 为结构化唯一权威，本表存结论文本（见 `agent-runtime-event-full-design.md` §4.4/§7.2）。

#### `event_skipped` — 跳过事件审计
```sql
id INTEGER PRIMARY KEY AUTOINCREMENT,
repo_key TEXT DEFAULT '', event_type TEXT DEFAULT '',
reason TEXT DEFAULT '', detail TEXT DEFAULT '',
created_at TEXT DEFAULT (datetime('now'))
```

#### `meta` — 键值元数据
```sql
key TEXT PRIMARY KEY, value TEXT NOT NULL
```
> 存储：`seeded_keys`、`execution_mode`（★ M2 新增，auto/direct/queue_remote，`queue_local` 已废除）、`oauth_gitcode_*`、`webhook_url` 等全局配置。

### 2.6 IM / 飞书

#### `global_im_bot_config` — 全局 IM Bot 配置
```sql
bot_app_id TEXT PRIMARY KEY,
bot_type TEXT NOT NULL,               -- 'chat_bot' / 'vod_bot'
im_provider TEXT DEFAULT 'feishu',
is_active INTEGER DEFAULT 1,
created_at TEXT DEFAULT (datetime('now'))
```

#### `repo_im_group_binding` — IM 群→仓库绑定
```sql
id INTEGER PRIMARY KEY AUTOINCREMENT,
im_provider TEXT DEFAULT 'feishu',
im_chat_id TEXT NOT NULL,
im_channel_name TEXT DEFAULT '',
repo_key TEXT NOT NULL,
bind_by TEXT DEFAULT '',
im_app_id TEXT DEFAULT '',
source TEXT DEFAULT '',
auto_impl_enabled INTEGER DEFAULT 0,
notify_enabled INTEGER DEFAULT 0,
created_at TEXT DEFAULT (datetime('now')),
UNIQUE (im_provider, im_chat_id, im_app_id)
```

#### `unified_user_mapping` — 通用用户映射
```sql
user_id INTEGER PRIMARY KEY,          -- 始终等于 user_accounts.id
gitcode_user TEXT DEFAULT '',
gitcode_nickname TEXT DEFAULT '',
feishu_open_id TEXT DEFAULT '',
feishu_name TEXT DEFAULT '',
github_login TEXT DEFAULT '',
dingtalk_user_id TEXT DEFAULT '',
updated_at TEXT DEFAULT (datetime('now'))
```

#### `vod_conversations` — VOD 群会话
```sql
id INTEGER PRIMARY KEY AUTOINCREMENT,
agent_session_id TEXT NOT NULL,        -- → devops_agent_session.id
im_provider TEXT DEFAULT 'feishu',
im_chat_id TEXT NOT NULL,
im_channel_name TEXT DEFAULT '',
runtime_session_id TEXT DEFAULT '',    -- ★ 通用 runtime 会话（原 backend_session_id）
status TEXT DEFAULT 'active',
created_at TEXT DEFAULT (datetime('now')),
last_active_at TEXT DEFAULT (datetime('now')),
UNIQUE(agent_session_id, im_chat_id)
```

### 2.7 聊天

#### `chat_conversations` — Chat 对话
```sql
id TEXT PRIMARY KEY,
agent_session_id TEXT DEFAULT '',      -- → chat_agent_session.id
user_id INTEGER NOT NULL,
runtime_session_id TEXT DEFAULT '',    -- ★ 通用 runtime 会话（原 backend_session_id）
user_gitcode_token TEXT DEFAULT '',
repo_key TEXT DEFAULT '',
im_channel_id TEXT DEFAULT '',
title TEXT DEFAULT '',
status TEXT DEFAULT 'active',
created_at TEXT DEFAULT (datetime('now')),
updated_at TEXT DEFAULT (datetime('now')),
session_id TEXT DEFAULT ''             -- 历史遗留列（迁移残留，未使用）
```
> ⚠️ `session_id` 为旧迁移残留列（代码已用 `runtime_session_id`），可清理。

#### `chat_messages` — Chat 消息
```sql
id INTEGER PRIMARY KEY AUTOINCREMENT,
conversation_id TEXT NOT NULL,         -- → chat_conversations.id
role TEXT NOT NULL, content TEXT NOT NULL,
created_at TEXT DEFAULT (datetime('now'))
```

#### `chat_agent_session` — Chat Agent 配置+会话
```sql
id TEXT PRIMARY KEY,
user_id INTEGER DEFAULT 0,
repo_key TEXT DEFAULT '',
source TEXT, repository TEXT,          -- 历史迁移残留
status TEXT,
runtime TEXT DEFAULT 'opencode',       -- ★ runtime 名（原 backend）
skill TEXT DEFAULT '',
skill_id INTEGER DEFAULT 0,
im_provider TEXT DEFAULT '',
im_app_id TEXT DEFAULT '',
im_app_secret TEXT DEFAULT '',
event_history TEXT DEFAULT '[]',
created_at TEXT, last_active_at TEXT,
runtime_session_id TEXT DEFAULT '',    -- ★（原 backend_session_id）
runtime_id INTEGER DEFAULT 0           -- ★ 引用 agent_runtimes.id（原 backend_id）
```
> ⚠️ `source/repository/status` 为旧 `sessions` 迁移残留列，可清理。

### 2.8 任务中心

#### `tasks` — 用户任务
```sql
id INTEGER PRIMARY KEY AUTOINCREMENT,
user_id INTEGER NOT NULL,
assignee TEXT NOT NULL,
repo_key TEXT NOT NULL,
issue_number INTEGER NOT NULL,
title TEXT DEFAULT '',
task_description TEXT DEFAULT '',
conversation_id TEXT DEFAULT '',
status TEXT DEFAULT 'active',
completed_reason TEXT DEFAULT '',
created_at TEXT DEFAULT (datetime('now')),
completed_at TEXT DEFAULT NULL
```

#### `bot_seen_messages` — bot 已读消息去重（WS ack 幂等）
```sql
app_id TEXT NOT NULL,
message_id TEXT NOT NULL,
seen_at REAL NOT NULL,
PRIMARY KEY (app_id, message_id)
```

### 2.9 系统/历史遗留

| 表 | 用途 | 现状 |
|----|------|------|
| `sqlite_sequence` | SQLite 自增序列（AUTOINCREMENT 辅助） | 系统表 |
| `users` | 旧用户表 | 已合并到 `user_accounts`（迁移源） |
| `bot_accounts` | 旧 bot 账户表 | 已合并到 `user_accounts`（迁移源） |
| `pipeline_instances` | 旧 pipeline 实例 | 代码无引用，废弃 |
| `system_config` | 旧系统配置 | 代码无引用，废弃 |
| `user_sessions` | 旧会话 token 表 | 代码无引用，废弃 |
| `user_managed_repos` | 用户-仓库关联（拆分方案） | 设计文档中，代码未实施 |

> 注：`backend_agents`（v3.1 表）已被 `agent_runtimes` 取代并移除；`agent_registry` 更早已废弃。

---

## 三、表关系图

```
                    ┌─────────────────────────────┐
                    │         user_accounts        │
                    │  id (PK) · username (UNIQUE)  │
                    └───┬──────┬──────┬──────┬─────┘
                        │      │      │      │
        user_id FK      │      │      │      │ user_id FK
   ┌────────────────────┘      │      │      └────────────────────┐
   ▼                            │      │                          ▼
┌─────────────────┐    ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
│ user_credentials │    │ user_vms     │  │ managed_repos│  │unified_user_mapping│
│ (user_id,prov)   │    │ (user_id)    │  │ (repo_key PK)│  │ (user_id PK)  │
└─────────────────┘    └──────────────┘  └──────┬───────┘  └──────────────┘
        │                                        │
        │                                vm_id FK │
        │                              ┌──────────┘
        │                              ▼
        │                    ┌──────────────────┐
        │                    │ user_vm_repos    │
        │                    │ (vm_id→vms.id)   │
        │                    └──────────────────┘
        │
        │  user_id FK
        ▼
┌──────────────────┐
│  user_repos      │  (user_id, repo_key)
└──────────────────┘
```

```
   repo_key 关联（逻辑外键，非物理 FK）
                                 │
   ┌────────────┬───────────────┼───────────────┬──────────────┐
   ▼            ▼               ▼               ▼              ▼
┌─────────┐ ┌──────────┐ ┌───────────────┐ ┌────────────┐ ┌──────────────┐
│devops_agent_session│ │ events   │ │repo_im_group_binding│ │vod_conversations │
│(repo_key, │ │(repo_key)│ │(repo_key)    │ │(agent_session_id)│
│ agent_type) │ └──────────┘ └─────────────┘ └────────────┘
└─────┬───────┘
      │
      │ runtime_id FK（★ new-version2）
      ▼
┌──────────────────────────────┐
│      agent_runtimes          │  ← 引擎注册/健康态
│  id (PK) · credential_id     │
└──────┬───────────────┬───────┘
       │               │ runtime_id FK
       │               ▼
       │       ┌──────────────────────────┐
       │       │      agent_tasks         │  ★ 任务队列（M1）
       │       │  agent_session_id FK     │
       │       │  runtime_id FK（确定路由） │
       │       └──────────┬───────────────┘
       │                  │ event_seq
       │                  ▼
       │       ┌──────────────────┐
       │       │   events（审计）  │
       │       └──────────────────┘
       │
       │ credential_id FK
       ▼
┌──────────────────────────┐
│   runtime_credentials    │  ★ RuntimeAgent 凭据（M3，rta_）
│  id (PK) · token_hash    │
└──────────────────────────┘
```

```
  多 IM Bot（全局 bot + secret 分布存储）
  ┌──────────────────────────┐
  │   global_im_bot_config   │
  │  bot_app_id (PK)         │
  └──────┬────────────┬──────┘
         │            │ im_app_id FK（VOD）
         │            ▼
         │   ┌────────────────────────┐
         │   │  devops_agent_session  │
         │   │  im_app_id+secret      │
         │   └──────────┬─────────────┘
         │            │ agent_session_id FK
         │            ▼
         │   ┌──────────────────┐
         │   │vod_conversations │
         │   └──────────────────┘
         │
         │ chat_bot → secret 在 chat_agent_session（全局配置行）
         ▼
  ┌──────────────────────┐
  │   repo_im_group_binding│
  └──────────────────────┘
```

```
  Chat 对话
  ┌──────────────────────┐
  │  chat_agent_session  │  1 ──< N ──┐  agent_session_id FK
  │  (user_id+repo_key)  │            │
  └──────────┬───────────┘            ▼
             │                 ┌──────────────────┐
             │ user_id         │ chat_conversations│
             │                 │ (runtime_session)│
             │                 └────────┬─────────┘
             │                          │ conversation_id FK
             │                          ▼
             │                 ┌──────────────────┐
             │                 │  chat_messages   │
             ▼                 └──────────────────┘
   （配置与会话统一在 chat_agent_session）
```

---

## 四、关键关联说明

### 4.1 实体关系总结

| 关系 | 类型 | 关联字段 |
|------|------|---------|
| user_accounts → user_credentials | 1:N | user_id |
| user_accounts → user_vms | 1:N | user_id |
| user_accounts → user_repos | 1:N | user_id |
| user_accounts → managed_repos | 1:N | user_id（兼容）/ repo_key |
| user_accounts → unified_user_mapping | 1:1 | user_id |
| user_vms → user_vm_repos | 1:N | vm_id |
| managed_repos → devops_agent_session | 1:N | repo_key |
| **agent_runtimes → devops_agent_session** | 1:N | **runtime_id**（★） |
| **agent_runtimes → agent_type** | 1:N | **default_runtime_id**（★） |
| **agent_runtimes → chat_agent_session** | 1:N | **runtime_id**（★） |
| **runtime_credentials → agent_runtimes** | 1:N | **credential_id**（★，凭据→设备） |
| **devops_agent_session → agent_tasks** | 1:N | **agent_session_id**（★，任务） |
| **agent_runtimes → agent_tasks** | 1:N | **runtime_id**（★，确定路由） |
| **events → agent_tasks** | 1:N | **event_seq**（★，事件档案） |
| devops_agent_session → vod_conversations | 1:N | agent_session_id |
| devops_agent_session → agent_type | N:1 | agent_type |
| chat_agent_session → chat_conversations | 1:N | agent_session_id |
| chat_conversations → chat_messages | 1:N | conversation_id |
| global_im_bot_config → devops_agent_session (VOD) | 1:N | im_app_id |
| global_im_bot_config → chat_agent_session (Chat) | 1:N | im_app_id |
| global_im_bot_config → repo_im_group_binding | 1:N | im_app_id |

> 注：系统为 SQLite 单库（WAL），**物理外键约束未强制启用**，所有表间关系为**逻辑外键**（应用层查询关联 + `db_lock` 保障）。

### 4.2 事件路由 + 任务执行链路（new-version2）

```
webhook 事件 → routing.get_domain_agent(event_type)
              → agent_type.subscribe_events LIKE '%"event_type"%'
              → devops_agent_session 查 runtime_id + skill（runtime_id → agent_runtimes 反查引擎名）
              → append_event() 写 events 表（seq）
              → init_event_json() 建 event.json 档案段
              → enqueue_task() 写 agent_tasks（runtime_id=devops_agent_session.runtime_id，确定路由）
                      │
                 execution_mode:
                  ├── direct ──────────► 就地 stream_agent 执行（本机引擎，原逻辑）
                  └── queue_remote ────► RuntimeAgent 边端 claim（/api/runtime/*）→ 执行 → complete
                      │
                 complete → parse_agent_output_json → finalize_event_archive（event.json）
                          → update_event_complete（events 表 result）
```

### 4.3 多 Bot 链路

```
BotManager.load_from_db()
  → global_im_bot_config 每行一个 BotContext
  → secret 来源：
       chat_bot → chat_agent_session.im_app_secret（全局配置行 repo_key=''）
       vod_bot  → devops_agent_session.im_app_secret
  → chat_bot → process_chat_async → repo_im_group_binding (im_app_id)
  → vod_bot  → process_vod_async  → devops_agent_session (im_app_id)
                                   → vod_conversations
```

---

## 五、new-version2 关键设计对照（表 → 设计文档）

| 表/字段 | 对应设计 |
|---------|----------|
| `agent_runtimes`（credential_id/provider/binary/status/last_seen_at） | `agent-runtime-design.md` §5.2 / D7 |
| `runtime_credentials` | `agent-runtime-design.md` §5.1 / D2 |
| `agent_tasks`（skill_files/server_url） | `agent-runtime-event-full-design.md` §4.3 / `skill-delivery-remote.md` |
| `agent_tasks` 原子抢单 + D9 串行槽 | `concurrency-design.md` §2.2/2.3 |
| `events.runtime`（原 backend） | M0 术语重构 |
| WAL / busy_timeout | `concurrency-design.md` §2.1 |
| `meta.execution_mode` | `implementation-plan.md` §三 |

---

## 六、遗留问题与建议（new-version2 视角）

| 问题 | 说明 | 建议 |
|------|------|------|
| `chat_conversations.session_id` 残留列 | 旧迁移残留，代码用 `runtime_session_id` | 可清理 |
| `chat_agent_session.source/repository/status` 残留列 | 旧 `sessions` 迁移残留 | 可清理 |
| `users`/`bot_accounts`/`pipeline_instances`/`system_config`/`user_sessions` | 历史遗留/迁移源 | 确认后 DROP |
| `agent_runtimes` 本地引擎 status 恒 offline | 本地引擎（credential_id=0）无边端心跳，属正常（由 installed/healthy 派生健康态） | 前端已按 installed/healthy 展示"就绪" |
| 物理外键未启用 | SQLite 无 FK 强制 | 应用层 db_lock + 逻辑校验保障 |
| `runtime_credentials` 无 GC | 无 7 天清理（对标 multica GC） | M5 演进项 |
