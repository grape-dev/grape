# Task: Issue #2133

- [x] T1 认领闸门显式挡 spawn_failed（service.py）
- [x] T2 数据库结构：4 个追踪列 + engine_lifecycle_audit + 幂等迁移
- [x] T3 recovery 模块：时间窗告警/degraded、ok streak、场景加回、任务对账、审计
- [x] T4 心跳集成：spawn_failed / ok / fail / 其他分支挂接
- [x] T5 后端一键加回端点（settings）
- [x] T6 前端引擎卡片 degraded 高亮 + 一键加回按钮
- [x] T7 单测 + #1982 复现链路回归（tests/runtime/test_issue_2133_recovery.py）
- [x] T8 运行相关测试（1982 探活/fd、health convergence、2133 新增）