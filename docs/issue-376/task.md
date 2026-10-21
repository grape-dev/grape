# 任务拆解: Issue #376

| # | 任务 | 状态 |
|---|------|------|
| 1 | `transfer_business_data()` user_repos 两条 SQL 派生表包裹（grape_account_db.py） | ✅ |
| 2 | `latency.py` 超限清理 SQL 派生表包裹 | ✅ |
| 3 | 同类排查全库 `UPDATE/DELETE ... [NOT] IN (SELECT ... FROM 同表)`（其余命中均为跨表，安全） | ✅ |
| 4 | `table_maintenance.py` SQLite 分支清理 SQL 统一包裹写法 | ✅ |
| 5 | `validate_binding_ticket` 增加 `consume=False`；新增 `consume_binding_ticket()` | ✅ |
| 6 | transfer-bind 端点票据消费移到 `_ga_db.commit()` 成功后 | ✅ |
| 7 | 修正 `test_mysql_compat.py` 过期断言（`tasks` → `issue`，与翻译层实际输出对齐） | ✅ |
| 8 | 新增 `tests/test_issue_376_mysql_transfer_bind.py`（SQL 文本/SQLite 语义/票据两步/API 重试/翻译层 五组） | ✅ |
| 9 | SQLite 回归：新增测试 96 passed；相关子集与基线失败集合 diff 零新增 | ✅ |
| 10 | MySQL 8.0 实测（部署后观察，生产双主 500→410 连发序列消失） | ☐ 待部署验证 |

## 提交计划（原子提交）
1. `fix: #376 派生表包裹消除同表子查询（MySQL 1093）`：grape_account_db.py（SQL 部分）+ latency.py + table_maintenance.py + 对应测试。
2. `fix: #376 票据校验/消费拆分，失败重试不再 410`：grape_account_db.py（票据部分）+ grape_auth.py + 对应测试 + test_mysql_compat.py 断言修正。
