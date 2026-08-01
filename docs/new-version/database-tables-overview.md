# 系统数据表全景与关系梳理

> 数据文件：`data/sessions.db`（SQLite 单库）
> 采集时间：2026-08-01

---

## 一、表总览

系统共有 **22 张业务表 + 5 张历史遗留表**。

### 按领域分组

| 领域 | 表 | 说明 |
|------|----|------|
| **用户体系** | `user_accounts`, `user_credentials`, `user_vms`, `user_vm_repos`, `user_repos`, `oauth_states` | 用户、凭据、云主机、仓库缓存、OAuth |
| **仓库托管** | `managed_repos`, `user_managed_repos` | 仓库级配置 + 用户-仓库多对多关联 |
| **Agent 配置** | `devops_agent_session`, `agent_type`, `skill`, `agent_registry`, `event_route_config`, `backend_agents` | Agent 会话、类型、技能、注册表、事件路由 |
| **事件流水线** | `events`, `meta` | 事件审计、元数据 |
| **IM / 飞书** | `im_bot_config`, `im_channels`, `user_im_bindings`, `feishu_bindings`, `feishu_users`, `vod_conversations` | 多 Bot、群绑定、用户映射、VOD 会话 |
| **聊天** | `chat_conversations`, `chat_messages`, `chat_config`, `chat_agent_session` | Chat 对话、消息、配置、会话 |
| **历史遗留** | `users`, `bot_accounts`, `pipeline_instances`, `system_config`, `user_sessions` | 旧表/迁移源 |

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
> 已移除的废弃字段：`gitcode_webhook_token`（仓库级→managed_repos）、`gitcode_oauth_*`（全局级→meta 表）、`webhook_url`（全局级→meta 表）、`bot_usernames`（仓库级→managed_repos.bot_username）。
> 详见 `user-accounts-field-cleanup.md`。

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
user_id INTEGER NOT NULL, full_name TEXT NOT NULL,
owner TEXT DEFAULT '', name TEXT DEFAULT '',
description TEXT DEFAULT '', role TEXT DEFAULT '',
is_admin INTEGER DEFAULT 0,
synced_at TEXT DEFAULT (datetime('now')),
PRIMARY KEY (user_id, full_name)
```

#### `oauth_states` — OAuth 授权状态
```sql
state TEXT PRIMARY KEY, provider TEXT NOT NULL,
data_json TEXT DEFAULT '{}', expires REAL NOT NULL
```

### 2.2 仓库托管

#### `managed_repos` — 仓库级配置表（每仓库一行）
```sql
id INTEGER PRIMARY KEY AUTOINCREMENT,
repo_owner TEXT NOT NULL,
repo_name TEXT NOT NULL,
full_name TEXT NOT NULL UNIQUE,        -- owner/repo 全局唯一
webhook_id INTEGER DEFAULT 0,          -- repo 级 GitCode webhook 配置 ID
bot_gitcode_token TEXT DEFAULT '',     -- repo 级 DevOps Bot token（实际操作身份）
gitcode_webhook_token TEXT DEFAULT '', -- repo 级 webhook 验证 token
bot_username TEXT DEFAULT '',          -- repo 级 bot 用户名（self-comment 检测）
created_at TEXT DEFAULT (datetime('now'))
```
> 仓库级配置（webhook/token/bot_username）每仓库只存一份。

#### `user_managed_repos` — 用户-仓库关联表（多对多）
```sql
id INTEGER PRIMARY KEY AUTOINCREMENT,
user_id INTEGER NOT NULL,              -- → user_accounts.id
repo_id INTEGER NOT NULL,              -- → managed_repos.id
is_admin INTEGER DEFAULT 0,            -- 该用户对该仓库的管理员状态
created_at TEXT DEFAULT (datetime('now')),
UNIQUE (user_id, repo_id)
```
> 一个 user 可关联多个 repo，一个 repo 可关联多个 user。
> 设计说明见 `managed-repos-normalization.md`。

### 2.3 Agent 配置

#### `devops_agent_session` — DevOps Agent 运行时配置（原 `agent_sessions`）
```sql
id TEXT PRIMARY KEY,                   -- UUID
repo_key TEXT NOT NULL,                -- owner/repo
agent_type TEXT NOT NULL,              -- vod/issue/coding/qa/sre（repo 级，不含用户级 chat）
backend TEXT DEFAULT 'opencode',
skill TEXT DEFAULT '',
oc_session_id TEXT DEFAULT '',         -- opencode 会话（pipeline 事件用）
repo_gitcode_token TEXT DEFAULT '',    -- repo 级 bot token
status TEXT DEFAULT 'idle',
created_at TEXT DEFAULT (datetime('now')),
last_active_at TEXT DEFAULT (datetime('now')),
im_provider TEXT DEFAULT '',
im_app_id TEXT DEFAULT '',
im_app_secret TEXT DEFAULT '',
vod_bot_app_id TEXT DEFAULT '',        -- VOD 专用：关联 im_bot_config
UNIQUE(repo_key, agent_type)
```
> 2026-08 由 `agent_sessions` 重命名而来，存 DevOps Agent（VOD/Issue/Coding/QA/SRE）的运行时配置。

#### `agent_type` — Agent 类型定义表
```sql
agent_type TEXT PRIMARY KEY,        -- vod/issue/coding/qa/sre
display_name TEXT NOT NULL, description TEXT DEFAULT '',
default_backend TEXT DEFAULT 'opencode', icon TEXT DEFAULT '',
order_num INTEGER DEFAULT 0,        -- >0 为 DevOps Agent（事件路由依据）
subscribe_events TEXT DEFAULT '[]', -- JSON 数组，事件路由
created_at TEXT DEFAULT (datetime('now'))
```

#### `skill` — 通用技能表（官方/仓库/个人）
```sql
id INTEGER PRIMARY KEY AUTOINCREMENT,
skill_name TEXT NOT NULL,           -- 技能名
display_name TEXT DEFAULT '',
description TEXT DEFAULT '',
agent_type TEXT DEFAULT '',          -- 分类：vod/issue/coding/qa/sre/chat
skill_path TEXT DEFAULT '',          -- SKILL.md 相对路径
scope TEXT DEFAULT 'official',       -- official/repo/user
owner_id TEXT DEFAULT '',            -- repo→repo_key；user→user_id
is_active INTEGER DEFAULT 1,
created_at / updated_at,
UNIQUE (skill_name, scope, owner_id)
```
> 可见性：official（所有仓库）/ repo（仅指定仓库）/ user（仅指定用户）。
> SKILL.md 内容存文件系统（官方 `skills/{path}/`、个人 `skills/custom/{user_id}/`、仓库 `skills/repo/{repo_key}/`）。
> 详见 `skill-table-rebuild.md`。

#### `agent_registry` — Agent 引擎注册表
```sql
backend_name TEXT PRIMARY KEY,
display_name TEXT NOT NULL, description TEXT DEFAULT '',
cli_name TEXT DEFAULT '', is_active INTEGER DEFAULT 1,
created_at TEXT DEFAULT (datetime('now'))
```

#### `event_route_config` — 事件路由配置
```sql
id INTEGER PRIMARY KEY AUTOINCREMENT,
repo_key TEXT DEFAULT '', event_type TEXT NOT NULL,
skill TEXT NOT NULL, priority INTEGER DEFAULT 0,
created_at TEXT DEFAULT (datetime('now'))
```

#### `backend_agents` — 后端引擎探测状态
```sql
name TEXT PRIMARY KEY, display_name TEXT DEFAULT '',
installed INTEGER DEFAULT 0, cli_path TEXT DEFAULT '',
version TEXT DEFAULT '', healthy INTEGER DEFAULT 0,
pip_package TEXT DEFAULT '',
updated_at TEXT DEFAULT (datetime('now'))
```

### 2.4 事件流水线

#### `events` — 事件审计
```sql
seq INTEGER PRIMARY KEY AUTOINCREMENT,
repo_key TEXT NOT NULL, event_type TEXT NOT NULL,
source TEXT DEFAULT 'gitcode', issue_id TEXT DEFAULT '',
title TEXT DEFAULT '', summary TEXT DEFAULT '',
actor TEXT DEFAULT '', payload TEXT DEFAULT '{}',
agent_type TEXT DEFAULT '', backend TEXT DEFAULT '',
skill TEXT DEFAULT '', status TEXT DEFAULT 'pending',  -- pending/running/completed/failed
exit_code INTEGER DEFAULT NULL, duration REAL DEFAULT NULL,
agent_logs TEXT DEFAULT '[]',
created_at TEXT DEFAULT (datetime('now')),
started_at TEXT DEFAULT '', completed_at TEXT DEFAULT ''
```

#### `meta` — 键值元数据
```sql
key TEXT PRIMARY KEY, value TEXT NOT NULL
```
> 存储 seeded_keys、ws_feishu_app_id、oauth_gitcode_client_id/secret/redirect_uri 等全局配置。

### 2.5 IM / 飞书

#### `im_bot_config` — 飞书 Bot 凭证（多 Bot）
```sql
bot_app_id TEXT PRIMARY KEY,
bot_app_secret TEXT NOT NULL,
bot_type TEXT NOT NULL,               -- 'chat_bot' / 'vod_bot'
created_at TEXT DEFAULT (datetime('now'))
```
> BotManager 从此表加载所有 Bot 建立 WS 连接。

#### `im_channels` — IM 渠道绑定（通用）
```sql
id TEXT PRIMARY KEY, provider TEXT NOT NULL,
channel_id TEXT NOT NULL, channel_name TEXT DEFAULT '',
repo_key TEXT NOT NULL, source TEXT DEFAULT 'gitcode',
bind_by TEXT DEFAULT '', auto_impl_enabled INTEGER DEFAULT 0,
notify_enabled INTEGER DEFAULT 0, account TEXT DEFAULT 'default',
created_at TEXT DEFAULT (datetime('now')),
UNIQUE (provider, channel_id)
```

#### `user_im_bindings` — 用户 IM 绑定
```sql
id INTEGER PRIMARY KEY AUTOINCREMENT,
user_id INTEGER NOT NULL, provider TEXT NOT NULL,
channel_type TEXT NOT NULL DEFAULT 'direct_message',
channel_id TEXT NOT NULL, enabled INTEGER DEFAULT 1,
created_at TEXT DEFAULT (datetime('now')),
UNIQUE (user_id, provider, channel_type)
```

#### `feishu_bindings` — 飞书群→仓库绑定（Chat Bot）
```sql
chat_id TEXT PRIMARY KEY,              -- 飞书群 ID
repo_key TEXT NOT NULL, source TEXT DEFAULT 'gitcode',
bind_by TEXT DEFAULT '',
account TEXT DEFAULT 'default',        -- 【已迁移】= chat bot app_id
auto_impl_enabled INTEGER DEFAULT 0,
notify_enabled INTEGER DEFAULT 0,
created_at TEXT DEFAULT (datetime('now'))
```

#### `feishu_users` — 飞书用户↔GitCode 用户映射
```sql
repo_key TEXT NOT NULL, gitcode_user TEXT NOT NULL,
feishu_open_id TEXT NOT NULL, feishu_name TEXT DEFAULT '',
user_id INTEGER DEFAULT 0,
updated_at TEXT DEFAULT (datetime('now')),
PRIMARY KEY (repo_key, gitcode_user)
```

#### `vod_conversations` — VOD 群会话
```sql
id INTEGER PRIMARY KEY AUTOINCREMENT,
agent_session_id TEXT NOT NULL,        -- → devops_agent_session.id
feishu_chat_id TEXT NOT NULL,
feishu_chat_name TEXT DEFAULT '',
oc_session_id TEXT DEFAULT '',         -- VOD 独立 opencode 会话
status TEXT DEFAULT 'active',
created_at TEXT DEFAULT (datetime('now')),
last_active_at TEXT DEFAULT (datetime('now')),
UNIQUE(agent_session_id, feishu_chat_id)
```

### 2.6 聊天

#### `chat_conversations` — Chat 对话
```sql
id TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
session_id TEXT DEFAULT '', user_gitcode_token TEXT DEFAULT '',
repo_key TEXT DEFAULT '', im_channel_id TEXT DEFAULT '',
title TEXT DEFAULT '', status TEXT DEFAULT 'active',
created_at TEXT DEFAULT (datetime('now')),
updated_at TEXT DEFAULT (datetime('now'))
```

#### `chat_messages` — Chat 消息
```sql
id INTEGER PRIMARY KEY AUTOINCREMENT,
conversation_id TEXT NOT NULL,         -- → chat_conversations.id
role TEXT NOT NULL, content TEXT NOT NULL,
created_at TEXT DEFAULT (datetime('now'))
```

#### `chat_config` — Chat 配置
```sql
user_id INTEGER PRIMARY KEY,
backend TEXT DEFAULT 'opencode', skill TEXT DEFAULT '',
feishu_app_id TEXT DEFAULT '', feishu_app_secret TEXT DEFAULT '',
updated_at TEXT DEFAULT (datetime('now'))
```

#### `chat_agent_session` — Chat Agent 会话（原 `sessions`）
```sql
id TEXT PRIMARY KEY,
repo_key TEXT UNIQUE, source TEXT, repository TEXT,
status TEXT, event_history TEXT DEFAULT '[]',
oc_session_id TEXT DEFAULT '',
account TEXT DEFAULT 'default',
backend TEXT DEFAULT '',
opencode_session_id TEXT DEFAULT '',
codearts_session_id TEXT DEFAULT '',
created_at TEXT, last_active_at TEXT
```
> 2026-08 由 `sessions` 重命名而来，存 Chat Agent（飞书）的会话历史 + opencode/codearts 会话 ID。

### 2.7 历史遗留表

| 表 | 用途 | 现状 |
|----|------|------|
| `users` | 旧用户表 | 已合并到 `user_accounts`（迁移源） |
| `bot_accounts` | 旧 bot 账户表 | 已合并到 `user_accounts`（迁移源） |
| `pipeline_instances` | 旧 pipeline 实例 | 代码无引用，废弃 |
| `system_config` | 旧系统配置 | 代码无引用，废弃 |
| `user_sessions` | 旧会话 token 表 | 代码无引用，废弃 |

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
│ user_credentials │    │ user_vms     │  │ managed_repos│
│ (user_id,prov)   │    │ (user_id)    │  │ (user_id,    │
└─────────────────┘    └──────────────┘  └──────┬───────┘  │  owner,repo)  │
        │                                        │          └──────────────┘
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
│  user_repos      │  (user_id, full_name)

└──────────────────┘
```

