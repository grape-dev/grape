# Issue #3923 实现方案

## 一、改动点

| 文件 | 改动 | 说明 |
|------|------|------|
| `src/kernel/token_resolver.py` | `TokenResult` 增 `attribution`/`attribution_detail`；`resolve_exec_token` 区分来源异常 | `PortNotBound` → `unavailable`；其它异常 → `unattributed`；链跑完仍空 → `no_credential` |
| `src/domains/runtime/engine/repogit.py` | 新增 `bare_head_ref` / `ensure_bare_head` 等；`ensure_repo_base` 建树前校验；`worktree add` 失败自愈重试一次；失败现场 + `worktree_failure_error` | HEAD 自愈、git 原文透传、删除猜测性文案 |
| `src/domains/runtime/engine/agent.py` | 新增 `worktree_block_result`；`_execute_task_impl` 改用它 | 凭据归因成立才报凭据，否则透传 git 原文 |
| `tests/runtime/test_issue_3923_worktree_head_and_attribution.py` | 新增 15 条用例 | HEAD 自愈 / 重试 / 三态归因 / 反向回归 |

## 二、关键设计决策

| 决策 | 选项 | 选择 | 理由 |
|------|------|------|------|
| worker 归因方式 | 继续归因 / 不归因 | 不归因，如实透传 | worker 无装配层，任何归因都是猜测 |
| 三态表达 | 复用 `stage` / 新增字段 | 新增 `attribution` | 与「确实无凭据」区分，且不改既有 `stage` 对外语义 |
| HEAD 自愈时机 | 失败后 / 建树前 | 建树前校验 + 失败重试兜底 | 减少一次失败并可记录明确修复日志 |
| 失败现场作用域 | 仓库级 / 任务级 | `(repo, issue, agent)` 三元组 + 上限 200 | 并发任务互不覆盖，也不无限增长 |

## 三、HEAD 自愈算法

1. `symbolic-ref -q HEAD` 读当前目标；以 `refs/heads/` 开头且该 ref 存在 → 合法，直接返回。
2. 否则按候选顺序（注入基线 → `refs/remotes/origin/*` → `refs/heads/*`）挑第一个可用分支。
3. 本地分支缺失时从 `refs/remotes/origin/<branch>` 用 `update-ref` 建（不 checkout、不动工作区）。
4. `symbolic-ref HEAD refs/heads/<branch>` 落修复，日志 `repogit head repaired previous=... current=...`。
5. 无任何可用分支 → `ok=False` 并写 `detail`（如实上报「修不了」，不伪造成功）。

## 四、错误面分层

1. `worktree_credential_error`：只有归因**跑完**且结论为空才返回 `credential_<stage>`；
   `unavailable`/`unattributed` 不再落 `not_configured`。
2. `worktree_failure_error`：凭据归因成立 → 报凭据/空仓；否则给
   `<stage>: git worktree add 失败；git 原文：<stderr>`（含 HEAD 自愈结果）+
   「凭据归因不可用（未据此判定为凭据问题）」。
3. `agent.worktree_block_result`：先问凭据归因，再透传失败现场。

## 五、验证

```bash
python3 -m pytest tests/runtime/test_issue_3923_worktree_head_and_attribution.py \
  tests/runtime/test_repogit.py tests/runtime/test_issue_3713_task_credential_stage.py \
  tests/api/test_issue_3713_dual_entry_credential.py tests/api/test_git_credential_token_plane.py \
  tests/runtime/test_issue_2245_git_writable.py tests/runtime/test_issue_2514_workspace_binding.py -q
# → 118 passed
```
