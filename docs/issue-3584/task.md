# Tasks: Issue #3584

- [x] 负载采集辅助（_engine_loads / _attach_loads）
- [x] 选择决策（_pick_decision：最小负载 + 轮转 + 日志）
- [x] pick_engine legacy 多引擎接入（SRE 同走通用链，Issue #3396 去特例）
- [x] explain_engine_selection 暴露 load
- [x] 单元测试 23 例（tests/runtime/test_issue_3584_engine_load_balance.py）
- [x] 既有测试回归核对（失败集合与基线一致，无新增失败）
- [ ] gt_kit.py 环境级分布回归（QA/运维执行）
