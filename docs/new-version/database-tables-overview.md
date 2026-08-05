# 系统数据表全景与关系梳理

> 数据文件：`data/sessions.db`（SQLite 单库）
> 采集时间：2026-08-02（v3 优化后）

---

## 一、表总览

系统共有 **20 张业务表 + 6 张历史遗留表**。

### 按领域分组

| 领域 | 表 | 说明 |
|------|----|------|
| **用户体系** | `user_accounts`, `user_credentials`, `user_vms`, `user_vm_repos`, `user_repos`, `oauth_states` | 用户、凭据、云主机、仓库缓存、OAuth |
| **仓库托管** | `managed_repos` | 用户-仓库关联 + 仓库级配置 |
| **Agent 配置** | `devops_agent_session`, `agent_type`, `skill`, `event_route_config`, `backend_agents` | Agent 会话、类型、技能、事件路由、引擎 |
| **事件流水线** | `events`, `meta` | 事件审计、元数据 |
| **IM / 飞书** | `global_im_bot_config`, `repo_im_group_binding`, `unified_user_mapping`, `vod_conversations` | 全局 bot、群绑定、用户映射、VOD 会话 |
| **聊天** | `chat_conversations`, `chat_messages`, `chat_agent_session` | Chat 对话、消息、配置+会话 |
| **历史遗留** | `users`, `bot_accounts`, `pipeline_instances`, `system_config`, `user_sessions`, `user_managed_repos` | 旧表/迁移源/未实施 |

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

#### `managed_repos` — 用户-仓库关联 + 仓库级配置
```sql
user_id INTEGER NOT NULL,             -- → user_accounts.id
repo_owner TEXT NOT NULL,
repo_name TEXT NOT NULL,
full_name TEXT NOT NULL,              -- owner/repo
webhook_id INTEGER DEFAULT 0,         -- repo 级 GitCode webhook 配置 ID
is_admin INTEGER DEFAULT 0,           -- 该用户对该仓库的管理员状态
bot_gitcode_token TEXT DEFAULT '',    -- repo 级 DevOps Bot token（实际操作身份）
gitcode_webhook_token TEXT DEFAULT '',-- repo 级 webhook 验证 token
bot_username TEXT DEFAULT '',         -- repo 级 bot 用户名（self-comment 检测）
created_at TEXT DEFAULT (datetime('now')),
UNIQUE (user_id, repo_owner, repo_name)
```
> 仓库级配置（webhook/token/bot_username）当前存在 `user_id+repo` 维度，多 user 加入同一 repo 时冗余。
> 拆分方案见 `managed-repos-normalization.md`（拟拆为 managed_repos + user_managed_repos）。

### 2.3 Agent 配置

#### `devops_agent_session` — DevOps Agent 运行时配置
```sql
id TEXT PRIMARY KEY,                   -- UUID
repo_key TEXT NOT NULL,                -- owner/repo
agent_type TEXT NOT NULL,              -- vod/issue/coding/qa/sre（repo 级，不含用户级 chat）
backend_id INTEGER DEFAULT 0,          -- 引用 backend_agents.id（默认 opencode）
skill TEXT DEFAULT '',                  -- skill_name（冗余保留，供调试/回退）
skill_id INTEGER DEFAULT 0,             -- 引用 skill.id（skill 主键，prompt 构建经 skill_id 查 skill_name/skill_path）
backend_session_id TEXT DEFAULT '',    -- 通用 backend 会话（支持 opencode/codearts/cursor/codex）
repo_gitcode_token TEXT DEFAULT '',    -- repo 级 bot token
status TEXT DEFAULT 'idle',
im_provider TEXT DEFAULT '',           -- IM 平台：feishu/wecom/dingtalk
im_app_id TEXT DEFAULT '',             -- 关联的 IM bot app_id
im_app_secret TEXT DEFAULT '',         -- IM bot app_secret
created_at TEXT DEFAULT (datetime('now')),
last_active_at TEXT DEFAULT (datetime('now')),
UNIQUE(repo_key, agent_type)
```
> 由 `agent_sessions` 重命名而来。v3 将 `vod_bot_app_id` 通用化为 `im_app_id/im_app_secret/im_provider`。
> v3.1 将 `backend`（字符串）改为 `backend_id`，引用 `backend_agents.id`（可区分同名不同 scope 的 backend）。