```
                    ┌─────────────────────────────┐
                    │      managed_repos          │   ← bot_username + bot_gitcode_token（仓库级，每 repo 一行）
                    │  (id, owner, repo, full_name UNIQUE)
                    └───────────┬─────────────────┘
                                │ repo_id
                                ▼
                    ┌─────────────────────────────┐
                    │   user_managed_repos        │   ← 多对多关联
                    │  (user_id, repo_id, is_admin)
                    └──────┬──────────────┬───────┘
                           │              │
             user_id FK    │              │
   ┌───────────────────────┘              └─────── 通过 full_name/repo_key 关联（逻辑外键）
   ▼
┌──────────────┐
│ user_accounts│
└──────────────┘

   repo_key / full_name 关联（逻辑外键，非物理 FK）
                                │
   ┌────────────┬───────────────┼───────────────┬──────────────┐
   ▼            ▼               ▼               ▼              ▼
┌─────────┐ ┌──────────┐ ┌─────────────┐ ┌────────────┐ ┌──────────────┐
│devops_agent_session│ │ events   │ │feishu_bindings│ │im_channels │ │feishu_users  │
│(repo_key,   │ │(repo_key)│ │(repo_key)    │ │(repo_key)  │ │(repo_key)    │
│ agent_type) │ └──────────┘ └─────────────┘ └────────────┘ └──────────────┘
└─────┬───────┘
      │
      │ agent_type FK（逻辑）
      ▼
┌──────────────────┐
│ agent_type │  ← subscribe_events 驱动事件路由
│  (agent_type PK)  │
└──────────────────┘
      │
      │
      ▼
┌──────────────────────┐
│   events（审计）       │
│  agent_type 关联      │
└──────────────────────┘
```

