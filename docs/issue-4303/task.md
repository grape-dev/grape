# Issue #4303 任务拆解（Task）

| # | 任务 | 文件 | 验证 |
|---|------|------|------|
| 1 | RED：新增 `tests/runtime/test_4303_sweep_own_dsn.py`（三库拓扑方言层 + 级联回填 + 重启窗口 + 逐条降级/最后手段 bulk 绑定） | tests/runtime/test_4303_sweep_own_dsn.py | 新用例在未修复代码上按预期红（TypeError 路径） |
| 2 | 既有受护栏影响测试文件加 `_PROCESS_STARTED_AT` autouse fixture | test_cleanup.py / test_pending_self_report_convergence.py / test_issue_587_event_convergence.py | 既有用例与基线失败集一致（无新增失败） |
| 3 | GREEN：`_commit()` 自递归修复 | cleanup.py | 单测 + 既有套件 |
| 4 | GREEN：重启恢复窗口护栏（`_PROCESS_STARTED_AT` + `_restart_grace_minutes()` + 入口判定） | cleanup.py | 用例 3 |
| 5 | GREEN：`_second_sweep_cursor` 方言层修复 | cleanup.py | 用例 1/2 |
| 6 | GREEN：per-seq 重构 `_recover_stuck_events`（逐条降级 + 最后手段 bulk 占位符绑定） | cleanup.py | 用例 1/2/4 |
| 7 | CI 接线：新测试文件加入 windows-smoke.yml linux-backend 步骤 | .github/workflows/windows-smoke.yml | `python -c "import yaml; ..."` 校验通过 |
| 8 | 回归：新文件 + 4 个受影响测试文件 + tests/arch/test_cleanup_import_closure.py | — | 与基线失败集 diff=0（无新增失败） |
| 9 | 自审查（5 轴 + 开源前置增量检查）→ commit/push → 建 PR → Issue 评论 → A2A QA → tasks complete | — | runbook Step 5-8 |
