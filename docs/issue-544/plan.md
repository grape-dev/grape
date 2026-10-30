# Plan: Issue #544 实现方案

## 模块与改动

### L1 统计任务异步化
- 新增 `src/observability/slo_backfill.py`：
  - `schedule(start, end)`：合并去重入队（单 daemon worker，`GRAPE_SLO_BACKFILL_MIN_INTERVAL` 默认 5s 限流）；
  - worker：对窗口内缺 `engine_avail_total` 行的闭合小时，按连续段扫 `obs_runtime_samples` 聚合，经 `metric_rollup.write_bucket` 写 `engine_avail_online/total`（零样本小时写 total=0 行）；`loops.beat/fail("obs_slo_backfill")`；
  - `pending()`/`drain_for_test()` 测试辅助。
- `src/observability/slo.py::_availability_buckets`：
  - 覆盖判定改为"engine_avail 行存在"（不再 t>0 过滤后判缺）；
  - 缺失闭合小时 → `slo_backfill.schedule()` + 计入 pending，**不再同步 raw-scan**；
  - 完全无 rollup 行 → 同样转后台；返回值改为 `(buckets, pending_hours)`；
  - `compute()` 把 `avail_backfill_pending_hours` 放进 extra（透出到 SLO 结论）。
- 测试 seam：`compute()` 输出 + rollup 表行（公开行为）。

### L2 幂等键
- `db_setup.py`：events 加列 `idempotency_key TEXT DEFAULT NULL` + 唯一索引 `(repo_key, idempotency_key)`（幂等迁移）。
- `event_store.append_event(..., idempotency_key="")`：非空写入 INSERT。
- `webhook.py::a2a_endpoint`：解析键（body 顶层优先，payload 兜底）；charset `^[A-Za-z0-9_:.-]{1,128}$` 校验 400；命中已有事件 → `{"status":"ok","seq":原seq,"idempotent":true,...}` 早退；append 后以 `payload.idempotency_key` 透出。
- `A2AHandoffBody` 增 `idempotency_key: str = ""`。

### L3 留痕与核对
- 指纹 `_a2a_fingerprint(repo_key, issue, agent)` = sha256 前 16 hex（不含 pr，与实现一致）；正式 A2A 事件 payload 带 `fingerprint`。
- endpoint 全体包裹：异常且尚未建事件 → 尽力写失败留痕事件（`a2a_trace: true`、fingerprint、error、status=failed），返回 500 + fingerprint。
- 新增 `GET /api/a2a/status?repository=&issue_number=&agent=&idempotency_key=`：鉴权同 POST；返回 `found/events[]`（按键精确查 + 按指纹 payload LIKE 查）。

### L4 兜底推广 + 断链提示
- `event_pipeline.py` 路由段：`pull_request_merged` 无路由且 sre 启用 → `agent_type="sre"`（默认 merge 订阅兜底）。
- `pr_merged_sre_retry.py`：`_mark_retry_exhausted` 与 event_pipeline 中 merge→sre 派发失败且未安排重试处，调 `_notify_issue_broken_chain()`（真实 Issue 号才发；`_post_result_to_issue` 以 sre 身份）。

### 文档
- `help/api/12-a2a.md` + `docs/openapi/12-a2a.md`：幂等键字段、`GET /api/a2a/status`、超时重试指引（60s、带键 ≤2 次、超时先核对）。

## 提交顺序（TDD 红→绿）

1. `test(#544): L1 缺失小时转后台回填（红）` → `feat(#544): slo_backfill 队列 + _availability_buckets 去同步 raw-scan（绿）`
2. `test(#544): L2 幂等键去重（红）` → `feat(#544): events 幂等键列/索引 + a2a 幂等早退（绿）`
3. `test(#544): L3 失败留痕 + 核对接口（红）` → `feat(#544): a2a 留痕 + GET /api/a2a/status（绿）`
4. `test(#544): L4 merge 默认 sre + 断链评论（红）` → `feat(#544): merged 默认 sre 兜底 + 断链 Issue 提示（绿）`
5. `docs(#544): help/openapi A2A 幂等键与核对接口`

## 风险

- 唯一索引迁移：存量行为空列（NULL）不冲突，幂等可重跑；MySQL 无 `IF NOT EXISTS` 的索引创建按 db_setup 既有容错模式（异常吞掉告警）。
- 后台回填写 total=0 行改变 `series` 语义：读取侧仍按 t>0 过滤，仅覆盖判定用行存在性，口径不变。
