# 1. 平台配置总览

> 面向自建 / 私有化部署 Grape 的运维与平台管理员：装好之后**配什么、在哪配、怎么验证**。
> 普通用户用法见 [使用文档](../usage/overview.md)，接口契约见 [API 文档](../api/README.md)。

Grape 的配置分布在四个层面。改对地方、用对优先级，是自建部署排障的第一步。

## 四层配置面全景

| 配置面 | 位置 | 管什么 | 谁可改 |
|--------|------|--------|--------|
| **Admin 控制台（DB meta）** | Admin Console → 全局配置（`PUT /api/admin/config`，写 DB meta 表） | 全局 Bot Token、OAuth 凭据、webhook 地址/密钥（Issue #345 后 DB 为运行时权威） | 管理员（Admin Console） |
| **configs/ 文件** | `configs/*.json`（由 `*.example.json` 复制而来） | 数据库后端、邮件、场景同步、引擎调度、登录白名单、安全项 | 服务器文件（运维） |
| **环境变量** | `grape.env`（systemd `EnvironmentFile=/etc/grape/grape.env`）或部署平台注入 | OBS 对象存储、JWT/Cookie 安全、连接池、SMTP 兜底等 | 服务器文件 / 部署环境 |
| **DB meta 表** | `meta` 表（key-value） | 运行期由服务写入/读取的权威值（OAuth 凭据首次启动自动迁入） | 服务自身 + Admin 控制台 |

## 优先级关系（谁覆盖谁）

1. **数据库后端开关**：环境变量 `DEVOPS_DB_BACKEND` > `configs/db-backend.json` 的 `backend` 字段 > 默认 `sqlite`（见 [数据库与同步](05-database-and-sync.md)）。
2. **场景同步**：环境变量 `GRAPESKILLS_REPO_URL` / `GRAPESKILLS_REPO_BRANCH` / `SCENE_SYNC_INTERVAL` / `GRAPE_DEV_TOKEN` > `configs/scene-sync.json`（见 [数据库与同步](05-database-and-sync.md)）。
3. **全局 Bot / OAuth / webhook**：首次启动把 `configs/global-bot-token.json`、`configs/oauth-providers.json` 迁入 DB meta；之后 **DB 为运行时权威**，改文件不再生效，须走 Admin 控制台（见 [GitCode 集成](01-gitcode-integration.md)、[GitHub 集成](02-github-integration.md)）。
4. **邮件**：`configs/email.json` 为主，`SMTP_*` 系列环境变量为服务端兜底（见 [存储与邮件](04-storage-and-email.md)）。

## 改完怎么生效（重启 vs 热生效）

| 配置项 | 生效方式 |
|--------|---------|
| `configs/email.json`（邮件） | **无需重启**（每次发送时读取） |
| Admin 控制台改 DB meta（Bot Token / webhook 等） | 保存即生效（运行时读 DB） |
| `configs/db-backend.json`、`scene-sync.json`、`engine-selection.json`、`login-whitelist.json`、`security.json` | **重启服务生效** |
| 环境变量（`OBS_*`、`JWT_SECRET`、`COOKIE_SECURE`、`DB_POOL_*` 等） | **重启服务生效** |

## 配置示例：典型的 configs/ 清单

```
configs/
├── db-backend.json          # 复制自 db-backend.example.json
├── email.json               # 复制自 email.example.json
├── scene-sync.json          # 复制自 scene-sync.example.json
├── engine-selection.json    # 复制自 engine-selection.example.json
├── login-whitelist.json     # 复制自 login-whitelist.example.json
├── security.json            # 复制自 security.example.json
├── oauth-providers.json     # 复制自 oauth-providers.example.json（首次启动迁入 DB）
└── global-bot-token.json    # 复制自 global-bot-token.example.json（首次启动迁入 DB）
```

## 如何验证生效

- **重启后服务健康**：`GET /api/health`（或控制台「系统监控」页）返回正常。
- **配置被读取**：服务启动日志会打印关键配置加载信息（如 MySQL 连接池 `DB pool` 相关日志、场景同步 interval）。
- **Admin 控制台配置**：保存后在 Admin Console 重新打开全局配置页，字段回显新值即已写入 DB meta。

> ⚠️ **注意**
>
> 本篇以下各篇（01~06）按集成领域逐项给出「在哪配 → 配置示例 → 如何验证」三件套。敏感值一律用占位符表示（如 `<your-ak>`、`<your-client-secret>`），请替换为你自己的真实值，切勿提交到仓库。
