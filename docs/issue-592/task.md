# Tasks: Issue #592

- [x] 定位弹窗数据源（/api/dashboard agent_engines + dashboard.js _openEngineModalImpl）
- [x] 根因 A：状态派生误用远端心跳口径 → 新增本机直连探活口径 derive_local_runtime_health
- [x] 根因 B：local 档无标签历史行被 agent_type 过滤 → query_engines local 默认 all
- [x] dashboard.py / settings.py 接入新口径
- [x] 新增测试 tests/test_issue_592_engine_modal_local.py
- [x] 回归：test_issue_227_engine_modal / test_engine_select / 相关 settings 测试
