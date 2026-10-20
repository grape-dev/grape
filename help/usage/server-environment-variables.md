# 12. 服务端环境变量速查表

> 面向：Grape 服务端部署与运维。列出服务端读取的环境变量、含义、默认值与出处。
> 敏感信息：本文只列**变量名与功能语义**，不含任何真实密钥值；示例一律 `<xxx>` 占位。

---

## 〇、约定

- **默认值**：来自代码（`config.py` 等）的 `os.environ.get("X", <默认>)`，未设置时取其默认。
- **出处**：变量在代码中读取的模块（如 `config.py`、`auth/providers.py`），供排查定位。
- **注入方式**：systemd `Environment=`、`configs/security.json`（安全类）、`configs/*.json`、根目录 `.env`（`src/api/run.py` 用 `os.environ.setdefault` 加载，**不覆盖已存在的环境变量**）。
- 完整清单以代码为准；本文覆盖 `src/` 全部 `os.environ.get/getenv` 读取点（审计于 `2026-10-20`）。

---

## 一、核心安全类

| 环境变量 | 作用 | 默认 | 出处 |
|----------|------|------|------|
| `JWT_SECRET` | JWT 签名密钥（HS256）。未配置时生产拒绝启动（fail-closed）；开发/测试回退进程内随机密钥 | 空 | `api/jwt_utils.py` |
| `COOKIE_SECURE` | refresh_token cookie 的 Secure 标志。生产 HTTPS 必须 `true`；开发 HTTP 配 `false` | `true` | `config.py`、`api/main.py` |
| `TRUSTED_HOSTS` | Host 白名单（逗号分隔）。OAuth redirect_uri fallback 时校验 Host header，防开放重定向；不在白名单回退首个可信 Host | 空（不校验） | `auth/providers.py` |
| `TRUSTED_PROXY_IPS` | 可信反向代理 IP（用于客户端来源判定） | 空 | `api/routes/grape_auth.py` |
| `GRAPE_DEV_VERIFICATION_CODE` | 开发/测试固定验证码（非生产可用）；生产启用会绕过真实邮件验证码 | 空 | `config.py`、`auth/email_service.py` |

> 生产安全门禁（`api/main.py`）按 `configs/security.json` 的 `env` 字段判定：仅字面 `production` 触发全量检查（JWT 强密钥 + 密钥轮换池 + 邮件 + 凭据加密 + COOKIE_SECURE + OAuth redirect_uri）。`JWT_SECRET` 与 `security.json` 相互独立：前者决定签名密钥是否稳定，后者决定门禁开关。

## 二、邮件服务（生产门禁必需）

| 环境变量 | 作用 | 默认 | 出处 |
|----------|------|------|------|
| `SMTP_HOST` | SMTP 服务器地址（与 `configs/email.json` 二选一） | 空 | `config.py`、`api/main.py` |
| `SMTP_PORT` | SMTP 端口 | `25` | `config.py` |
| `SMTP_USERNAME` | SMTP 认证用户名 | 空 | `config.py` |
| `SMTP_PASSWORD` | SMTP 认证密码 | 空 | `config.py` |
| `SMTP_TLS` | SMTP 是否启用 TLS | `true` | `config.py` |
| `SMTP_FROM_EMAIL` | 发件人地址（默认见 `config.py`） | 代码默认 | `config.py` |
| `RESEND_API_KEY` | Resend 邮件 API Key（与 SMTP 二选一） | 空 | `config.py`、`api/main.py` |

## 三、数据库

| 环境变量 | 作用 | 默认 | 出处 |
|----------|------|------|------|
| `DEVOPS_DB_BACKEND` | 后端类型 `sqlite` / `mysql` | 读 `configs/db-backend.json` | `mysql_compat.py` |
| `DEVOPS_DB_URL` | MySQL 连接 URL（可含用户名密码） | 空 | `mysql_compat.py` |
| `DEVOPS_DB_HOST` / `PORT` / `USER` / `PASSWORD` / `NAME` | MySQL 连接拆分参数 | 从配置文件取默认 | `mysql_compat.py` |
| `DEVOPS_DB_PATH` | SQLite 库路径 | `data/sessions.db` | `config.py` |
| `DB_POOL_MIN` / `MAX_CACHED` / `MAX_CONNECTIONS` | DB 连接池参数 | `2`/`16`/`32` | `mysql_compat.py` |
| `DB_POOL_DISABLED` | 禁用连接池 | 继承环境 | `mysql_compat.py` |
| `DB_BACKUP_KEEP` | 保留 DB 备份份数 | 代码默认 | `session_manager.py` |

## 四、对象存储 OBS

