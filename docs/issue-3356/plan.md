# Plan: Issue #3356 — /api/repos/member stale-while-revalidate + 并行回源

## 方案设计

### 改动点
1. `src/domains/mgmt/routes/repos.py`
   - 新增进程内 singleflight 状态 `_REFRESH_INFLIGHT`（key = `(user_id, providers)`，值为 `threading.Event`）。
   - 新增 `_fetch_member_repos_parallel()`：用 `ThreadPoolExecutor` 并行执行各 provider 的 `with_oauth_token(user, p, _fetch)`；401 作软失败记录 `_reauth_provider`，非 401 `HTTPException` 进 `fatal_errors` 并同时按失败平台处理；整轮回源受 `MEMBER_REFRESH_DEADLINE` 截止，超时未完成平台按失败处理，`pool.shutdown(wait=False)` 不阻塞事件释放。
   - 新增 `_merge_member_repos()` / `_save_member_repos()`：复用原「按 repo_key 去重 + 失败平台旧缓存补齐 + 只替换成功平台」语义。
   - 新增 `_refresh_member_repos_cache()` / `_schedule_member_refresh()`：后台刷新主体与 singleflight 调度。
   - `list_member_repos()`：
     - 未过期 → 直接返回缓存（原行为）。
     - 有缓存且过期 / `refresh=1` → 立即返回缓存，并（若未在刷新）启动后台刷新；响应带 `_cached=True`、`_stale`（过期时）、`_refreshing`（实际启动刷新时）。
     - 无缓存 → 同步并行回源一次后返回（返回结构与原一致）。
2. `tests/mgmt/test_3356_member_cache_swr.py`（新增）：覆盖命中、过期 SWR、`refresh=1`、首次并行、后台失败降级、singleflight。
3. 更新既有 member 相关测试以适配新语义（过期/refresh 改后台刷新）及当前 `list_platform_links` 依赖。

### 关键实现细节
- 后台刷新使用 daemon 线程，异常全捕获并记 `log.warning`；失败时不写库，缓存保持过期状态以便下次重试。
- 后台刷新仅在 `_claim_member_refresh()` 成功时执行，结束后 `event.set()` 释放，等待者可见。
- 后台刷新遇到非 401 硬错误只把该平台记为失败并记 warning，健康平台结果照常合并落库；仅全部平台失败（含整轮超时）时跳过写库。
- 并行度 `max_workers = min(len(providers), 8)`，不改变各 provider 内部分页逻辑。
- 首次无缓存路径同步等待并行结果（同样受 deadline 约束）；非 401 `HTTPException` 仍原样抛出；401 软失败分支保持原 401/reaut/引导页语义。

### 测试矩阵
| 用例 | 前置 | 期望 |
|---|---|---|
| 缓存命中 | 未过期缓存 | 不回源，`_cached=True` |
| 过期 SWR | 过期缓存 + 慢回源 | 立即返回旧缓存（`_stale=True`），后台完成落库 |
| refresh=1 | 未过期缓存 | 立即返回缓存，后台回源一次，不阻塞 |
| 首次回源 | 无缓存 + 双平台慢回源 | 并行完成（耗时≈单平台），合并落库 |
| 后台失败 | 过期缓存 + 回源抛错 | 保留旧缓存，不落库，不抛错 |
| singleflight | 过期缓存 + 连续请求 | 刷新在飞期间只回源一轮 |

### 风险与对策
- 后台线程泄漏：daemon + try/finally 释放 Event。
- 失败后反复触发：保留过期状态，由下次请求触发；不提前写 `synced_at`。
- 并行限流：并行度=平台数（≤8），平台内分页请求保持原样。

## 任务拆解
- [x] T1 新增 SWR/并行/singleflight 测试（RED）
- [x] T2 实现 `repos.py` 缓存与并行回源改造（GREEN）
- [x] T3 更新既有 member 相关测试并回归
- [x] T4 自审查（5 轴 + 开源前置检查）
- [x] T5 commit + push `feat/issue-3356` + 建 PR #1747（base=`weekly-mys`）+ Issue 评论 + A2A QA