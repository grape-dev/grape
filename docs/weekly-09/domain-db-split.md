# 领域库拆分（2026-11）

把单库 `grape_ljt_db` 拆成三个领域库，四进程按角色各自连接自己的库：

| 进程 | 数据库 | 用户 | 密码 |
|------|--------|------|------|
| mgmt (:38081) | `grape-mgmt-db` | `grape-mgmt` | `Grape@123` |
| event (:38082) / worker (:38083) | `grape-runtime-db` | `grape-runtime` | `Grape@123` |
| insight (:38084) | `grape-insight-db` | `grape-insight` | `Grape@123` |

event 与 worker 同属 runtime 域，共用一个库。

## 表归属

按"代码里的引用方 + 写入方"判定归属（`obs_*`、`api_latency_log` 固定归 insight；
含 `agent_tasks`/`events`/`issue` 的运行时写入链路归 runtime；其余配置/账户类归 mgmt）：

- `grape-mgmt-db`：47 张基表（账户、组织、仓库、场景、技能、凭据、设置、审计、meta、GSB 注册表…）
- `grape-runtime-db`：32 张基表（issue、issue_state、events、agent_tasks、task_messages、chat_*、锁…）
- `grape-insight-db`：11 张基表（obs_*、api_latency_log…）

非归属方的表在本库里是**跨库视图**（`CREATE VIEW ... AS SELECT * FROM <owner>.<table>`），
因此：

- 读写都打到 owner 库，三库之间不会出现同一张表的多份漂移副本；
- 单表 `SELECT/INSERT/UPDATE/DELETE` 走视图仍然有效（MySQL 可更新视图）；
- 需要额外授权：三个用户互相持有对方库的 `SELECT/INSERT/UPDATE/DELETE`。

## 配置注入（重要）

systemd 的 `EnvironmentFile=.env` 在 `Environment=` 之后加载，**会覆盖 drop-in 里写的
`DEVOPS_DB_*`**。所以领域库的用户/库名统一在 `scripts/grape-systemd-run.sh` 里按角色
`export`（脚本 export 晚于 .env 注入），密码取 `GRAPE_DOMAIN_DB_PASSWORD`（默认 `Grape@123`）。

## 验证（2026-11-09 实测）

- 两机 8 进程 `active`，`:38081-38084 /api/ping` 全 200
- MySQL 连接归属：`grape-mgmt→grape-mgmt-db`、`grape-runtime→grape-runtime-db`、`grape-insight→grape-insight-db`（两机各一套）
- 业务接口：mgmt `/api/issues/mine`、`/api/repos/member`、`/api/events/detail/<seq>`；
  insight `/api/obs/overview`、`/api/dashboard/ops/overview`、`/api/dashboard/efficiency`；
  worker `/internal/runtimes`；event `/api/a2a/status` —— 均 200
- 写入落点：`mgmt.audit_logs`（登录审计）、`runtime.task_messages`、`insight.obs_log`、
  `mgmt.gsb_service_registry`（8 服务 online，心跳新鲜）

## 注意事项

1. **迁移 DDL**：`db_setup` 在启动时执行 `CREATE/ALTER TABLE`，对视图会失败并只告警
   （迁移不会被盖章）。后续 schema 变更请在 owner 库执行，再让视图自然生效。
2. **回滚**：`grape_ljt_db` 保留未动；回滚只需让 `grape-systemd-run.sh` 不再覆盖
   `DEVOPS_DB_*`（或临时把 `.env` 的 `DEVOPS_DB_*` 指回 `grape_ljt_db`）后重启四进程。
3. **备份**：三库需分别备份；跨库视图本身不落数据。
4. **可用性耦合**：mgmt 库不可用时，runtime/insight 中指向 mgmt 的视图会报错
   （例如账户状态校验），这是拆库后新增的依赖方向。
## Grafana 数据源（拆库后必须同步改）

`grape-mysql` 数据源原来指向 `grape_ljt_db`。拆库后心跳写入 `grape-mgmt-db`，
继续读旧库会让「GSB 服务注册表（MySQL 实时）」等面板全部显示离线（数据停在切换前）。

已改为：`database=grape-mgmt-db`、`user=grape-mgmt`（`/api/datasources/uid/grape-mysql/health` → OK）。
`grape-mgmt-db` 里对 runtime/insight 的表是跨库视图，所以 5 条看板 SQL 全部照常执行。

注意：Grafana 容器（`lgtm`）没有挂载卷，数据源/看板都存在容器可写层，
容器重建会丢失 —— 建议后续给 `/var/lib/grafana` 挂持久卷。