```
  飞书 Bot 相关（多 Bot 隔离）
  ┌──────────────────────────┐
  │       im_bot_config      │
  │  bot_app_id (PK)         │
  │  bot_type: chat/vod      │
  └──────┬────────────┬──────┘
         │            │
         │            │ vod_bot_app_id FK
         │            ▼
         │   ┌────────────────────────┐
         │   │  devops_agent_session  │
         │   │  vod_bot_app_id        │
         │   └──────────┬─────────────┘
         │            │ agent_session_id FK
         │            ▼
         │   ┌──────────────────┐
         │   │vod_conversations │
         │   │ (chat_id,        │
         │   │  agent_session)  │
         │   └──────────────────┘
         │
         │ chat bot app_id
         ▼
  ┌──────────────────┐
  │ feishu_bindings  │  (chat_id PK)
  │ account=app_id   │
  └──────────────────┘
```

```
  Chat 对话
  ┌──────────────────────┐
  │  chat_conversations  │  1 ──< N ──┐
  │  id (PK)             │            │ conversation_id FK
  └──────────┬───────────┘            ▼
             │                 ┌──────────────────┐
             │ user_id         │  chat_messages   │
             │                 │  (conversation)  │
             │                 └──────────────────┘
             ▼
  ┌──────────────────────┐
  │     chat_config      │  (user_id PK)
  └──────────────────────┘
```

