# 6. 数据库后端与官方同步

## 6.1 数据库后端（SQLite → MySQL 切换）

**在哪配**：`configs/db-backend.json`（复制 `db-backend.example.json`）。开关优先级：环境变量 `DEVOPS_DB_BACKEND` > 该文件 `backend` 字段 > 默认 `sqlite`。重启服务生效。

**配置示例**：

```json
{
  "backend": "mysql",
  "mysql": {
    "host": "127.0.0.1",
    "port": 3306,
    "user": "<your_mysql_user>",
    "password": "<your_mysql_password>",
    "database": "<your_mysql_database>"
  }
}
```

- `"backend": "sqlite"`（或不建该文件）= 默认本地 SQLite。
- `"backend": "mysql"` = 启用 MySQL 兼容层（`src/mysql_compat.py`），连接信息取 `mysql` 段。

**如何验证**：重启后启动日志出现 MySQL 连接池初始化信息（`DB pool (min=…, max_cached=…, max_conn=…)`）且无连接报错；控制台数据（Issue/流水线历史）正常读写。

### 6.1.1 连接池参数（可选）

**在哪配**：环境变量，重启生效。

| 变量 | 默认 | 说明 |
|------|------|------|
| `DB_POOL_MIN` | 2 | 预热连接数 |
| `DB_POOL_MAX_CACHED` | 16 | 最大缓存连接数 |
| `DB_POOL_MAX_CONNECTIONS` | 32 | 最大连接总数 |
| `DB_POOL_DISABLED` | — | 设 `1` 回滚到旧的每线程裸连接模式（灰度安全网） |

**如何验证**：启动日志回显 `DB pool (min=…, max_cached=…, max_conn=…)` 三个值与所配一致。

## 6.2 官方技能 / 场景同步

Grape 定期从官方仓库（grape-skills）同步技能包与场景模板。

**在哪配**：`configs/scene-sync.json`（复制 `scene-sync.example.json`）。环境变量 `GRAPESKILLS_REPO_URL` / `GRAPESKILLS_REPO_BRANCH` / `SCENE_SYNC_INTERVAL` / `GRAPE_DEV_TOKEN` **优先覆盖**该文件。重启生效。

**配置示例**：

```json
{
  "repo_url": "https://github.com/grape-dev/grape-skills",
  "repo_branch": "main",
  "sync_interval": 1800,
  "gitcode_api_token": "<your-gitcode-token-if-private-repo>"
}
```

| 字段 | 说明 |
|------|------|
| `repo_url` | 官方技能仓库地址 |
| `repo_branch` | 同步分支 |
| `sync_interval` | 同步间隔**秒数**，默认 `1800` = **30 分钟一个周期** |
| `gitcode_api_token` | 私有仓库认证 token（也可用环境变量 `GRAPE_DEV_TOKEN`） |

**如何验证**：服务运行 30 分钟内，控制台「技能/场景」列表出现官方新增条目；启动日志含 scene-sync 加载信息且无拉取报错。把 `sync_interval` 临时调小可加快验证节奏（验证完记得改回）。
