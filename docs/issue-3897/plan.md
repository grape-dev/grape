# Plan: Issue #3897

## A 组 — Autopilot 失败归因（runtime/engine/scheduler.py）

1. 新增纯函数 `_autopilot_failure_cause(exit_code, final_text) -> str`：
   `exit_code=<n>; output=<最后一条非空行，截断 300>;`，无输出时
   `output=<empty: agent produced no stdout/stderr>`（非空 cause 恒成立）。
2. `_finalize_autopilot_task`：`status != success` 时把该 cause 写入
   `autopilot_executions.error`（部署证据失败时用证据原因覆盖并保留 exit_code）。
3. `_execute_task` 收口：`retries=N/M` 改为可解释字段——
   `attempts=<attempt+1>/<max_retry+1> max_retry=<n> retry_skip=<原因>`
   （`attempts` 含首次执行，不是「重试次数」；口径已在字段名里写明）；
   原因取值：环境性失败码（不重试）/ `max_retry=0`（任务配置不重试）/
   `reached_max_retry`。收口日志不设独立 `exit=` 字段：退出码由 `error=` 内嵌的
   cause 前缀携带（`exit_code=<n>; output=…`，None/非法统一归一为 -1 并带注记，
   与落库的 `autopilot_executions.error` 同文）；另有独立的 `error_code=`（业务
   失败码，非退出码）。
4. 不改重试策略本身：本次只补可观测性，避免改动 #3800 退避/熔断语义。

## B 组 — external_identities（uow / db_setup / schema_probe / 迁移）

1. `uow(domain: str | None = None)`：`domain` 非空走
   `get_domain_engine(domain)`（既有跨域原语，缺 DSN 显式抛 `DomainUnavailable`，
   不回落进程库）；默认 `None` 行为零变化。
2. `grape_account_db` 4 处 `_uow()`（get_user_identities / get_user_identities_with_tokens /
   mark_identity_pat / backfill_pat_identity_tokens）与
   `identity_lifecycle._default_uow` 改 `uow("mgmt")`：identity 读写永远落在 owner 库。
3. 缺表降级：新增 `_degrade_identity_read(op, exc)`——识别 1146 /
   `DomainUnavailable` / `ProgrammingError`，同一 `op` 只 WARN 一次（带 remediation：
   `python3 scripts/domain_view_freshness.py --repair`），其余降 DEBUG 并按 `op` 计数；
   返回 `[]` 保持非致命。
4. 启动自检：`schema_probe.relation_kind(cursor, table)`（返回 `table`/`view`/`None`，
   MySQL 查 information_schema.tables，SQLite 查 sqlite_master）；`db_setup._check_identity_relations()`
   在 `init_database()` 迁移段后执行，缺失关系打 ERROR（含库名/route/remediation），
   存在打 INFO。失败不阻断启动。
5. 部署侧：`scripts/view_create_missing_allowlist.txt` 补 `external_identities`、
   `grape_accounts`（owner=mgmt → runtime/insight 视图由 `--repair` 补建）。
   前置条件：worker/event 进程必须可见 `GRAPE_DSN_MGMT`（或共享 `GRAPE_3DB_ENV`），
   否则 `route_of("mgmt")` 抛 `DomainUnavailable` → 身份解析降级为 `[]`；
   `db_setup._check_identity_domain()` 在启动自检里把该前置变成一条 ERROR（含 remediation）。
6. 迁移 `202611230005`（mgmt 域）：存量库幂等补建 `external_identities` 基表 +
   缺列（status/unbound_at/token_expires_at），post_check 断言表存在——
   覆盖「代码已上线、老库 `_create_tables` 老轨已冻结、表未建」的漂移。
   （跨域视图不在此迁移里建：视图唯一构造实现是 `scripts/view_ddl_builder.py`，
   应用进程内建跨 schema 视图会破坏「谁拥有谁写」边界。）

## C 组 — IM 断连 + webhook 验签

1. `supervisor`：新增传输层异常分支（`BrokenPipeError` / `ConnectionResetError` /
   `ConnectionAbortedError` / `ConnectionRefusedError` / `asyncio.IncompleteReadError` /
   `TimeoutError`），置于通用 `except Exception` **之前**：
   WARNING 单行 `transport error: inst=... type=... cause=... errno=...` +
   `flap_by_cause["ws_transport"]` + 既有指数退避；通用分支保留（真 bug 仍全栈 ERROR）。
2. `routes/webhook.py`：新增 `_token_fingerprint(value) -> "sha256:<12hex>"|"absent"`；
   gitcode/github token mismatch 日志补 `expected=<fp> actual=<fp> ip=...`，不打印明文。

## D 组 — 噪音与泄漏

1. `token_resolver.RepoBotToken._fetch`：历史列命中默认 `log.debug`；
   WARN 按 `(repo, field)` 限频（`_FALLBACK_WARN_INTERVAL_SECONDS = 3600`），
   保留可观测性（下线判据 grep 仍有 1 条/仓库/小时）。
2. `im/adapters/feishu/channel.py`：endpoint 取用改短生命周期客户端，
   `try/finally: await api.close()`，消除自建 aiohttp session 泄漏。

## 提交拆分（红/绿）

- `test(#3897): A 组 …` → `fix(#3897): A 组 …`（同理 B/C/D）
- 顺序 A→B→C→D，每组一个红提交 + 一个绿提交；文档提交在前。

## 风险

| 风险 | 缓解 |
|---|---|
| `uow("mgmt")` 在单库/测试下路由 local，零回归 | `route_of` 单库返回 local，测例覆盖 |
| 域 DSN 缺失时 identity 读抛 `DomainUnavailable` | 一次性降级 WARN + 启动自检 ERROR |
| 限频日志掩盖回退根因 | 1 条/仓库/小时 + 逐次 debug + 审计表 `repo_bot_hit_legacy_column` 仍在 |
| supervisor 传输分支吞掉真 bug | 只对明确传输异常降级，其余仍全栈 ERROR |
