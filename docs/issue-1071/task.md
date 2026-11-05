# 任务拆解：Issue #1071

| # | 任务 | 产出 | 验证 |
|---|------|------|------|
| 1 | 红测试：活跃流豁免（T1）+ 告警（T6）在现实现下失败 | tests/test_issue_1071_pending_stream_exemption.py | pytest 跑红 |
| 2 | 绿实现：sweep 判死前 task_messages 活跃豁免（600s 窗） | src/runtime/scheduler.py | T1 转绿 |
| 3 | 绿实现：PENDING_SELF_REPORT_TIMEOUT_SECONDS 1800→5400 | src/runtime/scheduler.py | T4/T5 转绿 |
| 4 | 绿实现：complete_task 终态拒收 + 成功上报 + 证据齐全 → 告警日志 | src/runtime/service.py | T6 转绿 |
| 5 | 沉默判死/无消息回退/全链路回归（T2/T3/T7） | 测试文件补全 | 全绿 |
| 6 | 回归：test_issue_537 + test_issue_588 全量通过 | — | pytest 0 fail |
| 7 | 自审查（5 轴）+ 前置增量检查（黑名单拦截 + 敏感扫描） | — | 两条通过 |
| 8 | push + 建 PR（base=weekly-10）+ Issue 评论 + A2A QA | PR | 链路完整 |
