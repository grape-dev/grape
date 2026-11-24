# Spec — Issue #3588 容器 overlayfs 下 SQLite WAL/SHM 失效导致接口持续 500

> 来源：VOD 四要素收敛（comment 202470556）+ Issue Agent Spec（comment 202471676，评级 ready）。本文件为开发落地版摘录。

## 目标
- overlayfs 类容器环境中，`sessions.db-wal` / `sessions.db-shm` 被删除/替换（copy-up 以「删旧建新」方式替换文件）后：
  1. 主连接（`session_manager.db`，模块级长连接）能检测失效并自动重建连接（含 PRAGMA 重放），接口由持续 500 转自动恢复；
  2. outbox consumer 复用同一套失效检测与重连（退避重试，不重复投递——认领态 `pending→sending` 持久化在库，重连后不重发已终态行）；
  3. `bulk_read.reader()` 保持短连接独立完成，连接期探测 + 单次重试，消除「偶发 500」；
  4. 部署配置提供 overlayfs 兼容选项：`DEVOPS_DB_JOURNAL_MODE`（journal 模式可选，默认 wal 不变）+ 既有 `DEVOPS_DB_PATH`（数据目录持久卷）文档化；
  5. 失效/恢复事件输出结构化日志，`/healthz` 增加 `db_status` / `wal_valid` 信号。

## 非目标
- 不改数据模型/业务语义；不做历史迁移；不引入外部数据库。

## 验收标准
- [ ] 模拟删除/替换 WAL/SHM 后：主连接 execute 自动重建并成功；outbox run_once 恢复消费；bulk_read 恢复读。
- [ ] 结构化日志报告「失效」与「恢复」事件（`event=db_stale_detected` / `event=db_reconnect_ok`）。
- [ ] `/healthz` 含 `db_status` / `wal_valid`。
- [ ] 部署文档说明 overlayfs 推荐做法（持久卷 + journal 模式选项）。
- [ ] 现有测试通过；新增测试覆盖失效检测与重连路径。
