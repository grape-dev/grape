# managed_repos 多对多规范化实施方案

> 目标：解决 `managed_repos` 表混合「仓库级配置」与「用户-仓库关系」两类数据导致的冗余冲突问题。
> 关联文档：`database-tables-overview.md`

---

## 一、问题分析

### 1.1 现状表结构

```sql
CREATE TABLE IF NOT EXISTS managed_repos (
    user_id INTEGER NOT NULL,            -- B. 用户维度
    repo_owner TEXT NOT NULL,
    repo_name TEXT NOT NULL,
    full_name TEXT NOT NULL,             -- owner/repo
    webhook_id INTEGER DEFAULT 0,        -- A. 仓库维度（应每 repo 唯一）
    is_admin INTEGER DEFAULT 0,          -- B. 用户维度
    bot_gitcode_token TEXT DEFAULT '',   -- A. 仓库维度（应每 repo 唯一）
    gitcode_webhook_token TEXT DEFAULT '', -- A. 仓库维度（应每 repo 唯一）
    bot_username TEXT DEFAULT '',        -- A. 仓库维度（应每 repo 唯一）
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE (user_id, repo_owner, repo_name)  -- 主键是 用户+仓库
)
```

### 1.2 核心问题

表中混合了**两种不同粒度的数据**：

| 类别 | 字段 | 真实粒度 |
|------|------|---------|
| **A. 仓库级属性** | `webhook_id`、`bot_gitcode_token`、`gitcode_webhook_token`、`bot_username` | 一个 repo 全局只有一份 |
| **B. 用户-仓库关系** | `user_id`、`is_admin`、`created_at` | 一个 user 对一个 repo 一条 |

**A 类字段存在 B 的 `user_id+repo` 维度下**，导致：

1. **配置冗余**：同一 repo 被 N 个 user 加入，A 类字段就冗余 N 份
2. **数据不一致**：不同 user 行上的 token/webhook 可能不同（服务器实测：`Jason7602/devops-bot` 有 user_id=7 和 user_id=8 两行，一行有 token 一行无）
3. **查询不确定**：`get_bot_token_by_repo`/`get_webhook_token_by_repo` 用 `LIMIT 1` 取任意一行，无法保证取到正确配置
4. **更新语义错误**：`update_managed_repo_bot_token(user_id, ...)` 只更新该 user 的行，但 bot token 是 repo 级共享的

---

## 二、目标设计

### 2.1 表 1：`managed_repos` — 仓库级配置表（每仓库一行）

```sql
CREATE TABLE managed_repos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_owner TEXT NOT NULL,
    repo_name TEXT NOT NULL,
    full_name TEXT NOT NULL UNIQUE,          -- owner/repo 全局唯一
    webhook_id INTEGER DEFAULT 0,            -- repo 级 GitCode webhook 配置
    bot_gitcode_token TEXT DEFAULT '',       -- repo 级 DevOps Bot token（实际操作身份）
    gitcode_webhook_token TEXT DEFAULT '',   -- repo 级 webhook 验证 token
    bot_username TEXT DEFAULT '',            -- repo 级 bot 用户名（self-comment 检测）
    created_at TEXT DEFAULT (datetime('now'))
)
```

### 2.2 表 2：`user_managed_repos` — 用户-仓库关联表（多对多）

```sql
CREATE TABLE user_managed_repos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,                -- → user_accounts.id
    repo_id INTEGER NOT NULL,                -- → managed_repos.id
    is_admin INTEGER DEFAULT 0,              -- 该用户对该仓库的管理员状态
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE (user_id, repo_id)
)
```

### 2.3 关系

```
user_accounts (1) ───< (N) user_managed_repos >─── (1) managed_repos

- 一个 user 可关联多个 repo
- 一个 repo 可关联多个 user
- 仓库级配置（webhook/token/bot_username）只存一份在 managed_repos
```

### 2.4 收益

1. **配置唯一**：repo 的 webhook/token/bot_username 无冗余、无冲突
2. **多对多自然表达**：一 repo 多 user、一 user 多 repo
3. **查询简化**：`get_bot_token_by_repo` 直接命中唯一行，去掉 `LIMIT 1`
4. **更新语义正确**：修改 bot token 等仓库级配置无需关心 user

---

## 三、代码改动清单

