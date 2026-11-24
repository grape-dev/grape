# Plan: Issue #4169 抽共享 VCS 错误映射，scenes 分支端点接入

## 切片
1. **切片 1（红→绿，共享模块）**：新增 `src/domains/mgmt/routes/_vcs_error_map.py`
   - `upstream_status_code(err_or_data)`：dict 先取 `status_code` 字段；再按 `failed (\d{3})`（repos 形态）
     与 `HTTP (\d{3})`（VcsApiClient 形态）双正则兜底；取不到返回 0。
   - `host_502_detail(stage, err, message="", host_status="partial", missing=None, step="")`：
     沿用 repos.py 语义（`code=502`、`error_code=UPSTREAM_<STAGE>[_<code>]`、`retryable`、
     `upstream{stage,status,retryable}`），**detail 不含上游原文**。
   - `map_vcs_error(err_or_data, *, conflict_on=(), conflict_markers=(), stage="vcs", message="", ...) -> HTTPException`：
     404→404「仓库不存在或无权访问」；401/403→403 `TOKEN_PERMISSION_INSUFFICIENT`；
     `conflict_on` 命中或 `conflict_markers` 文本命中→409「分支已存在」；其余→502。
     返回异常对象（调用方 `raise`），副作用仅一条 `log.warning`（原文只落日志）。
2. **切片 2（重构，行为零变更）**：`repos.py` 改为从共享模块导入 `upstream_status_code` /
   `host_502_detail`，模块级保留 `_upstream_status_code` / `_host_502_detail` 私有名别名（`_UPSTREAM_CODE_RE` 保留）。
3. **切片 3（GET）**：`list_repo_branches` 删除 `raise RuntimeError(f"{error} {detail}")` 中转，
   `data["error"]` 直接入 `map_vcs_error(..., stage="scenes_branches")`；`except HTTPException: raise`
   置于 `except Exception` 之前，`except Exception` 走 `map_vcs_error` 兜底 502。失败不入缓存（原逻辑已保证）。
4. **切片 4（POST）**：`create_repo_branch` 的 error-dict 分支与 `except Exception` 统一走
   `map_vcs_error(..., conflict_on=(409, 422), conflict_markers=("already exists", "已存在"), stage="scenes_branch_create")`；
   409/422 文案保持「分支已存在」；401/403 统一 `TOKEN_PERMISSION_INSUFFICIENT`（detail 附维护者权限提示）。

## 关键设计决策
| 决策 | 选择 | 理由 |
|------|------|------|
| 映射逻辑归属 | 共享模块 | 避免逐端点漏改（#383 教训） |
| 状态码来源 | 字段优先 + 双正则兜底 | dict 已带 status_code；字符串路径两形态并存 |
| 502 detail | 结构化（host_status/missing/step/upstream） | #383/#395 防泄露约定 |
| 映射函数返回 vs 抛出 | 返回 HTTPException | 便于单测直接断言 code/detail，调用方 `raise` |

## 测试 seam
- `tests/mgmt/test_issue_4169_scenes_branches_error_map.py`：
  - 共享模块纯函数单测（字段优先、双正则、超时无码、trace 无原文）；
  - TestClient + mock `VcsApiClient._card_or_legacy_get` / `create_branch`，GET/POST × {404, 401, 403, timeout, 500}
    断言状态码、文案、响应体不含 `Project not found`/上游 URL；
  - POST 409/422 → 409；失败响应不入 `_branches_cache`。
- 复用 `tests/mgmt/test_scenes_branches_cache.py` 的 JWT + `repo_key` 注入写法（同一台 TestClient）。

## 提交拆分
- `test(#4169): 四分支错误映射用例`（红）
- `feat(#4169): 抽 _vcs_error_map 并接入 scenes 分支端点`（绿）