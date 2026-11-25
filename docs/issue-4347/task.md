# Task Breakdown: Issue #4347

| # | 任务 | 状态 |
|---|------|------|
| 1 | 新增 `src/domains/runtime/routes/auth_failure_budget.py`（IP 失败预算 + 封禁 + 日志聚合器） | 完成 |
| 2 | `runtime.py` 两个 rta_ 鉴权函数接入统一失败出口（429/封禁/聚合日志） | 完成 |
| 3 | `tests/runtime/conftest.py` 每用例重置 gate（防 testclient 单 IP 累计污染既有用例） | 完成 |
| 4 | `tests/runtime/test_4347_auth_failure_budget.py` 单测（预算计数 / 429 路径 / 聚合节流） | 完成（11 passed ×2 轮） |
| 5 | 回归基线对照（test_runtime_protocol / test_agent_internal_auth：失败集与改动前一致，零回归） | 完成 |
| 6 | 自审查（5 轴 + pre-pr-release-check：黑名单拦截 + 敏感增量扫描 + commit 全英文） | 完成 |
| 7 | 提交 feat/issue-4347 → 建 PR（base=arch-weekly-11-ljt）→ Issue 评论 → A2A 交办 QA | 完成 |

## Out of Scope（后续迭代）

- P1-1：按 `rta_` 前缀查询 runtime 归属的管理台/CLI（便于吊销与通知宿主）；
- 网关层通用限流（#4181 自身计划推进）；
- 来源 IP（120.46.32.239 / 124.243.148.211）封禁与凭据吊销（运维处置）。