---

## 四、关键关联说明

### 4.1 实体关系总结

| 关系 | 类型 | 关联字段 |
|------|------|---------|
| user_accounts → user_credentials | 1:N | user_id |
| user_accounts → user_vms | 1:N | user_id |

| user_accounts → user_repos | 1:N | user_id |
| user_accounts → user_managed_repos | 1:N | user_id |
| user_managed_repos → managed_repos | N:1 | repo_id |
| **user_accounts ↔ managed_repos** | **N:M**（通过 user_managed_repos） | user_id + repo_id |
| user_vms → user_vm_repos | 1:N | vm_id |
| managed_repos → devops_agent_session | 1:N | repo_key |
| devops_agent_session → vod_conversations | 1:N | agent_session_id |
| devops_agent_session → agent_type | N:1 | agent_type |
| chat_conversations → chat_messages | 1:N | conversation_id |
| im_bot_config → devops_agent_session (VOD) | 1:N | vod_bot_app_id |
| im_bot_config → feishu_bindings (Chat) | 1:N | account=app_id |

> 注：系统为 SQLite 单库，**物理外键约束（FOREIGN KEY）未强制启用**，所有表间关系为**逻辑外键**（通过应用层查询关联）。

### 4.2 事件路由链路

```
webhook 事件 → routing.get_domain_agent(event_type)
              → agent_type.subscribe_events LIKE '%"event_type"%'
              → 匹配 agent_type（如 issue/coding）
              → devops_agent_session 查 backend + skill
              → append_event() 写 events 表
              → _execute_agent() 执行 opencode
```

