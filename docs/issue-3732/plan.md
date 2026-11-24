# Issue #3732 方案设计（plan）

## 总体方案

persistence 层补**域感知 engine/session 工厂**；mgmt 域 ORM 读写显式传
`domain="mgmt"`；连接归属只问 `db_registry.route_of(domain)`（本域/单库回落进程主库，
跨域缺 DSN 显式抛 `DomainUnavailable`，绝不回落）；读失败走 strict 通道 + 结构化日志；
启动时做「域表 ↔ DSN 归属」抽样自检。

## 1. persistence 域工厂

### `engine.py::get_domain_engine(domain)`
- 判据：只问 `db_registry.route_of(domain)`（`local` → 主库引擎；`domain` → 域 DSN
  引擎；其余 → 抛 `DomainUnavailable`）。不另立 `lib_enabled() and dsn_of()` 判据，
  避免两处口径漂移；也**不要求连接探活成功**——`create_engine` 不连库，且避免
  `domain_db()` 的退避/拉黑状态把 engine 选择带偏（临时连接失败回落进程库会重新
  制造 1146）。
- DSN → SQLAlchemy URL：mysql → `mysql+pymysql://user:pass@host:port/db?charset=utf8mb4`
  （复用 `db_registry.parse_dsn` + `quote_plus`，密码 URL 编码）；`sqlite:` scheme
  直通（双库 fixture 测试通道，生产 DSN 仅 mysql）。
- 缓存：复用 `_engine_cache` + `_engine_cache_lock`，键 `("domain", domain, url)`；
  `cache_clear()` 同时清域 engine。
- 本域进程/单库拓扑 → `return get_engine()`（`get_db_url()` 不动）；跨域缺 DSN →
  `route_of` 抛 `DomainUnavailable`，engine 模块以 `DomainUnavailable as
  DomainEngineUnavailable` 别名导出供调用方捕获（契约层 `dispatch_identity` 据此
  转一次可见告警，与 `tests/mgmt/test_account_contract_routing.py` S1–S5 一致）。
- `create_engine` 在 `get_domain_engine` 内局部导入：用例以
  `monkeypatch.setattr("sqlalchemy.create_engine", …)` 作 seam（上条 S3）。

### `uow.py::uow(domain=None)`
- 出口语义不变（commit / 异常 rollback / finally close）；`domain` 非空时 bind 到
  `get_domain_engine(domain)`，否则 `get_engine()`（无参语义零变化）；
- 有 `info` 的 session 打 `info["domain"]` 归属标记（`getattr` 容错，伪 session
  的用例 seam 不受影响）——日志/审计据此报出实际域名；
- 基线已把域路由统一到本入口（Issue #3897），本 Issue 不再另设 `domain_uow`
  包装，避免两套工厂并存。

## 2. grape_account_db.py 读点路由

模块内沿用基线 seam `_uow`（= `persistence.uow.uow`，`from … import uow as _uow`），
四个 ORM 调用点统一传 owner 域 `_uow(_IDENTITY_DOMAIN)`：

| 位置 | 函数 | 结论（域限定） |
|------|------|------|
| L667 | `get_user_identities` | external_identities（mgmt 域表）ORM 读 → mgmt 域 session |
| L819 | `get_user_identities_with_tokens` | 同上 + strict 通道 |
| L1775 | `mark_identity_pat` | ei 表 ORM 读+写同 session 一个事务 → mgmt 域 session（写路径事务边界不变） |
| L1810 | `backfill_pat_identity_tokens` | ei / user_credentials 均 mgmt 表；ORM 侧读 + 第一段 ORM 写在同一 session；第二段搬家落写走 `_mg_db` 裸 SQL（本来已按域）→ ORM 侧改 mgmt 域 session，裸写不动 |

`list_platform_links` 无直接 ORM 点（组合前两者 + `get_credentials` 裸 SQL）→ 间接修复。

### strict 失败通道
`get_user_identities_with_tokens(grape_user_id, errors=None, strict=False)`：
- 异常分支日志补 `domain=mgmt`、目标库名（mgmt DSN 库名，回落 `DEVOPS_DB_NAME`）、
  `missing_schema(exc)`；
