# 方案设计 — Issue #3433

## 后端

1. 新增 `src/domains/runtime/agents/failure_info.py`
   - `build_failure_info()`：统一构造 `{error_type, phase, phase_label, message, runtime, agent, exit_code}`。
   - `fallback_failure_info()` / `classify_phase()`：按 exit_code 与报错文本推断阶段（timeout / terminated / execution 等）。
   - `parse_failure_info()`：兼容 dict / JSON 字符串，字段截断防止超大堆栈污染存储。
2. `events.failure_info` 新列
   - `schema_ddl._EVENTS_TABLE_DDL` 建表加列；
   - `db_setup._migrate_tables` 幂等补列；
   - 注册 safe 迁移 `issue3433-events-failure-info-column`（strict + allow_copy，失败抛错不盖章）。
3. 统一收尾写库
   - `update_event_complete(..., failure_info=None)`：status=failed 时若未提供有效结构化原因，
     自动按 result/exit_code/agent/runtime 兜底；成功/取消不写该字段。
   - `update_event_status(..., failure_info=None)`：failed 分支同样兜底写入（历史裸调用路径）。
   - 列不存在时静默降级（老库未迁移期间不阻断终态写入）。
4. 关键调用点显式提供阶段/异常类型
   - `event_pipeline._fail_event`：从 diag.phase/code 推导；prompt 失败标 phase=prompt。
   - `event_pipeline._execute_agent` / `direct_executor._execute_agent` 顶层未捕获异常：phase=execution，error_type=异常类名，message=堆栈摘要。
   - direct/worker 流结束失败：按 exit_code 与 errors 判定 timeout/terminated/execution/completion_gate。
   - `service.fail_task`：worker 上报失败 → 结构化归因。
   - scheduler pending 超时判死 → phase=timeout；孤儿回收 attempts 封顶 → phase=recover。
   - executor 停机 kill → phase=terminated。
   - 人工终止 → phase=cancelled（update_event_status 显式传入）。
5. `GET /api/events/detail/{seq}` 返回 `failure_info`（列缺失时降级为 null，不影响详情读取）。

## 前端（EventView.vue）

- failed 且 `failure_info` 存在：渲染结构化失败卡片（原因 / 阶段 / 异常类型 / 引擎 / Agent），
  并提供「重试」「联系人工」按钮。
- failed 但无结构化字段：展示原有 result/errors + 历史事件降级提示，仍保留重试/联系人工入口。
- 顶栏事件序号旁加复制按钮，一键复制 `#<seq>`。
- 文案补充 zh/en 两份 events.json 与组件内 DEFAULTS。

## 风险与缓解

| 风险 | 缓解 |
|------|------|
| 收尾改动影响成功路径 | 仅在 status=failed 分支写 failure_info，成功字段与归档不变 |
| 老库未补列 | 写库与读取均按列存在性降级 |
| 报错过大 | message 截断 800 字符 |