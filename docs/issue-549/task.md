# Task: Issue #549 任务拆解

- [x] T1 根因定位：`_req` RuntimeError 折叠 + `_vcs_req` 统一 502 包装
- [x] T2 引入 `UpstreamAPIError`（status_code/message/method/url，继承 RuntimeError 兼容既有捕获）
- [x] T3 `_req` / `_req_query` / `_github_req` 上游 ≥400 改抛 `UpstreamAPIError`（去截断）；网络异常保持 RuntimeError
- [x] T4 `_vcs_req` 分流：UpstreamAPIError → 透传上游状态码/message；其余 → 502
- [x] T5 回归测试：404/403 透传、网络 502 不回归、正常路径不回归
- [x] T6 全量相关测试通过（tests/test_git_repo_api.py 等）
- [x] T7 自审查 + 敏感增量扫描 + 提交 + PR（base=weekly-08-mys）
