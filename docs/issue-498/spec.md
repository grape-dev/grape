# Spec: Issue #498 — 引擎健康检测口径不一致 + 小样本误判 + 无自愈

> 基线分支：weekly-08-mys | 里程碑：M1 判定侧 → M2 透出与呈现 → M3 验证回归

## 目标
修复引擎健康检测三处缺陷：
1. 前端/检测侧「在线」与「健康异常」口径不一致：runtime 13031 页面显示在线，却因窗口内 1 次模型流偶发失败被标记异常，且异常原因不可见。
2. 健康判定无时效抗抖动：#439 已加最小样本门槛（HEALTH_MIN_SAMPLES=5，分数稀释 + 连败惩罚豁免），但 consecutive_failures 无时间窗衰减——单次偶发失败（如 3 天前）长期保持 cf=1，前端据此渲染「连续失败/异常描边」。
3. 无自愈：探活/心跳正常 + 窗口内无新失败时，consecutive_failures 不清零、健康分不回升（失败率仍按 7 天窗累计）。

## Scope (In)
- `src/runtime/health_scorer.py`：失败时间窗衰减（默认 72h 无新失败 → consecutive_failures 清零、失败不计入健康分有效口径 failed_effective）；统一异常判定派生 health_abnormal / health_reason（心跳失联 > 连败超限 > 低分，最小样本门槛与 is_healthy 同口径）；参数 HEALTH_FAILURE_DECAY_HOURS 可配置。
- `src/api/db_setup.py`：runtime_health_scores 增列 failed_tasks_effective / last_failure_at（_ensure_column 幂等迁移）。
- `src/api/routes/settings.py`：agent-runtimes 列表/详情透出 health_abnormal / health_reason / health_last_failure_at / health_failed_tasks_effective（异常原因可见）。
- `src/api/routes/runtime.py`：machine-health 端点随 scorer 返回值自动携带新字段（描述同步更新）。
- 前端 `runtimes.js`：_healthHelpHint / _isAbnormalEngine / _abnormalBorderColor 优先采用后端 health_abnormal / health_reason 口径（旧缓存回退老逻辑）；worker_version_stale 独立琥珀色提示，不影响异常描边。
- locales zh/en：新增 runtimes.label.worker_version_stale。
- `tests/test_health_scorer.py`：扩展抗抖动（衰减清零）与自愈（分数回升/标记消除）用例；`tests/js/` 新增前端口径一致性用例。

## Out of Scope
- codearts CLI 模型流偶发异常根因（上游模型服务）；worker 自动升级；#495 真坏引擎修复。

## 验收标准
- runtime 13031：上线后 5 分钟刷新周期内自动恢复（cf=0、score=100、无异常标记、无异常描边），在线口径与健康口径一致；若仍异常 → reset 健康分；探活失败 → 置 offline。
- 单次偶发失败（含已衰减的旧失败）不再长期判异常；衰减窗口可配置。
- 探活正常 + 无新失败 → 异常标记消除、健康分回升（自动，无需人工 reset）。
- worker_version_stale 与健康异常区分展示。
- 现有测试全通过 + 新增测试通过。
