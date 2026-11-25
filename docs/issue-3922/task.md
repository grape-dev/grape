# Task: Issue #3922 实施拆解

> 基线：`arch-weekly-11-ljt`（`feat/issue-3922` 工作分支）；PR base = `arch-weekly-11-ljt`。

## T1 · 红：新增用例（tests/mgmt/test_issue_3922_branch_ref_validation.py）
- [ ] `validate_branch_name()` 反例：`-x`、`refs/heads/x`、`refs/remotes/x`、`a b`、`a..b`、`x.lock`、
      `a~b`、`a^b`、`a:b`、`a?b`、`a*b`、`a[b`、`a\b`、`a"b`、`a'b`、`@`、`@{x}`、`a//b`、`/a`、`a/`、`.a`、
      控制字符、256 字符超长 → 原因串非空
- [ ] 正例：`main`、`feat/issue-3922`、`release/v1.2.3`、`a-b_c.d`、`x/y/z` → 原因串为空
- [ ] `map_upstream_error(400, detail)` → 400 + 可读原因 + 无内网 URL/token
- [ ] `map_upstream_error(409/422, conflict_on=(409,422))` → 409「分支已存在」
- [ ] `map_upstream_error(500, ...)` / `map_upstream_error(0, "timeout ...")` → 502
- [ ] `map_upstream_error(404/401/403)` → 404 / 403（既有语义不变）
- [ ] POST 路由 × 非法名 → 400 且 `create_branch` 调用次数 0
- [ ] POST 路由 × 上游 400 → 400（message 可读、不含内网 URL/token）
- [ ] POST 路由 × 上游 500 → 502

## T2 · 绿：`src/kernel/git_ref.py`
- [ ] `validate_branch_name(name) -> str`（非法返回可操作中文原因，合法返回 `""`）
- [ ] `is_valid_branch_name(name) -> bool`
- [ ] 常量 `MAX_BRANCH_NAME_LENGTH = 255`

## T3 · 绿：`src/domains/mgmt/routes/_vcs_error_map.py`
- [ ] `upstream_error_message(detail) -> str`（dict/JSON 抽 message → `sanitize_upstream_error` → token 脱敏）
- [ ] `map_upstream_error(status_code, detail, *, ...) -> HTTPException`（4xx 透传 / 5xx·无码 → 502）
- [ ] `map_vcs_error` 行为零变更（#4169 契约不受影响）

## T4 · 绿：`src/domains/mgmt/routes/scenes.py: create_repo_branch`
- [ ] 判空之后、取 token 之前接入 `validate_branch_name` → 400 `INVALID_BRANCH_NAME`
- [ ] 错误分支 `map_vcs_error` → `map_upstream_error`（`stage="scenes_branch_create"`，保留 409/403/404 语义）

## T5 · 回归与自审
- [ ] `python3 -m pytest tests/mgmt/test_issue_3922_branch_ref_validation.py tests/mgmt/test_issue_4169_scenes_branches_error_map.py -q` 全绿
- [ ] 关联回归：`tests/mgmt/test_scenes_branches_cache.py`、`tests/api/test_openapi.py`
- [ ] `make lint`（改动文件的 ruff/flake 口径）
- [ ] 5 轴自审 + pre-pr 增量检查（黑名单拦截、敏感信息扫描、commit message 全英文）

## T6 · 交付
- [ ] 逐文件 add + 原子提交（全英文 commit message）
- [ ] push `feat/issue-3922` → 建 PR（base `arch-weekly-11-ljt`）
- [ ] Issue 评论告知 + A2A 通知 QA 审查