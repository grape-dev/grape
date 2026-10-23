# Plan: Issue #437

## 改动点

### 1. `src/observability/metric_rollup.py` — 补 availability 预聚合写入路径
- `SUM_METRICS` 增加 `engine_avail_online` / `engine_avail_total`
- `compute_bucket()` 新增第 ⑥ 步：按桶聚合 `obs_runtime_samples`（`SUM(online)`、`COUNT(*)`），
  写入 `global` 维；由既有 60s 追赶式 `catch_up` 循环自动回填 30 天小时桶

### 2. `src/observability/slo.py` — 可用率读 rollup + 周期可配 + cleanup 降频
- `_availability_buckets()` 重写：
  - 已闭合小时桶 → `metric_rollup.series(['engine_avail_online','engine_avail_total'], ['global'])`
  - 当前未闭合桶 → 原始表现算（有界，仅本小时）
  - rollup 无 availability 行 → 回退全窗原始扫描 + 日志（过渡期兼容，升级后自动消失）
- 新增 `eval_interval_sec()`：meta `slo_eval_interval_sec` > env `SLO_EVAL_INTERVAL_SEC` > 300，下限 60
- `evaluate_all()`：cleanup 只在小时桶变更时执行（模块级 `_last_cleanup_hour`）

### 3. `src/api/main.py` — 评估与告警循环解耦
- `_obs_alert_loop` 内 SLO 评估改为独立周期触发（`loop.time()` 单调钟对比 `eval_interval_sec()`），
  不再每个 30s tick 都跑

### 4. 测试
- `test_obs_metric_rollup.py`：小时桶 availability 预聚合口径
- `test_obs_slo.py`：可用率读 rollup；`eval_interval_sec` 配置源与钳制

## 风险与缓解
| 风险 | 缓解 |
|---|---|
| rollup 回填期无 availability 数据 | 回退原始扫描 + 日志，口径不变 |
| 周期调大导致违约发现延迟 | 默认 300s、可热更新，manageable |
| 旧测试依赖原始扫描路径 | 回退分支保留，旧测试仍走它 |
