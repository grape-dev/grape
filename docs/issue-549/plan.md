# Plan: Issue #549 实现方案

## 根因
- `src/user/repo_manager.py _req()`：上游 ≥400 折叠为 `RuntimeError("GitCode API ... failed (404): <resp.text[:500]>")`——状态码只剩字符串片段、类型丢失、正文截断。
- `src/api/git_repo_api.py _vcs_req()`：`except Exception` 统一包装 `HTTPException(502)`——上游真实状态码全部被 502 覆盖（contents 不存在路径 404 → 502 误导，#558 pulls merge 405 同根因）。

## 改动
1. `src/user/repo_manager.py`：
   - 新增 `UpstreamAPIError(RuntimeError)`：属性 `status_code` / `message` / `method` / `url`；`str()` 保持 `GitCode API {method} {url} failed ({code}): {message}` 格式（兼容 `_extract_http_code` 正则与既有字符串判定）。
   - `_req()`：≥400 → `raise UpstreamAPIError(...)`（不截断 message）；`RequestException` → `RuntimeError`（不变，代理自身故障）。
   - `_req_query()`：`HTTPError` → `UpstreamAPIError`（携带 `e.code`）；`URLError` → `RuntimeError`（不变）。
   - `_github_req()`：`HTTPError` → `UpstreamAPIError`；`URLError` → `RuntimeError`（不变）。
   - `_extract_http_code()`：优先读 `err.status_code` 属性，正则兜底。
2. `src/api/git_repo_api.py` `_vcs_req()`：
   - `except UpstreamAPIError as e` → `raise HTTPException(status_code=e.status_code, detail={"code": e.status_code, "message": <上游 message 摘要>})`（透传，warning 日志保留）；
   - 其余异常 → 502（不变，仅代理自身故障）。
   - provider 分发路径（gitcode `_req` / github `_github_req`）均经此分流，全部 git-repo 子命令统一收敛（与 #558 同根因）。
3. `tests/test_git_repo_api.py`：新增透传回归（fixture mock `_req` 抛 `UpstreamAPIError`）：
   - contents 不存在路径 404 → 404 + `detail.code == 404`；
   - 上游 403 → 403；网络 `RuntimeError` → 502 不回归；
   - 正常路径 200 不回归。

## 兼容性
- `UpstreamAPIError` 继承 `RuntimeError`：`_check_collaborator_admin` / `add_collaborator` / `remove_collaborator` / `get_member_permission` / `fetch_repos_with_role` 等既有 `except RuntimeError` 零改动。
- 502 仍保留给：token 解析失败（`_repo_token`）、网络不可达、非上游异常——语义收紧为「代理自身故障」。

## 风险
- 上游 5xx 也透传原码（502/503 语义不变，来源更准确）；调用方按状态码分支即可。
