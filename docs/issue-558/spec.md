# Spec — Issue #558（编码基线，承接 issue agent Spec 评论 197141693）

> 基线分支：weekly-08-mys（场景注入，已核验存在；前轮 Spec 的 weekly-09 为跨流水线移交前旧基线，不作开发基准）。

## 目标
1. `git-repo pulls merge` 失败时透传上游真实 HTTP 状态码与完整错误 message（不再 502 包装、不截断）。
2. `pulls merge` 补充 `merge_method`（merge/squash/rebase）与 `title` 参数，契约/服务端/Agent 直调全链路透传。

## 范围
- `src/user/repo_manager.py`：新增结构化 `VcsUpstreamError`（携带 status_code + 完整 body）；`_req` / `_req_query` / `_github_req` 上游非 2xx 改抛该异常，去除 [:500] 截断（message 格式保持 `... failed (code): body` 以兼容 `_extract_http_code`）。
- `src/api/git_repo_api.py`：`_vcs_req` 捕获 `VcsUpstreamError` → 以上游真实状态码抛 `HTTPException`（detail 携带完整 message）；其余异常维持 502。`merge_pull` 增加 `PRMergeBody`（merge_method/title，空值不透传，保证存量行为不变）。
- `src/vcs/api_client.py`：`merge_pr(pr_number, merge_method="", title="")` 透传参数（gitlab squash 映射 `{"squash": True}`）；`_do_request` 错误 dict 增加 `status_code` 字段，`detail` 保留上游全文。
- `docs/openapi/10-git-repos.md`：merge 端点补充 body 参数与错误透传说明（OpenAPI 契约由 FastAPI 路由自动生成，CLI `grape refresh` 后生效）。

## 验收
- 上游 405 → CLI 收到 405 + 完整 message（非 502）。
- `merge_method=squash&title=x` 透传至上游 body；缺省不改变存量请求。
- 上游 2xx 成功路径回归不变；代理自身异常仍 502。
