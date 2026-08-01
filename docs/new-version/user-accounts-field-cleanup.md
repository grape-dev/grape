# user_accounts 字段收敛方案

> 目标：将 user_accounts 表中归属错误的字段迁移到正确位置，彻底移除冗余列。
> 关联文档：`database-tables-overview.md`、`managed-repos-normalization.md`
> 状态：**方案已确认，暂未实施**

---

## 一、问题概述

`user_accounts` 表中混入了 **3 类归属错误的字段**，与用户身份无关：

| 字段 | 真实归属 | 现状 |
|------|---------|------|
| `gitcode_webhook_token` | **仓库级** | 冗余残留在 user_accounts；实际 webhook 验证已用 `managed_repos.gitcode_webhook_token` |
| `gitcode_oauth_client_id` / `gitcode_oauth_client_secret` / `gitcode_oauth_redirect_uri` | **全局级** | 已迁移 meta 表（`oauth_*` key），user_accounts 中残留 |
| `webhook_url` | **全局级**（服务器回调地址） | 每账户冗余复制（服务器实测 5 个账户值完全相同） |

### 证据

1. **webhook 验证**：`get_webhook_token_by_repo`（db.py:861）查 `managed_repos`，不读 user_accounts
2. **OAuth 配置**：`auth.py` / `settings.py` 已改从 `get_meta_value("oauth_gitcode_*")` 读取
3. **webhook_url**：`config.py` 自动检测一次后复制到所有账户；服务器实测 `http://203.0.113.2:8080/webhook/gitcode` 在全部 5 个账户中相同

---

## 二、目标设计

### 2.1 `user_accounts` 收敛后结构

```sql
CREATE TABLE user_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL DEFAULT '',
    gitcode_id TEXT DEFAULT '',           -- GitCode OAuth 用户 ID
    gitcode_api_token TEXT DEFAULT '',    -- 用户个人 GitCode token
    feishu_app_id TEXT DEFAULT '',
    feishu_app_secret TEXT DEFAULT '',
    huawei_cloud_ak TEXT DEFAULT '',
    huawei_cloud_sk TEXT DEFAULT '',
    CODEARTS_CLI_AK TEXT DEFAULT '',
    CODEARTS_CLI_SK TEXT DEFAULT '',
    password TEXT DEFAULT '',
    created_by INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
)
```

**移除的列**：`gitcode_webhook_token`、`gitcode_oauth_client_id`、`gitcode_oauth_client_secret`、`gitcode_oauth_redirect_uri`、`webhook_url`、`bot_usernames`

> `bot_usernames` 一并移除（已在 `managed-repos-normalization.md` 中迁移至 `managed_repos.bot_username`）。

### 2.2 迁移目标位置

| 原字段 | 迁往 | key / 列 |
|--------|------|---------|
| `webhook_url` | `meta` 表 | `webhook_url`（全局 key） |
| `gitcode_webhook_token` | `managed_repos` | 已有列（保持不变） |
| `gitcode_oauth_*` | `meta` 表 | `oauth_gitcode_client_id` 等（已迁移） |
| `bot_usernames` | `managed_repos` | `bot_username`（已迁移） |

---

## 三、代码改动清单

| 文件 | 改动点 |
|------|--------|
| **`src/user/db.py`** | ① DDL 移除 6 列；② `USER_ACCOUNT_FIELDS` 移除对应字段；③ `save_user_account` 移除 bot_usernames 特判；④ init_db 迁移 INSERT 移除对应列；⑤ 新增 meta 迁移逻辑（webhook_url）；⑥ 删除死代码 `resolve_user_account_by_webhook_token` |
| **`src/config.py`** | `_load_accounts` 不再写 webhook_url 到账户，改为检测后写 meta 表 |
| **`src/api/routes/repos.py`** | `webhook_url = acc.get(...)` → `get_meta_value("webhook_url")` |
| **`src/api/routes/settings.py`** | `webhook_url` 读取 → `get_meta_value("webhook_url")` |
| **`src/api/deps.py`** | 删除死代码 `get_webhook_token`（引用已移除字段） |

---

## 四、数据迁移

在 `src/user/db.py` 的 `init_db()` 中新增迁移（幂等，先于 DDL 重建）：