| 环境变量 | 作用 | 默认 | 出处 |
|----------|------|------|------|
| `OBS_ENABLED` | 启用 OBS | `false` | `config.py` |
| `OBS_ENDPOINT` / `OBS_BUCKET` | OBS 地址与桶名 | 见 `config.py` | `config.py` |
| `OBS_ACCESS_KEY` / `OBS_SECRET_KEY` | OBS 凭据 | 空 | `config.py` |
| `OBS_PREFIX` | OBS 对象前缀 | `grape/prod` | `config.py` |
| `OBS_URL_EXPIRES` | 预签名 URL 过期秒 | `300` | `config.py` |
| `OBS_LOCAL_CACHE` | 本地缓存目录 | 空 | `config.py` |
| `OBS_MEM_CACHE_MAX_BYTES` / `OBS_ZIP_CACHE_MAX_BYTES` / `OBS_ZIP_CACHE_MAX_VERSIONS` | 存储缓存上限 | 代码默认 | `storage/__init__.py` |

## 五、调度 / 运行时（Worker）

| 环境变量 | 作用 | 默认 | 出处 |
|----------|------|------|------|
| `DEVOPS_EXECUTION_MODE` | 执行模式（`direct` / `queue_local` / `queue_remote`） | 读配置 | `runtime/service.py` |
| `RUNTIME_MAX_CONCURRENT_TASKS` | 每实例并发上限 | 代码默认 | `api/event_pipeline.py` |
| `RUNTIME_TOKEN` | Worker 注册/心跳用服务端 token | 空 | `runtime/agent.py` |
| `RUNTIME_SERVER_URL` | Worker 回连服务端地址 | 空 | `runtime/agent.py` |
| `RUNTIME_INSTALL_DIR` | Worker 安装目录 | 空 | `runtime/agent.py` |
| `RUNTIME_STALE_SECONDS` | Worker 心跳过期秒 | 代码默认 | `runtime/service.py` |
| `RUNTIME_TASK_TIMEOUT` | 单任务超时秒 | `3600` | `agent/hermes_acp.py` |
| `RUNTIME_TEXT_SILENT_TIMEOUT` / `RUNTIME_TOOL_SILENT_TIMEOUT` | 沉默/工具超时 | 代码默认 | `agent/hermes_acp.py`、`agent/hwcloud_acp.py` |
| `RUNTIME_OBS_FETCH_TIMEOUT` / `RETRIES` | Worker 拉取技能/场景超时与重试 | 代码默认 | `runtime/agent.py` |
| `RUNTIME_PROBE_INTERVAL` | Worker 探活间隔 | 代码默认 | `runtime/agent.py` |
| `RUNTIME_REREGISTER_INTERVAL` / `UPGRADE_*` | Worker 重注册/升级节奏 | 代码默认 | `runtime/agent.py` |
| `RUNTIME_DEVICE_NAME` | Worker 设备名 | 空 | `runtime/agent.py` |
| `RUNTIME_ROOT_MODE` / `GRAPE_AGENT_SANDBOX` | 特权/沙箱模式开关 | `off` | `runtime/agent.py`、`config.py` |
| `RUNTIME_EXTRA_SKILL_DIRS` | 额外技能目录（冒号分隔） | 空 | `runtime/agent.py` |
| `HERMES_HOME` | hermes 主目录 | `~/.hermes` | `config.py` |
| `HERMES_TOOL_PROGRESS_MODE` | hermes 进度模式 | 代码默认 | `runtime/hermes.py` |
| `GRAPE_LOCAL_CLAIMER` | 本地 claimer 模式 | 代码默认 | `runtime/local_claimer.py` |

## 六、流水线 / 事件

| 环境变量 | 作用 | 默认 | 出处 |
|----------|------|------|------|
| `GRAPE_PIPELINE_MODE` | 流水线模式（`legacy` / `plugin`） | 代码默认 | `api/event_pipeline.py` |
| `GRAPE_STOP_TIMEOUT` | 优雅停机超时秒 | `20` | `api/run.py` |
| `SLOW_REQUEST_THRESHOLD_MS` | 慢请求阈值（监控） | `300` | `config.py` |
| `CONVERSATION_NO_OUTPUT_TIMEOUT` | 会话无输出超时（events） | 代码默认 | `api/routes/events.py` |
| `GRAPE_MAIN_PORT` | 主 API 端口（prompt URL 构造） | 代码默认 | `prompt_builder.py` |

## 七、IM / 飞书

| 环境变量 | 作用 | 默认 | 出处 |
|----------|------|------|------|
| `BOT_LEASE_TTL` / `RENEW_INTERVAL` / `SWEEP_INTERVAL` / `GRACE` / `RENEW_FAIL_THRESHOLD` | bot 租约参数 | 代码默认 | `config.py` |
| `BOT_LEASE_DISABLED` | 关闭 bot 租约 | `false` | `config.py` |
| `IM_INSTANCE_TIMEOUT` / `STEAL_AFTER` / `ACQUIRE_COOLDOWN` / `HASH_VNODES` / `LEASE_STICKY` | 多实例 IM 分配参数 | 代码默认 | `config.py` |

## 八、场景 / 技能 / 知识库

