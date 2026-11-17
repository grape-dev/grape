# Issue #2925 — 方案设计（plan）

## 三层收敛

1. **范围有界**：`get_pipeline` 在构建 pipelines 前调用
   `_backfill_window_titles(..., window_size=_need)`，只处理当前页窗口
   （`1 + history_offset + history_limit`）内的 issue。窗口外保持占位，
   不触发任何外呼。
2. **执行有界**：新增 `event_pipeline.fetch_issue_titles_batch`
   - token 通过 `_resolve_issue_read_token` 单次解析；
   - `VcsApiClient` 单次构建，多线程共享（客户端无状态）；
   - `ThreadPoolExecutor` 并发上限默认 8（`GRAPE_PIPELINE_TITLE_FETCH_CONCURRENCY`，1~16）；
   - 总 deadline 默认 4s（`GRAPE_PIPELINE_TITLE_FETCH_DEADLINE`），单请求超时默认 3.5s；
   - 到点即返回，`shutdown(wait=False, cancel_futures=True)` 不等待排队任务。
3. **状态有界**：
   - 占位判定抽为模块级 `_is_placeholder_title`，兜底触发、写回条件、批量复核共用；
   - 只写「非占位」的真实标题，SQL 护栏为
     `WHERE repo_key IN (...) AND issue_number=? AND (title='' OR title IS NULL OR title=<观测占位值>)`，
     避免并发写入的真实标题被回滚；
   - 未取到真实标题：写 `title_fetched_at`（epoch 秒字符串）形成负缓存；
   - 列不可用时降级为进程内 TTL 负缓存（`GRAPE_PIPELINE_TITLE_NEG_CACHE_TTL`，默认 6h）。

## 数据结构

`issue.title_fetched_at VARCHAR(64) DEFAULT ''`，迁移
`202611170043 issue2925-issue-title-fetched-at`（`_ensure_column(..., strict=True)`，
幂等、补列失败不盖章）。

## 可观测

每次请求输出一行结构化日志：

```
[pipeline] title_backfill repo=... window=... candidates=... vcs_calls=...
           fetched=... failed=... cache_hit=... skipped_marked=... elapsed=...s
```

热请求满足 `vcs_calls=0`，可直接作为验收证据。

## 回滚

- 代码回滚即可恢复旧行为；新列对旧代码无副作用（旧代码不读该列）。
- 若担心冷请求仍偏慢，可下调并发/调大 deadline，无需改代码。

## 风险

| 风险 | 缓解 |
|------|------|
| 上游限流 | 默认并发 8，可配 1~16；失败静默降级为占位 |
| 负缓存导致真实标题长期不刷新 | TTL 默认 6h，可设 0 表示永久（仅成功写回解除） |
| 窗口外标题占位被误判回归 | 与 Issue 验收一致：窗口外显式不核实，日志 `window`/`candidates` 可核对 |

## 数值来源（架构闸 C-1 补齐）

| 常量 / 默认值 | 位置 | 来源 |
|---|---|---|
| 并发 8（范围 1~16） | `event_pipeline.py` `_title_fetch_concurrency` | spec 验收项 2「有限并发」；8 是「97 条 × 0.22s ÷ 8 ≈ 2.7s < 4s」的最小并发，上限 16 给压测留一倍余量 |
| 总 deadline 4.0s（0.5~30） | `_title_fetch_deadline` | spec 验收项 2 要求冷请求 ≤5s，4s 留 1s 给响应组装 |
| 单请求超时 3.5s（0.5~30） | `_title_fetch_timeout` | 须小于总 deadline；实测单次 issue 详情 0.44s，3.5s ≈ 8 倍余量 |
| 上界 30.0s | 同上两处 | 与「单次 HTTP 请求不超过 30s」同量级，超过等同本请求不返回 |
| 负缓存 TTL 21600s = 6h | `title_backfill.py` `_TITLE_NEG_CACHE_TTL_DEFAULT` | **量级默认值，未经实测**：按「一个工作日」取整，覆盖会话内热请求并允许上游改标题后当天重试；可 env 覆盖，0 = 永久 |
| 进程内缓存上限 4096 | `title_backfill.py` `_TITLE_NEG_CACHE_MAX` | 结构估算：单条约 200B，4096 条 ≈ 0.8MB；只承担「防无界增长」，非容量目标 |
| 标记读取分片 200 | `title_backfill.py` `_TITLE_MARKER_QUERY_CHUNK` | SQLite 单条 SQL 变量上限 999；单条 SQL 同时占 `len(repo_keys) + len(chunk)`，200 给 repo_key 侧留足余量 |

待测量清单（不改代码即可调优，均已登记 `.env.example`）：
1. `GRAPE_PIPELINE_TITLE_NEG_CACHE_TTL` 的默认 6h —— 缺「同仓热命中率」与「上游标题变更频率」实测；
2. 进程内缓存上限 4096 —— 缺目标部署的常驻内存预算实测；
3. 分片 200 —— 仅按 SQLite 上限推导，MySQL 后端无该限制（分片只影响往返次数）。