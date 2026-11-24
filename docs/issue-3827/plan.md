# Plan: Issue #3827 — 退避/熔断链路补齐（legacy id 路径 + 缺列兜底）

## 定位结论
`_probe_task_binding` / `_binding_failure_code` 已对 uid 池与 legacy id 池共用一次探测，
退避/熔断状态机（`_handle_environmental_failure` / `_scan_task_once` / `_try_resume_task`）
与绑定形态无关，因此 legacy id `17877` 的读写路径本身已覆盖。真正会导致「修复上线仍每小时
60 条空转」的代码缺口是：**`autopilot_executions.error_code` 落库失败时，`_read_exec_state`
把分类丢弃 → `_execute_task` 按普通失败推进 cron，退避链路被整条旁路**。

## 改动点
1. `src/domains/runtime/engine/scheduler.py`
   - 新增 `_classify_env_error(error_code, error)`：显式分类优先；为空时按本模块产出的
     稳定指纹（`_OFFLINE_LOG_FINGERPRINT` / `_STALE_LOG_FINGERPRINT`）回推分类。
   - `_read_exec_state` 的两条返回路径（正常列 / 老库缺列回落）统一过该分类器。
   - `_handle_environmental_failure` 的结构化 ERROR 行补 `n` 字段。
2. `tests/runtime/test_issue_3800_autopilot_offline_backoff.py`
   - 新增 `test_legacy_ids_hour_backoff_and_circuit`（legacy id 池假时钟 1h）。
   - 新增 `test_missing_error_code_column_falls_back_to_text_classification`（缺列兜底）。

## 设计取舍
- 不从第三方 prose 解析分类：指纹只取本模块 `_binding_failure_code` 自己产出的字面常量，
  与 Loki 看板聚合指纹同源，属于模块内契约；显式 `error_code` 永远优先。
- 不在运行时 DDL 补列（DDL 属迁移职责，`202611230003/4` 已登记）；读取侧兜底足以让退避
  链路在「迁移尚未盖到的主控」上仍然生效，为 M1 重启/M4 全网部署争取时间。

## 测试 seam
复用 `_scheduler_now` 单一假时钟 seam 与 `_scan_task_once` / `_execute_task` 判定路径，
不新增内部 seam。

## 风险
- 指纹回推仅作用于 `error_code` 为空的场景，误判概率极低；普通失败文案不含指纹。
- 不改绑定时序/引擎选择，行为面零变化。