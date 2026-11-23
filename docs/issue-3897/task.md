# Task: Issue #3897

- [ ] T0（文档）`docs/issue-3897/{spec,plan,task}.md` + Issue 评论（方案与 seam 声明）。
- [ ] T1（红）A 组：`tests/runtime/test_issue_3897_autopilot_failure_cause.py` ——
      失败 exec 必须落结构化 cause、收口日志含 `retries_attempted/max_retry/retry_skip` 且能解释 0 重试。
- [ ] T2（绿）A 组：`scheduler._autopilot_failure_cause` + `_finalize_autopilot_task` 写 cause +
      `_execute_task` 收口日志字段。
- [ ] T3（红）B 组：`tests/mgmt/test_issue_3897_identity_relation.py` ——
      `uow("mgmt")` 走域引擎（单库走 local）、1146/`DomainUnavailable` 一次性降级（同 op 只 1 条 WARN）、
      `schema_probe.relation_exists` 双后端、`_check_identity_relations` 缺失时 ERROR 且不抛。
- [ ] T4（绿）B 组：`uow(domain)`、`grape_account_db`/`identity_lifecycle` 改 mgmt 域、
      `schema_probe.relation_exists`、`db_setup._check_identity_relations`、迁移 `202611230005`、
      视图补建白名单。
- [ ] T5（红）C 组：`tests/runtime/im/test_issue_3897_transport_reconnect.py` +
      `tests/runtime/test_issue_3897_webhook_fingerprint.py` ——
      BrokenPipe 等不得落 `channel crashed`，必须 WARNING + 退避 + `ws_transport`；验签日志带指纹无明文。
- [ ] T6（绿）C 组：`supervisor` 传输异常分支；`routes/webhook.py` 指纹日志。
- [ ] T7（红）D 组：`tests/kernel/test_issue_3897_repobot_fallback_log.py` +
      `tests/runtime/im/test_issue_3897_feishu_endpoint_session.py`。
- [ ] T8（绿）D 组：`token_resolver` 回退日志限频降噪；`feishu/channel.py` endpoint session 关闭。
- [ ] T9 自审查（5 轴 + pre-pr 增量检查）→ 建 PR（base `arch-weekly-11-ljt`）→ Issue 评论 → A2A QA →
      `grape tasks complete`。
- [ ] T10（sre，非本期编码）按 A/B/C/D 四组 LogQL + 时间窗取证（前后计数对比）。
