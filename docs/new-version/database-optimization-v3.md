# 数据库 IM/会话表 第三轮优化方案

> 目标：IM 相关表通用化（支持飞书/钉钉/微信等），收敛合并冗余表，统一 backend 会话字段。
> 关联文档：`database-tables-overview.md`、`skill-table-rebuild.md`
> 状态：**方案已确认，暂未实施代码修改**

---

## 一、背景

前两轮已完成的优化：
1. `agent_sessions` → `devops_agent_session`（表重命名）
2. `sessions` → `chat_agent_session`（表重命名）
3. `agent_type_config` 拆分为 `agent_type` + `skill`
4. `vod_bot_app_id` → `im_app_id`（devops_agent_session 通用化）

当前 IM/会话相关表仍有以下问题：
- **飞书专用命名**：`feishu_bindings`/`feishu_users`/`feishu_chat_id`/`feishu_app_id` 等，无法扩展其他 IM
- **冗余表**：`im_channels`（与 feishu_bindings 功能重叠）、`user_im_bindings`（无调用）
- **backend 会话字段混乱**：`oc_session_id`/`opencode_session_id`/`codearts_session_id` 不统一
- **chat_config 独立表**：与 chat_agent_session 维度重叠

## 二、修改总览

### 2.1 表重命名（3 张）

| 原表 | 新表 | 变更 |
|------|------|------|
| `feishu_bindings` | `repo_im_group_binding` | 通用 IM 群→仓库绑定 |
| `feishu_users` | `unified_user_mapping` | 通用用户映射，user_id 主键 |
| `im_bot_config` | `global_im_bot_config` | 去 app_secret，全局 IM bot 配置 |

### 2.2 字段统一

**backend 会话字段：**

| 表 | 原字段 | 新字段 | 说明 |
|----|--------|--------|------|
| `devops_agent_session` | `oc_session_id` | `backend_session_id` | 通用 backend 会话（支持 opencode/codearts/cursor/codex） |
| `vod_conversations` | `oc_session_id` | `backend_session_id` | 同上 |
| `chat_agent_session` | `oc_session_id`/`opencode_session_id`/`codearts_session_id` | 合并为 `backend_session_id` | 移到 chat_conversations |

**IM 字段：**

| 表 | 原字段 | 新字段 |
|----|--------|--------|
| `vod_conversations` | `feishu_chat_id`/`feishu_chat_name` | `im_chat_id`/`im_channel_name` + `im_provider` |
| `chat_config`（合并后） | `feishu_app_id`/`feishu_app_secret` | `im_app_id`/`im_app_secret`/`im_provider` |

### 2.3 结构合并

**`chat_config` → 合并进 `chat_agent_session`（user+repo 维度）**

原 `chat_config` 是 user 维度配置（每用户 backend/skill/feishu_app），`chat_agent_session` 是 repo 维度会话。合并后：

```sql
chat_agent_session (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,           -- → user_accounts.id
    repo_key TEXT NOT NULL,             -- 关联仓库（可空=全局配置）
    backend TEXT DEFAULT 'opencode',
    skill TEXT DEFAULT '',
    im_provider TEXT DEFAULT '',        -- feishu/wecom/dingtalk
    im_app_id TEXT DEFAULT '',
    im_app_secret TEXT DEFAULT '',
    event_history TEXT DEFAULT '[]',
    created_at / last_active_at,
    UNIQUE (user_id, repo_key)
)
```

> 合并后 `chat_config` 表废弃，配置统一存 `chat_agent_session`。
> `backend_session_id` **不在此表**——backend 会话属于具体对话（`chat_conversations`），一个 agent 会话对应多个对话，各自有独立会话。

### 2.4 对话关联（新增 agent_session_id）

一个 `chat_agent_session`（user+repo 维度）对应多个 `chat_conversations`（具体对话记录）。
`chat_conversations` 新增 `agent_session_id` 字段，建立对话与 agent 会话的显式关联。

```
chat_agent_session (1) ────< (N) chat_conversations
   id  <────────────────────  agent_session_id
```

### 2.5 废弃死表（2 张）

| 表 | 原因 |
|----|------|
| `im_channels` | 与 `repo_im_group_binding` 功能重叠（都是"群→仓库"绑定） |
| `user_im_bindings` | 无业务调用（死表） |

## 三、目标表结构（最终）

### 3.1 `repo_im_group_binding`（原 feishu_bindings）

```sql
CREATE TABLE repo_im_group_binding (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    im_provider TEXT DEFAULT 'feishu',   -- feishu/wecom/dingtalk
    im_chat_id TEXT NOT NULL,            -- IM 群 ID
    im_channel_name TEXT DEFAULT '',     -- 群名称
    repo_key TEXT NOT NULL,              -- 绑定的仓库
    bind_by TEXT DEFAULT '',             -- 绑定人
    im_app_id TEXT DEFAULT '',           -- 绑定的 IM bot app_id（原 account）
    auto_impl_enabled INTEGER DEFAULT 0,
    notify_enabled INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE (im_provider, im_chat_id, im_app_id)
)
```

