# Spec: Issue #463 — agent_tasks 表体积治理（编码阶段）

> 来源：Issue Agent 已发布 Spec（评论 #1），本文件为其编码落地版。目标分支 weekly-XX。

## Objective
对 `agent_tasks` 表做四层治理，遏制体积无上限增长（单行 100KB–1MB+ MEDIUMTEXT 内联、行永不删除）。

## In Scope（与 Issue Spec 一致）
- **P0**：`src/runtime/table_maintenance.py` 清理字段 3→10 个（补 `project_info_files`/`project_info_meta`/`knowledge_files`/`env_extra`/`errors`/`stderr`/`usage_json`），保留期 30→7 天。
- **P1**：新增 `delete_old_agent_tasks()`——completed/failed/cancelled 超 90 天分批整行 DELETE（复用 `_BATCH_SIZE=500`、6h 周期+分布式锁），连带删除该批任务的 `task_messages`。
- **P2**：`src/runtime/service.py` `enqueue_task` 写入侧：`*_file_urls` 已生成的文件不再写内联 MEDIUMTEXT 内容（置空串，worker 端 `_materialize_files` 三级降级：URL → content → storage_key，置空后走 URL/storage_key，无功能损失）。
- **P3**：`src/observability/alerts.py` 新增平台指标 `agent_tasks_rows` / `agent_tasks_mb` + 告警阈值规则。

## Out of Scope
- 历史存量一次性回溯（由 P1 分批自然消化）；task_messages/events 策略调整；skill_files 全量 OBS 化。

## Acceptance
- 清理字段 ≥10、completed 7 天置空；90 天终态分批删行；URL 优先单写；指标+告警上线；新增测试且现有测试全通过。
- 运行验证（起服务观察维护任务）由 sre Agent 执行，编码阶段不做。
