# Task Breakdown: Issue #4347

| # | 任务 | 状态 |
|---|------|------|
| 1 | 新增 `src/domains/runtime/routes/auth_failure_budget.py`（IP 失败预算 + 封禁 + 日志聚合器） | 完成 |
| 2 | `runtime.py` 两个 rta_ 鉴权函数接入统一失败出口（429/封禁/聚合日志） | 完成 |
| 3 | `tests/runtime/conftest.py` 每用例重置 gate（防 testclient 单 IP 累计污染既有用例） | 完成 |
| 4 | `tests/runtime/test_4347_auth_failure_budget.py` 单测（预算计数 / 429 路径 / 聚合节流 / XFF 伪造负向用例） | 完成（19 passed ×2 轮） |
| 5 | 回归基线对照（test_runtime_protocol / test_register_insert_cloud_metadata：失败集与改动前一致，零回归） | 完成 |
| 6 | 自审查（5 轴 + pre-pr-release-check：黑名单拦截 + 敏感增量扫描 + commit 全英文） | 完成 |
| 7 | 提交 feat/issue-4347 → 建 PR（base=arch-weekly-11-ljt）→ Issue 评论 → A2A 交办 QA | 完成 |
| 8 | 复审修复（C1 XFF 伪造 / R1 Retry-After 对齐 / R2 空 IP 留痕）+ nginx XFF 覆盖式 | 完成（19 passed） |
| 9 | 同步基线（rebase 到 arch-weekly-11-ljt，消除 diff 膨胀与非 FF） | 完成（增量收敛为本 Issue 12 文件） |
| 10 | 第三轮复审收尾：19 例接入 `.github/workflows/lint.yml` CI 白名单；MAX_IPS 淘汰改为保留活跃封禁；XFF 覆盖式部署次序写入 `deploy/README.md`/`.env.example`；5 个新 env 登记 `help/usage/server-environment-variables.md` | 完成（19 passed） |

## Out of Scope（后续迭代）

- P1-1：按 `rta_` 前缀查询 runtime 归属的管理台/CLI（便于吊销与通知宿主）；
- 网关层通用限流（#4181 自身计划推进）；
- 来源 IP（`<attacker-ip-1>` / `<attacker-ip-2>`）封禁与凭据吊销（运维处置）。
