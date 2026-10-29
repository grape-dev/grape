# Plan: Issue #516 — 超时配置化 + 自动重试/checkpoint + 聚合观测

## 总体架构（分层）

```
配置层  src/runtime/task_timeout.py     resolve_task_timeout / timeout_retry_count
        configs/task-timeout.example.json（可选 JSON 配置，env GRAPE_TASK_TIMEOUT_CONFIG 指定路径）
执行层  src/runtime/agent.py            _wrap: 超时控制点配置化 + 有限次重试循环
                                        _execute_task: checkpoint 周期落盘 + 重试注入续跑上下文
快照层  src/runtime/checkpoint.py       TaskCheckpoint（temp+rename 原子写 / 加载 / 清理）
        src/runtime/resource_stack.py   dispose 时清理 checkpoint 文件
服务端  src/runtime/service.py          running 回收阈值按 agent_type 超时对齐（防 7200s 双跑）
观测层  src/observability/alerts.py     _repo_metrics 新增 task_timeout 指标 + 种子告警规则
        src/observability/metric_rollup.py  task_timeout 加入 SUM_METRICS 历史聚合
```

## M1 超时配置化

### 解析优先级（高 → 低）
1. 任务级显式 env：task `env_extra` 里 `RUNTIME_TASK_TIMEOUT`（手动调参，setdefault 语义不覆盖）
2. per-skill：配置文件 `per_skill`（task["skill"]）
3. per-repo：配置文件 `per_repo`（task["repo_key"]）
4. agent 默认：`AGENT_TIMEOUT_DEFAULTS = {"coding": 7200}`（其余 agent 不回退）
5. 全局进程 env `RUNTIME_TASK_TIMEOUT`（兼容既有部署）
6. 兜底 3600

### 配置文件 schema（configs/task-timeout.json，可选）
```json
{
  "agent_defaults": {"coding": 7200},
  "per_repo": {"owner/repo": 7200},
  "per_skill": {"skill-name": 7200},
  "timeout_retries": 1,
  "retry_backoff_sec": 30
}
```
mtime 缓存加载；非法值忽略并回退（不抛）。

### 接入点
- `agent.py _wrap`：`task_timeout, src = resolve_task_timeout(t)`，日志带来源。
- `service.py reap_stale_runtimes`：running 任务回收阈值按该任务 agent_type 的超时解析值（对齐 coding=7200，防 worker 还在跑被服务端 3600s 重派双跑）。

## M2 自动重试 + checkpoint

### 重试
- `_wrap` 执行段改为 attempts 循环：`1 + timeout_retry_count(t)`（默认 1，env `RUNTIME_TASK_TIMEOUT_RETRIES` / 配置文件 `timeout_retries`）。
- 每次超时：kill 引擎 → 退避 `retry_backoff_sec`（默认 30）→ 下一轮 `_execute_task` 前 task 打 `_grape_resume_attempt` 标记。
- 最终仍超时才 fail，消息保留 `task timed out after {N}s` 签名（M3 观测依赖）+ attempt 数。

### checkpoint
- `TaskCheckpoint(task_id)`：内存 tail（最近 40 条事件摘要）+ 周期（默认 60s）原子落盘（temp+rename），路径 `~/.cache/grape-task-checkpoints/<tid>.json`（env `GRAPE_CHECKPOINT_DIR` 可覆盖）。
- `_execute_task` 流式循环内喂事件；成功路径 `TaskResourceStack.dispose()` 清理快照文件（终态无需续跑）；超时取消路径不清理，供重试加载。
- 重试（`_grape_resume_attempt ≥ 2`）时 `_execute_task` 加载快照：prompt 追加「上次尝试超时 + 最近进度事件」续跑上下文，env 注入 `GRAPE_RESUME_CHECKPOINT`。
- 损坏快照降级：加载失败视为无快照从零跑。

## M3 观测
- `alerts.py _repo_metrics`：新增 `task_timeout` 指标 = 失败窗口内 `status='failed'` 且 `result_text/errors LIKE '%timed out after%'` 的任务数（按 repo GROUP BY）。
- 种子规则 `task_timeout`（metric, user 级, gt 0, window 600s, for 300s, warning）。
- `metric_rollup.py`：`task_timeout` 加入 SUM_METRICS，`compute_bucket` 增加超时失败计数查询（历史聚合/趋势）。

## 测试计划
- `tests/test_task_timeout_config.py`：优先级全链（skill>repo>agent>env>默认）、coding=7200、其余不回退、显式 env setdefault、非法配置回退、重试次数解析。
- `tests/test_task_checkpoint.py`：原子写/加载/清理、损坏降级、tail 截断。
- `tests/test_task_timeout_retry.py`：_wrap 超时重试 ≥1 次、重试成功不 fail、耗尽后 fail 消息签名、resume 上下文注入。
- `tests/test_obs_task_timeout_metric.py`：_repo_metrics 超时计数、种子规则存在、rollup SUM_METRICS 包含。

## 风险与对策
- 核心路径改动：默认值对非 coding 完全等价旧值 3600，回归风险低。
- 重试资源翻倍：次数上限 1-2 + 退避间隔。
- 服务端/worker 超时不一致双跑：service.py 回收阈值同源解析。
