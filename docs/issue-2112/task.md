# Issue #2112 任务拆解

| # | 切片 | 产出 | 验证 |
|---|------|------|------|
| 1 | 表结构迁移 | `db_setup.py` 两张表 + 索引 + register_migration | `PRAGMA table_info` |
| 2 | 取消逻辑抽取 | `runtime/issue_cancel.py`，`cancel_issue` 路由复用 | 既有 cancel 测试 |
| 3 | 审计事件登记 | `auth/audit.py` 6 个 EVENT_TYPES | 审计落库用例 |
| 4 | 编排核心 | `mgmt/org_dispatch.py` 状态机/场景/建单/汇总/重试 | 单元用例 |
| 5 | 下派 API | `routes/org_dispatches.py` 4 类端点 | TestClient 用例 |
| 6 | 路由注册 | `grape_server.py` + `main.py` | `/api/orgs/{id}/dispatches` 可达 |
| 7 | sweeper | `server_tasks.py` 注册/回收 | `sweep_once()` 用例 |
| 8 | 测试 | `tests/mgmt/test_org_dispatch.py` 8 类场景 | pytest 全绿 |