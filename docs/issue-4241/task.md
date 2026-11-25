# Task: Issue #4241

> 基线 `arch-weekly-11-ljt`｜分支 `feat/issue-4241`｜PR #2105 按裁定不复用（按新基线重建）

- [x] T1 复核新基线缺陷仍在：`list_managed_repos` 仍为 `bool(webhook_id)`；`get_webhook_url` 仍 server-config 优先；
      `managed_repos` 无地址列；全仓无 `webhook_stale`。
- [x] T2 `managed_repos.webhook_url` 列：DDL + 存量库幂等补列（`legacy_column_migrations`）。
- [x] T3 `add_managed_repo` / `_update_repo_bot_token` 写入实际注册地址；`get_managed_repos` 随行返回。
- [x] T4 `get_webhook_url()` 反转为 meta 权威 + server-config 一次性种子（`_WEBHOOK_URL_SEEDED`）。
- [x] T5 三态判定 `webhook_state_for_row` / `webhook_state` + 地址归一化。
- [x] T6 `/repos/managed` 输出 `webhook_ok`(兼容) + `webhook_stale` + `webhook_state`；`/repos/member` 托管行带 `webhook_state`。
- [x] T7 托管/建仓/修复/bot token 重建成功路径回写实际注册地址（6 个 add_managed_repo 点位 + repair_host + db 重建链）。
- [x] T8 `_check_host_status` 增 `webhook_stale`（零远程调用，不覆盖 partial/bot_missing 诊断），修复后自愈。
- [x] T9 `mgmt.public.webhook_state` 跨域只读门面。
- [x] T10 前端：DashboardView 横幅 stale 分支；ReposView 三态徽标 + 修复托管按钮；host-status.js + zh/en 文案。
- [x] T11 新增 `tests/mgmt/test_issue_4241_webhook_state.py`（16 用例：四态 + 列表输出 + 仪表盘自愈 + 权威源）。
- [x] T12 文档 `docs/issue-4241/{spec,plan,task}.md`。
- [x] T13 回归验证（mgmt/insight 套件 + lint + check-arch）、自审查、敏感信息增量扫描、建 PR、A2A QA。