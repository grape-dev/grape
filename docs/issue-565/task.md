# Task: Issue #565 拆解

| # | 任务 | 状态 |
|---|------|------|
| 1 | docs/issue-565 spec/plan/task 文档 | done |
| 2 | event_pipeline.py：模块级滑动窗口限流 `_bot_open_recommend_rate_limited` | done |
| 3 | event_pipeline.py：自循环检测主路径放行 + `evt["_bot_opened"]` 标记 | done |
| 4 | event_pipeline.py：fallback 路径同构放行 | done |
| 5 | event_pipeline.py：2.0c 限流插入（放行后、路由前；子任务不占配额） | done |
| 6 | event_pipeline.py：`_redispatch_vod_after_recommend` 执行棒短路 | done |
| 7 | runtime/service.py：worker 侧回调按建单人还原 `_bot_opened` | done |
| 8 | tests/test_bot_opened_recommend.py（T1–T5） | done |
| 9 | 既有测试回归（453/path_b/pipeline/528/task_complete 等） | done |
| 10 | 自审查 + pre-pr 检查 + PR + A2A QA | done |
