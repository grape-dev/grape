# Plan — Issue #558

## 分层方案（自底向上）

1. **provider/请求层（repo_manager.py）**
   - `class VcsUpstreamError(RuntimeError)`：`status_code: int`、`body: str`（上游响应全文）。继承 RuntimeError → 存量 `except RuntimeError` 分支零破坏。
   - `_req`（gitcode/requests）：`resp.status_code >= 400` → `raise VcsUpstreamError(f"GitCode API {method} {url} failed ({code}): {text}", code, text)`，去掉 `[:500]`。
   - `_req_query`（urllib）：HTTPError → 同上，body 全文。
   - `_github_req`：同上。
2. **网关层（git_repo_api.py）**
   - `_vcs_req`：新增 `except VcsUpstreamError` 分支（先于通用 Exception）→ `HTTPException(status_code=e.status_code, detail={"code": ..., "message": e.body or str(e)})`；通用异常分支保留 502。
   - `merge_pull`：body 模型 `PRMergeBody(merge_method="", title="")`，payload 过滤空值（空 dict → 传 `None` 保持存量 `body=None` 行为）。
3. **Agent 直调层（api_client.py）**
   - `merge_pr` 增加 `merge_method`/`title` 可选参数：gitcode/github 传过滤后 body；gitlab `squash` → `{"squash": True}`。
   - `_do_request` 错误返回补 `status_code`，`detail` 去截断。
4. **契约/文档**
   - FastAPI 路由签名+description 自动进 OpenAPI；手写文档 `docs/openapi/10-git-repos.md` merge 端点补参数表与 405 透传说明。

## 测试计划（tests/test_git_repo_api.py + 新增 tests/test_issue_558_merge_passthrough.py）
- 单元：`_req` mock 405 响应 → VcsUpstreamError(status_code=405, body 全文 >500 字符不截断)。
- 网关：PUT merge 上游 405 → HTTP 405 + 完整 message；上游非结构化异常（超时 RuntimeError）→ 502。
- 参数：merge_method=squash&title → 上游 body 断言；缺省 → body 为 None（存量）。
- 回归：merge 2xx 返回不变；现有 test_git_repo_api.py 全量通过。
