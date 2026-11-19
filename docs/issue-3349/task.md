# Issue #3349 — 任务拆解（task）

- [x] T1 `src/user/repo_manager.py`：`_req` / `_github_req` 支持 `timeout` 形参（默认 30s，向后兼容）
- [x] T2 `src/api/issues_center.py`：新增常量 + `_find_open_issue_by_title_ledger`（台账查重）
- [x] T3 `src/api/issues_center.py`：`_find_open_issue_by_title` 重构为「台账优先 + 有界 VCS 探针 + 显式超时」
- [x] T4 `src/api/issues_center.py`：`_post_gitcode_issue` / `_post_github_issue` 显式 `timeout` 形参
- [x] T5 `src/api/issues_center.py`：`create_task_via_issue` 去同步化（dedup / 成员校验 / 建单 / PATCH 全部 `asyncio.to_thread`）
- [x] T6 `src/api/issues_center.py`：建单超时/放弃策略（504 + `error=upstream_timeout` + `retryable` + `idempotency_key`）+ 对账探针
- [x] T7 `src/runtime/agent.py`：`_submit_linked_repo_prs` 卸载到线程
- [x] T8 `src/api/routes/webhook.py`：A2A 两处 VCS 检查卸载到线程
- [x] T9 `src/api/routes/issues.py`：附件同步评论卸载到线程
- [x] T10 `tests/test_issue_3349_dedup_offload.py`：新增覆盖（台账查重 / 有界探针 / 线程卸载 / 504 放弃 / 重试幂等 / 静态守卫）
- [x] T11 回归：`pytest`（issues 相关子集 + 新增用例）全绿，自审查（5 轴 + 开源前置增量检查）
- [x] T12 QA 审查修复（PR #1769）：CI 门禁接入本测试文件；401 刷新重试超时复用对账/504 语义；scenes 体验前检查卸载；测试断言/文档修正；clarify 存量缺陷单 #3428
