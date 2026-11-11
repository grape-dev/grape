# Plan: Issue #1938 实现方案

## 1. 模块划分

新增**单一真源**模块 `src/domains/runtime/engine/cascade.py`，集中补偿枚举、终态映射、文案渲染与展示判定；
其余改动都是「接线」（service / scheduler / routes / observability / db_setup）。

```
cascade.py
├── 常量：TRIGGER_UPDATE_TASK_STATUS / TRIGGER_SWEEPER / TRIGGER_CONVERGE_EVENT
│        REASON_BACKFILLED / REASON_SYNTHESIZED
│        CASCADE_NOTICE = "系统补偿，原始回报丢失"
├── _TERMINAL_STATE_MAP：任务终态 → 事件终态 + exit_code 兜底
├── cascade_event_terminal_state(task_status, exit_code) -> (event_status, exit_code)
├── is_cascade_result_text(text) -> bool               # 历史文案识别（展示层兜底）
├── build_cascade_result(...) -> str                   # 字段 → 补偿文案（系统补偿，原始回报丢失｜…）
├── display_meta(reason, report_missing, ...) -> dict  # 展示层标注 + is_agent_conclusion
├── fetch_display_meta(cursor, seqs) -> dict           # 批量读归因（老库缺列 → 空 dict 兜底）
└── record_compensation(...)                           # Prometheus 计数（异常吞掉）
```

## 2. 数据模型（events 表新增 5 列）

| 列 | 类型 | 语义 |
|----|------|------|
| `cascade_reason` | VARCHAR(64) DEFAULT '' | `report_missing_backfilled` / `report_missing_synthesized` |
| `report_missing` | INTEGER DEFAULT 0 | 1 = 补偿发生（原始回报确定丢失） |
| `triggered_by` | VARCHAR(64) DEFAULT '' | `update_task_status` / `sweeper` / `converge_event` |
| `cascade_task_status` | VARCHAR(32) DEFAULT '' | 原始任务终态（含 `timeout_failed`） |
| `cascade_prev_event_status` | VARCHAR(32) DEFAULT '' | 补偿前事件最后已知状态（`running`/`pending`/…） |

落库方式：`src/api/db_setup.py` ①`_EVENTS_TABLE_DDL` 补列（新库）；②`_ensure_column` 迁移块（老库，幂等、MySQL 安全）。

## 3. 写入路径改造（`service.py`）

### 3.1 `_cascade_event_terminal`（唯一写点）

```python
def _cascade_event_terminal(cursor, event_seq, status, exit_code=None,
                            triggered_by=cascade.TRIGGER_UPDATE_TASK_STATUS,
                            task_status="", task_result_text=None):
```

1. 读事件当前状态；不存在或已终态 → `return False`（幂等，保持既有语义）。
2. `cascade.cascade_event_terminal_state(task_status or status, exit_code)` 得
   `(event_status, event_exit_code)`：`timeout_failed → ('failed', exit_code or -1)`，绝不写 `completed/0`。
3. 真实结论：`task_result_text` 未传 → 同事务同游标
   `SELECT result_text FROM agent_tasks WHERE event_seq=? AND result_text!='' ORDER BY created_at DESC LIMIT 1`；
   命中且不是补偿文案 → `report_missing_backfilled` + 回填文本（结构化 JSON 同步写 `structured_output`）。
4. 未命中 → `report_missing_synthesized` + `cascade.build_cascade_result(...)` 渲染文案。
5. 单条 UPDATE 写 `status/exit_code/completed_at/result/structured_output/cascade_*`。
6. `event_active_guard` 释放逻辑原样保留（同事务 DELETE，失败仅告警）。
7. 成功则 `cascade.record_compensation(agent_type, scene_id, trigger, task_status, repo_key)`。

### 3.2 `update_task_status`

- 新增 `cascade_trigger: str = TRIGGER_UPDATE_TASK_STATUS` 关键字参数（不进 SQL 列清单，安全）。
- 收敛触发条件由 `status in _TERMINAL_STATUSES` 改为 `status in TASK_TERMINAL_STATUSES`（纳入 `timeout_failed`）。
- 调用点把 `cascade_trigger` 与 `kw.get("result_text")` 透传给 `_cascade_event_terminal`。

### 3.3 `converge_event_to_task`

