# Plan — Issue #540

## 任务拆解

1. **新增 `src/repo_migrate.py`**：`migrate_repo_key(old, new_owner, new_repo, operator_id)`
   - 原子 DB 事务（managed_repos / user_repos / _REPO_KEY_TABLES / skill / meta）
   - UNIQUE 冲突处理（删旧留新，镜像 `_migrate_repo_key_tables` 行级处理）
   - 工作区目录物理迁移（DB 提交后）
   - `RepoMigrateError` + 审计日志

2. **新增端点 `POST /api/repos/migrate`**（`src/api/routes/repos.py`）
   - `MigrateRepoBody` pydantic model
   - 前置校验（源存在 / 目标不存在 / provider 一致 / 目标 VCS maintainer 权限）
   - 调用 `migrate_repo_key`，返回统计

3. **测试 `tests/test_repo_migrate.py`**
   - 批量改写全表生效
   - UNIQUE 冲突删旧留新
   - 失败回滚无半改写
   - 工作区目录迁移
   - 前置校验（目标已存在 / 源不存在）

4. **前端 `settings.js`**：迁移按钮 + 目标组织/仓库输入对话框
5. **i18n**：zh/en `settings.host.migrate_*` 键

## 文件清单

- `src/repo_migrate.py`（新增）
- `src/api/routes/repos.py`（新增端点 + MigrateRepoBody）
- `tests/test_repo_migrate.py`（新增）
- `src/api/static/js/pages/settings.js`（迁移 UI）
- `src/api/static/locales/zh/settings.json`（i18n）
- `src/api/static/locales/en/settings.json`（i18n）
