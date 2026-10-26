# Issue #455 方案设计（Plan）

> 0→1 安装引导（OSS 主线）改进 —— 按 M1→M2→M3 分批实现。

## 总体思路

文档先行（README/requirements/doctor + 新自检清单）；产品缺陷按「建表完整性 → 落库事务 → 错误上下文 → 前置校验 → 重试」逐层修复。
运行验证（起本地服务观测）由 sre/联调执行，编码阶段用一次性验证模式。

## M1（P1）

### 1. Python ≥3.10 声明 + 启动版本检查

- `README.md` / `README.zh.md`「环境要求」：Python `>= 3.10`（原 3.9）。
- `requirements.txt` / `requirements-local.txt` 头部注释：`Python 版本要求：>= 3.10`。
- `scripts/doctor.py` `check_python_version()`：低于 3.10 置 error（阻断），提示升级。
- `src/api/run.py` 顶部（import 之前）：`sys.version_info < (3, 10)` → 输出显式指引并 `sys.exit(1)`。
- `src/api/main.py` 模块顶部同加（防御直接 uvicorn src.api.main 的启动方式）；`start_server.py` 同加（浅封装也拦截）。

### 2. 对外基址配置 + 未配置告警

- 文档：README「配置」表 + `configs/server-config.example.json` 说明（server_base_url / webhook_url）。
- `src/user/db.py`：新增 `is_server_base_url_configured()` / `get_server_base_url_warning()` 判断：
  - server-config.json 的 server_base_url 或 meta.server_base_url 非空、且不以 loopback（localhost/127.0.0.1/0.0.0.0）开头 → 已配置；
  - 空或 loopback → 未配置。
- `src/api/main.py` lifespan：启动时如未配置 → `log.warning` 明确告警（OAuth 回调/安装命令将指向 localhost，需配置 server_base_url）。
- `src/api/routes/health.py`：`/api/health` 增加 `server_base_url_configured`（bool）与 `server_base_url_warning`（str，未配置时给指引文案）；保持免鉴权与字段兼容。

### 3. OAuth 凭据配置文档

- README「复制 Bot Token 配置」改为指向 `configs/oauth-providers.json`（从 `oauth-providers.example.json` 复制），说明字段：
  - provider 键：`gitcode` / `github`；
  - 凭据字段：`client_id` / `client_secret`（首个空值幂等写入 meta；改后需重启或调 admin 配置接口使缓存失效——代码路径以 `migrate_oauth_providers` 为准，写错文件/字段空 → `/api/auth/login/{provider}` 400 not_configured）；
  - 协议字段：`auth_url` / `token_url` / `user_url` / `redirect_uri` / `scope` 等。
- README「配置」表补充 `configs/oauth-providers.json`。

### 4. SQLite 建表补齐 + 清理存在性检查

- `src/api/db_setup.py`：新增 `_PIPELINE_INSTANCES_DDL`（`CREATE TABLE IF NOT EXISTS pipeline_instances`，含 `repo_key` 列，幂等），加入 `_NEW_TABLES`。
- `src/api/onboarding.py` `cleanup_stale_repo_data`：删除前先取 `sqlite_master` 表集合（复用 `_existing_tables` 思路），缺表跳过，不产生 WARNING 噪音。
- `src/repo_cleanup.py` 已有 `_existing_tables` 存在性检查，核对 `_REPO_KEY_TABLES` 与 `_NEW_TABLES` 对齐。

### 5. GitHub OAuth 落库读回校验

- `src/api/routes/grape_auth.py` `_handle_oauth_login` 未绑定分支（GitHub 同样路径走此公共函数，但校验在公共处对 provider=github 全量生效；GitCode 路径不改变语义）：
  - `create_grape_account_oauth` 成功后：用 `get_grape_account_by_id(id)` 读回，NULL → `log.error` + `_oauth_error_redirect(create_failed)`；
  - `create_external_identity` 成功后：用 `get_external_identity(provider, external_id)` 读回，NULL → `log.error` + `soft_delete_account` 回滚 + `_oauth_error_redirect(bind_failed)`；
  - 两处读回校验通过后才继续 `_ensure_personal_org` / JWT / 302。
- 目的：修「日志称成功但落库无痕」——对 provider=github 的自动创建路径强制读回，失败不再静默 302。

## M2（P2）

### 6. 托管接口 502 上下文 + 可重试

