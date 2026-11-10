# Issue #1588 实施方案（Coding）

需求/Spec 权威来源：Issue #1588 评论「[issue agent] 📋 Spec Document — Issue #1588」（基线 weekly-10，行号已复核一致）。本文件只记录实施层方案，不重复 Spec。

## 交付范围（本次 A+B+E，一个 PR、两个原子提交）

### M1 提交（A 批 + E 批，零风险）
- `src/user/db.py` 删除 6 个 0 引用函数：`get_user_by_github_id`、`link_github_user`、`link_user_provider`、`_get_username_by_id`、`get_token_version`、`increment_token_version`（连带「JWT token_version 管理」空段头）。
- `src/runtime/engine_select.py` `_repo_user_id` docstring：删除「user_accounts 二级回退 / JOIN user_accounts」过时描述，改为与实现一致的 `grape_accounts`/`external_identities` 回退链说明。

### M2 提交（B 批 + 测试等价改写）
- `src/user/db.py` 删除 12 个函数：`create_user`、`get_user`、`get_user_by_username`、`list_users`、`delete_user`、`update_password`、`verify_user`、`get_user_by_gitcode_id`、`link_gitcode_user`、`get_all_user_accounts`、`get_user_account`、`save_user_account`、`delete_user_account`；连带删 `_deprecated_user_accounts_fn`、`USER_ACCOUNT_FIELDS`、模块头 deprecated 映射表、孤儿 `_hash_password`（仅被待删函数引用）。
- 测试改写（等价替换，不丢覆盖度）：
  - `tests/test_user_db.py`：夹具 `_mk_user/_cleanup_user` 切 `create_grape_account`/`soft_delete_account`；账户 CRUD 断言改写为 `grape_account_db` 等价用例；gitcode 绑定用例改写为 `create_external_identity`/`get_external_identity`；老表账户扩展用例改写为 `update_profile` 等价用例。
  - `tests/test_auth_routes.py`：`_cleanup` 去掉老表兜底；`test_create_user_failure_redirect` patch 目标改 `src.api.routes.grape_auth.create_grape_account_oauth`（当前回调建号 seam）。
  - `tests/test_issue_367_skill_scope_priority.py`：夹具切 `create_grape_account`/`soft_delete_account`。
  - `tests/test_tasks.py`：`TestUserMappingBackfill` 改为镜像当前 session_manager 启动回填 SQL（源 `grape_accounts`+`external_identities`），夹具切 `create_grape_account`/`create_external_identity`/`soft_delete_account`/`delete_external_identity`；teardown 子查询源改 `grape_accounts`。
  - `tests/ui_test_playwright.py`（e2e，默认排除）：建号/查号/清理 SQL 切 `grape_accounts`。

## Out of Scope（不动）
- C 批：`init_db()` user_accounts 建表/迁移段、`_USER_ACCOUNTS_DDL`、`src/api/main.py` L651-652、`src/api/grape_server.py` L230-231。
- D 批：`batch_update_last_active`、`get_credentials`、`migrate_oauth_providers()`/`migrate_accounts_from_json()`（含 default 账户迁移段）。

## 测试 seam
- 账号/绑定 seam：`src/auth/grape_account_db.py` 公开 API（`create_grape_account`/`soft_delete_account`/`verify_grape_account`/`update_password`/`update_profile`/`create_external_identity`/`get_external_identity`），复用现有 seam，不新建。
- 回填 seam：`session_manager` 启动回填 SQL（测试内等价镜像）。

## 验收（对应 Spec 5 条）
1. `grep -rn "_deprecated_user_accounts_fn" src/ tests/` 归零
2. `grep -rn "user_accounts" src/` 仅剩 init_db 历史段与 db_setup DDL/legacy 快照
3. pytest 全量通过，test_user_db.py 不再构造 user_accounts 数据
4. 手工验证项（刷新 token/登出再登入/修改密码）——由 QA/人工执行，PR 描述中标注
5. 启动日志无 deprecation warning（装饰器已整体删除）
