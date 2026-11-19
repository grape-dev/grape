# Spec: Issue #2132 — 用户面冻结：观测页慢查询持锁 + 建单路径同步读对象存储（生产 hotfix twin）

> 来源：Issue #2132（Spec 已由 Issue Agent 发布并经复核确认）。本文为**旧布局生产修复 twin** 落地版：
> 开发基线 `the legacy-layout branch`（weekly-11 合流、旧布局），实现与 PR #1271（base `the reviewed ljt line`，已过 QA）
> 等价的 A1-A3 / B1-B3 改动；两条线共用同一 Spec 验收口径。

## 目标

切断「一次慢活拖停整台主控」的两条传导链：

- **A 线（观测页）**：`runtime_history` 不再全量拉回 2000 万级采样表并持全局 `db_lock` 做聚合；页面不可见时不再轮询。
- **B 线（建单）**：知识正文的对象存储读取不再阻塞事件循环；知识条目数/并发数有上限且截断可观测；同仓内容复用进程内缓存。

## 范围

### In Scope

- A1 `runtime_history` 追加 `sampled_at >= cutoff` SQL 谓词（命中 `idx_obs_sample_rt`），Python 时间过滤保留兜底。
- A2 `db_lock` 只包 `execute/fetchall`，行转换/分桶聚合移到锁外。
- A3 设备分区 10s 轮询按 `document.visibilityState` 启停：切后台停表、切回立即刷新一次。
- B1 `collect_knowledge_files` 的对象存储读取经 `asyncio.to_thread` 出事件循环（新增异步入口 + 建单前置预热）。
- B2 知识条目上限（`KNOWLEDGE_MAX_FILES`）与 OBS 读取并发上限（`KNOWLEDGE_READ_CONCURRENCY`），支持按仓覆盖；截断落 warning + 计数。
- B3 repo 级进程内读缓存：key = storage_key（含 repo 前缀）+ content_hash，带 TTL（`KNOWLEDGE_CACHE_TTL_SEC`）与容量上限（`KNOWLEDGE_CACHE_MAX`）。
- QA 复审修复项（对齐 PR #1271）：建单预热时延预算、`member_files_pi` 同口径限流、回退收窄到方言类错误 + 行数上限、TTL 0/负值回退默认、配置按 mtime 缓存、缓存/截断计数经 `/health/realtime` 暴露、防御性截断最新优先。

### Out of Scope

- `routes/` 其余约 300 处大块 `with db_lock` 的全量重构。
- 采样保留期清理（`cleanup_expired_samples`）——已单独立项 **#2219**。
- 接口返回结构 / 前端页面改版（契约不变）。

## 技术落点（本分支为旧布局，与 PR #1271 新布局路径映射）

| 模块（旧布局） | 对应 PR #1271 新布局 | 改动 |
|---|---|---|
| `src/observability/rollup.py` | `src/domains/insight/observability/rollup.py` | A1 SQL 谓词 + 回退；A2 锁外聚合 |
| `src/api/routes/observability.py` | `src/domains/insight/routes/observability.py` | A2 锁外行转换 |
| `src/api/static/js/pages/observability.js` | 同路径 | A3 可见性轮询 |
| `src/api/knowledge.py` | `src/api/knowledge.py` | B1 并发预读；B2 上限；B3 TTL 缓存 |
| `src/runtime/service.py` | `src/domains/runtime/engine/service.py` | B1 异步入口 / 预热；B2 防御性截断 |
| `src/api/issues_center.py` | `src/api/issues_center.py` | B1/B3 建单前置预热（预算受限） |
| `src/api/health_endpoints.py` | `src/api/health_endpoints.py`（+`server_common.py`） | 缓存/截断计数运维可观测 |

> 旧布局无 `src/api/server_common.py`（三进程拆分属 arch 线），运维暴露只挂 `health_endpoints.py`。

## 验收标准（与 PR #1271 同口径）

1. A1：`EXPLAIN QUERY PLAN` 命中 `idx_obs_sample_rt`；接口 p95 < 500ms（现网验证，A1 以当前未清理数据规模为基准）。
2. A2：聚合在锁外执行（单测以持锁探针钉死）；压测 1 小时无「≥3 请求同秒收尾」窗口。
3. A3：切后台停轮询、切回立即刷新并恢复轮询（前端回归用例）。
4. B1：收集/OBS 读取在线程池执行，事件循环线程不出现 `ObsClient` 栈。
5. B2：超限截断有 warning 日志 + `knowledge_cache_stats().truncated_files` 计数。
6. B3：缓存命中不回源；TTL 到期回源；容量上限淘汰最旧条目。
7. 现有相关测试通过 + 新增用例覆盖谓词/锁外聚合/IO 线程化/上限截断/缓存 TTL。

## 风险

- 旧库 `sampled_at` 若存在非 ISO 格式：谓词下推会漏读 → 已保留 Python 过滤兜底，且同仓其它模块（SLO/metric_rollup）已在用同口径谓词。
- 条目上限可能影响知识完整性：默认值取宽（1000），支持按仓覆盖，截断可观测，先观察再收紧。
- 建单前置预热为纯缓存优化，失败仅告警，不影响建单主流程与返回语义。
