# Task: Issue #1076

- [x] T1 `IssueUpdateBody.milestone_id` 前置校验（非数字/负数可读 4xx）
- [x] T2 GitCode milestone number 预校验（只认 `number`，不认 REST 内部 id）
- [x] T3 回读不一致改 422 + 失败回滚原 milestone，5xx 仅保留上游故障
- [x] T4 测试：合法 number、3/999999/非数字 4xx、失败保持原值、上游 5xx
- [x] T5 文档：取值来源与错误码语义（help/api + docs/openapi）