#### `agent_type` — Agent 类型定义表
```sql
agent_type TEXT PRIMARY KEY,        -- vod/issue/coding/qa/sre
display_name TEXT NOT NULL, description TEXT DEFAULT '',
default_backend_id INTEGER DEFAULT 0, -- 引用 backend_agents.id（默认 opencode），v3.1 由 default_backend 改名
icon TEXT DEFAULT '',
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

#### ~~`agent_registry`~~ — Agent 引擎注册表（已废弃，v3.1 删除）
> 原存 `backend_name`（主键）+ `cli_name`，与 `backend_agents` 职责重叠。
> v3.1 废弃删除，统一用 `backend_agents`（含 scope/owner_id，id 为主键）。

#### `event_route_config` — 事件路由配置
```sql
id INTEGER PRIMARY KEY AUTOINCREMENT,
repo_key TEXT DEFAULT '', event_type TEXT NOT NULL,
skill TEXT NOT NULL, priority INTEGER DEFAULT 0,
created_at TEXT DEFAULT (datetime('now'))
```

#### `backend_agents` — 后端引擎探测状态（backend_id 主数据表）
```sql
id INTEGER PRIMARY KEY AUTOINCREMENT,  -- 独立 id，被各表 backend_id 引用
name TEXT NOT NULL,                    -- 引擎名（小写：opencode/codearts/cursor/hermes）
display_name TEXT DEFAULT '',
installed INTEGER DEFAULT 0, cli_path TEXT DEFAULT '',
version TEXT DEFAULT '', healthy INTEGER DEFAULT 0,
pip_package TEXT DEFAULT '',
scope TEXT DEFAULT 'global',           -- global/repo/user 三级
owner_id TEXT DEFAULT '',              -- scope=repo 存 repo_key；scope=user 存 user_id
updated_at TEXT DEFAULT (datetime('now')),
UNIQUE (name, scope, owner_id)
```
> scope 分级：`global`（系统级引擎探测）、`repo`（仓库级专用引擎配置）、`user`（用户级自定义引擎）。
> v3.1 起成为 backend_id 引用表：`devops_agent_session.backend_id`、`agent_type.default_backend_id`、`chat_agent_session.backend_id` 均引用本表 id。
> 幂等 upsert（按 name+scope+owner_id），跨刷新 id 稳定；name 统一小写存储。

### 2.4 事件流水线

#### `events` — 事件审计
```sql
seq INTEGER PRIMARY KEY AUTOINCREMENT,
repo_key TEXT NOT NULL, event_type TEXT NOT NULL,
source TEXT DEFAULT 'gitcode', issue_id TEXT DEFAULT '',
title TEXT DEFAULT '', summary TEXT DEFAULT '',
actor TEXT DEFAULT '', payload TEXT DEFAULT '{}',
agent_type TEXT DEFAULT '', backend TEXT DEFAULT '',   -- backend 保留字符串快照（执行时引擎名，审计用，不改 id）
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
> 存储 seeded_keys、ws_feishu_app_id、oauth_gitcode_client_id/secret/redirect_uri、webhook_url 等全局配置。

### 2.5 IM / 飞书

#### `global_im_bot_config` — 全局 IM Bot 配置
```sql
bot_app_id TEXT PRIMARY KEY,
bot_type TEXT NOT NULL,               -- 'chat_bot' / 'vod_bot'
im_provider TEXT DEFAULT 'feishu',    -- feishu/wecom/dingtalk
is_active INTEGER DEFAULT 1,
created_at TEXT DEFAULT (datetime('now'))
```
> 由 `im_bot_config` 重命名而来，已去掉 `bot_app_secret`（secret 存各 agent_session.im_app_secret）。
> BotManager 从此表加载所有 Bot 建立 WS 连接。

#### `repo_im_group_binding` — IM 群→仓库绑定
```sql
id INTEGER PRIMARY KEY AUTOINCREMENT,
im_provider TEXT DEFAULT 'feishu',    -- feishu/wecom/dingtalk
im_chat_id TEXT NOT NULL,             -- IM 群 ID（原 chat_id/feishu_chat_id）
im_channel_name TEXT DEFAULT '',      -- 群名称
repo_key TEXT NOT NULL,               -- 绑定的仓库
bind_by TEXT DEFAULT '',              -- 绑定人
im_app_id TEXT DEFAULT '',            -- 绑定的 IM bot app_id（原 account）
auto_impl_enabled INTEGER DEFAULT 0,
notify_enabled INTEGER DEFAULT 0,
created_at TEXT DEFAULT (datetime('now')),
UNIQUE (im_provider, im_chat_id, im_app_id)
```
> 由 `feishu_bindings` 重命名而来，通用化支持多 IM 平台。

