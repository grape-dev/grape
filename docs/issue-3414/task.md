# Tasks: Issue #3414

- [x] T0 根因定位：grape-kit #6 traceback 确认 `cannot import name 'detect_engine_model'`；现行基线经 public wrapper 调用期已由内层 except 兜住，残余 500 面为 public 门面自身导入失败 → agent-runtimes 500 → 派单链路断。
- [x] T1 settings.py `_attach_engine_models` public 导入失败降级（防陈旧部署 500）。
- [x] T2 branch_guard.py canonical 依赖基线复核（#2706 收敛点已满足）+ 补 traceability 注释（无功能变更）。
- [x] T3 新增 tests/test_import_symbol_integrity.py（全仓导入符号完整性冒烟）。
- [x] T4 worker 包 import 闭包冒烟补 engine_model；新增 issue-3414 行为单测。
- [x] T5 回归相关测试（settings / branch_guard / worker_package）。
- [x] T6 自审查（5 轴 + 开源前置检查）→ 建 PR → Issue 评论 → A2A QA。
