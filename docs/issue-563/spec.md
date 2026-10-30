# Spec: Issue #563 — pulls comments 补 delete/update 并修复 create --json @file 静默失败

## Objective
- 为 `grape git-repo pulls comments` 契约补齐 `delete` / `update` 命令（本仓服务端 git-repo 代理端点 + OpenAPI 契约）。
- 修复 `create --json @file` 文件读取失败静默行为（CLI 端，grape-cli 仓库范围）。

## Scope
### In Scope（本仓 grape-dev/grape）
- `src/api/git_repo_api.py`：新增 `PATCH/DELETE /api/git-repo/{owner}/{repo}/pulls/{number}/comments/{comment_id}` 端点（operation_id=`gitrepo_pulls_comments_update` / `gitrepo_pulls_comments_delete`），透传 GitCode v5 `PATCH/DELETE /repos/{owner}/{repo}/pulls/comments/{id}`。
- `src/api/openapi.py`：两个新 operation_id 登记进 `_SESSION_ONLY_OPERATIONS`（session_only scope）。
- `src/api/main.py`：契约版本 2.1.0 → 2.2.0（触发旧 CLI 升级提示链路）。
- `help/api/10-git-repos.md`：补命令文档与示例。
- `tests/test_git_repo_api.py`：新增 update/delete URL/方法/body 映射测试。

### Out of Scope（本仓不可实现）
- CLI 端 `--json @file` 读取失败的报错行为属于 grape-cli 公开仓库（gitcode.com/grape-dev/grape-cli）：实测 CLI 0.3.3 已显式报错 `cannot read --json file '...'` 且退出码 1，验收项已满足，无需本仓改动。

## Acceptance Criteria
- [ ] 新契约端点进 OpenAPI（`grape refresh` 后 CLI 自动获得 `pulls comments delete/update` 命令）
- [ ] update 透传 PATCH + `{"body": ...}` 到 `/pulls/comments/{id}`；delete 透传 DELETE
- [ ] 契约版本号 2.2.0
- [ ] 现有测试全部通过 + 新增测试通过
