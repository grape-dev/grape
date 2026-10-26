# Issue #466 — 修复跨平台联合开发场景关联仓库凭据解析

> 目标分支：`weekly-XX` | 开发分支：`feat/issue-466` | 优先级 P1 | 2026-10-26

## 1. 背景与根因

跨平台联合开发场景（GitHub 主仓 + GitCode 关联仓，或反向）下，coding/sre agent 对关联仓库的
git 读写全链路不可用，存在两个断点：

### 断点一：git-credential 接口 provider 按会话主仓解析（`src/api/routes/repos.py`）

`_repo_provider` 依赖 `parse_repo_key(user["repo_key"])[0]`。会话 token（tk_）的 repo_key 按
**主仓**（`github:huaweicloud-mate/hdcommunity-toolkit`）签发；agent 请求关联仓凭据
`GET /api/repos/OPCTeam/HDBlogService/git-credential` 时，服务端仍按 GitHub 解析，返回 `ghp_`
token → 访问 gitcode.com 全部 403/404。

同平台场景「碰巧同宿主」不受影响——问题只在跨平台组合暴露。

### 断点二：worker 侧关联仓 token 只在本地解析（`src/runtime/repogit.py`）

`ensure_linked_repo_worktrees` / `ensure_repo_base_multi` 对每个关联仓调 `Repo(rk).bot_token`，
走 worker 本地 sqlite（`managed_repos` 空表，worker 端无 MySQL）→ 必然解析为空 →
`no token for gitcode:OPCTeam/HDBlogService, skipping` → worktree 初始化失败。
服务端明明托管着关联仓有效 bot token，却未随任务下发。

## 2. 方案

与 `GRAPE_SESSION_TOKEN` 分发链路对齐（Spec 关键设计决策选 A 为主）：

### 2.1 断点一修复：provider 按请求路径目标仓 repo_key 前缀解析

`_repo_provider` 改为按 URL 路径中的目标仓 repo_key 前缀（`gitcode:` / `github:`）解析：

```
owner/repo 路径段带 "gitcode:"/"github:" 前缀 → 按前缀定 provider
无前缀 → 回退 parse_repo_key(user["repo_key"])[0]（会话主仓，向后兼容）
```

路径如 `/api/repos/gitcode:OPCTeam/HDBlogService/git-credential`。FastAPI path 参数可含冒号，
CLI（`grape git --repo gitcode:o/r`）与 agent 均可直接带前缀请求；同平台无前缀请求行为不变。

### 2.2 断点二修复：关联仓 token 随 env 分发 + worker env 优先

- **服务端**：env 组装处（`event_pipeline.build_task_env` / `_enqueue_pipeline_task` /
  direct 执行路径，即所有注入 `GRAPE_LINKED_REPOS` 的位置）对每个关联仓解析
  `get_repo_bot_token(repo_key)`（服务端进程内解析，按 provider 分流），注入
  `env_extra["GRAPE_LINKED_REPO_TOKENS"]` = JSON `{"<repo_key>": "<token>"}`。
  仅任务生命周期内注入（随 agent_tasks.env_extra 存储，与 GRAPE_SESSION_TOKEN 同生命周期），不写日志。
- **worker**（`repogit.py`）：`ensure_linked_repo_worktrees` / `ensure_repo_base_multi` 的 token
  获取顺序改为：**env `GRAPE_LINKED_REPO_TOKENS` 优先 → 回退本地 `Repo(rk).bot_token`**
  （本地/直连 MySQL 模式保持可用）。
- env 变量名做归一化键（repo_key 原文即键，JSON 解析后直接按 repo_key 查）。

### 2.3 安全边界

- token 只进 `env_extra`（服务端 → worker 传输链路与 GRAPE_SESSION_TOKEN / repo_gitcode_token
  相同），不进 prompt、不进日志、不落盘；agent 侧本就收口到 grape CLI，不新增暴露面。
- push/建 PR 前敏感信息增量扫描（pre-pr-release-check）覆盖本次 diff，token 不入代码。

## 3. 任务拆解

| # | 任务 | 文件 | 类型 |
|---|------|------|------|
| T1 | 失败测试：断点一路径前缀解析/回退/403 | `tests/test_git_repo_api.py` | test(红) |
| T2 | 断点一实现 | `src/api/routes/repos.py::_repo_provider` | feat(绿) |
| T3 | 失败测试：断点二 env 优先/回退/跳过 | `tests/test_repogit.py` | test(红) |
| T4 | 断点二实现 + 服务端 env 注入 | `src/runtime/repogit.py` + `src/api/event_pipeline.py` | feat(绿) |
| T5 | 回归：现有测试套全绿 + 同平台不回归 | pytest 全量 | verify |

## 4. 测试 seam

- 断点一：HTTP 层 `GET /api/repos/{owner}/{repo}/git-credential`（TestClient +
  `app.dependency_overrides`，沿用 `tests/test_git_repo_api.py` 既有 seam）。
- 断点二：`ensure_linked_repo_worktrees(linked_repos, agent_type, issue_num, token, env=None)`
  新增 `env` 参数（公开 seam），mock `ensure_issue_worktree` 断言 token 传递与跳过行为。

## 5. 验收标准（对照 Spec）

- [ ] 跨平台换券：路径带 `gitcode:` 前缀 → provider=gitcode、token 来自 GitCode 解析链（不再 `ghp_`）。
- [ ] 无前缀 → 回退会话主仓（同平台不回归）。
- [ ] 未知前缀 → 403 防御保持。
- [ ] 关联仓 worktree 初始化：env 有 token → 不再 skip；env 无 token 且本地可解析 → 回退可用；
      两者皆无 → 维持 skip（不破坏现有降级语义）。
- [ ] 现有测试全绿 + 新增测试覆盖两断点。
