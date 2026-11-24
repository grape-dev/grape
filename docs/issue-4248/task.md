# Issue #4248 Task

- [x] `src/api/spa.py`：`request.body()` 移入 `try`，捕获 `ClientDisconnect`/`CancelledError` → `499`
- [x] `src/api/spa.py`：上游代理异常日志补 `type(exc).__name__`
- [x] `src/api/exception_handlers.py`：全局兜底**唯一实现**补类名；`ClientDisconnect` 降级 `499`（宣传真生效路径）
- [x] `src/api/server_common.py`：删除内联 `@app.exception_handler(Exception)`，`build_app` 改为调用 `install_exception_handlers`
- [x] `install_exception_handlers` 幂等：入口在 `build_app` 之后重复装配为 no-op，不再覆盖已注册 handler
- [x] 访问日志层：删除冗余 `elif status == 499`（499 本已走 `elif status >= 400` → warning），保留注释说明
- [x] 新增中断路径测试（499、无 500 ERROR、无空异常告警）
- [x] 新增 `CancelledError` → `499` 单测
- [x] 新增真实故障路径测试（上游不可达 → 502 + warning）
- [x] 新增异常类名日志测试 + 装配链测试（handler 单一来源 / 二次装配幂等）
- [x] `.github/workflows/lint.yml` 纳入两条新用例，PR 描述的用例计数可在 CI 复算
- [x] 现有测试回归通过
