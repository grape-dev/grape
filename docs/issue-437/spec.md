# Spec: Issue #437 — SLO 评估增量化 + engine_availability 改读小时桶 + 评估周期可配置

## 背景
SLO 周期评估每 30s 跟随告警循环执行，且 `engine_availability` 每轮对整个 30 天窗口全量扫描
`obs_runtime_samples`（60s 采样 ≈ 4.3 万行/引擎/天）。对一个每分钟才采样一次的指标，30s 一轮
全窗现算既浪费 DB 也占全局锁。

## 目标
1. `engine_availability` 读 `obs_metric_rollup` 小时桶预聚合（新增 `engine_avail_online` /
    `engine_avail_total` 两个 SUM 指标，由聚合循环按桶写入），不再全窗扫原始采样表。
    两级回退口径：
    - **部分回填期**（窗口内已有部分 `engine_avail_*` 桶）：缺失的闭合小时不静默丢弃，
      逐段回退有界原始扫描（连续缺失段合并为一次查询）；窗口起点落在小时中段时，
      头部不完整小时有界现算。头部小段 / 回填段与 rollup 行合并后**统一按桶时间排序**
      返回（`burn_series` 按序累计）。
    - **完全无 rollup 数据**（升级初期）：回退旧的全窗原始扫描 + 日志，口径与改动前一致。
    - 当前未闭合小时桶始终有界现算（仅本小时）。
2. SLO 评估与 30s 告警循环解耦：独立周期，meta 表 `slo_eval_interval_sec`（热更新）>
   环境变量 `SLO_EVAL_INTERVAL_SEC` > 默认 300s，下限 60s。
3. `cleanup_snapshots` 降低频率：只在评估小时桶变更时执行（原先每 30s 一次 DELETE）。

## 范围外
- 不改表结构（rollup 的 metric 是字符串值，天然免 DDL）
- 不改 `window_days` / `MAX_WINDOW_DAYS` / 告警循环 30s 频率
- 不新增 SLI 类型

## 验收标准
- `evaluate_all` 不再全窗口扫描原始采样表
- `engine_availability` 走 rollup 小时桶（含回退路径）
- 评估周期可配置、默认 ≥300s、与告警循环解耦
- 快照 / 违约 / 燃尽曲线无回归；现有测试通过 + 新增测试覆盖