### 3.2 `unified_user_mapping`（原 feishu_users）

```sql
CREATE TABLE unified_user_mapping (
    user_id INTEGER PRIMARY KEY,         -- → user_accounts.id
    gitcode_user TEXT DEFAULT '',        -- GitCode 用户名
    feishu_open_id TEXT DEFAULT '',      -- 飞书 open_id
    feishu_name TEXT DEFAULT '',         -- 飞书昵称
    github_login TEXT DEFAULT '',        -- 未来扩展：GitHub
    dingtalk_user_id TEXT DEFAULT '',    -- 未来扩展：钉钉
    updated_at TEXT DEFAULT (datetime('now'))
)
```

> 每用户一行，含多平台账号字段，支持未来扩展。

### 3.3 `global_im_bot_config`（原 im_bot_config）

```sql
CREATE TABLE global_im_bot_config (
    bot_app_id TEXT PRIMARY KEY,         -- IM bot app_id
    bot_type TEXT NOT NULL,              -- 'chat_bot' / 'vod_bot'（未来可扩展）
    im_provider TEXT DEFAULT 'feishu',   -- feishu/wecom/dingtalk
    is_active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now'))
)
```

> 已去掉 `bot_app_secret`（secret 统一存在各自 agent_session 的 `im_app_secret`）。

### 3.4 `devops_agent_session`（字段调整）

```sql
devops_agent_session (
    id TEXT PRIMARY KEY,
    repo_key TEXT NOT NULL,
    agent_type TEXT NOT NULL,            -- vod/issue/coding/qa/sre
    backend TEXT DEFAULT 'opencode',
    skill TEXT DEFAULT '',
    backend_session_id TEXT DEFAULT '',  -- 原 oc_session_id
    repo_gitcode_token TEXT DEFAULT '',
    status TEXT DEFAULT 'idle',
    im_provider TEXT DEFAULT '',
    im_app_id TEXT DEFAULT '',
    im_app_secret TEXT DEFAULT '',
    created_at / last_active_at,
    UNIQUE(repo_key, agent_type)
)
```

### 3.5 `vod_conversations`（字段调整）

```sql
vod_conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_session_id TEXT NOT NULL,      -- → devops_agent_session.id
    im_provider TEXT DEFAULT 'feishu',
    im_chat_id TEXT NOT NULL,            -- 原 feishu_chat_id
    im_channel_name TEXT DEFAULT '',     -- 原 feishu_chat_name
    backend_session_id TEXT DEFAULT '',  -- 原 oc_session_id
    status TEXT DEFAULT 'active',
    created_at / last_active_at,
    UNIQUE(agent_session_id, im_chat_id)
)
```

### 3.6 `chat_agent_session`（合并 chat_config，字段调整）

```sql
chat_agent_session (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,            -- 新增（合并自 chat_config）
    repo_key TEXT NOT NULL,              -- 原 chat_agent_session.repo_key
    backend TEXT DEFAULT 'opencode',     -- 原 chat_config.backend
    skill TEXT DEFAULT '',               -- 原 chat_config.skill
    im_provider TEXT DEFAULT '',         -- 原 chat_config.feishu 相关
    im_app_id TEXT DEFAULT '',
    im_app_secret TEXT DEFAULT '',
    event_history TEXT DEFAULT '[]',
    created_at / last_active_at,
    UNIQUE (user_id, repo_key)
)
```

### 3.7 `chat_conversations`（新增 backend_session_id + agent_session_id）

```sql
chat_conversations (
    id TEXT PRIMARY KEY,
    agent_session_id TEXT DEFAULT '',    -- 新增：→ chat_agent_session.id
    user_id INTEGER NOT NULL,
    backend_session_id TEXT DEFAULT '',  -- 新增（原 session_id，通用 backend 会话）
    user_gitcode_token TEXT DEFAULT '',
    repo_key TEXT DEFAULT '',
    im_channel_id TEXT DEFAULT '',
    title TEXT DEFAULT '',
    status TEXT DEFAULT 'active',
    created_at / updated_at
)
```

> 一个 `chat_agent_session`（user+repo 维度）对应多个 `chat_conversations`（具体对话）。
> `agent_session_id` 建立对话与 agent 会话的显式关联，替代原先依赖 `user_id+repo_key` 的间接关联。

## 四、数据迁移

