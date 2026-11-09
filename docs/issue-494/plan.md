# Plan: Issue #494

## 方案
1. 新增 `src/agent/failure_attribution.py`：统一归因文案构造（`timeout_error_text` / `process_exit_attribution` / `failure_summary`），全链路复用。
2. `src/agent/hwcloud_acp.py` / `src/agent/hermes_acp.py`：stopReason=timeout 收口处，errors 为空时补「执行超时（Ns）」显式归因（记录触发超时的实际秒数：静默超时 / 全局超时）。
3. `src/agent/executor.py`：CLI 后端 returncode=124（timeout 命令）/ -15（SIGTERM）补超时归因；静默超时强杀路径的 errors 文案改用统一 helper。
4. `src/api/event_pipeline.py` direct 失败收口：done 事件 errors 合并写入 `agent_tasks.errors`（不覆盖 #403 transient 记录）；`result_text` 为空时构造可读失败摘要 → 事件 result 透出（前端失败原因卡片数据源）。
5. `src/api/routes/events.py` 事件详情 API：task_info 增加 `task_errors`（解析 JSON、截断、限量）。
6. 前端 `src/api/static/js/pages/events.js` 失败态：失败原因卡片合并 result + task_errors，>200 字符截断 + 「展开全文/收起」；locales 增补键。
7. 测试 `tests/test_failure_attribution_494.py`：helper 单测 + fake ACP 子进程静默超时端到端（errors 含「执行超时」）+ 回归（非超时失败归因仍在）。

## coding 测试执行策略说明
「按模块增量 pytest」的执行模板属于平台侧注入的 coding skill（.skills/，非本仓 tracked 内容），本仓不可变更；本次以单测文件粒度（`pytest tests/test_failure_attribution_494.py`）增量执行验证，避免全量 600s 硬超时，并在 PR 说明中向平台侧提出模板改进建议。

## 任务拆解
- [x] T1 归因 helper 模块 + 单测
- [x] T2 hwcloud/hermes ACP timeout 归因
- [x] T3 executor returncode 归因
- [x] T4 event_pipeline 失败收口 errors 双写
- [x] T5 事件详情 API task_errors
- [x] T6 前端失败态 errors 摘要（截断+展开）
- [x] T7 回归验证（相关既有测试）
