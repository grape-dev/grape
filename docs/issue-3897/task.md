# Task: Issue #3897

> 状态口径：`[x]` = 已落地并验证（提交/测试可在 PR #2017 追溯）；`[ ]` = 不在本期编码范围，交接后续环节。

- [x] T0（文档）`docs/issue-3897/{spec,plan,task}.md` + Issue 评论（方案与 seam 声明）。
      提交 `71f439a60`。
- [x] T1（红）A 组：`tests/runtime/test_issue_3897_autopilot_failure_cause.py` ——
      失败 exec 必须落结构化 cause、收口日志含 `attempts/max_retry/retry_skip` 且能解释 0 重试。
      提交 `b9814d5f4`。
- [x] T2（绿）A 组：`scheduler._autopilot_failure_cause` + `_finalize_autopilot_task` 写 cause +
      `_execute_task` 收口日志字段。提交 `03dd404fb`。
- [x] T3（红）B 组：`tests/mgmt/test_issue_3897_identity_relation.py` ——
      `uow("mgmt")` 走域引擎（单库走 local）、1146/`DomainUnavailable` 一次性降级（同 op 只 1 条 WARN）、
      `schema_probe.relation_kind` 双后端、`_check_identity_relations` 缺失时 ERROR 且不抛。
      提交 `1e5c8fcb0`。
- [x] T4（绿）B 组：`uow(domain)`、`grape_account_db`/`identity_lifecycle` 改 mgmt 域、
      `schema_probe.relation_kind`、`db_setup._check_identity_relations`、迁移 `202611230005`、
      视图补建白名单。提交 `6921c26b5`。
- [x] T5（红）C 组：`tests/runtime/im/test_issue_3897_transport_reconnect.py` +
      `tests/runtime/test_issue_3897_webhook_fingerprint.py` ——
      BrokenPipe 等不得落 `channel crashed`，必须 WARNING + 退避 + `ws_transport`；验签日志带指纹无明文。
      提交 `5ce641cac`。
- [x] T6（绿）C 组：`supervisor` 传输异常分支；`routes/webhook.py` 指纹日志。提交 `dba09b40e`
      （后续 `3bd461b6f` 去重异常族、`1778e6611` 补齐换行）。
- [x] T7（红）D 组：`tests/kernel/test_issue_3897_repobot_fallback_log.py` +
      `tests/runtime/im/test_issue_3897_feishu_endpoint_session.py`。提交 `d2857de6d`。
- [x] T8（绿）D 组：`token_resolver` 回退日志限频降噪；`feishu/channel.py` endpoint session 关闭。
      提交 `0fec3292a`（`c92666c30` 改用 port 桩消除顺序假红）。
- [x] T9 自审查（5 轴 + pre-pr 增量检查）→ 建 PR（base `arch-weekly-11-ljt`）→ Issue 评论 →
      A2A QA → `grape tasks complete`。PR #2017。
- [ ] T10（sre，非本期编码）按 A/B/C/D 四组 LogQL + 时间窗取证（前后计数对比）——部署后执行。
- [x] T11（QA R1 Required）`exit_code=None`/非法值容错：`_coerce_exit_code` 归一化为
      `-1` + 注记，`_finalize_autopilot_task` 入口先归一化；回归用例
      `test_finalize_with_none_exit_code_reaches_terminal_state` 等 3 条。提交 `c41f9670f` / `57b49e812`。
- [x] T12（QA R1 Optional）部署前置可诊断：`db_setup._check_identity_domain()` 在启动自检中
      检查 mgmt 域路由，缺 `GRAPE_DSN_MGMT` → ERROR + remediation（不阻断启动）；
      用例 `test_startup_self_check_reports_mgmt_domain_unavailable`。提交 `c41f9670f` / `57b49e812`。
- [x] T13（QA R1 Nit）收口日志 `retries_attempted` → `attempts`（含首次执行口径）；
      spec/plan/task 三份文档命名统一为 `schema_probe.relation_kind` 并回填完成态。