- `strict=True` 重抛；默认 False 保持宽容语义（存量调用方零破坏）。

## 3. 调用方降级标注（读失败可分辨）

| 调用方 | 处理 |
|--------|------|
| `repo_roles._get_gitcode_login` / `_get_github_login` / `_get_platform_login` | `strict=True`，异常进各自 fail-closed except，日志标注 `credential_unavailable: mgmt_read_failed` |
| `src/domains/mgmt/vcs/base.py::GenericVcsProvider.fetch_owner_identity_token`（基线合并后由 kernel/token_resolver.py 迁入） | `strict=True`，except 日志标注 `credential_unavailable: mgmt_read_failed`（不再无声回落 bot token） |
| `mgmt/user/db.py` `get_credentials` | `strict=True` + 日志带原因 |
| `mgmt/user/db.py` `_is_github_user_token_repo` / `get_github_token` | `strict=True`；前者原 `except: pass` 改为记录告警（可见降级），fail-open 决策不变 |

外部行为不变（失败仍 fail-closed 返回空/None），差异是失败原因显式可见。

## 4. 启动自检（`db_registry.warn_domain_table_residency(role)`）

两类错配，**只告警不阻断**（单库无 DSN 时整体 no-op）：
1. 有 DSN 的域：抽样其 `table_ownership.json` 归属关键表（mgmt 必抽
   `external_identities`），经 `new_connection(domain)` 查
   `information_schema.tables`，表不在 DSN 库 → WARNING（DSN 配错库）；
2. 多库拓扑下 worker/event 角色缺 `GRAPE_DSN_MGMT` → WARNING
   （mgmt 域表跨域读将回落本进程库 → 1146 复发）。

接线：`grape_server` / `grape_worker_server` / `grape_event_server` /
`grape_insight_server` 在 `assert_role_database(role)` 后调用。

## 5. 测试方案（tests/kernel/test_issue_3732_domain_session.py）

- **回落**：无 DSN → `get_domain_engine("mgmt") is get_engine()`；
- **路由**：`GRAPE_DSN_MGMT` mysql 假 DSN → engine URL 指向 mgmt 库名、drivername `mysql+pymysql`；
- **双 DSN fixture（核心）**：mgmt 侧独立 sqlite 文件 DSN 建表插绑定行，进程主库
  无该用户数据 → `get_user_identities_with_tokens` / `get_user_identities` /
  `list_platform_links` 三函数都读到 mgmt 库行（旧实现返回 [] —— 红测试）；
- **strict**：mgmt 侧缺表 → 默认返回 [] + 告警；`strict=True` 重抛；
  `repo_roles._get_platform_login` fail-closed 返回 "" 且日志含 mgmt_read_failed；
- **启动自检**：域表不在 DSN 库 → problem 告警；worker 缺 GRAPE_DSN_MGMT →
  告警；单库无 DSN → 空；

## 风险与回退

- 双 engine 并存：域 session 仅限 mgmt 域表 ORM 事务，`pool_pre_ping` 沿用；
- sqlite 测试通道仅作用于 DSN scheme 直通，不影响生产（生产 DSN 仅 mysql，
  `db_registry.parse_dsn` 会拒绝其它 scheme）；
- 回退：`route_of` 返回 `local`（单库/本域进程）即整层回落现状行为；跨域缺 DSN 的
  `DomainUnavailable` 与基线既有契约降级路径同源，不新增失败面。

## 基线对齐（合并 arch-weekly-11-ljt fe1d51a33）

PR #1957 冲突消解时，目标基线已把「跨域连接归属」收敛为**全仓唯一判定**
`db_registry.route_of()`（`fix(db): route cross-domain work in one place, never fall back`
+ `tests/mgmt/test_account_contract_routing.py` S1–S5）。本 Issue 随之对齐，语义只增不减：

- `get_domain_engine` 改为复用 `route_of`：`local` → 主库；`domain` → 域 DSN engine；
  跨域缺 DSN → `DomainUnavailable`（module 别名 `DomainEngineUnavailable`），
  与基线的「绝不回落主库」口径一致；
