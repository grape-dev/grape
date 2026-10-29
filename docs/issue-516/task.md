# Task: Issue #516

## T1 配置层（M1）
- [ ] src/runtime/task_timeout.py：resolve_task_timeout / timeout_retry_count / 配置加载（mtime 缓存）
- [ ] configs/task-timeout.example.json
- [ ] tests/test_task_timeout_config.py

## T2 执行层接入（M1）
- [ ] agent.py _wrap 超时控制点替换为 resolve_task_timeout（日志带来源）
- [ ] service.py reap_stale_runtimes running 回收按 agent 超时对齐

## T3 checkpoint（M2）
- [ ] src/runtime/checkpoint.py TaskCheckpoint（原子写/加载/清理）
- [ ] resource_stack.py dispose 清理快照
- [ ] _execute_task 流式循环喂事件 + 成功清理
- [ ] tests/test_task_checkpoint.py

## T4 自动重试（M2）
- [ ] _wrap attempts 循环 + 退避 + resume 标记
- [ ] _execute_task 重试加载快照注入续跑上下文
- [ ] tests/test_task_timeout_retry.py

## T5 观测（M3）
- [ ] alerts.py task_timeout 指标 + 种子规则
- [ ] metric_rollup.py SUM_METRICS + compute_bucket
- [ ] tests/test_obs_task_timeout_metric.py

## T6 收尾
- [ ] 全量相关测试通过 + 新增测试
- [ ] 自审查（5 轴 + pre-pr 检查）
- [ ] PR → weekly-08-mys
