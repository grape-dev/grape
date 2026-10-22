# Spec — Issue #412 同类分页缺陷（scenes/skills、runtime/runtimes/{id}/tasks）

> 来源：Issue Agent 已发布 Spec（见 Issue #412 评论），本文件为 coding 侧落地基线摘要。

## 目标
消除两个列表端点因分页参数未校验直接进入 SQL LIMIT/OFFSET 而返回 500 并回显数据库错误原文的缺陷（与 #411 同源）：
- `GET /api/scenes/skills/{agent_type}`：`page=0` / `page_size=-1`
- `GET /api/runtime/runtimes/{id}/tasks`：`limit=-1`

## 范围（In）
- `src/api/routes/scenes.py` `list_skills_for_agent`：`page`/`page_size` 加 `Query(ge=1)`（`page_size` 加 `le=200` 上界）
- `src/api/routes/runtime.py` `list_runtime_tasks`：`limit` 加 `Query(50, ge=1, le=500)`
- 全局异常脱敏：`src/api/main.py:1079` 已由 #380/#395 落地（原文仅入日志，响应体通用文案 + request_id），本 Issue 核对覆盖度即可，不重复实现
- `tests/test_issue_412_pagination.py`：非法参数 → 422 且无 SQL 泄露 + 合法分页回归

## 范围（Out）
- #411 立单的 `/api/scenes/market`、`/api/knowledge` 缺陷（#380 已修 knowledge）
- 全局分页参数排查（后续专项）
- 列表业务逻辑变更

## 关键确认（Open Question 结论）
`runtime.py` `list_runtime_tasks` 的 `limit` **无** `limit=0` 表「全部」的语义：SQL 为 `... LIMIT ?`，`limit=0` 在 SQLite 返回 0 行（无业务分支处理 0），默认 50。对比 `scenes/market` 的 `limit>0 跳过分页` 是显式文档化语义，本端点不存在该约定 → 直接 `ge=1` 安全。

## 验收
- 非法分页参数返回 422，响应体无 SQL/表结构信息
- 合法分页行为不变
- QA 原子用例 AT-VAL-09（QA 侧清单）修复后应转 XPASS
- 所有现有测试通过
