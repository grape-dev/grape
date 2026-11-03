# Spec: git-repo contents 特殊字符路径 percent-encode（Issue #714）

> 来源：Issue #714 Spec Document（issue agent 已发布，本文件为开发侧镜像） | 目标分支：weekly-08-mys（与 weekly-09 同源 dc337845）

## 目标
`git-repo contents` 对含 `+`/中文/空格等特殊字符的路径逐段 percent-encode，使其与 ASCII 路径行为一致（200+内容或明确 404）。

## 范围
- In：`src/api/git_repo_api.py` `_vcs_url()` subpath 逐段编码；`tests/test_git_repo_api.py` 特殊字符用例。
- Out：#698（遍历效率）、#643（tree 404 语义重构）。

## 方案
subpath 以 `/` 为分隔逐段 `urllib.parse.quote(seg, safe="")` 后拼接（`+`→`%2B`、中文→UTF-8、空格→`%20`），与 owner/repo 编码策略对齐，`/` 分隔语义不变。

## 验收标准
- [x] 中文+空格路径转发 URL 含 UTF-8 编码与 `%20`，返回 200
- [x] `+` 路径转发 URL 含 `%2B`，返回 200
- [x] 不存在的特殊字符路径返回明确 404（上游透传，既有 test_upstream_404_passthrough + 新用例）
- [x] 新增用例断言 mock 捕获 URL 编码正确
- [x] ASCII 路径用例回归全绿
- [x] 全量测试通过（test_issues_crud_mapping 为基线既有失败，与本次改动无关，stash 验证）
