# Issue #2925 — `/api/pipeline` 标题兜底有界收敛（需求文档）

> 生成：Coding Agent | 2026-11-17 | 基线分支：weekly-mys

## 背景

`GET /api/pipeline/{owner}/{repo}` 单次请求稳定 22~37 秒（CPU 仅约 0.5s，其余在等外网 TLS I/O）。
根因有两处：

1. `_get_issue_pipeline`（`src/domains/runtime/routes/pipeline.py`）在占位标题时逐条串行调用
   `_fetch_issue_title_via_api` → `_vcs_get_issue`，每条都重新解析 token、新建客户端，97 条 = 97 次外呼。
2. 写回条件 `UPDATE issue ... (title='' OR title IS NULL)` 与占位判定 `_is_placeholder()`
   （空 / 事件类型名 / `A2A:` 等前缀）不同口径：标题本身是事件类型名时写回后仍判占位，
   下一轮继续外呼，**永不收敛**。

## 目标

把标题兜底改为「有界、可收敛」路径：

- 热请求（同仓第二次起）P95 < 1s 且 0 次 VCS 调用；
- 冷请求 97 条回填 ≤ 5s（有界并发 + 总 deadline，超时不再阻塞返回）；
- 同一仓反复请求不再重复外呼（已尝试标记形成负缓存）。

## 范围

- In：`routes/pipeline.py` 的兜底触发/窗口/写回；`pipeline/event_pipeline.py` 的
  token/客户端复用、有限并发、deadline；`issue.title_fetched_at` 列（走现有迁移通道）；
  新增测试。
- Out：接口响应结构；`active`/`history`/`stats`/`scene_id` 口径；`_pipeline_window`
  （#1077-P）；VCS 批量接口改造；其它 N+1 外呼。

## 验收标准

见 Issue #2925 Spec；本实现对应关系：

| 验收项 | 实现 |
|--------|------|
| 热请求 0 外呼 | `title_fetched_at` 负缓存（列不可用时进程内 TTL 缓存） |
| 冷请求 ≤ 5s | 并发 8（可配）+ 总 deadline 4s + 单请求超时 3.5s |
| 写回同口径 | 写回判定 = `_is_placeholder_title`；只写真实标题 |
| 仅窗口内兜底 | `_backfill_window_titles(window_size=_need)`，窗口外不触发 |
| 口径回归 | 未改响应字段与筛选口径；不新增逐条 token 查询 |
| 测试 | `tests/runtime/test_pipeline_title_backfill_2925.py` |