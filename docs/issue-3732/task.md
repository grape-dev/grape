# Issue #3732 任务拆解（task）

| # | 切片 | 提交 | 验证 |
|---|------|------|------|
| 1 | 失败测试先行：双 DSN 路由 / strict / 启动自检用例 | `test(#3732)` | 新用例在基线上红 |
| 2 | persistence 域工厂：`get_domain_engine` + `uow(domain)` 入口收敛 | `feat(#3732)` | 回落用例 + 路由用例绿 |
| 3 | grape_account_db 四 ORM 点改 `_uow(_IDENTITY_DOMAIN)` + strict 通道 + 结构化日志 | `feat(#3732)` | 双 DSN fixture 三读函数绿 |
| 4 | 调用方标注（repo_roles / vcs/base.fetch_owner_identity_token / user db） | `feat(#3732)` | strict 失败用例绿 |
| 5 | 启动自检 `warn_domain_table_residency` + 四 server 接线 | `feat(#3732)` | 自检正/负用例绿 |
| 6 | 全量回归：现有测试套 | — | pytest 全绿 |
| 7 | 自审查（5 轴 + pre-pr 增量检查）→ 建 PR → 评论 + A2A QA | — | — |

L667 / L1775 / L1810 域限定结论：均 mgmt 域表 ORM 事务，统一走 `_uow(_IDENTITY_DOMAIN)`
（详见 plan.md §2 表格与代码注释）。
