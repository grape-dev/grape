# Issue #4248 Plan — /faro/collect 客户端中断记账修正

## 代码改动

| 文件 | 改动 |
|------|------|
| `src/api/spa.py` | `request.body()` 移入本地 `try`；捕获 `ClientDisconnect` / `asyncio.CancelledError` 返回 `499`；代理异常日志补类名 |
| `src/api/server_common.py` | 全局兜底日志补 `type(exc).__name__`；`ClientDisconnect` 降级 info + `499`；访问日志显式 `499` → warning |
| `src/api/middleware.py` | 访问日志显式 `499` → warning，与 5xx ERROR 分离 |
| `tests/insight/test_issue_4248_faro_client_disconnect.py` | 新增：中断 → 499/无 ERROR；上游不可达 → 502 + warning；访问日志分级 |
| `tests/api/test_issue_4248_api_error_classname.py` | 新增：空异常串仍打印异常类名 |

## 数据流

- 客户端断开 → `request.body()` 抛 `ClientDisconnect` → handler 本地捕获 → `Response(499)` →
  访问日志 warning（`status=499`）→ 指标标签 `status=499`（非 5xx）。
- 上游失败 → httpx 异常 → handler warning（含类名）→ `Response(502)` → 访问日志 ERROR（保留真故障）。
- 其它端点未捕获异常 → 全局兜底 warning（含类名）→ `500`（语义不变）；`ClientDisconnect` 例外降级 `499`。

## 风险

- 499 为 nginx 约定的非标准状态码：Starlette/uvicorn 允许返回；对已断开的客户端响应为 no-op，
  nginx 侧本就记 499，口径一致。
- 捕获 `CancelledError` 会吞掉取消信号：仅在 handler 内、读取请求体阶段、客户端已断开时发生；
  返回前无后续 await，不影响其它协程。