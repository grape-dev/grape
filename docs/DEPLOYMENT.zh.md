# 上线部署

两条路径共用的上线步骤：启动形态、数据库、配置清单。

## 启动方式

上线前先确定运行方式：生产为**双进程**（主 API 3000 + Webhook 8080），开发为**单端口热加载**。

```bash
python -m src.api.run          # 生产：主 API 3000 + Webhook 8080
python -m src.api.run --dev    # 开发：单端口 3000，热加载
```

nginx 反向代理 / systemd / Docker 等生产部署示例随代码放在 `deploy/` 下：nginx 配置（`deploy/nginx-grape*.conf`）、systemd 单元（`deploy/systemd/`）、编排文件（`deploy/docker-compose.yml`）；文档地图见 `docs/DOCUMENTATION-GUIDE.md`，告警规则在 `deploy/alerts.yml`。

## 数据库

**默认 SQLite，什么都不用装** —— 首次启动自动建表，库文件是 `data/sessions.db`（`DEVOPS_DB_PATH` 可改路径）；Docker 部署时它落在 `grape-data` 卷里，`docker compose down -v` 会把卷一起删掉，状态全丢。

后端取值顺序：环境变量 `DEVOPS_DB_BACKEND` > `configs/db-backend.json` 的 `backend` 字段 > 默认 `sqlite`（重启生效）。

生产建议切 **MySQL 8.x**，三步：

```bash
# 1. 驱动（已随 requirements.txt 安装，可跳过；单装）
pip install pymysql DBUtils

# 2. 建库 —— 程序只管建表，不管建库；字符集必须 utf8mb4，否则中文与 emoji 乱码
mysql -h<host> -u<user> -p -e "CREATE DATABASE IF NOT EXISTS grape_db CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;"
```

```json
// 3. configs/db-backend.json（复制自 db-backend.example.json）
{
  "backend": "mysql",
  "mysql": {
    "host": "<db-host>",
    "port": 3306,
    "user": "<user>",
    "password": "<password>",
    "database": "grape_db"
  }
}
```

环境变量 `DEVOPS_DB_HOST` / `DEVOPS_DB_PORT` / `DEVOPS_DB_USER` / `DEVOPS_DB_PASSWORD` / `DEVOPS_DB_NAME` 优先于该文件（容器与集群推荐走环境变量），也可用一条连接串 `DEVOPS_DB_URL=mysql://<user>:<password>@<host>:3306/grape_db`；未配的字段有默认值：host `127.0.0.1`、port `3306`、user `grape`、database `grape_db`。

**不用手工建表，也不用跑迁移**：启动时 `CREATE TABLE IF NOT EXISTS` 建表 + 幂等 `ALTER` 迁移；SQLite 会在迁移前把旧库备份到 `data/backups/`（默认保留最近 5 份，`DB_BACKUP_KEEP` 可调）。验证：`curl -fsS http://127.0.0.1:3000/api/health`，启动日志出现 `MySQLCompatDB: connection pool initialized (min=2, max_cached=16, max_conn=32)`。

<details markdown="1">
<summary>生产细节与常见故障</summary>

