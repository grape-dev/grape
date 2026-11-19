# Issue #3349 — 实施方案（plan）

## 变更清单

| # | 文件 | 变更 |
|---|------|------|
| 1 | `src/user/repo_manager.py` | `_req` / `_github_req` 增加显式 `timeout` 形参（默认 30，向后兼容） |
| 2 | `src/api/issues_center.py` | 新增台账查重 `_find_open_issue_by_title_ledger`；`_find_open_issue_by_title` 改为「台账优先 + 有界 VCS 探针」；`_post_gitcode_issue`/`_post_github_issue` 支持显式超时；`create_task_via_issue` 全链路 `asyncio.to_thread` + 504 放弃策略 + 幂等键 |
| 3 | `src/runtime/agent.py` | `_execute_task` 内 `_submit_linked_repo_prs`（同步建 PR）改 `asyncio.to_thread` |
| 4 | `src/api/routes/webhook.py` | `a2a_endpoint` 内 `_a2a_repo_fallback_check` / `_a2a_issue_vcs_check` 改 `asyncio.to_thread` |
| 5 | `src/api/routes/issues.py` | `upload_issue_attachment` 内 `sync_issue_attachment_comment` 改 `asyncio.to_thread` |
| 6 | `tests/test_issue_3349_dedup_offload.py` | 新增测试（台账查重/有界探针/线程卸载/504 放弃/重试幂等/静态守卫） |

## 关键常量（`src/api/issues_center.py`）

```python
DEDUP_UPSTREAM_TIMEOUT = 8      # 查重探针单页超时（秒）
DEDUP_FALLBACK_MAX_PAGES = 3    # 台账不可用时的降级扫描上限（成本有界）
CREATE_HTTP_TIMEOUT = 15        # 建单 POST socket 超时（秒）
CREATE_UPSTREAM_TIMEOUT = 25    # 建单请求级放弃阈值（秒，须小于网关 upstream timeout）
RECONCILE_TIMEOUT = 5           # 建单超时后的对账探针超时（秒）
```

## 台账查重 SQL

```sql
SELECT issue_number FROM issue
 WHERE repo_key = ?
   AND TRIM(title) = ?
   AND vcs_exists = 1
   AND NOT (status = 'completed' AND completed_reason = 'issue_closed')
 ORDER BY id DESC LIMIT 1
```

- 命中 → 返回 number；
- 未命中且该仓库存在台账行（`SELECT 1 FROM issue WHERE repo_key=? LIMIT 1`）→ 台账权威，返回 None；
- 未命中且仓库无台账 / 查询异常 → 降级 VCS 探针（有界 3 页）。

## 实施顺序（TDD）

1. 先写失败测试：台账命中不触发上游；探针有界；dedup 在非主线程执行；超时 → 504；重试幂等。
2. 实现 `repo_manager` 超时形参 → 台账查重 + 有界探针。
3. 实现 `create_task_via_issue` 去同步化 + 504 放弃策略。
4. 改造其余 async 上下文的 VcsApiClient 调用点。
5. 回归：`pytest tests/` 相关子集，静态守卫。

## 风险

| 风险 | 处置 |
|------|------|
| 台账未同步的仓库（webhook 未配）里存在老 issue，超出探针窗口 → 漏查重 | 降级探针保留 3 页窗口；台账覆盖 Grape 建单 + webhook 回流两条来源；PR 中记录残余风险 |
| `asyncio.to_thread` 线程池饱和 | 默认 anyio 40 线程；建单链路每请求最多 3 次出站，远低于基线并发 |
| 建单 POST 超时但上游实际已建成 | 立即对账探针（最新一页）→ 命中视作成功；未命中返回 504 + 幂等键，重试先查重 |
| 关闭后同名重开 | 台账按 `completed_reason='issue_closed'` 排除已知关闭行，保持「同名 open 才拦」语义 |