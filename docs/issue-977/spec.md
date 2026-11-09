# Spec: Issue #977 — task-logs/{id}/info 增加 wrote_files 产物清单

> 目标分支: weekly-mys | 前序：Issue Agent Spec 评论（2026-11-04 16:50，评级 ready）

## Objective
- `GET /api/task-logs/{task_id}/info` 响应增补 `wrote_files` 数组，每条 `{path, bytes, tool, seq, first_seen_at}`，为主控提供「任务写过哪些文件」的独立事实源（消息链路丢失场景下的产物核对依据，来源 XOcto 复核报告 BUG-4）。
- 无数据任务返回 `[]`，其余字段与现状一致，向后兼容老前端。

## Scope
- In：`/info` 路由新增 `wrote_files` 派生查询与响应字段（只读，不改写入链路）；数据源 `task_messages` 的 write/patch tool_use `input_json`；契约（`task_logs_info` 响应 schema）与 `help/api/09-task-logs.md` 文档同步；新增 2 条单测。
- Out：不新增表/迁移、不扫描工作区、不改 `/messages?since=` 语义、前端展示改造、#975/#976 消息保真修复。

## Acceptance Criteria
1. 单测：种 write(57KB) 与 patch 两条 tool_use → `wrote_files` 含 2 条，bytes 与原文一致、seq 对得上。
2. 单测：任务无 write → `wrote_files == []`，其余字段与现状逐字段一致。
3. 契约：`task_logs_info` 响应 schema 增加 `wrote_files`（`/openapi.json` 由代码渲染），文档同步。
4. 所有现有测试通过；新增测试覆盖新功能。

## 关键设计决策
| 决策 | 选择 | 理由 |
|------|------|------|
| 派生 vs 落库新表 | 运行时派生查询 | 零迁移、零写入链路改动 |
| bytes 口径 | content/new_string 的 utf-8 字节数 | 数据源仅 task_messages，不扫盘 |
| 过滤位置 | 宽松 SQL + Python 精筛（工具名变体兜底 + input 含 path/content 判定） | 工具名存在 write/write_file/patch 变体 |
| 契约呈现 | 路由 `responses` schema（不改变运行时序列化） | 仓库无 response_model 先例，`responses` 只进契约零行为风险 |
