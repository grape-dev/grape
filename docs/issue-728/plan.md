# Plan: Issue #728 — 实现方案

## 改动清单
| 文件 | 改动 |
|------|------|
| `src/vcs/issue_ref_filter.py` | 新增纯函数模块：`strip_nonsemantic_context`（fenced code / inline code / blockquote 剥离）+ `extract_related_issue_nums(text, strict=False)`（默认全文 `#N`；strict 仅 `Closes/Related/关联` 前缀） |
| `src/api/git_repo_api.py` | ① `create_pull`：`related_issue_nums` 为空且 body 非空时经过滤函数自动提取（空列表跳过翻译，存量行为不变）；② 新增 `DELETE /{owner}/{repo}/pulls/{number}/issues/{issue_number}`（`gitrepo_pulls_issues_unlink`）：gitcode GET PR issues 校验存在 → PATCH 剩余 `inner_issue_nums`；未关联幂等返回；github 无原生关联幂等 no-op |
| `help/api/10-git-repos.md` | pulls 命令表新增 unlink 行 + CLI 示例；新增「PR body 提及 Issue 号的规避写法」说明段 |
| `tests/test_issue_728_pr_issue_unlink.py` | 过滤函数三语境 + strict 关键字 + 嵌套边界；create_pull 自动提取（代码块内 `#99` 不入 `inner_issue_nums`、正文 `#42` 入）；unlink 已关联/未关联幂等/github no-op |

## 测试策略
- 过滤函数：直接单测纯函数（fenced/inline/blockquote 三语境 + `Closes/Related/关联` strict + 嵌套代码块边界 + 去重/去 0）。
- API 层：复用 test_git_repo_api.py 的 mock 模式（dependency override + monkeypatch `repo_manager._req`），断言 POST /pulls payload 的 `inner_issue_nums` 与 unlink 的 GET→PATCH 调用序列、幂等（第二次无 PATCH）。
- 回归：`tests/test_git_repo_api.py`、`tests/test_git_provider.py` 全量通过。

## 风险与缓解
- 误伤合法关联：过滤只剥离非语义语境，正文自然语言 `#N` 默认仍捕获；strict 模式供收紧。
- unlink 误删：双路径参数显式 + GET 校验关联存在才 PATCH，未关联直接幂等返回。
