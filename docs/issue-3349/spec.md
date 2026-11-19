# Issue #3349 — 建单端点去同步化 + 上游超时/幂等策略

> 需求来源：Issue #3349 Body + Issue Agent 已发布 Spec（评论 202010797）、VOD 复核（评论 202127829）。

## 1. 问题

`POST /api/issues/{owner}/{repo}` → `create_task_via_issue()`（`src/api/issues_center.py`，`async def`）
→ `_find_open_issue_by_title()` 用**同步** `requests`/`urllib` 逐页翻页查重（`state=open&per_page=50`，
上限 1000 页），把事件循环独占。

实测（Issue Body）：40 open issue 1.09s / 210 open 5.8s / 312 open 5.55s（串行 7 页）/ 2000 open ≥30s；
并发 1 约 25 条/分钟，并发 10 反而降到 3 条/分钟并出现网关 502。同期 `/api/health` 持续 200、
无 OOM、服务单元 active、积压降下来后 502 秒级消失 → **事件循环阻塞导致网关排队超时**，不是服务崩溃。

同一函数内还有 4 处同步 VCS 调用（assignee 成员校验、建单 POST、多任务模式 body PATCH），
以及 4 处 async 上下文内的 `VcsApiClient` 调用点（linked PR 提交、A2A 两处可达性/存在性检查、
附件同步评论）。

## 2. 目标（可量化）

- 2000 open issue / 4 seeder 并发建单无 502；
- 单次建单耗时**不随 open issue 数线性增长**（查重退化为常数成本）；
- 并发吞吐不低于单并发基线（约 25 条/分钟）；
- async 路由内不存在同步 urllib 翻页查重（VCS 出站一律 `asyncio.to_thread`）；
- 上游超时/故障返回**可判别**失败（504 / 502 带错误码），重试按 owner/repo/title 幂等；
- `/api/health` 压测期间持续 200；现有测试通过 + 新增覆盖。

## 3. 范围

In scope：
- `src/api/issues_center.py`：查重改查平台库（本地 `issue` 台账）、VCS 兜底探针带显式超时、
  建单/成员校验/PATCH 全部 `asyncio.to_thread` + 请求级放弃阈值 + 504 语义。
- `src/user/repo_manager.py`：`_req` / `_github_req` 支持显式 `timeout` 形参（默认值保持 30s）。
- async 上下文内的其余 `VcsApiClient` 调用点：`src/runtime/agent.py`（`_execute_task`）、
  `src/api/routes/webhook.py`（`a2a_endpoint`）、`src/api/routes/issues.py`（`upload_issue_attachment`）。
- 新增测试 `tests/test_issue_3349_dedup_offload.py`。

Out of scope：
- 平台库（`issue` 台账）本身的写入链路（`ensure_issue_task` 已具备）；
- 网关 upstream timeout 配置（非本仓库）；
- `clarify` / `resume` 等其它端点的同步出站（各自独立缺陷单跟踪）。

## 4. 方案

### 4.1 查重：平台库为主，VCS 探针为降级

`_find_open_issue_by_title()` 保持原签名与返回语义（命中返回 number，未命中 None，凭据故障抛
`RuntimeError`），内部改两段式：

1. **平台库查重**（`_find_open_issue_by_title_ledger`）：查 `issue` 台账
   `repo_key + TRIM(title)`，排除已知在 VCS 侧关闭的行（`status='completed' AND completed_reason='issue_closed'`）、
   已删除行（`vcs_exists=0`）。命中 → 直接返回 number（O(1) 索引查询）。
2. **账本可判定性**：该仓库存在台账行 → 台账权威，未命中即「确无同名 open Issue」，**不再翻页**；
   该仓库无台账行（首次建单/台账未覆盖）或台账查询异常 → 降级 VCS 探针。
