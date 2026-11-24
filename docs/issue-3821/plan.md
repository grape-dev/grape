# Issue #3821 方案设计（plan）

## 核心思路

把「评论 id 解析」与「评论通道选择」从 pulls 专属实现上提为 issues/pulls 通用能力：

- id 解析：数字直传；十六进制在当前 scope 的扁平单评论端点解析，失败回退该 scope 评论列表按 `id`/`discussion_id` 匹配；均失败 422。
- 通道选择：pull 评论主通道 = `_pr_comment_endpoint(p)`（github=`issues`，其余=`pulls`，与 create 同源）；数字 id 直传主通道，仅当上游明确 400 `Note type is not correct` 时回退另一通道；十六进制 id 在解析阶段同时探测两个 scope 以确定通道。

## 1. 通用 id 解析（src/api/git_repo_api.py）

- 抽出模块级 `_first_digit_id(obj)`。
- `_resolve_comment_note_id(p, owner, repo, number, comment_id, token, scope)`：
  - 空 → 422「comment_id 不能为空」；
  - 纯数字 → 直传；
  - 非 hex 或纯 hex 无字母 → 422 可读文案；
  - 平台不支持 hex（`PR_COMMENT_ID_HEX` 缺省 False）→ 422 可读文案；
  - hex → `_resolve_hex_note_id_in_scope(..., scope)`，失败 422 可读文案。
- `_resolve_pull_comment_target(...) -> (note_id, scope)`：
  - 数字 → `(cid, 主通道)`，零额外请求；
  - hex → 依次探测主通道与另一通道，命中即返回该通道；均失败 422。

## 2. 通道端点路径

`_comment_write_subpath(p, scope, note_id, number)`：

| scope | 扁平（默认） | 非扁平 |
|---|---|---|
| pulls | `/pulls/comments/{id}`（`PR_COMMENT_FLAT_PATH`） | `/pulls/{number}/comments/{id}` |
| issues | `/issues/comments/{id}`（新增 `ISSUE_COMMENT_FLAT_PATH`，默认 True） | `/issues/{number}/comments/{id}` |

- `gitlab.py` 设 `ISSUE_COMMENT_FLAT_PATH = False`，并补 `rewrite_path`：`/issues/{n}/comments/{id}` → `/issues/{n}/notes/{id}`。
- gitcode/gitee/github 默认扁平，不新增平台字面量分支（走 `_pflag` 卡→模块回退）。

## 3. issues 评论 PATCH/DELETE

新增 `gitrepo_issues_comments_update` / `gitrepo_issues_comments_delete`：

- PATCH：`CommentBody`，复用 `_release_guard_reject`（target_issue=number，与 issue 评论 create 同口径）。
- DELETE：无 body。
- id 经 `_resolve_comment_note_id(..., scope="issues")` 解析后写上游 issue note 通道。

## 4. pulls 评论 update/delete

- 解析 `_resolve_pull_comment_target` 得到 `(note_id, scope)`。
- `_write_pull_comment_note` 按 scope 发请求；若主通道返回 400 `Note type is not correct`，回退另一通道；其他错误原样透传（不掩盖真实 4xx）。
- 数字 id 常见路径零额外请求；合并后回退创建的 issue note 仅多一次可预期的通道回退。

## 5. 契约与文档

- `contracts/openapi_baseline.json`：`python scripts/regenerate_openapi.py --update-baseline --reason "Issue #3821 ..."` 受控重定基（390→392，git-repo 组不变）。
- `contracts/required_operations.json`：新增两条 issue comments update/delete 登记（含 `command` = `grape git-repo issues comments update/delete`）。
- `help/api/10-git-repos.md`：新增 PATCH/DELETE issues comments 端点块与 CLI 示例，满足 `test_api_docs_sync` 文档同步门禁。

## 测试 seam

- 公开 seam：`/api/git-repo/{owner}/{repo}/issues|pulls/{number}/comments/{comment_id}` HTTP 行为（method/URL/body/状态码）。
- 不测内部 `_resolve_*` 私有函数；通过 TestClient + mock `repo_manager._req` 观察上游通道。