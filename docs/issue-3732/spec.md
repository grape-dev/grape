# Issue #3732 需求文档（spec）

## 背景（生产证据）

三库/四进程拓扑下，worker/event 进程的 mgmt 域 ORM 读路径复用进程全局 `get_engine()`
（进程主库 = runtime 库），把 mgmt 域表 `external_identities` 的查询打到 runtime 库，
MySQL 1146（`Table 'grape_runtime.external_identities' doesn't exist`）被
`except Exception → return []` 吞掉——「表查不到」被降级成「用户无任何第三方绑定」。
7 天 6218 条 WARNING；repo_roles / token_resolver / mgmt user db 的凭据解析全部静默降级。

## 目标

1. mgmt 域 ORM 读路径必须按域取连接，禁止复用进程全局 engine；
2. 读失败必须可分辨（strict 通道 + 结构化日志），禁止空列表代表读失败；
3. worker/event 进程能读到真实绑定，Loki 1146 WARNING 归零；
4. 单库部署（sqlite/单 MySQL）零行为变化。

## 范围

**In**
- `src/kernel/persistence/engine.py`：新增 `get_domain_engine(domain)`——连接归属只问
  `db_registry.route_of(domain)`：本域进程/单库拓扑 → `get_engine()`（零回归）；跨域有
  `GRAPE_DSN_<DOMAIN>` → 按域 DSN 建 engine 并缓存；跨域缺 DSN → 抛 `DomainUnavailable`
  （engine 侧以 `DomainEngineUnavailable` 别名导出），**绝不回落进程主库**；
- `src/kernel/persistence/uow.py`：用基线统一入口 `uow(domain)`（本域/单库回落主库；跨域缺
  DSN 抛 `DomainUnavailable`），session 打 `info["domain"]` 归属；`uow()` 无参语义不变；
- `src/domains/mgmt/auth/grape_account_db.py`：`get_user_identities`、`get_user_identities_with_tokens`、`mark_identity_pat`、`backfill_pat_identity_tokens` 四个 `_uow()` ORM 点改走 mgmt 域 session（`list_platform_links` 经由前两者间接修复）；
- `get_user_identities_with_tokens` 增加 `strict: bool = False`（对齐 `get_user_identities(strict=)`），异常日志补 domain / 目标库名 / `missing_schema()` 判据；
- 调用方降级标注：`repo_roles.py`（3 处）、`src/domains/mgmt/vcs/base.py::fetch_owner_identity_token`（随基线由 kernel/token_resolver.py 迁入）、`src/domains/mgmt/user/db.py`（`get_credentials` / `_is_github_user_token_repo` / `get_github_token`）→ 读失败显式化为 `mgmt_read_failed` 原因，不再以空列表蒙混；
- 启动自检：`db_registry.warn_domain_table_residency(role)`（复用 `assert_role_database` 场景 + `table_ownership.json`），四个 server 入口接线。

**Out**
- 不改表结构 / 不迁移；
- 不改前端（删除确认弹窗等沿用基线 #3694 的实现，本 Issue 不产生前端 diff）；
- 不改 `_mg_db` / `_has_refresh_token` 既有裸 SQL 路径（已正确按域路由）；
- `src/domains/mgmt/auth/identity_lifecycle.py` 仍默认 `uow()`（进程全局 engine）承载
  `external_identities` 的 ORM 事务（复审 Optional-3）：本次只收 `grape_account_db`
  的读路径，worker/event 对该服务的可达性未观察到——域路由留待后续 Issue；
- 不处理 #3427 / #3563 的文案与 OAuth 续期。

## 验收标准

1. 双 DSN fixture 断言 SQL 打 mgmt 库（覆盖三个读函数）；
2. mgmt 库不可达 / 表缺失时：strict=True 重抛；调用方收到显式失败原因而非空列表；降级路径有日志标注；
3. 单库零回归：`get_engine()` / `uow()` 无参语义不变，现有测试全绿；
4. L667 / L1775 / L1810 三个 `_uow()` 调用点域限定结论写入代码注释（均为 mgmt 域表 ORM 事务，统一走 mgmt 域 session）；
5. 启动自检：域表↔DSN 归属错配可复现告警（负向用例），单库不告警不阻断。

## 依据

- 表归属权威：`configs/table_ownership.json`（`external_identities`、`user_credentials` 均 mgmt）；
- 域 DSN 权威：`db_registry.dsn_of()` / `lib_enabled()`（`GRAPE_DSN_MGMT` 有 DSN 即三库拓扑）；
- 连接归属权威：`db_registry.route_of(domain)`（全仓唯一判定，跨域缺 DSN 直接抛
  `DomainUnavailable`）——本 Issue 的 engine 工厂不另立判据；
- 缺表判据：`src/kernel/db_errors.py::missing_schema`（SQLite no such table / MySQL 1146/1054/1051）。
