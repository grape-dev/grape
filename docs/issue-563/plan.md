# Plan: Issue #563

## 技术方案
契约驱动模式（同 #532/#526/#554/#558 先例）：服务端 git-repo 代理端点新增 delete/update → OpenAPI 自动渲染（openapi.py 收录对外安全路由）→ 契约版本 bump → CLI `grape refresh` 后自动生成 `grape git-repo pulls comments delete/update` 命令。

## 上游 API 映射
- GitCode v5（Gitee 系）：`PATCH /repos/{owner}/{repo}/pulls/comments/{id}`（改 PR 评论正文）、`DELETE /repos/{owner}/{repo}/pulls/comments/{id}`（删 PR 评论）。comment id 全局唯一，PR number 仅作网关侧上下文。
- 网关路径：`PATCH/DELETE /api/git-repo/{owner}/{repo}/pulls/{number}/comments/{comment_id}`（与 create/list 同族心智模型）。

## 改动清单
| 文件 | 改动 |
|------|------|
| src/api/git_repo_api.py | +2 端点（update_pull_comment / delete_pull_comment） |
| src/api/openapi.py | _SESSION_ONLY_OPERATIONS 登记 2 个新 operation_id |
| src/api/main.py | app.version 2.1.0 → 2.2.0 |
| help/api/10-git-repos.md | 新端点文档 + CLI 示例 |
| tests/test_git_repo_api.py | 新增 update/delete 透传映射测试 |
