# Issue #995 方案设计

## 数据层
- `issue` 表新增 `vcs_exists INTEGER NOT NULL DEFAULT 1`、`vcs_checked_at TEXT DEFAULT NULL`。
- 迁移沿用 `_ensure_column` 幂等补列（SQLite/MySQL 一致）；新库 DDL 直接包含。

## 索引层（src/api/issue_vcs_index.py）
- 按 `repo_key`（provider 前缀）分页拉取 issue 编号集合，gitcode/github 分别对齐。
- 进程内 TTL 缓存（300s）；失败/无 token/超时返回 `None`，短缓存 30s。
- `refresh_vcs_existence(repo_keys)` 比对台账并批量回写 `vcs_exists`/`vcs_checked_at`。
- `backfill_vcs_exists()` 启动后异步执行，仅处理未校验仓库，幂等可重跑。

## API 层
- `list_repo_issues` 先刷新存在性，再在 SQL `where` 追加 `vcs_exists != 0`（仅 `ok` provider），
  因此三种模式与分页 COUNT 口径一致；`unavailable` provider 不参与过滤（fail-open）。
- 新增 `include_deleted`（bool，接受 1/true）；返回项补 `vcs_exists`/`is_vcs_deleted`/`vcs_check`。
- PR 伪卡片不参与已删除过滤。

## 文档
- FastAPI operation description、`help/api/11-issues.md`、`docs/openapi/11-issues.md`。
