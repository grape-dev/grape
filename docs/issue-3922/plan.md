# Plan: Issue #3922 前置 ref 校验 + 上游 4xx 透传

## 切片

1. **切片 1（红）**：`tests/mgmt/test_issue_3922_branch_ref_validation.py`
   - `validate_branch_name()` 非法/合法正反例；
   - `map_upstream_error()` 纯函数契约（400 透传 + 脱敏、409 冲突、500→502、0/超时→502）；
   - TestClient + mock `VcsApiClient.create_branch` 的 POST 三路径（非法名不调上游 / 上游 400 透传 / 上游 500→502）。

2. **切片 2（绿，纯函数）**：新增 `src/kernel/git_ref.py`
   - `validate_branch_name(name) -> str`：合法返回 `""`，非法返回**可操作中文原因**（单一来源）；
   - 规则 = `git check-ref-format` + 平台约束：以 `-`/`.`/`/` 开头、以 `/`/`.`/`.lock` 结尾、
     `refs/heads/` 或 `refs/remotes/` 前缀、空格/`~^:?*[\`/单双引号、`..`、`@{`、`//`、
     控制字符、单字符 `@`、超长（255）。

3. **切片 3（绿，单点映射）**：`src/domains/mgmt/routes/_vcs_error_map.py` 新增
   `map_upstream_error(status_code, detail, *, stage=..., message=..., permission_message=...,
   conflict_on=(), conflict_markers=(), conflict_message="分支已存在", retry_hint=..., log_prefix=...)`
   - 顺序：`log.warning`（原文只落日志）→ conflict markers / `conflict_on` → 409 → 404 → 401/403 →
     **400≤code<500 透传**（message = 上游可读 message 经 `sanitize_upstream_error`，空则通用文案）→
     其余（5xx / 0）→ `host_502_detail()`。
   - 新增 `upstream_error_message(detail)` 辅助：dict/JSON 抽出 `message`/`error_message`/`detail`，
     再走 `sanitize_upstream_error()`，并额外脱敏 `token=/access_token=/private_token=` 形态。

4. **切片 4（绿，路由接入）**：`scenes.py: create_repo_branch`
   - 判空之后、取 token 之前插入 `validate_branch_name(body.branch_name.strip())`，命中即
     `HTTPException(400, {"code":400,"error_code":"INVALID_BRANCH_NAME","message":<原因>,"field":"branch_name"})`；
   - 错误分支由 `map_vcs_error` 改为 `map_upstream_error`（`stage="scenes_branch_create"`，
     `conflict_on=(409,422)`、`conflict_markers=("already exists","已存在")`），语义与 #4169 用例一致 + 新增 4xx 透传。

## 关键设计决策
| 决策 | 选择 | 理由 |
|------|------|------|
| 映射函数返回 vs 抛出 | 返回 `HTTPException`，调用方 `raise` | 与 `map_vcs_error` 一致，便于纯函数单测 |
| 是否改 `map_vcs_error` | 不改 | 读路径（GET）语义由 #4169 用例锁死（无 `conflict_on` 的 409→502） |
| 校验工具命名 | `validate_branch_name`（返回原因串）+ `is_valid_branch_name` | 路由只需 `if reason: raise 400`，原因即文案 |
| 校验时机 | 判空之后、取 token 之前 | 保证「非法名 0 次上游调用」，也让用例可断言 token 未取 |

## 测试 seam
- 纯函数：直接 import `src.kernel.git_ref` / `src.domains.mgmt.routes._vcs_error_map`。
- 路由：沿用 `tests/mgmt/test_issue_4169_scenes_branches_error_map.py` 的
  JWT(`grape_account_db` + `create_access_token`) + `repo_key=gitcode:owner/repo` + TestClient 写法，
  patch `src.domains.mgmt.user.db.get_repo_token_with_refresh` 与
  `src.domains.runtime.vcs.api_client.VcsApiClient.create_branch`。
- 断言「不调上游」用 `mock.call_count == 0`；断言脱敏用响应体不含上游 URL/密文关键字。

## 提交拆分
- `test: #3922 cover branch ref validation and upstream 4xx passthrough`（红）
- `feat: #3922 reject invalid branch refs with 400 before hitting upstream`（绿：git_ref + 路由前置校验）
- `fix: #3922 map upstream 4xx to their own status instead of 502`（绿：map_upstream_error + 路由接入）
- `docs: #3922 add implementation spec, plan and task breakdown`