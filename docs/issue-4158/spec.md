# Issue #4158 需求文档（Spec）：同名顶层定义覆盖导致 A4 全渠道通知退回飞书旧链路

## 1. 现象与证据

- 用户在非飞书渠道（微信 / 企微 / Slack）已连接的情况下，Issue 进入 `pending_review` /
  `awaiting_input` / 任务分配三个节点时**收不到任何通知**；
  指标侧只有 `skipped_no_ctx` 增长，`sent_ok` 不动。
- 日志形态：`[im-notify] type=pending_review ... ctx=none reason=no_ctx`，
  且「`open_id` 明明是空、`user_id` 明明是 UUID」也被判死。
- 根因：`issues_center.py` 里整段复制粘贴出**第二份** `_run_*_notify`（只走飞书卡片，
  `ctx is None` 即 `reason=no_ctx` 丢弃）。Python **后定义覆盖前定义**，A4 全渠道
  `notify_user` 扇出版被排在前面的旧实现静默顶掉；`_resolve_issue_notify_targets` /
  `_resolve_user_chat_bot` 同样是「旧版覆盖新版」。
- 同形态重复定义散落在 5 个文件（`issues_center.py` 22 处 / `runtime/public` 9 /
  `mgmt/public` 5 / `db_setup.py` 1 / `routes/issues.py` 1），另加 `mgmt/routes/skills.py` 1 处；
  全程**无任何静态门禁**可见（`ruff F811` 只看 import 别名重定义，不报顶层同名 def 副本）。

## 2. 目标

1. 三族通知（任务分配 / 待澄清 / 待审核）恢复 A4 全渠道扇出，`ctx=None` 不再是丢弃理由；
2. UUID 账号 id（D105）不被 `int(user_id)` 短路成「不可达」；
3. 6 个文件的顶层同名 def/class 归零，并加**静态门禁 + CI 入口**，防止复制粘贴复发；
4. 三族 `_IM_NOTIFY_COUNTERS` 的 else 口径一致：真无渠道 → `sent_failed` + `skipped_no_ctx`；
   有渠道但发送失败 → 只计 `sent_failed`。

## 3. 非目标

- 不改通知正文/卡片样式，不改 `notify_user` 扇出与 outbox 语义；
- 不清零全仓 `ruff F811` 的 72 处存量（import/局部别名重定义），只把它纳入棘轮（新增即失败）；
- 不处理 `tests/runtime/test_issue_pending_review.py` 在基线上的 `datatype mismatch`（既有环境/迁移问题）。

## 4. 验收

| # | 验收项 | 复算方式 |
|---|--------|----------|
| 1 | 6 文件顶层同名 def/class = 0 | `python3 scripts/check_duplicate_defs.py src`（基线 0） |
| 2 | `ctx=None` + 仅非飞书渠道 → 三族均 `sent_ok`，无 `skipped_*` | `pytest -q tests/runtime/test_issue_4158_notify_shadowing.py` |
| 3 | 真无渠道 → `skipped_no_ctx`；有渠道但失败 → 只 `sent_failed`（三族一致） | 同上（参数化用例 `test_no_channel_counts_skipped_no_ctx_for_all_families` / `test_channel_failure_is_not_counted_as_no_channel`） |
| 4 | UUID 账号 id 不被短路 | 同上（`test_resolve_issue_notify_targets_keeps_uuid`） |
| 5 | 门禁进 CI，新增同名定义/F811 即失败 | `.github/workflows/lint.yml` → `make lint`（`make lint` 内含 `check_duplicate_defs.py` + `check_f811.py`） |