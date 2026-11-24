# Issue #4248 Task

- [ ] `src/api/spa.py`：`request.body()` 移入 `try`，捕获 `ClientDisconnect`/`CancelledError` → `499`
- [ ] `src/api/spa.py`：上游代理异常日志补 `type(exc).__name__`
- [ ] `src/api/server_common.py`：全局兜底日志补类名；`ClientDisconnect` 降级 `499`
- [ ] 访问日志层：`499` 显式 warning，与 5xx ERROR 分离（server_common + middleware）
- [ ] 新增中断路径测试（499、无 500 ERROR、无空异常告警）
- [ ] 新增真实故障路径测试（上游不可达 → 502 + warning）
- [ ] 新增异常类名日志测试
- [ ] 现有测试回归通过