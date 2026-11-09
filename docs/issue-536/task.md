# Task: Issue #536 — 数据库迁移版本化与 DDL 安全带

## 切片拆解（TDD 红/绿分提交）

| # | 切片 | 提交 | 验收 |
|---|------|------|------|
| S1 | 版本表 DDL + 注册表 + 执行器 + legacy-stamp 盖帽 | test → feat ✅ | 二次启动 0 pending；首跑全量行为不变 |
| S2 | `_ensure_column` ALGORITHM 降级链（INSTANT→INPLACE,LOCK=NONE→ERROR 清单） | test → feat ✅ | MySQL mock 断言语句形态；SQLite 不变 |
| S3 | 迁移连接 lock_wait_timeout=10 + MDL 超时跳过+持有者摘要 | test → feat ✅ | SET SESSION 下发；1205 错误跳过不盖章 |
| S4 | `_destructive_ddl` 收敛 + min_code_version 门槛 | test → feat ✅ | 门槛未过跳过不盖章；门槛过执行 |
| S5 | CI grep 卡点 scripts/check-destructive-ddl.py + check-all.sh 挂载 | test → feat ✅ | diff 新增危险 DDL 未在 cleanup 块内 → 退出码非 0 |

## 实施记录（2026-10-30）

- 新增 `src/api/code_version.py`：主控自身版本（git commit 优先 → 内容指纹回退）；
- `src/api/db_setup.py`：
  - `_SCHEMA_MIGRATIONS_DDL` 挂入 `_NEW_TABLES`（全新库自然建出）；
  - `Migration` dataclass / `register_migration()` / `_run_migrations_with_conn()` 执行器
    （legacy-stamp 首跑全量+盖帽 → 增量 only；checksum 漂移先于 pending 判定告警；
    `_MdlWaitTimeout` 单独捕获附持有者摘要）；
  - `run_pending_migrations()` 替换 `init_database()` 三处调用点（锁机制原样复用）；
  - `_ensure_column` INSTANT → INPLACE,LOCK=NONE → `_DDL_MANUAL_REVIEW` ERROR 清单；
  - `_destructive_ddl()` 收口 6 类调用点：`_rename_column_if_exists`（RENAME+DROP）、
    `_migrate_tables` migrations 列表（skill DROP COLUMN）、repo_im_group_binding
    （DROP+RENAME）、devops_agent_session（DROP vod_bot_app_id）、vod_conversations
    （3×RENAME）、grape_accounts（MODIFY email）；
  - `_set_migration_session_lock_timeout(dconn, 10)` 在专用连接 GET_LOCK 后下发；
  - `_backfill_in_batches()`：LIMIT 1000 循环 + 每批独立事务 + 单批失败隔离；
- `scripts/check-destructive-ddl.py` + check-all.sh 挂载 + .gitignore 白名单；
- `scripts/set_min_code_version.py`：部署脚本配套（全集群升级完成后写 meta + 回读核验）；
- 测试：`tests/atomic/test_issue_536_migrations.py`（26 用例）+
  `tests/test_check_destructive_ddl.py`（9 用例）。

## 边界守卫（已验证）

- `_run_all_migrations()` 函数体一行未动（`TestRepoGuards` 守卫调用形态）；
- session_manager / mysql_compat 未改动（专用连接/锁机制复用）；
- SQLite 路径行为不变（ALGORITHM / SET SESSION 仅 MySQL 下发，`test_sqlite_unchanged` 守卫）。

## 验收命令

```bash
pytest tests/atomic/test_issue_536_migrations.py tests/test_check_destructive_ddl.py -v  # 35 用例
python3 scripts/check-destructive-ddl.py                                                # PASS
python3 scripts/set_min_code_version.py <版本>                                          # 部署末步
```