- 判据不再自带 `lib_enabled() and dsn_of()`（两处口径 → 一处），`_dsn_to_url()` 的
  mysql 拼装仍复用 `_mysql_url()`（密码保留字符单点防线），`sqlite:` scheme 直通仍仅
  作双库 fixture 测试通道；
- `fetch_owner_identity_token` 的实现随基线迁到 `src/domains/mgmt/vcs/base.py`，
  `strict=True` 与 `credential_unavailable: mgmt_read_failed` 标注在新位置重新落地；
- 前端 4 文件（`SkillsView.vue` / `shell/index.html` / 两张前端棘轮表）**不进本差异**：
  spec 的 In/Out 无前端项，删除确认弹窗以基线 #3694 的
  `tc2('skills.confirm.delete_skill', {name})` 插值实现为权威，本 PR 不回退基线。

## 基线二次合并（arch-weekly-11-ljt 35ee2424b，Issue #3897 落地后）

基线在此期间以 Issue #3897 把「identity 关系读写按域路由」独立落地（`uow(domain)` +
`_uow(_IDENTITY_DOMAIN)` 四个调用点 + 缺表一次性降级 `_degrade_identity_read`）。
本 Issue 与之收敛，避免两套并行实现：

- `persistence/uow.py` 只保留基线的 `uow(domain=None)`；本 Issue 的 `domain_uow`
  包装删除（无 src 调用点），域归属标记 `session.info["domain"]` 以 `getattr` 方式
  打在 `uow(domain)` 内（用例注入的伪 session 无 `info` 时按无标记跳过，不影响
  `tests/mgmt/test_issue_3897_identity_relation.py`）；
- `grape_account_db.py` 四个 ORM 点用基线 seam `_uow(_IDENTITY_DOMAIN)`（用例以
  `G._uow` 做 monkeypatch seam，两条实现不得各留一套）；本 Issue 的增量保留在
  **strict 通道**（`strict=True` 重抛）与**结构化失败日志**（`domain=` / `db=` /
  `missing_schema=`）上，与基线的「缺表按 op 只 WARN 一次 + 修复命令」并存：
  缺表/域不可用走降级告警，其余异常走结构化 WARNING；
- `engine.get_domain_engine` 与 `route_of` 的口径不变（见上一节）；
- 门禁步骤保留本 Issue 的 `test_issue_3732_domain_session.py`，与基线的
  #3699/#3758/#3771/#3826/#3848 步骤并存。

## 线性 rebase（arch-weekly-11-ljt 4da8adc45，ff-only 合入收敛）

基线 `arch-weekly-11-ljt` 在 #3897 之后被重写/前移，PR 头部的 3 个 `merge: sync`
提交使其不再是基线 tip 的后代；平台 `merge_method=ff`（仅允许快进）下
`non_ff_passed=false` —— 即用户看到的「变基合入错误」。本轮改为**线性 rebase**：

- 本 Issue 的 12 个提交按序重放到最新基线 tip 之上，`merge: sync` 提交全部丢弃；
  head 成为基线 tip 的后代，ff 可快进；
- 冲突一律**以基线为底、只叠加本 Issue 增量**，不回退基线任何实现：
  - `windows-smoke.yml`：保留基线全部 step（含 #3789/#3771/#3848/#3737/#3811/#3740），
    本 Issue 的门禁 step 追加在 linux-backend job 末尾；
  - `grape_worker_server.py`：保留基线 #3969 的 `drain_state.clear_drain()` /
    readiness 接线，只叠加 `warn_domain_table_residency("worker")` 一行；
  - `mgmt/user/db.py`：保留基线 #3789 的 `skill_source` / repo→official 降级口径，
    只叠加 `get_credentials` / `_is_github_user_token_repo` / `get_github_token`
    三处的 `strict=True` 与 `mgmt_read_failed` 标注；
  - `db_registry.py`：保留基线跨域回落告警的进程级去重实现（`_warned_cross_domains`
    + 锁），只新增 `warn_domain_table_residency`；
  - `engine.py` / `uow.py`：以基线的 `route_of` + `uow(domain)` 为底，叠加
    `_mysql_url()` 单点拼装、`_dsn_to_url()`（sqlite 测试通道）与
    `session.info["domain"]` 归属标记；
- 前端 4 文件不进本差异（见上一节）。