#### `unified_user_mapping` — 通用用户映射
```sql
user_id INTEGER PRIMARY KEY,          -- 直接等于 user_accounts.id（无占位 id）
gitcode_user TEXT DEFAULT '',         -- GitCode 用户名
gitcode_nickname TEXT DEFAULT '',     -- GitCode 昵称（来自 user_accounts.nickname）
feishu_open_id TEXT DEFAULT '',       -- 飞书 open_id
feishu_name TEXT DEFAULT '',          -- 飞书昵称
github_login TEXT DEFAULT '',         -- 未来扩展：GitHub
dingtalk_user_id TEXT DEFAULT '',     -- 未来扩展：钉钉
updated_at TEXT DEFAULT (datetime('now'))
```
> 由 `feishu_users` 重建而来，user_id 主键且**始终等于 user_accounts.id**（不生成负数占位 id）。每用户一行，支持多平台账号。绑定仓库时同步刷新 gitcode_nickname / feishu_open_id / feishu_name；若 gitcode_user 未在 user_accounts 中且无已有记录则跳过。

#### `vod_conversations` — VOD 群会话
```sql
id INTEGER PRIMARY KEY AUTOINCREMENT,
agent_session_id TEXT NOT NULL,        -- → devops_agent_session.id
im_provider TEXT DEFAULT 'feishu',
im_chat_id TEXT NOT NULL,              -- IM 群 ID（原 feishu_chat_id）
im_channel_name TEXT DEFAULT '',       -- 群名称（原 feishu_chat_name）
backend_session_id TEXT DEFAULT '',    -- 通用 backend 会话（原 oc_session_id）
status TEXT DEFAULT 'active',
created_at TEXT DEFAULT (datetime('now')),
last_active_at TEXT DEFAULT (datetime('now')),
UNIQUE(agent_session_id, im_chat_id)
```

### 2.6 聊天

#### `chat_conversations` — Chat 对话
```sql
id TEXT PRIMARY KEY,
agent_session_id TEXT DEFAULT '',      -- → chat_agent_session.id
user_id INTEGER NOT NULL,
backend_session_id TEXT DEFAULT '',    -- 通用 backend 会话（原 session_id）
user_gitcode_token TEXT DEFAULT '',
repo_key TEXT DEFAULT '',
im_channel_id TEXT DEFAULT '',
title TEXT DEFAULT '',
status TEXT DEFAULT 'active',
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

#### `chat_agent_session` — Chat Agent 配置+会话（合并 chat_config）
```sql
id TEXT PRIMARY KEY,
user_id INTEGER DEFAULT 0,             -- → user_accounts.id（合并自 chat_config）
repo_key TEXT DEFAULT '',              -- 关联仓库（可空=全局配置）
backend_id INTEGER DEFAULT 0,          -- 引用 backend_agents.id（默认 opencode），v3.1 由 backend 改名
skill TEXT DEFAULT '',                 -- skill_name（冗余保留），合并自 chat_config
skill_id INTEGER DEFAULT 0,            -- 引用 skill.id（skill 主键，prompt 构建经 skill_id 查 skill_name/skill_path）
im_provider TEXT DEFAULT '',           -- feishu/wecom/dingtalk
im_app_id TEXT DEFAULT '',             -- 关联的 IM bot app_id
im_app_secret TEXT DEFAULT '',         -- IM bot app_secret
event_history TEXT DEFAULT '[]',
created_at TEXT, last_active_at TEXT
```
> 由 `sessions` 重命名，v3 合并了 `chat_config`（user 维度配置）。
> `backend_session_id` 不在本表——backend 会话属于具体对话（chat_conversations），一个 agent 会话对应多个对话。

### 2.7 历史遗留表

| 表 | 用途 | 现状 |
|----|------|------|
| `users` | 旧用户表 | 已合并到 `user_accounts`（迁移源） |
| `bot_accounts` | 旧 bot 账户表 | 已合并到 `user_accounts`（迁移源） |
| `pipeline_instances` | 旧 pipeline 实例 | 代码无引用，废弃 |
| `system_config` | 旧系统配置 | 代码无引用，废弃 |
| `user_sessions` | 旧会话 token 表 | 代码无引用，废弃 |
| `user_managed_repos` | 用户-仓库关联（拆分方案） | 设计文档中，代码未实施 |

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
│ (user_id,prov)   │    │ (user_id)    │  │ (user_id,    │  │ (user_id PK)  │
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
│  user_repos      │  (user_id, full_name)
└──────────────────┘
```

```
   repo_key / full_name 关联（逻辑外键，非物理 FK）
                                │
   ┌────────────┬───────────────┼───────────────┬──────────────┐
   ▼            ▼               ▼               ▼              ▼
┌─────────┐ ┌──────────┐ ┌───────────────┐ ┌────────────┐ ┌──────────────┐
│devops_agent_session│ │ events   │ │repo_im_group_binding│ │vod_conversations │
│(repo_key,   │ │(repo_key)│ │(repo_key)    │ │(agent_session_id)│
│ agent_type) │ └──────────┘ └─────────────┘ └────────────┘
└─────┬───────┘
      │
      │ agent_type FK（逻辑）
      ▼
┌──────────────────┐
│ agent_type │  ← subscribe_events 驱动事件路由
│  (agent_type PK)  │
└──────────────────┘
      │
      ▼
┌──────────────────────┐
│   events（审计）       │
│  agent_type 关联      │
└──────────────────────┘
```

