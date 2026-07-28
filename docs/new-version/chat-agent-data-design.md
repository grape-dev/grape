# Chat Agent + IM 数据设计方案

## 1. 四张新表

### im_channels — IM 群/频道（仓库级）

```sql
CREATE TABLE IF NOT EXISTS im_channels (
    id              TEXT PRIMARY KEY,
    provider        TEXT NOT NULL,
    channel_id      TEXT NOT NULL,
    channel_name    TEXT DEFAULT '',
    repo_key        TEXT NOT NULL,
    source          TEXT DEFAULT 'gitcode',
    bind_by         TEXT DEFAULT '',
    auto_impl_enabled INTEGER DEFAULT 0,
    notify_enabled  INTEGER DEFAULT 0,
    account         TEXT DEFAULT 'default',
    created_at      TEXT DEFAULT (datetime('now')),
    UNIQUE (provider, channel_id)
)
```

用途：IM 群与仓库的绑定关系

### user_im_bindings — 用户通知通道（用户级）

```sql
CREATE TABLE IF NOT EXISTS user_im_bindings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    provider     TEXT NOT NULL,
    channel_type TEXT NOT NULL DEFAULT 'direct_message',
    channel_id   TEXT NOT NULL,
    enabled      INTEGER DEFAULT 1,
    created_at   TEXT DEFAULT (datetime('now')),
    UNIQUE (user_id, provider, channel_type)
)
```

用途：Agent 通过 user_id 找到用户的 IM 私信通道

### chat_conversations — 对话会话

```sql
CREATE TABLE IF NOT EXISTS chat_conversations (
    id             TEXT PRIMARY KEY,
    user_id        INTEGER NOT NULL,
    repo_key       TEXT DEFAULT '',
    im_channel_id  TEXT DEFAULT '',
    title          TEXT DEFAULT '',
    status         TEXT DEFAULT 'active',
    created_at     TEXT DEFAULT (datetime('now')),
    updated_at     TEXT DEFAULT (datetime('now'))
)
```

### chat_messages — 对话消息

```sql
CREATE TABLE IF NOT EXISTS chat_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    role            TEXT NOT NULL,
    content         TEXT NOT NULL,
    created_at      TEXT DEFAULT (datetime('now'))
)
```

## 2. 表关系

```
user_accounts (id)
  ├──< chat_conversations (user_id)
  │      └──< chat_messages (conversation_id)
  ├──< user_im_bindings (user_id)
  └──  IM bind commands

im_channels (id)
  ├──  repo_key → 仓库事件通知
  └──> chat_conversations (im_channel_id)
```

## 3. Agent 发通知链路

VOD Agent → user_im_bindings（user_id=7）→ channel_id → im/feishu.py → 飞书私信

## 4. 系统对话框流程（不走飞书）

用户在 Chat Agent 页面输入消息 → POST /api/chat/send → 写入 chat_conversations → 写入 chat_messages（role=user）→ 处理 → 写入 chat_messages（role=agent）→ 返回前端

## 5. 迁移

feishu_bindings → im_channels（provider='feishu', channel_id=chat_id）
feishu_users 暂保留