- `src/api/routes/repos.py` `host_repo`：
  - 新增 `_upstream_error_detail(stage, exc, code_hint)` 辅助：从 `_req` RuntimeError 提取上游状态码（正则 `failed \((\d{3})\)`），响应 detail 增加 `upstream`（阶段）+ `error_code`（`UPSTREAM_UPSTREAM_STATUS` 等）+ `timeout` 提示 + `retryable: true`；
  - 兜底 502 detail 不再只给 `托管服务暂时不可用`：补充 `"stage": "host_repo"` 与 `"retryable": true`、`"retry_hint"` 文案（可稍后重试或使用 self-heal repair-host 端点）；
  - 日志保留原文（防内部 URL 泄露约束不变，响应不拼上游原文 URL）。
- 可重试路径：已有 `POST /repos/{owner}/{repo}/repair-host`（幂等自愈），在 502 detail 中提示前端可调。

### 7. 非所有者托管 403 前置校验 + UI 提示

- `host_repo` GitCode 分支已存在 `is_admin` 前置校验（403 PERMISSION_DENIED）；GitHub 分支补前置归属/权限校验：
  - 托管前用用户 token 查仓库 owner（`GET /repos/{owner}/{repo}`）或协作者权限，非 owner 且非 admin（>=40）→ 403 `PERMISSION_DENIED`，message 明确「请使用该仓库所有者的凭据或授予机器人管理员权限」；
  - 上游 401/403 → 403 `TOKEN_PERMISSION_INSUFFICIENT`（已有逻辑保留）。
- 前端 `src/api/static/js/pages/repos.js` `hostAndSelect` error 分支：按后端 `error_code` 映射 i18n toast：
  - `PERMISSION_DENIED` → `repos.host.permission_denied`（明确「请使用该仓库所有者的凭据或授予机器人管理员权限」）；
  - `TOKEN_PERMISSION_INSUFFICIENT` → `repos.host.token_insufficient`；
  - 其它 502 → `repos.host.upstream_error`（含可重试文案）。
- locales：`zh/repos.json` / `en/repos.json` 新增以上 key。前端失败时把 `e.message`（后端中文/英文文案）拼接展示，保证明示。

## M3（P3）

### 8. 拉取路径有限次重试 + 镜像/代理文档

- `src/runtime/repogit.py`：
  - `_NETWORK_ERR_RE = re.compile(r'(could not resolve|connection (timed out|refused|reset)|failed to connect|http/2|rpc failed|remote end hung up|early eof|operation timed out|unable to access)', re.I)`；
  - 新 `_run_git_retry(args, token, timeout, retries=3, base_delay=1.0)`：`_run_git` 返回非 None 且 returncode!=0 且 stderr 命中网络类错误 → 退避重试（1s/2s/4s）；4xx（stderr 中 `returned error: 401/403/404`）或 returncode==0 快速返回；
  - `ensure_repo_base` 的 `init`/`remote add`/`fetch` 用 retry 版本（fetch 网络敏感）。
- `src/worker_package.py` `_git(["clone", ...])`：包一层网络类重试（复用 repogit 的检测函数或本地正则）。
- 文档：README「安装」补充镜像/代理建议（PyPI 镜像已有；新增 Git/GitHub 代理说明），并在 `docs/0-to-1-checklist.md` 网络阶段提及。

### 9. 0→1 安装自检清单

- 新增 `docs/0-to-1-checklist.md`：
  - 阶段化检查（环境 → 拉取/依赖 → 配置 → 启动 → OAuth 登录 → 托管 → 首个任务）；
  - 每步「操作 / 期望结果 / 失败排查」；
  - 含 Python 版本、server_base_url、oauth-providers.json 必配项；
  - README 增加该文档链接。

## 测试计划

- 新增/更新测试：
  - `tests/test_issue455_startup.py`（或并入现有）：
    - `run.py` / `main.py` 版本检查逻辑单测（monkeypatch sys.version_info）；
    - `get_server_base_url` 未配置判断与 health 字段；
    - `db_setup` 建表后 `pipeline_instances` 存在；`cleanup_stale_repo_data` 缺表时静默跳过；
    - grape_auth GitHub 自动创建读回校验：mock `get_grape_account_by_id` 返回 None → 302 到 oauth_error；
    - repos host_repo：mock 上游 403 → 403 PERMISSION_DENIED；502 detail 含 retryable；
    - repogit `_run_git_retry`：mock subprocess 网络错误 2 次失败第 3 次成功；4xx 快速失败。
- 运行 `python -m pytest tests/ -x -q`（抽相关模块；全量回归按仓库既有方式）。