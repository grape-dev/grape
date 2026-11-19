# Issue #3349 — 任务拆解（task）

- [ ] T1 `src/user/repo_manager.py`：`_req` / `_github_req` 支持 `timeout` 形参（默认 30s，向后兼容）
- [ ] T2 `src/api/issues_center.py`：新增常量 + `_find_open_issue_by_title_ledger`（台账查重）
- [ ] T3 `src/api/issues_center.py`：`_find_open_issue_by_title` 重构为「台账优先 + 有界 VCS 探针 + 显式超时」
- [ ] T4 `src/api/issues_center.py`：`_post_gitcode_issue` / `_post_github_issue` 显式 `timeout` 形参
- [ ] T5 `src/api/issues_center.py`：`create_task_via_issue` 去同步化（dedup / 成员校验 / 建单 / PATCH 全部 `asyncio.to_thread`）
- [ ] T6 `src/api/issues_center.py`：建单超时/放弃策略（504 + `error=upstream_timeout` + `retryable` + `idempotency_key`）+ 对账探针
- [ ] T7 `src/runtime/agent.py`：`_submit_linked_repo_prs` 卸载到线程
- [ ] T8 `src/api/routes/webhook.py`：A2A 两处 VCS 检查卸载到线程
- [ ] T9 `src/api/routes/issues.py`：附件同步评论卸载到线程
- [ ] T10 `tests/test_issue_3349_dedup_offload.py`：新增覆盖（台账查重 / 有界探针 / 线程卸载 / 504 放弃 / 重试幂等 / 静态守卫）
- [ ] T11 回归：`pytest`（issues 相关子集 + 新增用例）全绿，自审查（5 轴 + 开源前置增量检查）