```sql
-- 1. feishu_bindings → repo_im_group_binding
RENAME TABLE feishu_bindings TO repo_im_group_binding;
ALTER TABLE repo_im_group_binding RENAME COLUMN chat_id TO im_chat_id;
ALTER TABLE repo_im_group_binding RENAME COLUMN account TO im_app_id;
ALTER TABLE repo_im_group_binding ADD COLUMN im_provider TEXT DEFAULT 'feishu';
ALTER TABLE repo_im_group_binding ADD COLUMN im_channel_name TEXT DEFAULT '';

-- 2. feishu_users → unified_user_mapping
RENAME TABLE feishu_users TO unified_user_mapping;
-- 重建主键为 user_id（先临时表去重）

-- 3. im_bot_config → global_im_bot_config
RENAME TABLE im_bot_config TO global_im_bot_config;
ALTER TABLE global_im_bot_config DROP COLUMN bot_app_secret;
ALTER TABLE global_im_bot_config ADD COLUMN im_provider TEXT DEFAULT 'feishu';
ALTER TABLE global_im_bot_config ADD COLUMN is_active INTEGER DEFAULT 1;
-- app_secret 已存在各 devops_agent_session.im_app_secret

-- 4. backend 会话字段
ALTER TABLE devops_agent_session RENAME COLUMN oc_session_id TO backend_session_id;
ALTER TABLE vod_conversations RENAME COLUMN oc_session_id TO backend_session_id;
ALTER TABLE chat_conversations RENAME COLUMN session_id TO backend_session_id;

-- 4.5 chat_conversations 新增 agent_session_id（关联 chat_agent_session）
ALTER TABLE chat_conversations ADD COLUMN agent_session_id TEXT DEFAULT '';
-- 回填：按 user_id + repo_key 关联到 chat_agent_session.id
UPDATE chat_conversations
SET agent_session_id = (
    SELECT cas.id FROM chat_agent_session cas
    WHERE cas.user_id = chat_conversations.user_id
      AND cas.repo_key = chat_conversations.repo_key
    LIMIT 1
);

-- 5. vod_conversations IM 字段
ALTER TABLE vod_conversations RENAME COLUMN feishu_chat_id TO im_chat_id;
ALTER TABLE vod_conversations RENAME COLUMN feishu_chat_name TO im_channel_name;
ALTER TABLE vod_conversations ADD COLUMN im_provider TEXT DEFAULT 'feishu';

-- 6. chat_config 合并进 chat_agent_session
-- 迁移 chat_config.backend/skill/feishu_app_id/feishu_app_secret → chat_agent_session
INSERT INTO chat_agent_session (user_id, repo_key, backend, skill, im_app_id, im_app_secret)
SELECT user_id, '', backend, skill, feishu_app_id, feishu_app_secret FROM chat_config;
DROP TABLE chat_config;

-- 7. 废弃表
DROP TABLE IF EXISTS im_channels;
DROP TABLE IF EXISTS user_im_bindings;
```

## 五、代码改动清单

| 文件 | 改动 |
|------|------|
| `src/session_manager.py` | feishu_bindings/feishu_users/chat_config/chat_agent_session/chat_conversations CRUD 改表名+字段；create_conversation 写入 agent_session_id |
| `src/api/db_setup.py` | DDL 重建 + 迁移逻辑 + 废弃表 DROP |
| `src/im/feishu.py` | 绑定/用户映射/WS 检查改字段 |
| `src/im/vod_handler.py` / `vod_handler_async.py` | im_chat_id/backend_session_id |
| `src/api/routes/chat.py` | chat_config 合并、im_* 字段、agent_session_id 关联 |
| `src/im/handler.py` / `handler_async.py` | chat_agent_session 字段 |
| `src/api/routes/agents.py` | global_im_bot_config 引用、配置读写 |
| `src/user/db.py` | unified_user_mapping 相关、backend_agents scope 支持 |
| `src/api/static/js/pages/agents.js` | 前端字段同步 |
| `src/api/routes/settings.py` / `auth.py` | im_bot_config 引用改 global_im_bot_config；backend-agents 刷新传 scope |

## 六、验证清单

- [ ] 迁移后 5 张表重命名正确，字段更新
- [ ] `chat_config` 数据合并进 `chat_agent_session`，旧表删除
- [ ] `im_channels` / `user_im_bindings` 删除
- [ ] 全局 `oc_session_id` 无残留（全部 backend_session_id）
- [ ] VOD 绑定/WS 启动/仓库归属判断正常（im_chat_id/im_app_id）
- [ ] Chat 配置/会话正常（user+repo 维度）
- [ ] `chat_conversations.agent_session_id` 正确关联 chat_agent_session
- [ ] 前端配置页正常（im_app_id/im_app_secret）
- [ ] 幂等性：重复启动无错误

## 七、风险与回滚

| 风险 | 应对 |
|------|------|
| SQLite RENAME COLUMN 版本限制 | SQLite 3.25+ 支持；旧版本用临时表重建 |
| unified_user_mapping 主键重建数据丢失 | 先临时表去重（user_id 重复保留最新） |
| chat_config 合并维度冲突 | chat_agent_session 用 UNIQUE(user_id, repo_key) 保证唯一 |
| 前端依赖旧字段 | agents.js 同步更新 |
| 迁移中途失败 | 各步 try/except 幂等，旧表保留备份 |