| 文件 | 改动点 |
|------|--------|
| **`src/user/db.py`** | ① DDL 重建两表；② `add_managed_repo` → 写 managed_repos（repo 存在则复用 id）+ user_managed_repos（关系）；③ `get_managed_repos(user_id)` → JOIN 两表返回；④ `get_bot_token_by_repo` / `get_webhook_token_by_repo` / `resolve_repo_by_webhook_token` / `get_all_bot_usernames` → 只查 managed_repos（唯一行，去 LIMIT 1）；⑤ `update_managed_repo_*` → 去掉 user_id，按 repo 更新；⑥ `remove_managed_repo` → 删关联 + 无其他 user 关联时删仓库行；⑦ `get_all_managed_repos` → JOIN |
| **`src/api/routes/repos.py`** | join/host/remove 端点适配新 `add_managed_repo` / `remove_managed_repo` 签名 |
| **`src/api/routes/settings.py`** | gitcode-repo 配置：GET 通过 `get_managed_repos(user_id)` JOIN 结果读；PUT 更新仓库级字段（去 user_id 语义） |
| **`src/user/repo_manager.py`** | `_get_managed_webhook` → 按 `full_name` 查 webhook_id（去掉 user_id） |
| **`src/api/deps.py`** | 无改动（`get_managed_repos` 返回结构保持兼容） |
| **`src/im/handler_async.py`** / **`handler.py`** | 无改动（用 `get_managed_repos`） |

---

## 四、数据迁移

在 `src/user/db.py` 的 `init_db()` 中新增迁移逻辑（幂等）：

```python
# 1. 检测旧表结构（有 user_id 列且无 id 主键列 = 旧结构）
c.execute("PRAGMA table_info(managed_repos)")
cols = [r[1] for r in c.fetchall()]
if "user_id" in cols and "id" not in cols:
    # 2. 重命名旧表
    ALTER TABLE managed_repos RENAME TO managed_repos_legacy

    # 3. 创建新 managed_repos（仓库级，每 repo 一行）
    CREATE TABLE managed_repos (...新结构...)

    # 4. 去重填充：每个 full_name 保留配置最全的一行
    INSERT INTO managed_repos (repo_owner, repo_name, full_name, webhook_id,
                               bot_gitcode_token, gitcode_webhook_token, bot_username, created_at)
    SELECT repo_owner, repo_name, full_name,
           MAX(webhook_id), MAX(bot_gitcode_token), MAX(gitcode_webhook_token),
           MAX(bot_username), MIN(created_at)
    FROM managed_repos_legacy GROUP BY full_name

    # 5. 创建关联表
    CREATE TABLE user_managed_repos (...结构...)

    # 6. 填充关联表
    INSERT INTO user_managed_repos (user_id, repo_id, is_admin, created_at)
    SELECT l.user_id, m.id, l.is_admin, l.created_at
    FROM managed_repos_legacy l
    JOIN managed_repos m ON l.full_name = m.full_name

    # 7. 删除旧表
    DROP TABLE managed_repos_legacy
```

> 注意：`MAX(bot_gitcode_token)` 等聚合在 SQLite 对空串返回空串，非空值优先（空串 < 实际值）。如需更精确的"非空优先"合并逻辑，可用 `COALESCE(MAX(NULLIF(col,'')), MAX(col))`。

---

## 五、函数签名调整

### 5.1 `add_managed_repo`（重构）

```python
# 旧：add_managed_repo(user_id, repo_owner, repo_name, full_name, webhook_id, is_admin, bot_gitcode_token, gitcode_webhook_token, bot_username)
# 新：
def add_managed_repo(user_id, repo_owner, repo_name, full_name,
                     webhook_id=0, is_admin=True,
                     bot_gitcode_token="", gitcode_webhook_token="", bot_username="") -> bool:
    """确保仓库行存在（全名唯一），并建立用户关联。仓库级字段仅在创建时写入。"""
    # 1. 查询或创建 managed_repos（按 full_name）
    # 2. 插入 user_managed_repos（user_id, repo_id, is_admin）
```

### 5.2 `get_managed_repos`（重构，保持返回结构兼容）

