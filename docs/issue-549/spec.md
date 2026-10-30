# Spec: Issue #549 — contents 访问不存在路径返回 HTTP 502 包装的 404，错误码语义误导

> 来源：Issue Agent Spec（Issue #549 评论）| 基线分支 weekly-08-mys（场景注入基线）

## 目标
- grape 服务端 git-repo 代理层透传上游错误码：`src/user/repo_manager.py _req()` 将上游 ≥400 响应折叠为 `RuntimeError` 字符串（状态码类型信息丢失、`[:500]` 截断），`src/api/git_repo_api.py _vcs_req()` 将一切非 HTTPException 异常统一包装为 `HTTPException(502)`，导致 `grape git-repo contents --path <不存在路径>` 返回 HTTP 502 包裹的 404。
- 502 语义误导自动化排障与用户判断；真实 404/403/405 被统一折叠，调用方无法按状态码分支处理。
- 成功标准：不存在路径返回 404 语义统一包络（保留上游 message）；502 仅出现在代理自身故障（连接失败/超时/非上游响应异常）；存在路径行为不回归。

## 范围
- In：
  - `src/user/repo_manager.py`：引入 `UpstreamAPIError`（承载上游 `status_code`/`message`/`method`/`url`）；`_req()` 上游 ≥400 抛 `UpstreamAPIError` 替代 `RuntimeError` 字符串，去除 `[:500]` 截断；`_req_query()` / `_github_req()` 同步抛 `UpstreamAPIError`；网络异常仍为 `RuntimeError`（代理自身故障）。
  - `src/api/git_repo_api.py` `_vcs_req()`：捕获 `UpstreamAPIError` → 透传上游状态码与 message（404 → 404 统一错误包络）；其余异常 → 502（代理自身故障）。
  - 透传机制自动覆盖 git-repo 代理全部子命令（contents/branches/commits/tree/issues/pulls 等，与 #558 同根因统一收敛）。
  - `tests/test_git_repo_api.py` 透传回归测试。
- Out：grape-cli 侧无需改码；`_vcs_req` 之外的既有 502 端点（如 close_issue 的业务 502）行为不变。

## 向后兼容
- `UpstreamAPIError` 继承 `RuntimeError`：仓库内既有 `except RuntimeError` 调用点（add_collaborator / remove_collaborator / get_member_permission 等）零改动、字符串判定逻辑保留（`_extract_http_code` 正则兼容）。

## 验收标准
- [ ] 上游 404（contents 不存在路径）→ HTTP 404，detail 包络 `{"code":404,"message":"<上游 message>"}`
- [ ] 上游 403 → HTTP 403；上游 405/429 等按真实状态码透传
- [ ] 网络异常（连接失败）→ 仍 HTTP 502（代理自身故障）
- [ ] 存在路径正常返回，行为不回归（现有测试全通过）
- [ ] `UpstreamAPIError` 兼容既有 `except RuntimeError` 调用点（全量相关测试通过）
