# Issue #4248 Spec — /faro/collect 客户端中断不再被误记为 500

## 目标

`/faro/collect` 反代在客户端主动中断（页面关闭/切换，nginx 记 `499`）时不再被记成服务端
`500 ERROR`；日志/指标可区分「客户端中止」与「真故障」。真实上游故障语义不变（仍 `502`）。

## 背景

- `src/api/spa.py` 的 `/faro/collect` 中 `body = await request.body()` 位于本地 `try` 之外，
  `except Exception` 只兜住 httpx 段。
- 浏览器卸载页面时 `request.body()` 抛 `ClientDisconnect`（`str()` 为空串），直落
  `src/api/server_common.py` 全局兜底 `log.warning(f"API error ({path}): {exc}")` → 返回 `500`，
  访问日志再按 5xx 记 ERROR。
- 生产窗口实测：mgmt `POST /faro/collect status=500` ×26、空异常 `API error (/faro/collect):` ×13、
  nginx `499` ×13；`db_lock` 长持持为放大条件（#3927），非成因。

## 方案

1. `src/api/spa.py`：把 `await request.body()` 移入本地 `try`；显式捕获
   `ClientDisconnect` / `asyncio.CancelledError` → 返回 `499`，仅 debug 记录，不打 ERROR。
2. `src/api/spa.py`：上游代理异常日志补 `type(exc).__name__`，空异常串不再盲区。
3. `src/api/exception_handlers.py`（**唯一实现**）：全局兜底日志补 `type(exc).__name__`；
   `ClientDisconnect` 降级为 info 并返回 `499`（不返 500）。`server_common.build_app` 删除内联
   handler，改为调用 `install_exception_handlers`；入口在其后再调一次是幂等 no-op，避免二次注册覆盖。
4. 访问日志层（`server_common._attach_request_log` / `middleware.install_request_middleware`）：
   `499` 本就不进 5xx：既有 `elif status >= 400` 即记 warning，无需新增分支（删除冗余 `== 499` 分支）；
   指标按状态码标签自然分离。

## Out of scope

- 全局 `db_lock`（#3927，仅放大条件）
- #3743（405）、#4242（503）、#3756（/openapi 500）
- Faro SDK 前端大幅改造

## 验收

- 模拟在途断开：`/faro/collect` 返回 `499`，无 `status=500` ERROR、无空异常 `API error` 警告。
- 客户端中止（499）与服务端 5xx 可区分，`499` 不计入 5xx 错误率。
- 空异常时 `API error` 日志含异常类名。
- `CancelledError`（进程关停/超时取消）同样收敛为 `499`，不落 500。
- 全局兜底 handler 只有一份实现；入口二次装配不覆盖（装配链测试覆盖）。
- 上游不可达仍返回 `502` 并记 warning。
- 请求数与访问日志条数 1:1 可核对。
- 现有测试通过；新增测试覆盖客户端中断与真实故障两条路径。
