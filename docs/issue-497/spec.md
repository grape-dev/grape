# Spec: Issue #497 — GitCode 登录名变更后 OAuth 登录同步 external_login（治本，消除建单 assignee 404）

> 依据 Issue Agent 已发布的 Spec（评论 id 196674229），本文件为 coding 执行用精简版。

## Objective
OAuth 登录回调 binding 命中时自动检测并同步 GitCode 登录名变更（external_id 不变、login 变），四表一致：
external_identities.external_login、grape_accounts.username、unified_user_mapping 平台列、login_whitelist。
建单 assignee 解析（#343 口径取 external_login）不再 404。

## Scope
- src/auth/grape_account_db.py：新增 `sync_external_login_on_rename`（四表同步 + audit_logs）；`_sync_um_platform_col` 从 grape_auth.py 移入本模块复用。
- src/api/routes/grape_auth.py：`_handle_oauth_login` 已绑定分支 + rebind 路径接入对比与同步。
- src/auth/audit.py：EVENT_TYPES 增加 `login_rename`。
- src/api/issues_center.py：不改逻辑（仅验证）。

## Acceptance Criteria
- 改名用户重新 OAuth 登录后四表自动同步为新登录名；
- username 更新，UNIQUE 冲突时保留旧 username 且 external_login 仍正确；
- audit_logs 记录 old/new；
- 随后建单 assignee 解析成功，不再 "user <旧名> not found" 404；
- 未改名用户登录/建单零回归；现有测试全过 + 新增测试覆盖（TDD）。