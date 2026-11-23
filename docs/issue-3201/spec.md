# Issue #3201 Spec：接管 active 期间统一拦截自动派发

## 目标

接管（takeover）会话 active 时，同一 Issue 的自动派发（webhook / A2A / 重派）统一被拦截；
autopilot 巡检任务无真实 Issue 维度，按其所辖仓库的 active 接管冻结：
- 不建 task、不推进节点状态；
- 被拦 webhook 事件入库并置 `status=skipped_takeover`，可审计、不排队重放；
- A2A 入口返回 409 `already_taken_over`；
- 接管节点状态固定 `running(takeover)`，不被后续 `completed` 覆盖；
- token 过期按不活跃自愈，不永久冻结 Issue。

## 范围

- `src/domains/runtime/pipeline/event_pipeline.py`：`_create_direct_task` 与 `_enqueue_pipeline_task` 两个建 task 出口统一守门。
- `src/domains/runtime/engine/takeover.py`：active 判定自愈、忽略计数、finish 返回计数。
- `src/domains/runtime/routes/webhook.py`：A2A 入口 409 文案。
- `src/api/pipeline_status.py`：active 接管锁定 running，过滤 skipped_takeover。
- `src/domains/mgmt/routes/takeover.py`：列表/详情暴露已忽略条数。

## 验收

1. 接管 active → webhook `issue_comment` 不建 task，事件 `status=skipped_takeover`；
2. 接管 active → A2A 派发返回 409 `already_taken_over`；
3. 接管 active → finish 触发的下一节点派发正常建 task；
4. token 过期 → 守门放行；
5. 同仓另一个 Issue 不受影响；
6. 接管 active 且已有 completed 事件 → 节点状态仍为 running；
7. `skipped_takeover` 不参与最后事件映射，也不被视为正在执行；
8. finish 与接管详情返回本次已忽略 N 条自动派发。
