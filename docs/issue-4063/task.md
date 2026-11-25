# Issue #4063 任务拆解（Task）

- [x] 事件派发：`issue_comment` 不消费 `pending_review`（event_pipeline）。
- [x] 置位幂等：内容快照 + `changed` 判据（issues_center）。
- [x] 迁移：`issue.review_points` / `issue.review_refs`（注册式 202611240003）。
- [x] 读模型：detail / status get 只读 `review_point.{status,references}`。
- [x] 契约说明：权威 `issue.status` 与投影字段关系写入 operation description。
- [x] 回归测试：`tests/runtime/test_issue_4063_review_point_residency.py`。
- [x] 既有用例更新：`test_issue_2381_review_checkpoint`、`test_issue_comment_resume_ledger`（评论不再消费审核点）。
- [ ] QA 复核 AC1-AC6。

## 验收命令
```bash
python -m pytest tests/runtime/test_issue_4063_review_point_residency.py \
  tests/runtime/test_issue_2381_review_checkpoint.py -q
```