# Spec: Issue #623 — pulls comments update 的 comment_id 兼容十六进制 id 与 note_id 契约对齐

> 承接 issue agent 发布的 Spec（评级 ready）；本文为开发落位版。目标分支：weekly-08-mys（本流水线工作基线）。

## Objective
- 修复 `grape git-repo pulls comments update --comment_id` 传 create 响应中的十六进制 `id` 时返回 422 的问题（服务端 FastAPI 路径参数声明为 `int`，十六进制串直接校验失败）。
- 使开发者回用 create 响应主键即可正确更新评论；文档明确 `id` 与 `note_id` 的契约区别。

## Root Cause
- `src/api/git_repo_api.py` 的 `update_pull_comment` 端点声明 `comment_id: int`；GitCode create 响应返回十六进制串 `id`，十六进制串无法通过 int 路径参数校验 → 本服务 422。

## Scope
### In Scope
- `src/api/git_repo_api.py`：update 端点 `comment_id` 改为 `str` 并新增 `_resolve_pull_comment_note_id` 解析层——纯数字直传（向后兼容）；十六进制（全 hex 且含字母）经上游 `GET /pulls/comments/{hex_id}`（该上游端点 id 为 string 形态）取数字 note_id，失败回退 `GET /pulls/{number}/comments` 按 `discussion_id` 匹配同一评论对象；解析不到 → 明确 422。
- `help/api/10-git-repos.md`：补 update `--comment_id` 契约说明（note_id 推荐 / 十六进制兼容 / 非法 422）。
- `tests/test_git_repo_api.py`：三类入参回归测试（十六进制、数字、非法/不可解析）。
- OpenAPI 契约随端点 description 自动更新（comment_id 类型 int → string，description 补 Issue #623 契约）。

### Out of Scope
- #585（create 侧 GitHub 通道 422）、grape-cli 客户端侧改动、delete 端点形态（保持数字 note_id）、pulls files 分页。

## Acceptance Criteria
- [x] 十六进制 id 调 update 不再 422，正确映射 note_id 后透传 PATCH
- [x] 数字 note_id 行为不变（向后兼容，无额外上游调用）
- [x] 非法/不可解析 comment_id 返回明确 422 错误信息
- [x] help/api 文档与端点 description 明确契约说明
- [x] 现有测试通过（test_issues_crud_mapping 为存量失败，与本改动无关，stash 验证过）
- [x] 新增测试覆盖三类入参（hex 单评论映射 / hex 列表回退 / 数字直传 / 非法 422 / 不可解析 422）
