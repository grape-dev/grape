# Plan: Issue #498 实现方案

## 方案概述
在现有 health_score / total_tasks / failed_tasks / consecutive_failures 统计体系上叠加：
1. **失败衰减窗口**（M1）：`HEALTH_FAILURE_DECAY_HOURS`（默认 72h）。`_fetch_task_stats` 新增 `failed_effective`（completed_at ≥ 衰减起点的失败数，T 格式 UTC 字面量比较，复用 `_to_sql_utc`）与 `last_failure_at`（最近一次失败时间）；最近失败超出衰减窗口 → `consecutive_failures` 清零。健康分改用 failed_effective 计算（自愈：分数回升），failed_tasks 保留 7 天窗口信息口径。
2. **统一异常口径**（M1）：`_derive_health_flag(data)` → `{health_abnormal, health_reason}`，判定顺序：heartbeat_alive=False → `heartbeat_lost`；（total ≥ MIN_SAMPLES 且）cf ≥ MAX_CONSECUTIVE_FAILURES → `consecutive_failures`；score < HEALTH_SCORE_THRESHOLD → `low_score`；否则健康。写路径与读路径（DB 档）均派生，settings 路由透出。
3. **呈现一致**（M2）：前端优先消费 health_abnormal / health_reason（旧缓存无字段时回退现有逻辑，向后兼容）；worker_version_stale 独立琥珀标签。
4. **持久化**：runtime_health_scores 增列 failed_tasks_effective / last_failure_at（幂等迁移）。

## 关键决策
| 决策 | 选择 | 理由 |
|------|------|------|
| 衰减只清 cf 还是同时剔除有效失败数 | 两者（failed_effective） | 仅清 cf 时失败率仍压分，自愈不彻底；13031 案例（3 天前 1 失败）预期直接回到 100 |
| 衰减窗口时长 | 72h 默认 + env 可调 | 与 Spec 一致；大于常见模型服务抖动恢复周期 |
| 异常口径位置 | 后端派生 health_abnormal/reason，前端消费 | 单一事实源，杜绝两口径再分叉 |
| 兼容 | 只增字段不改旧字段语义 | 旧缓存/旧前端回退老逻辑 |

## 影响面
health_scorer.py（核心）、db_setup.py（迁移）、settings.py / runtime.py（透出）、runtimes.js + locales（呈现）、test_health_scorer.py + tests/js（测试）。

## 风险
- 阈值过松漏报真坏引擎：衰减只影响 72h 前的失败；新鲜连败 ≥2 仍立即计分；is_healthy 拒派逻辑保留。
- 存量 1/1 标记：上线后 5 分钟定时刷新按新规则重算，13031 自动恢复。
