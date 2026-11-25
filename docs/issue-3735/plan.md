# Plan — Issue #3735 飞书渠道 ClientSession 生命周期收尾

## 方案（最小改动，方案 A 的装配层补全）

`FeishuChannel` 已无 session（#3897 D）。剩余漏点是**装配层按 app_id 缓存的 `FeishuAsync`**：
丢弃引用而不 close，session 只能等 GC，aiohttp 在回收时打 `Unclosed client session`。

### 改动点

1. `src/domains/runtime/im/engine_wiring.py`
   - 新增模块级 `async def _close_api_quietly(api, app_id)`：
     `close()` 存在则调用（协程则 await），异常 → `log.warning("[im-engine] im api close
     failed app=%s: %r")`，永不抛。
   - 新增模块级 `def _schedule_api_close(api, app_id)`：在**运行中的 loop** 上
     `create_task(_close_api_quietly(...))`；无 loop（同步 UI 线程）直接返回，交给
     `ImEngine.stop()` 收尾，避免跨 loop 误用。
   - `ImEngine._drop_cached_ctx(app_id)`：pop `_ctx_cache` 并对旧 api 调度关闭。
   - `ImEngine._close_cached_apis()`（async）：stop 时逐个 await 关闭 `_ctx_cache` 与
     进程级 `_feishu_api_cache` 的 api，并清空两个缓存。
   - `stop()`：用 `await self._close_cached_apis()` 取代裸 `self._ctx_cache.clear()`。
   - `_prune_built_apps()`：app 不在活渠道（让渡/重建/抢租约失败）时同时
     `_drop_cached_ctx(app_id)`。
   - 工厂内 4 处 `engine._ctx_cache.pop(app_id, None)` → `engine._drop_cached_ctx(app_id)`。

2. `src/domains/runtime/im/adapters/feishu/channel.py`
   - `_default_endpoint_fetcher` 的 `finally`：`await api.close()` 包 `try/except`，
     失败 `log.warning("[feishu:%s] endpoint client close failed: %r", app_id, e)`，
     不覆盖 `(url, code)` 返回值与原异常（契约 2）。

3. 测试 `tests/runtime/im/test_issue_3735_feishu_session_lifecycle.py`
   - 真实 `aiohttp.ClientSession`：stop → `api._session.closed is True`，两个缓存清空；
   - 重复 stop 幂等；
   - `_prune_built_apps` 让渡路径关闭并移出缓存；
   - `_drop_cached_ctx` 覆盖重建路径关闭旧 api；
   - close 抛异常 → WARNING（含 app_id）+ stop/prune 正常返回；
   - `_default_endpoint_fetcher` close 失败仍返回原结果；
   - 回归守卫：`FeishuChannel` 实例不持有 `FeishuAsync`。

## 时序 / 并发

- `stop()`：`supervisor.stop()`（渠道全断）→ 关闭缓存 api → 清 `_repliers` → 注销全局引擎句柄。
- 工厂/prune 都在 Supervisor 扫描的 loop 内，`create_task` 即随下一轮调度执行，不阻塞建连。
- 关闭幂等：`FeishuAsync.close()` 仅关 `_own_session` 自有 session，二次调用无副作用；
  `_get_session()` 对已关闭 session 会重新懒建（自愈），不会把死 session 永久留下。

## 影响面

- 仅 IM 装配层与飞书适配器；不改 Supervisor/Channel 契约、不改其他渠道。
- 现有 `tests/runtime/im/test_engine_wiring.py`（stop 幂等、回滚路径）必须保持绿。

## 验证命令

```bash
python3 -m pytest tests/runtime/im/test_issue_3735_feishu_session_lifecycle.py \
  tests/runtime/im/test_issue_3897_feishu_endpoint_session.py \
  tests/runtime/im/test_engine_wiring.py -q
make lint
python3 scripts/check-arch.py
```