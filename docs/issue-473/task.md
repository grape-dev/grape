# Task: Issue #473

- [x] T1 后端：`_rt_in_range` helper + `get_agent_runtimes`/`get_runtime_devices` 新增 `range` 参数与过滤
- [x] T2 前端：runtimes.js 范围 chips 控件 + `range` 参数透传 + 清除联动
- [x] T3 测试：tests/test_issue_473_range_filter.py（红→绿）+ tests/js/test_issue_473_range_filter.js
- [x] T4 回归：pytest 相关模块 + node JS 测试通过
- [x] T5 自审查（5 轴 + pre-pr 增量检查）→ 建 PR（base weekly-09）→ 评论 → A2A QA → tasks complete

## PR #668 审查修复（QA 交办，追加）

- [x] T6 先红：tests/test_issue_473_local_engine_range.py——本人本机引擎 range=user 可见（红：输出 owner 恒空不可达）+ 非法 range 应 422（红：静默回落全量）
- [x] T7 Required 修复：`_rt_in_range` is_local 条目改按 DB 行 owner_id（local_owner 参数）判定归属；`_build_runtime_devices` 本机设备行补 `local_owner_id`（含挂到主控机凭据设备分支）
- [x] T8 Nice-to-have ①②③：range 参数 Literal[""/"all"/"repo"/"user"] 非法 422；range 遮蔽内置注释说明（文件无内置 range() 调用）；windows-smoke.yml 新增 linux-backend job 接入 #473 测试
- [x] T9 JS 源断言同步（RTRange/local_owner 断言）+ docs（spec 验收标准补 PR #668 修复项、plan 追加修复方案与提交拆分）
- [x] T10 回归：test_issue_473_range_filter（10 例）+ test_issue_473_local_engine_range（7 例）+ #338/#371 兄弟用例 + node 源断言全通过
