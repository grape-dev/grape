# 任务拆解 — Issue #3588

| # | 切片 | 测试 | 提交 |
|---|------|------|------|
| 1 | db_health 核心失效检测与重连（SelfHealingSQLiteDB + cursor 包装 + inode 周期校验 + db_health 信号） | tests/kernel/test_issue3588_db_health.py（先红） | test → feat |
| 2 | session_manager 主连接接入代理 + `DEVOPS_DB_JOURNAL_MODE` 可配 | 同上（journal_mode 用例） | feat |
| 3 | engine_wiring `_connect` SQLite 分支接入代理（outbox 仓储零改动受益）+ 消费者退避 | tests/kernel/test_issue3588_db_health.py outbox 用例 | feat |
| 4 | bulk_read 连接期探测与 stale 重试 | 同上 bulk_read 用例 | feat |
| 5 | /healthz db_status/wal_valid 信号 | 手测 + 现有 readiness 测试回归 | feat |
| 6 | 部署文档 overlayfs 兼容说明 | — | docs |
