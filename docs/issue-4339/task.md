# Issue #4339 任务拆解

| # | 任务 | 产出 | 验证 |
|---|------|------|------|
| T1 | 建表 DDL 升级 | `src/domains/runtime/schema_ddl.py` 的 `payload MEDIUMTEXT DEFAULT '{}'` | 静态用例断言 DDL 文本 |
| T2 | ALTER 常量 | `src/domains/runtime/migrations_ddl.py::ALTER_EVENTS__EVENTS_PAYLOAD_MEDIUMTEXT`（含 `MODIFY COLUMN`） | 静态用例断言关键字 |
| T3 | runtime 幂等迁移 | `src/domains/runtime/migrations/events_payload_mediumtext.py`（探测短路 / 非 owner 跳过 / 复检 / post_check） | 假 cursor 用例覆盖 5 条分支 |
| T4 | 迁移注册 | `src/api/db_setup.py` 注册 `202611250001`（runtime） | `test_migration_version_integrity` / `test_migration_track_guard` |
| T5 | 写入口守卫 | `src/domains/runtime/event_store.py`（>16MB ERROR 拒绝、>1MB WARNING） | 真 SQLite 库用例 |
| T6 | 卡片表同批整改 | `src/domains/mgmt/schema_ddl.py` + `migrations_ddl.py` + `migrations/channel_outbound_card_payload_mediumtext.py` + 注册 `202611250002` | 静态 DDL 断言 + 迁移分支用例 |
| T7 | 版本号门禁 | `202611250001/2` 均大于基线 `max_version=202611230012`；基线快照属发布评审产物，本 PR 不刷新 | `test_migration_version_integrity` |
| T8 | 存量坏行口径 | PR 描述中的只读盘点 SQL + 处置结论（接受缺失 / 按需重放） | 人工复核 |
| T9 | 门禁 | 目标测试 + `tests/kernel/test_migration_parity_stage1.py` + `tests/api/test_destructive_ddl_stamp_scenarios.py` + `make lint` + `scripts/check-arch.py` | 全绿 |

依赖顺序：T1/T2 → T3 → T4 → T7；T5 独立；T6 依赖 T3 的形态先例（复制结构，独立版本号）。
T8 只写文档，不改代码，确保 DDL 变更与数据处置分离。