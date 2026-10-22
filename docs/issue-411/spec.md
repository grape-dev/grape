# Spec: Issue #411 — 场景市场列表非正分页参数返回 500 并回显数据库错误原文

> 完整需求 Spec 由 Issue Agent 发布于 Issue #411 评论（2026-10-21），本文件为编码实现引用版。

## Objective
修复 `GET /api/scenes/market` 分页参数校验缺陷：`page`/`page_size` 未加 `ge=1` 约束，负数/零值进入 SQL
`LIMIT/OFFSET` 触发 500（`OFFSET must not be negative` 等），且响应体曾回显数据库错误原文（含 SQL/表/字段名），
属「5xx + 信息泄露」双重问题（P1 / security）。

## Root Cause（已核实）
- `src/api/routes/scenes.py:163-167` `list_scenes_market`：`page: int = 1, page_size: int = 20`，无下限约束，
  FastAPI 只拦截非整数（422），负数/零通过签名校验。
- `src/user/db.py:3655` `list_scene_market`：`offset = (page - 1) * page_size` 拼入
  `LIMIT {page_size} OFFSET {offset}`。`page=0` → `OFFSET -20`；`page_size=-1` → `LIMIT -1`，SQLite 报错。
- `list_scene_market` 内部无 try/except，DB 异常直接抛出 → `src/api/main.py:1079`
  `@app.exception_handler(Exception)` 捕获 → 已脱敏返回
  `{"code": 500, "message": "服务器内部错误，请稍后重试", "request_id": ...}`（main.py:1086，符合预期）。
- `src/api/errors.py` `api_error()` 不接收 `str(e)` 作为对外 message。✓（其 internal 参数仅供日志，不用于本链路）
- 另核实 scenes.py 中 3 处 `HTTPException(500, detail=str(e))`（1058/1573/2056 行）分属
  POST 创建 / 自定义场景创建 / 高级选项保存，均不在 GET market 列表调用链上，超出本单范围。

## Scope
### In
- `list_scenes_market` 的 `page`/`page_size` 改为 `Query(default=1, ge=1)` / `Query(default=20, ge=1)`。
- 保留 `limit` 原语义（`limit>0` 跳过分页，精选场景用）不动。
- 新增 `tests/test_issue_411_scenes_market_page_validation.py`（含 AT-VAL-06 原子用例：
  `page=0`、`page=-1`、`page_size=0`、`page_size=-1`、非整数 → 422；合法分页 → 200 回归；
  `limit>0&featured_only=1` 精选逻辑不回归；全局异常脱敏单测）。

### Out
- `/api/knowledge` 同类缺陷（已单独提单）。
- #412 中 scenes/skills、runtime/runtimes/{id}/tasks 两处同类端点（统一公共分页校验层，另单处理）。
- scenes.py POST/PUT 端点既有 `detail=str(e)`（不在列表调用链）。

## Success Criteria
1. `GET /api/scenes/market?page=1&page_size=-1` → 422，响应体无 SQL/表结构信息；
2. `GET /api/scenes/market?page=0&page_size=20` → 422；
3. `GET /api/scenes/market?page=1&page_size=0` → 422；
4. 合法分页（`page=1&page_size=20`）行为不变（200 回归）；
5. `limit>0` 跳过分页的精选场景逻辑不受影响；
6. 全局异常处理对任意未捕获 DB 异常返回脱敏 message；
7. 原子用例 AT-VAL-06 由 xfail（QA 回归记录）转为 XPASS（本实现落地为正常断言并测试通过）；
8. 已有测试通过 + 新增测试覆盖新功能。