```python
# 1. webhook_url → meta 表（取首个非空值）
c.execute("SELECT webhook_url FROM user_accounts WHERE webhook_url!='' LIMIT 1")
row = c.fetchone()
if row and row[0]:
    INSERT OR REPLACE INTO meta (key, value) VALUES ('webhook_url', row[0])

# 2. OAuth 配置补迁移（default 账户清理时已完成，此处兜底检查）
c.execute("SELECT gitcode_oauth_client_id, gitcode_oauth_client_secret, gitcode_oauth_redirect_uri FROM user_accounts WHERE gitcode_oauth_client_id!='' LIMIT 1")
row = c.fetchone()
if row:
    INSERT OR REPLACE INTO meta VALUES ('oauth_gitcode_client_id', row[0])
    INSERT OR REPLACE INTO meta VALUES ('oauth_gitcode_client_secret', row[1])
    INSERT OR REPLACE INTO meta VALUES ('oauth_gitcode_redirect_uri', row[2])

# 3. 迁移完成后再移除列
```

### SQLite 移除列的实现

SQLite 3.35+ 支持 `ALTER TABLE DROP COLUMN`，但需满足条件。稳妥做法：

```python
# 方案 A：ALTER TABLE DROP COLUMN（SQLite 3.35+）
for col in ["gitcode_webhook_token", "gitcode_oauth_client_id", "gitcode_oauth_client_secret",
            "gitcode_oauth_redirect_uri", "webhook_url", "bot_usernames"]:
    try:
        ALTER TABLE user_accounts DROP COLUMN {col}
    except: pass

# 方案 B（兼容旧 SQLite）：重建表
CREATE TABLE user_accounts_new (...收敛后结构...);
INSERT INTO user_accounts_new (id, username, password_hash, gitcode_id, gitcode_api_token,
    feishu_app_id, feishu_app_secret, huawei_cloud_ak, huawei_cloud_sk,
    CODEARTS_CLI_AK, CODEARTS_CLI_SK, password, created_by, created_at)
SELECT id, username, password_hash, gitcode_id, gitcode_api_token,
    feishu_app_id, feishu_app_secret, huawei_cloud_ak, huawei_cloud_sk,
    CODEARTS_CLI_AK, CODEARTS_CLI_SK, password, created_by, created_at
FROM user_accounts;
DROP TABLE user_accounts;
ALTER TABLE user_accounts_new RENAME TO user_accounts;
```

---

## 五、函数签名调整

### 5.1 `config.py` — `_load_accounts`

```python
# 旧：检测 webhook_url 后 save_user_account(n, a) 写入每个账户
# 新：
def _detect_webhook_url(): ...   # 保持不变

# 检测一次，写入 meta 表
if not get_meta_value("webhook_url"):
    url = _detect_webhook_url()
    if url:
        set_meta_value("webhook_url", url)
```

### 5.2 `repos.py` — 读取 webhook_url

```python
# 旧
acc = user_db.get_user_account(user["username"]) or {}
webhook_url = acc.get("webhook_url", "")
# 新
from src.user.db import get_meta_value
webhook_url = get_meta_value("webhook_url")
```

### 5.3 `settings.py` — 读取 webhook_url

```python
# 旧
"webhook_url": acc.get("webhook_url", "http://localhost:8080/webhook/gitcode"),
# 新
"webhook_url": get_meta_value("webhook_url") or "http://localhost:8080/webhook/gitcode",
```

### 5.4 删除死代码

```python
# src/api/deps.py — 删除
def get_webhook_token(owner: str, repo: str) -> str: ...

# src/user/db.py — 删除
def resolve_user_account_by_webhook_token(token: str) -> str | None: ...
```

---

## 六、实施步骤

1. **`src/user/db.py`**：DDL 收敛 + `USER_ACCOUNT_FIELDS` 收敛 + `save_user_account` 清理 + init_db 迁移（webhook_url/oauth 补迁 + DROP COLUMN）
2. **`src/config.py`**：webhook_url 写入 meta 表
3. **`src/api/routes/repos.py`** / **`settings.py`**：webhook_url 读取改 meta 表
4. **`src/api/deps.py`**：删除死代码
5. **语法检查 + 本地验证**（迁移 + 字段移除）
6. **更新 `database-tables-overview.md`**：user_accounts 表结构收敛
7. **部署 + 服务器验证**（webhook_url 从 meta 读取、OAuth 登录正常、webhook 验证正常）

---

## 七、风险与回滚

| 风险 | 应对 |
|------|------|
| SQLite DROP COLUMN 不支持 | 用方案 B（重建表），先备份 |
| webhook_url 检测失败（无法获取公网 IP） | 保留默认 `http://localhost:8080/webhook/gitcode` 兜底 |
| OAuth 配置迁移遗漏 | 迁移逻辑同时检查 user_accounts 残留值补迁 |
| 前端依赖 get_user_account 返回结构 | 移除的是废弃字段，前端不依赖（settings/profile 已改 meta） |
