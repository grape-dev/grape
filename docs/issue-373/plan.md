# Plan — Issue #373 开发者文档

## 技术方案
纯文档新增 + 帮助中心挂载，复用 `help/` 现有加载渲染机制（`src/help_page_gen.py`）。

### 事实核实清单（逐一对照源码）
| 配置面 | 事实来源 |
|--------|---------|
| admin 控制台 → DB meta（gitcode_api_token 等 9 字段，`PUT /api/admin/config`） | `src/admin/routes.py` `ConfigBody`、`src/api/db_setup.py` Issue #345 迁移 |
| oauth-providers.json（gitcode/github 各字段、redirect_uri 格式） | `configs/oauth-providers.example.json` |
| global-bot-token.json（首次启动迁 DB meta，DB 为运行时权威） | `configs/global-bot-token.example.json` description |
| webhook 路径 `POST /webhook/gitcode`、`POST /webhook/github` | `src/api/routes/webhook.py` |
| IM lease：一致性哈希环 + lease，日志 `connected after acquiring lease` | `src/im/feishu_async.py`（cluster-im-lease-stability-design.md） |
| IM 端到端验证：`POST /api/obs/alerts/test-notify`（向本人 IM 身份发测试告警） | `src/api/routes/observability.py` |
| OBS_* 环境变量（ENABLED/ENDPOINT/BUCKET/ACCESS_KEY/SECRET_KEY/PREFIX/URL_EXPIRES） | `src/config.py:153-159` |
| email.json（env/mode/smtp/resend，改后无需重启） | `configs/email.example.json` |
| db-backend.json（backend 开关，`DEVOPS_DB_BACKEND` 环境变量优先） | `src/session_manager.py:47`、`src/mysql_compat.py` |
| 连接池 DB_POOL_MIN/MAX_CACHED/MAX_CONNECTIONS/DISABLED | `src/mysql_compat.py:657-673` |
| scene-sync.json（repo_url/branch/sync_interval=1800/token；4 个环境变量覆盖） | `configs/scene-sync.example.json` |
| security.json（env 门禁 / jwt.secret_keys kid 轮换 / credential_keys） | `configs/security.example.json`、`src/config.py:200` |
| COOKIE_SECURE | `src/auth/cookie_utils.py:20-21` |
| login-whitelist.json（OAuth 登录白名单，独立于注册白名单） | `src/auth/signup_policy.py` |
| engine-selection.json（enabled/scopes/rule/filters/local_priority=90） | `configs/engine-selection.example.json`、`src/runtime/engine_select.py` |

### 前端接入（help_page_gen.py）
1. 新增 `DEVELOPER_DIR` / `DEVELOPER_ORDER`（00~06 顺序）。
2. `_developer_data()`（复用 reference 的渲染管线：prefix id / blockquote 装饰 / .md 链接改写 `#/developer/{stem}`）。
3. `build_data()` 增加 `"developer"` 键。
4. SPA JS：`route()` 白名单加 `developer`；`topbar` 加「开发者文档」链接；首页加第四张卡片；`sideList` 分区标题映射。
5. `generate()` 计数含 developer。

### 测试
- `tests/test_help_directory.py`：`EXPECTED_DEVELOPER_FILES` + `TestDeveloperFiles`（存在/数量/非空/命名/无残留 HTML）。
- `tests/test_help_page_generation.py`：landing 含「开发者文档」、`#/developer` 路由、developer 数据字段/首 H1/计数；`test_source_count` 修正为完整公式（含 reference + developer）。

### 风险
- 内容与实际配置不符 → 全部字段名对照源码核实（见上表）。
- 泄露凭据 → 占位符纪律 + 提交前 `check-doc-redaction.py --dir help`。
- 帮助中心接入遗漏 → 测试断言 developer 区块渲染。
