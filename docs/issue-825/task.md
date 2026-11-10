# Tasks: Issue #825

- [x] T1 文档：spec/plan/task（docs/issue-825/）
- [x] T2 src/im/engine_wiring.py：开关 + 迁移接线 + Sqlite 仓储（dedup/binding）+ LegacyCompatLeaseStore + ImEngine 装配 + Feishu 工厂（Router 六注入点真实实现）
- [x] T3 src/api/main.py：开关分支 + legacy 封装 + mode watcher 秒级切回 + shutdown/health 收口
- [x] T4 tests/im/test_engine_wiring.py：开关两态 / 注入点集成 / 回滚 / lease 互斥 / 迁移幂等
- [x] T5 回归 tests/im 全绿
- [x] T6 自审查（5 轴 + pre-pr 增量检查）→ PR → 评论 → A2A QA
