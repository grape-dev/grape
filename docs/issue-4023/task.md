# Issue #4023 任务拆解

| # | 任务 | 状态 |
|---|---|---|
| 1 | RED：先写 `test_issue_4023_baseline_failure_visibility.py` 契约用例 | ✅ db4e6a56f |
| 2 | GREEN：`format_one_line()/to_log_fields()/parse_baseline_error_text()` + `_format_failure_error()` + 截断护栏 | ✅ b698cd783 |
| 3 | 打点/重置/key 归一 + errors JSON 落库 + result_text 复用 `_err_log` | ✅ f1bea460d |
| 4 | 复审 Required：用例接入 `lock-regression` CI 门禁 | ✅ 本轮 |
| 5 | 复审 Optional：`.env.example` 登记三个环境变量 | ✅ 本轮 |
| 6 | 复审 Optional：`parse_baseline_error_text` 需 issue+reason 才判基线错误（+用例） | ✅ 本轮 |
| 7 | 复审 Optional：成功重置 issue 级全量清链口径在 docstring/spec 明确 | ✅ 本轮 |
| 8 | 复审 Optional：`result_text` 硬截断改复用 `_err_log` | ✅ 本轮 |
| 9 | 补 `docs/issue-4023/{spec,plan,task}.md`（消除悬空 Spec 引用） | ✅ 本轮 |