- 终态判定改 `TASK_TERMINAL_STATUSES`（`timeout_failed` 任务可补偿）。
- 传 `triggered_by=converge_event`、`task_status`、`task["result_text"]`。

### 3.4 `scheduler.py`（sweeper）

3 个 `update_task_status` 调用点（`:639` autopilot 收尾 / `:1384` pending 自报超时 / `:1520` 引擎离线判死）
统一传 `cascade_trigger="sweeper"`。

## 4. 展示层

新增读取侧字段（不破坏既有契约）：

- `src/domains/runtime/routes/events.py :: list_events` → 每项补
  `result_kind`（`agent` / `system_compensation_backfilled` / `system_compensation_synthesized`）、
  `is_agent_conclusion`（bool）、`cascade`（归因字典）。
- `src/domains/runtime/routes/events.py :: get_event(seq)`（事件详情）→ 同上。
- `src/domains/runtime/routes/issues.py :: _query_pr_subscription_events` → 同上。
- `src/domains/runtime/routes/issues.py :: _group_events_by_issue / _pipeline_from_events` → segment 补
  `is_agent_conclusion` / `cascade`，`output.conclusion` 对合成补偿显式标注「系统补偿，原始回报丢失」。

历史行（无新字段）用 `cascade.is_cascade_result_text()` 兜底识别，标注为「系统补偿（历史记录，无归因）」。

## 5. 可观测

- `src/domains/insight/observability/__init__.py`：新增
  `CASCADE_COMPENSATION = Counter("grape_event_cascade_compensation_total", labels=["agent_type","scene_id","trigger","task_status"])`
  + `record_cascade_compensation(...)`（标签规范化：空值 → `unknown`）。
- `alerts.py :: _repo_metrics()`：新增仓库维度 `cascade_compensation`（窗口内 `report_missing=1` 事件计数，
  SQL 侧 GROUP BY；对缺列老库 try/except 降级不出指标）。
- `alerts.py :: SEED_RULES`：新增 `cascade_compensation` 规则（user 级，`gt 3`，窗口 10min，持续 5min，
  `seed_source=GrapeCascadeCompensationHigh`）。
- `deploy/alerts.yml`：新增同名 Prometheus 规则（与种子规则一一对应，保持历史约定）。
- `metric_rollup.py`：`cascade_compensation` 进 `SUM_METRICS`，按桶从 events 统计（趋势可查）。

## 6. 测试（TDD：先红后绿）

`tests/runtime/test_issue_1938_cascade_compensation.py`

| 用例 | 断言要点 |
|------|---------|
| 三条触发路径 | `triggered_by` = `update_task_status` / `sweeper` / `converge_event` |
| 文案由字段生成 | 含 `系统补偿，原始回报丢失` + `补偿路径=` + `原始任务终态=` + `最后已知事件状态=`；不含 `任务已完成：级联收敛` |
| 无拼串兜底 | 四终态全覆盖后，任何补偿 result 都不含 `级联收敛：任务` |
| `timeout_failed` 映射 | 事件 `status='failed'`、`exit_code=-1`（非 0）、`cascade_task_status='timeout_failed'`；补偿端点不再 `task_not_terminal` |
| 真实结论回填 | `report_missing_backfilled`；`result` = task.result_text；结构化 JSON → `structured_output` 回填 |
| 幂等 | 二次补偿 `noop`；已终态事件不被覆盖；字段不被改写 |
| 守卫不退化 | 收敛后 `event_active_guard` 即时释放 |
| 展示层 | `display_meta` → `is_agent_conclusion`：合成 false / 回填 true；`list_events` 响应含标注字段 |
| 指标 | Counter 计数（prometheus 存在时读样本值）；`_repo_metrics().cascade_compensation`；种子规则存在且幂等 |

验收命令：`python3 -m pytest tests/runtime/test_issue_1938_cascade_compensation.py tests/runtime/test_issue_587_event_convergence.py tests/insight/test_obs_alerts.py tests/insight/test_obs_task_timeout_metric.py -q`

## 7. 交付顺序（原子提交）

> 实际以**单个原子提交**交付（同一需求内三处改动互为前提，拆开会让中间态测试红）：

1. `feat: #1938 structured cascade compensation attribution and backfill`（cascade.py + service/scheduler/db_setup + observability + 展示层 + 测试 + 文档）