```
  多 IM Bot（全局 bot + secret 分布存储）
  ┌──────────────────────────┐
  │   global_im_bot_config   │
  │  bot_app_id (PK)         │
  │  bot_type · im_provider  │
  └──────┬────────────┬──────┘
         │            │
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
         │   │ (im_chat_id,     │
         │   │  agent_session)  │
         │   └──────────────────┘
         │
         │ chat_bot → secret 在 chat_agent_session（全局配置行）
         ▼
  ┌──────────────────────┐
  │   repo_im_group_binding│
  │  (im_chat_id, im_app_id)│
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
             │                 │ (backend_session)│
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
| user_accounts → managed_repos | 1:N | user_id |
| user_accounts → unified_user_mapping | 1:1 | user_id |
| user_vms → user_vm_repos | 1:N | vm_id |
| managed_repos → devops_agent_session | 1:N | repo_key |
| devops_agent_session → vod_conversations | 1:N | agent_session_id |
| devops_agent_session → agent_type | N:1 | agent_type |
| backend_agents → devops_agent_session | 1:N | backend_id |
| backend_agents → agent_type | 1:N | default_backend_id |
| backend_agents → chat_agent_session | 1:N | backend_id |
| chat_agent_session → chat_conversations | 1:N | agent_session_id |
| chat_conversations → chat_messages | 1:N | conversation_id |
| global_im_bot_config → devops_agent_session (VOD) | 1:N | im_app_id |
| global_im_bot_config → chat_agent_session (Chat) | 1:N | im_app_id |
| global_im_bot_config → repo_im_group_binding | 1:N | im_app_id |

> 注：系统为 SQLite 单库，**物理外键约束（FOREIGN KEY）未强制启用**，所有表间关系为**逻辑外键**（通过应用层查询关联）。

### 4.2 事件路由链路

```
webhook 事件 → routing.get_domain_agent(event_type)
              → agent_type.subscribe_events LIKE '%"event_type"%'
              → 匹配 agent_type（如 issue/coding）
              → devops_agent_session 查 backend_id + skill（backend_id → backend_agents 反查引擎名）
              → append_event() 写 events 表
              → _execute_agent() 执行 opencode
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

## 五、数据规模（服务器实测 2026-08-02）

| 表 | 行数 | 表 | 行数 |
|----|------|----|------|
| user_accounts | 5 | events | 165+ |
| user_credentials | 2 | devops_agent_session | 12 |
| managed_repos | 5 | agent_type | 5 |
| user_repos | 41 | skill | 11（10 官方+1 user） |
| global_im_bot_config | 2 | ~agent_registry~ | 已废弃删除（v3.1） |
| repo_im_group_binding | 2 | event_route_config | 15 |
| unified_user_mapping | 0 | meta | 5+ |
| chat_agent_session | 3 | backend_agents | 2（opencode/codearts） |
| chat_messages | 40 | vod_conversations | 1 |
| chat_conversations | 0 | oauth_states | 0 |

---

## 六、遗留问题与建议

| 问题 | 说明 | 建议 |
|------|------|------|
| `users`/`bot_accounts` 遗留 | 迁移源表，仍存数据 | 确认迁移完整后可 DROP |
| `pipeline_instances`/`system_config`/`user_sessions` | 代码无引用 | 可安全 DROP |
| **`managed_repos` 冗余冲突** | 仓库级配置存在 `user_id+repo` 维度，多 user 时冗余/不一致 | 按 `managed-repos-normalization.md` 拆分为 managed_repos + user_managed_repos |
| `repo_im_group_binding` 旧列残留 | 服务器保留 chat_id/account 旧列（RENAME 部分失败） | 清理旧列，统一 im_chat_id/im_app_id |
| ~~`backend_agents` scope 未生效~~ | ~~服务器表还是旧结构（name 主键）~~ | 已重建 id+scope+owner_id（v3.1） |
| `unified_user_mapping` 主键未重建 | 服务器保留 repo_key 复合主键 | 迁移为 user_id 主键 |
| 物理外键未启用 | SQLite 无 FK 强制 | 应用层已通过 db_lock + 逻辑校验保障 |
| ~~user_accounts 废弃字段~~ | webhook_token/oauth_*/webhook_url/bot_usernames | 已移除列，见 `user-accounts-field-cleanup.md`（已完成） |
