# Plan: Issue #4334

## 模块设计
`src/api/watchdog_redispatch_guard.py`

| 能力 | 函数 | 数据源 |
|------|------|--------|
| 终态判定 | `evaluate_pipeline_terminal`（纯） | issue.status/stage + pipeline 段 |
| 外部阻塞识别 | `detect_external_block`（纯） | labels / payload / carried_context |
| 重派识别 | `is_redispatch_payload`（纯） | payload 标记 + context 标记 |
| 频次统计 | `count_recent_redispatches` | `events` 最近 a2a_event payload |
| 上限判定 | `redispatch_limit_state` | 计数 + 环境变量上限/窗口 |
| context 保护 | `resolve_original_context` | 源事件 seq / 最近同目标 context |
| 综合守卫 | `guard_watchdog_redispatch` | 以上组合，仅 coding |

## 接入点
1. `a2a_terminal_guard.guard_terminal_dispatch`：existing 终态未命中时调用综合
   守卫，命中则返回带 reason/detail 的短路结果（`run_a2a_dispatch` 会置事件
   cancelled）。
2. `patrol_compensate.RecheckHandler`：`issue_terminal` 之后追加 pipeline 终态
   预检，命中落 `false_positive` 并记录判据来源。
3. `patrol_compensate.DispatchHandler`：payload 增加 `redispatch_mode=watchdog`
   与 `redispatch_of`；context 为空时回填原任务结论，仍为空则转人工。

## 判定矩阵
| 信号 | 判据来源 | 动作 |
|------|----------|------|
| external-block | issue.label / payload / carried_context | skip + 留痕 |
| status/stage 终态 | issue.status / issue.stage | skip + 留痕 |
| 最新段 qa completed | pipeline.segment.seq=N | skip + 留痕 |
| 同 Issue 重派次数 ≥ 上限 | events.payload context 标记计数 | 拦截 + 留痕 |
| 重派 context 为空 | payload.context | 回填空则拦截 |

## 配置
- `WATCHDOG_REDISPATCH_LIMIT`（默认 3）
- `WATCHDOG_REDISPATCH_WINDOW_SECONDS`（默认 1800）

## 风险
- 预检查询为只读、失败放行，不改变正常派发行为。
- 只作用于 coding 目标；`force` 仍可人工越过 #2816 终态门禁。