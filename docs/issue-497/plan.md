# Plan: Issue #497 — 登录名变更同步实现方案

## 方案概述
OAuth 回调「已绑定」分支 + rebind 路径做登录名一致性对比，不一致时调 `sync_external_login_on_rename` 四表原子同步并写 audit_logs。对比先行、只有不一致才写，避免每次登录无谓写放大。

## 改动点

### 1. src/auth/grape_account_db.py
- 迁移 `_sync_um_platform_col`（原在 grape_auth.py，数据层归属本模块，避免循环 import）。
- 新增 `sync_external_login_on_rename(grape_user_id, provider, new_external_login, old_external_login="") -> bool`：
  1. new 为空或 new==old → 返回 False（无变更不写）；
  2. UPDATE external_identities.external_login（status IN (1,4)）；
  3. UPDATE grape_accounts.username；UNIQUE 冲突捕获 → 保留旧 username（不阻断）；
  4. 复用 `_sync_um_platform_col` 同步 unified_user_mapping 平台列；
  5. UPDATE login_whitelist 对应 provider 列（旧名→新名），UNIQUE 冲突捕获不阻断；
  6. log_audit("login_rename", detail={provider, old_login, new_login})。

### 2. src/api/routes/grape_auth.py
- `_handle_oauth_login` 已绑定分支：token 更新后，对比 `binding["external_login"]` 与 `user_info.external_login`，不一致 → 调 sync。
- rebind 路径：`restore_external_identity` 成功后，对比 `unbound["external_login"]` 与 `user_info.external_login`，不一致 → 调 sync。

### 3. src/auth/audit.py
- EVENT_TYPES 增加 `"login_rename"`。

### 4. src/api/issues_center.py
- 不改逻辑；新增测试验证 assignee fallback 在同步后取到新名。

## 测试策略（TDD）
- tests/test_issue_497_login_rename_sync.py：
  - 同步函数单元：四表同步（含 username 更新）、UNIQUE 冲突保留旧 username、未改名不写、audit 留痕；
  - 路由级：已绑定分支改名登录 → 库内四表更新；rebind 路径改名 → 更新；
  - issues_center assignee fallback 验证（构造已同步用户，断言 assignee 解析为新名）。