### 4.3 多 Bot 链路

```
BotManager.load_from_db()
  → im_bot_config 每行一个 BotContext
  → chat_bot → process_chat_async → feishu_bindings (account=app_id)
  → vod_bot  → process_vod_async  → devops_agent_session (vod_bot_app_id)
                                   → vod_conversations
```

---

## 五、数据规模（服务器实测）

| 表 | 行数 | 表 | 行数 |
|----|------|----|------|
| user_accounts | 5 | events | 165 |
| user_credentials | 2 | devops_agent_session | 12 |
| managed_repos | 5（含 user_id=7/8 重复行，待拆分迁移） | agent_type | 5 |
| user_managed_repos | —（拆分后新建） | skill | 10（官方） |
| user_repos | 41 | agent_registry | 12 |
| feishu_bindings | 2 | event_route_config | 15 |
| feishu_users | 0 | meta | 5 |
| chat_agent_session | 2 | backend_agents | 2 |
| chat_messages | 40 | im_bot_config | 2 |
| chat_config | 1 | vod_conversations | 1 |
| users（遗留） | 2 | bot_accounts（遗留） | 1 |

---

## 六、遗留问题与建议

| 问题 | 说明 | 建议 |
|------|------|------|
| `users`/`bot_accounts` 遗留 | 迁移源表，仍存数据 | 确认迁移完整后可 DROP |
| `pipeline_instances`/`system_config`/`user_sessions` | 代码无引用 | 可安全 DROP |
| **`managed_repos` 冗余冲突** | 仓库级配置存在 `user_id+repo` 维度，多 user 时冗余/不一致 | 按 `managed-repos-normalization.md` 拆分为 managed_repos + user_managed_repos |
| `chat_agent_session.account`/`feishu_bindings.account` 默认值 | 代码已显式传入 app_id/repo_key | 默认值不影响业务 |
| 物理外键未启用 | SQLite 无 FK 强制 | 应用层已通过 db_lock + 逻辑校验保障 |
| ~~user_accounts 废弃字段~~ | webhook_token/oauth_*/webhook_url/bot_usernames | 已移除列，见 `user-accounts-field-cleanup.md`（已完成） |
