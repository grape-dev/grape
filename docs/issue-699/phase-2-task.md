# Phase 2 Task Breakdown（Issue #699）

| # | 任务 | 产出 | 状态 |
|---|------|------|------|
| 1 | 文档 | docs/issue-699/phase-2-{spec,plan,task}.md | ☑ |
| 2 | kernel/db.py | 注册表 + t_* + qualify_sql + SQLite 代理 | ☑ |
| 3 | 挂接 | session_manager / mysql_compat | ☑ |
| 4 | contracts 空壳 | MgmtService / RuntimeService / InsightService | ☑ |
| 5 | 迁移脚本 | scripts/migrate_schemas.py（sqlite+mysql，幂等） | ☑ |
| 6 | codemod 工具 | scripts/replace_table_refs.py | ☑ |
| 7 | 测试 | tests/test_kernel_db.py（TC-2.1/2.3/2.6 + 改写安全） | ☑ |
| 8 | 回归 | 全量 pytest + check-arch 通过 | ☑ |
| 9 | 收尾 | push feat/issue-699 → PR !836 追加 Phase 2 → Issue 评论 → A2A QA | ☑ |

风险与缓解：
- 字面量/列名误改写 → 字面量分段 + 表名语法位置限定 + 专项单测。
- 存量库前缀切换 → migrate_schemas.py 幂等 RENAME + 自动备份；灰度开关默认关。
- MySQL 真机验收缺失 → 脚本交付 + Phase 6 集群模式统一验收（与设计 §12 一致）。
