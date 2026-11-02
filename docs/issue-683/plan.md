# Plan: Issue #683

## 方案
1. `src/api/errors.py`：新增 `extract_allowed_values(pattern)` 与 `validation_error_response(exc)`。
   - 从 `exc.errors()` 逐条提取 `loc`（query 参数名）与 `msg`；
   - `ctx.pattern` 解析合法值（`^(a|b|)$` → `[a, b]`，解析失败降级 "invalid value"）；
   - 返回 `JSONResponse(422, {"code": 422, "message": ...})`，message 格式：
     `query param 'status' invalid: allowed values are [active, processing, ...]`。
2. `src/api/main.py`：在全局 Exception handler 旁注册
   `@app.exception_handler(RequestValidationError)`，所有路由的 Query pattern 校验失败统一受益。
3. 测试 `tests/test_issue_683_422_detail.py`：
   - 非法 `status=all`/`status=open` → 422，`code==422`，message 含 `status` 与全部合法值；
   - 合法 `status=processing` 不回归；`sort_order` 非法值同样含合法值枚举。

## 风险与缓解
- 全局 handler 影响其他端点 422 格式 → 统一 `{code,message}` 信封与 errors.py 约定一致；跑全量 API 测试回归。
- pattern 解析失败 → 降级 "invalid value"，不阻塞。
