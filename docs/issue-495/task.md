# 任务拆解：Issue #495

- [x] T1 新增 circuit_breaker 模块（计数 / 熔断 / 成功清零 / 阈值可配置）
- [x] T2 claim_by_tag 增加 healthy=1 门控
- [x] T3 claim（legacy）增加 healthy=1 门控
- [x] T4 fail_task 钩子：连续失败计数
- [x] T5 complete_task 钩子：成功清零
- [x] T6 新增单测 tests/test_issue_495_circuit_breaker.py
- [x] T7 回归现有相关测试（claim / local_claimer / engine_select 等）
