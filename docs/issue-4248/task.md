# Issue #4248 Task

- [x] `src/api/spa.py`：`request.body()` 移入 `try`，捕获 `ClientDisconnect`/`CancelledError` → `499`
- [x] `src/api/spa.py`：上游代理异常日志补 `type(exc).__name__`
- [x] `src/api/server_common.py`：全局兜底日志补类名；`ClientDisconnect` 降级 `499`
- [x] 访问日志层：`499` 显式 warning，与 5xx ERROR 分离（server_common + middleware）
- [x] 新增中断路径测试（499、无 500 ERROR、无空异常告警）
- [x] 新增真实故障路径测试（上游不可达 → 502 + warning）
- [x] 新增异常类名日志测试
- [x] 现有测试回归通过