3. **VCS 探针**（`_find_open_issue_by_title_vcs`）：最多 `DEDUP_FALLBACK_MAX_PAGES`（默认 3）页、
   `per_page=50`、**单页显式超时 `DEDUP_UPSTREAM_TIMEOUT=8s`**；GitHub 仍走 Search API（单请求，
   精确匹配 title）。探针失败 → `RuntimeError`（保持 Issue #343「查重失败不放行」语义，避免重复建单）。

成本：常规路径 1 次本地索引查询（与 open issue 数无关）；降级路径最多 3 个上游请求（有界）。

### 4.2 建单链路去同步化

`create_task_via_issue` 内所有 VCS/慢 IO 出站移出事件循环：

| 调用 | 改造 |
|------|------|
| `_find_open_issue_by_title`（含 401 刷新后重试） | `await asyncio.to_thread(...)` |
| `_gitcode_assignee_member_check` | `await asyncio.to_thread(...)` |
| `_post_gitcode_issue` / `_post_github_issue`（含 401 重试） | `await asyncio.to_thread(..., timeout=CREATE_HTTP_TIMEOUT)`，外层 `asyncio.wait_for(..., CREATE_UPSTREAM_TIMEOUT)` |
| `_patch_issue_body` / `_patch_github_issue_body` | `await asyncio.to_thread(...)` |

其余 async 调用点同样 `asyncio.to_thread`：`_submit_linked_repo_prs`（agent 执行线程）、
`_a2a_repo_fallback_check` / `_a2a_issue_vcs_check`（A2A 入口）、`sync_issue_attachment_comment`（附件上传）。

### 4.3 显式上游超时 / 放弃策略 + 重试幂等

- 幂等键：`idempotency_key = "<owner>/<repo>/<title>"`，随 409 / 502 / 504 响应返回，便于调用方重试对账。
- 建单 POST：socket 超时 15s（`CREATE_HTTP_TIMEOUT`），请求级放弃阈值 25s（`CREATE_UPSTREAM_TIMEOUT`，
  显著小于网关 upstream timeout）。
- 上游超时/放弃 → 立即做一次**有界对账探针**（VCS 最新一页，5s 超时）：
  - 命中 → 视作已建成（`reconciled=true`），继续正常建档并返回成功，不重复建单；
  - 未命中/探针不可用 → **504** `{"code":504,"error":"upstream_timeout","retryable":true,"idempotency_key":...}`，
    与「成功」可判别；同标题重试会先走查重，已建成则返回 409（含 `issue_number`），不重复建单。
- 查重失败（凭据/网络）→ 保持 401 `reauth_required` / 502 `查重失败` 可判别语义。

### 4.4 关键设计决策

| 决策 | 选项 | 选择 | 理由 |
|------|------|------|------|
| 查重数据源 | ① VCS 全量翻页 ② 平台库 ③ 平台库 + VCS 探针 | ③ | ② 消除 O(n) 翻页；③ 在台账未覆盖的仓库上仍能拦住跨通道重复单 |
| 仓库无台账时的扫描上限 | 原 1000 页 / 有界 3 页 | 有界 3 页 | 翻页成本必须与 open issue 数解耦（验收项） |
| async 内同步出站 | ① 保留 ② async client ③ `asyncio.to_thread` | ③ | 与同仓既有实践一致（`src/runtime/bot_token_health.py` 等），改动面可控 |
| 上游超时后的结果 | ① 直接 502 ② 对账探针 + 504 | ② | 避免「其实已建成」与失败混淆，同时保证重试幂等 |

## 5. 验收

- [ ] `pytest tests/test_issue_3349_dedup_offload.py` 全绿（去同步化 / 超时放弃 / 重试幂等 / 台账查重）；
- [ ] `pytest tests/test_issues_api.py tests/test_issue_453_outbound_ledger.py tests/test_issue_523_create_403_attribution.py ...` 现有回归全绿；
- [ ] 静态守卫：`create_task_via_issue` 内无裸同步 VCS 调用；async 路由内无同步 urllib 翻页；
- [ ] 压测（2000 open / 4 seeder）：无 502、health 持续 200、建单耗时不随 issue 数增长（由 sre/QA 复测）。