- 数据库账号需要该库的 `CREATE` / `ALTER` / `INDEX` 权限 —— 建表与迁移都在启动时自动做。
- 连接池参数用环境变量调：`DB_POOL_MIN`(2) / `DB_POOL_MAX_CACHED`(16) / `DB_POOL_MAX_CONNECTIONS`(32)；出问题可设 `DB_POOL_DISABLED=1` 回到每线程裸连接。
- MySQL 8 默认 `caching_sha2_password`：`pymysql` 配合 `cryptography`（随 `python-jose[cryptography]` 已在依赖里）即可握手；若仍失败，改用 `mysql_native_password` 建账号。
- 管理台的精选排序另读一个 admin 库（默认 `grape_admin_db`，`ADMIN_DB_*` 可覆盖）：连不上会静默降级，不影响主流程。
- `deploy/docker-compose.yml` 只起应用、**不含 MySQL 服务**；Docker + MySQL 要把配置指向外部实例（`./configs` 已挂载进容器，改完重启容器生效）。
- 切换后端不会自动搬数据：SQLite 上的历史数据要自行迁移（仓库只带 `scripts/export_mysql_to_sqlite.py` 这条 MySQL → SQLite 的反向导出）。
- **容器 overlayfs 兼容（Issue #3588）**：部分容器存储驱动（overlayfs copy-up）会以「删除旧文件 + 新建同名文件」替换 SQLite 的 WAL/SHM 文件（`sessions.db-wal` / `sessions.db-shm`），进程持有的连接随即失效并持续报 `disk I/O error`、接口 500。grape 已内置运行期自愈：检测失效后自动重建连接并重放 PRAGMA（日志关键词 `event=db_stale_detected` / `event=db_reconnect_ok`，`/healthz` 有 `db_status` / `wal_valid` 信号）。部署侧建议：① 数据目录务必落在**持久卷**（容器内如 `grape-data` 卷，或用 `DEVOPS_DB_PATH` 显式指向卷内路径），避免 WAL/SHM 落进容器可写层；② 若失效仍反复出现，可用 `DEVOPS_DB_JOURNAL_MODE` 改 journal 模式（如 `truncate` / `delete`，默认 `wal` 不变——改动会影响写并发，谨慎评估）。
- 故障对照：`ModuleNotFoundError: pymysql` → 驱动没装；`Access denied` → 账号、授权或库名不对；中文乱码 → 库不是 `utf8mb4`；重启后数据"消失" → 仍在 SQLite 且 `grape-data` 卷被 `down -v` 删了。

</details>

## 配置

> OAuth 凭据、对外基址与机器人凭据的具体填法见 **[CONFIGURATION.zh.md](CONFIGURATION.zh.md)**。

`拷贝` = 不在本仓，首次启动前从同名 `.example.json` 拷入。`同步` = 部署时由同步脚本生成。`随仓` = 本仓版本受控。

| 文件 | 来源 | 说明 |
| --- | --- | --- |
| `configs/server-config.json` | 拷贝 | 本地体验可跳过；对外服务时必配。对外基址（`server_base_url` / `webhook_url`），不配则 OAuth 回调指向 localhost |
| `configs/oauth-providers.json` | 拷贝 | 可选。GitCode / GitHub 的 OAuth 凭据种子（要用第三方登录或托管仓库才需要），启动时导入 DB；之后在管理台「平台凭据」维护。`redirect_uri` 须与 OAuth App 登记的回调地址一致 |
| `configs/global-bot-token.json` | 拷贝 | 全局机器人令牌 + 旧版 OAuth 字段 |
| `configs/login-whitelist.json` | 拷贝 | 可选。GitCode 登录白名单种子，仅在首次启动且表为空时导入一次；之后由管理台「OAuth 登录白名单」维护 |
| `configs/db-backend.json` | 拷贝 | 数据库后端（默认 SQLite，生产建议 MySQL 8.x —— 详见上文「数据库」） |
| `configs/agent-description.json` | 随仓 | Agent 类型种子（事件订阅、默认引擎） |
| `configs/agent-skill-mapping.json` | 随仓 | 研发场景 → Agent × Skill 映射 |
| `configs/engine-selection.json` | 随仓 | 哪个 Agent 用哪个引擎；未配置时不会静默回落 `opencode` |
| `configs/skill-description.json` | 同步 | 技能注册表，由 [grape-skills](https://gitcode.com/grape-dev/grape-skills) 经 `scripts/sync_skills.sh` 生成 |
| `configs/project-templates.json` | 同步 | 项目模板注册表，由 [grape-template](https://gitcode.com/grape-dev/grape-template) 经 `scripts/sync_templates.sh` 生成，模板目录落到 `examples/` |