```python
def get_managed_repos(user_id) -> list[dict]:
    """返回该用户关联的托管仓库（含仓库级配置），返回结构不变"""
    SELECT m.repo_owner, m.repo_name, m.full_name, m.webhook_id,
           um.is_admin, m.bot_gitcode_token, m.gitcode_webhook_token,
           m.bot_username, um.created_at
    FROM user_managed_repos um
    JOIN managed_repos m ON um.repo_id = m.id
    WHERE um.user_id = ?
    ORDER BY m.full_name
```

### 5.3 仓库级查询（去 LIMIT 1）

```python
def get_bot_token_by_repo(repo_owner, repo_name) -> str:
    SELECT bot_gitcode_token FROM managed_repos WHERE repo_owner=? AND repo_name=?

def get_webhook_token_by_repo(full_name) -> str:
    SELECT gitcode_webhook_token FROM managed_repos WHERE full_name=?
```

### 5.4 更新函数（去 user_id，按 repo 更新）

```python
def update_managed_repo_bot_token(repo_owner, repo_name, bot_token) -> bool:
    UPDATE managed_repos SET bot_gitcode_token=? WHERE repo_owner=? AND repo_name=?

def update_managed_repo_bot_username(repo_owner, repo_name, bot_username) -> bool:
    UPDATE managed_repos SET bot_username=? WHERE repo_owner=? AND repo_name=?

def update_managed_repo_webhook_token(repo_owner, repo_name, webhook_token) -> bool:
    UPDATE managed_repos SET gitcode_webhook_token=? WHERE repo_owner=? AND repo_name=?
```

> 关联表 `user_managed_repos` 单独提供 `update_user_repo_admin(user_id, repo_id, is_admin)`。

### 5.5 `remove_managed_repo`（重构）

```python
def remove_managed_repo(user_id, repo_owner, repo_name) -> dict | None:
    """删除用户关联；若该 repo 无其他 user 关联，则同时删除仓库行。返回被删仓库信息。"""
    # 1. 查 repo_id
    # 2. DELETE user_managed_repos WHERE user_id=? AND repo_id=?
    # 3. 若 managed_repos 中该 repo 无剩余关联 → DELETE managed_repos
```

---

## 六、端点适配

### 6.1 `src/api/routes/repos.py`

| 端点 | 改动 |
|------|------|
| `POST /join` | 调 `add_managed_repo`（创建仓库行 + 关联，is_admin=False） |
| `POST /host` | 调 `add_managed_repo`（仓库行已存在则复用，更新 webhook_id/token，is_admin=True） |
| `POST /remove` | 调 `remove_managed_repo`（删关联，无其他 user 时删仓库行） |
| `PUT /bot-token` | 调 `update_managed_repo_bot_token`（去 user_id） |

### 6.2 `src/api/routes/settings.py`

| 端点 | 改动 |
|------|------|
| `GET /gitcode-repo/{owner}/{repo}` | 从 `get_managed_repos(user_id)` JOIN 结果读 webhook_token/bot_username |
| `PUT /gitcode-repo/{owner}/{repo}` | 调 `update_managed_repo_*`（去 user_id，按 repo 更新） |

---

## 七、实施步骤

1. **`src/user/db.py`**：DDL 重建 + init_db 迁移逻辑 + 函数重构（5.1-5.5）
2. **`src/api/routes/repos.py`**：端点适配
3. **`src/api/routes/settings.py`**：端点适配
4. **`src/user/repo_manager.py`**：`_get_managed_webhook` 按 full_name 查
5. **语法检查 + 本地验证迁移**（确认旧表→两表数据正确）
6. **更新 `database-tables-overview.md`**：替换 managed_repos 表结构，新增 user_managed_repos
7. **部署 + 服务器验证**（数据迁移 + join/host/token 流程回归）

---

## 八、风险与回滚

| 风险 | 应对 |
|------|------|
| 迁移时旧表结构探测失败 | 用 `PRAGMA table_info` 检测，幂等保护（异常捕获） |
| 前端依赖 get_managed_repos 返回结构 | 保持返回字段不变（full_name/is_admin/webhook_id/bot_gitcode_token/...） |
| 并发 join 同一 repo | `INSERT OR IGNORE` managed_repos（full_name UNIQUE）保证幂等 |
| 迁移中途失败 | 旧表重命名为 `_legacy` 保留，新表建好后 DROP，失败可回查 |
