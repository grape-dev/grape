# Task: Issue #975

- [x] T1 client.py：`_request` 记录 last_http_error；新增 ReportBatchError；
      report_messages 三态（dict/None/{}/raise）
- [x] T2 message_report.py：split_resend 二分拆批 + degrade_failed_batch
      （占位 + 截断入队）
- [x] T3 agent.py：_flush_report_batch（成功才清 batch、retry 保留、terminal
      中断、降级、seq 对账日志）；执行循环两处 flush 点接入
- [x] T4 worker_report_queue.py：kind=messages 条目 + 补报重放（ack 出队/
      网络保留/4xx 占位出队/终态出队）
- [x] T5 service.py + routes/runtime.py：peek_next_seq、seq_start 预约落库、
      /messages 回执带 seq 范围、批次回执日志
- [x] T6 新增测试 tests/test_issue_975_report_batch.py（413 拆批无缺号、
      占位可见+入队、网络保留 batch、410 终态、重放出队、seq 预留）+
      既有相关测试全过（test_event_loop_block 1 例为基线既有失败，与本改动无关）
- [x] T7 自审查（5 轴 + pre-pr 增量检查）→ 建 PR → 评论 → A2A QA
