# Plan — Issue #412 实现方案

复用 #411/#380 统一方案：FastAPI 参数层 `Query(ge=1)` 拦截 → 自动 422；全局异常 handler（`src/api/main.py:1079`，#380/#395 已落地）兜底脱敏数据库错误原文。

## 改动点

| # | 文件 | 改动 |
|---|------|------|
| 1 | `src/api/routes/scenes.py` | 导入 `Query`；`list_skills_for_agent` 的 `page: int = Query(1, ge=1)`、`page_size: int = Query(20, ge=1, le=200)` |
| 2 | `src/api/routes/runtime.py` | 导入 `Query`；`list_runtime_tasks` 的 `limit: int = Query(50, ge=1, le=500)` |
| 3 | `tests/test_issue_412_pagination.py` | 新增：非法参数 422（page=0/page_size=-1/page_size=0/limit=-1/limit=0）+ 响应体无 SQL 泄露断言 + 合法分页回归 + 全局异常脱敏单测 |

## 设计决策
- `ge=1` 而非夹取：与 #380/#395 knowledge、agents.py/events.py 等既有 `Query(ge=1)` 约定一致，FastAPI 原生 422，零业务代码。
- `le` 上界：`page_size le=200`（对齐 knowledge page_size 上界）、`limit le=500`（对齐 observability limit 上界），防过大分页拖库。
- 全局异常 handler 已存在（#380/#395），本 Issue 仅以单测锁定「DB 异常不回显原文」行为。

## 测试策略（TDD）
先写测试（RED：当前 500）→ 加 Query 约束（GREEN）→ 回归全量相关测试。
