# Issue #466 — 任务执行记录

## 状态

- [x] T1 红：`tests/test_git_repo_api.py::TestRepoCredentialProviderByPath`（5 用例，提交 7db8e74e2 时 3 红）
- [x] T2 绿：`src/api/routes/repos.py::_repo_provider` 路径前缀解析（8cc751b05）
- [x] T3 红：`tests/test_repogit.py::TestLinkedRepoTokenFromEnv`（6 用例全红，03cca2291）
- [x] T4 绿：`repogit.py` env 优先 + `event_pipeline.py` 三处服务端注入 + `agent.py` 传 env（c6058c85f）
- [x] T4b 自审查修复：带前缀路径 clone_url/workspace 泄漏前缀 → `_strip_provider_prefix`（1fc48be1f）
- [x] T5 回归：相关模块 64 passed（test_git_repo_api / test_repogit / test_issue_466_linked_repo_tokens /
      test_token_injection_r20）；全量套件中 test_openapi 3 失败、test_pipeline 5 失败、
      test_issues_crud_mapping 1 失败、aiohttp 缺失 5 文件收集错误——均已在基线
      `origin/weekly-XX` 复核为预存问题，与本改动无关。

## 提交

| commit | 类型 | 说明 |
|--------|------|------|
| 7db8e74e2 | test | 断点一路径前缀解析失败测试（红）+ spec/plan/task 文档 |
| 8cc751b05 | feat | 断点一实现（绿） |
| 03cca2291 | test | 断点二 env 优先失败测试（红） |
| c6058c85f | feat | 断点二实现 + 服务端 token 分发（绿） |
| 1fc48be1f | fix | 自审查发现：clone_url/workspace 剥离 provider 前缀 |

## 备注

- `_write_askpass` 的 env 回退链（BOT_*/USER_*）不动：worktree 内 agent 自发 git 走
  grape CLI → 服务端换券（断点一修复后跨平台即可用）。
- `remove_issue_worktree_if_pushed` / `_submit_linked_repo_prs` 的 push token 用
  `_get_task_token(task)`（主仓 token）——跨平台关联仓 push 由 `grape git`（断点一）承担，
  自动提交路径同平台不变。此为本切片边界，已在 PR body 说明。