| 环境变量 | 作用 | 默认 | 出处 |
|----------|------|------|------|
| `GRAPESKILLS_REPO_URL` / `BRANCH` | 官方场景技能仓库地址与分支 | 代码默认 | `storage/scene_sync.py` |
| `SCENE_SYNC_INTERVAL` / `SKILL_SYNC_INTERVAL` | 场景/技能同步间隔 | 代码默认 | `storage/scene_sync.py`、`storage/skill_sync.py` |
| `GRAPE_DEV_TOKEN` | 开发用仓库访问 token | 空 | `storage/scene_sync.py` |
| `SKILL_STORAGE` | 技能存储模式（`auto`/本地/OBS） | `auto` | `config.py` |
| `SKILL_STORAGE_DIR` | 技能存储目录 | 代码默认 | `config.py` |
| `SKILL_MAX_EXTRACT_BYTES` | 技能内容提取大小上限 | 代码默认 | `api/routes/skills.py` |
| `KNOWLEDGE_CACHE_MAX` | 知识库缓存条目上限 | `1000` | `api/knowledge.py` |
| `KNOWLEDGE_MAX_CONTENT_BYTES` | 知识正文大小上限（默认 100KB） | `102400` | `api/routes/knowledge.py` |
| `GRAPE_LINKED_REPOS` | 关联仓库信任域配置 | 空 | `prompt_builder.py` |

## 九、AI 模型 / VLM / 云凭据（服务端）

| 环境变量 | 作用 | 默认 | 出处 |
|----------|------|------|------|
| `VLM_BASE_URL` / `VLM_API_KEY` / `VLM_MODEL` / `VLM_MAX_TOKENS` | 图片理解 VLM 配置 | 空/代码默认 | `agent/image_understanding.py` |
| `HWCLOUD_PROVIDER_BASE_URL` / `API_KEY` / `MODELS` | 华为云 provider | 空 | `agent/hwcloud_acp.py` |
| `CODEARTS_MODEL` | CodeArts 模型 | 空 | `runtime/codearts.py` |
| `GRAPE_DEVSTAGE_FLAVOR_ID` / `CORES` / `MEMORY` | DevStage 模板规格 | 代码默认 | `api/routes/devstage.py` |
| `DEVOPS_DEVSTAGE_BASE_URL` | DevStage 服务地址 | 空 | `im/devstage_client.py` |

## 十、管理 / 部署

| 环境变量 | 作用 | 默认 | 出处 |
|----------|------|------|------|
| `GRAPE_ADMIN_PASSWORD` | admin 初始化密码（首启） | 代码默认提示 | `api/db_setup.py` |
| `ADMIN_CONSOLE_URL` / `ADMIN_BETA_APPLY_API_KEY` | 管理台地址 / 申请 API key | 空 | `config.py` |
| `ADMIN_DB_HOST` / `PORT` / `USER` / `PASSWORD` / `NAME` | 管理台独立库（admin-db-split） | 空 | `featured_order.py` |
| `GIT_COMMIT` | 构建时注入的提交号（监控展示） | 空 | `observability/__init__.py` |
| `METRICS_ADDR` | 监控指标暴露地址 | 代码默认 | `observability/__init__.py` |

## 十一、联网 / 代理

| 环境变量 | 作用 | 默认 | 出处 |
|----------|------|------|------|
| `HTTPS_PROXY` / `https_proxy` | OAuth / 配置拉取的 HTTPS 代理 | 空 | `auth/providers.py`、`admin/config_store.py` |
| `PIP_INDEX_URL` | worker 打包时 pip 源 | 空 | `worker_package.py` |
| `UV_PYTHON_DIR` | uv python 目录 | 空 | `worker_package.py` |
| `GRAPE_CLI_REPO` | worker CLI 仓库地址 | 代码默认 | `worker_package.py` |
| `GITHUB_TOKEN` / `GITCODE_PAT` | 模板拉取用 GitHub/GitCode 个人 token | 空 | `api/routes/templates.py,110` |

---

## 十二、常见组合

| 部署形态 | 需关注变量 |
|----------|------------|
| 生产 HTTPS | `JWT_SECRET`、`COOKIE_SECURE=true`、`TRUSTED_HOSTS`、OAuth `redirect_uri`（`configs/oauth-providers.json`）、`SMTP_HOST`/`RESEND_API_KEY` |
| MySQL 后端 | `DEVOPS_DB_BACKEND=mysql` + `DEVOPS_DB_HOST/PORT/USER/PASSWORD/NAME` |
| OBS 存储 | `OBS_ENABLED=true` + `OBS_ENDPOINT/BUCKET/ACCESS_KEY/SECRET_KEY` |
| Worker 远端 | `RUNTIME_TOKEN`、`RUNTIME_SERVER_URL`、`RUNTIME_INSTALL_DIR` |
| 开发/测试 | `DEVOPS_DB_PATH`（独立测试库）、`COOKIE_SECURE=false`、`GRAPE_DEV_VERIFICATION_CODE` |

---

## References

- `src/config.py`：核心配置常量（DB/OBS/邮件/租约/IM）
- `src/auth/security_config.py`：`security.json`（env / JWT 池 / credential_keys）
- `src/api/jwt_utils.py`、`src/api/main.py`：JWT 密钥与启动门禁
- `src/auth/providers.py`：OAuth 与 `TRUSTED_HOSTS`
- `src/mysql_compat.py`、`src/session_manager.py`：数据库
- `configs/*.example.json`、`.env.example`：配置模板