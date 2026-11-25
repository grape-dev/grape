# Spec — Issue #3735：飞书渠道重建泄漏 aiohttp ClientSession

## 目标

让 `FeishuAsync` 的 aiohttp `ClientSession` 生命周期与**持有它的装配层/渠道生命周期**绑定：
渠道停止、凭证热重建（指纹变化）、安装让渡、引擎 stop 时显式且幂等地关闭 session，
消除 `Unclosed client session` 告警，阻断 fd / aiohttp 连接池随重建次数累积。

## 背景（基线核对结论，arch-weekly-11-ljt）

Issue 正文的三条根因中，**渠道内 `api = FeishuAsync()` 挂闭包那条已在 #3897 D 修复**：

- `adapters/feishu/channel.py:_default_endpoint_fetcher` 改为短生命周期客户端，
  `finally: await api.close()`（每个连接尝试都关，测试见
  `tests/runtime/im/test_issue_3897_feishu_endpoint_session.py`）；
- `FeishuChannel` 已不持有任何 `FeishuAsync` 实例，`connect()` 只用 `(ws_url, code)`。

**仍然存在同类未收尾路径（本单范围）**：

1. `engine_wiring.ImEngine._ctx_for()` 按 `app_id` 懒建并缓存 `FeishuAsync`（handler 出站/
   媒体下载要用），该实例首次调用即懒建 `aiohttp.ClientSession`。
2. 这些缓存在以下 drop 点只是 `pop`/`clear`，**从不 close**：
   - `ImEngine.stop()` → `self._ctx_cache.clear()`；
   - 渠道工厂覆盖 `_api_overrides`（钉钉/企微/Slack/Telegram/微信）→
     `engine._ctx_cache.pop(app_id, None)`（4 处）；
   - 渠道让渡/租约失败 → `_prune_built_apps()` 只清 `_built_apps`。
3. `engine_wiring.feishu_api()` 的进程级 `_feishu_api_cache` 从不释放（引擎重启后
   旧 session 还绑定在旧 loop 上）。

即：**渠道重建/让渡/引擎重启仍会漏掉一个已建立的 session**，与 Issue 观测
（1 次重建 ≈ 1 个未关闭 session）同因同类。

## 范围

### In Scope

- `engine_wiring.py`：为装配层缓存的平台 api 增加收尾关闭路径
  （`stop()` / 覆盖重建 / 让渡 prune），关闭失败只 WARNING（带 app_id），不抛、不阻塞装配。
- `adapters/feishu/channel.py`：`_default_endpoint_fetcher` 的 `finally` 关闭失败不得
  覆盖原返回值/原异常（仅 WARNING，带 app_id）。
- `tests/runtime/im/test_issue_3735_feishu_session_lifecycle.py`：覆盖 stop / 重建 /
  让渡 / 重复关闭幂等 / 关闭失败降级 / 渠道不持有 FeishuAsync 实例。

### Out of Scope

- 崩溃诱因（`BrokenPipeError → ConnectionClosedError 3003`）的异常归一与退避（#3698 / #3897）。
- 其他 IM 渠道适配器自身的 session/transport 生命周期（企微 transport、Slack client 等）。
- 方案 B（进程级共享 session 复用）：不影响本单验收，另行评估。

## 契约

1. **收尾关闭**：装配层丢弃 `(ctx, api)` 对时，若 `api` 提供 `close()`，必须调用（可 await
   则 await）；重复调用幂等（`FeishuAsync.close()` 自带 `_own_session` 判定）。
   同步装配点无运行 loop 时**不得丢弃**：挂进 `_pending_api_closes` 保留引用，由
   `ImEngine.stop()` 在其 loop 上收尾（`_drain_pending_api_closes`）——pair 已从缓存 pop，
   丢弃即等于永不关闭。
2. **失败降级**：关闭异常只记 WARNING（含 app_id），不向上抛，不影响渠道切换/引擎 stop。
3. **不阻塞装配**：同步装配点（渠道工厂）以 task 形式异步关闭，不 await。
4. **渠道不持 session**：`FeishuChannel` 实例不得持有 `FeishuAsync` 字段（回归守卫）。

## 验收标准

- [ ] `ImEngine.stop()` 关闭 `_ctx_cache` 与 `_feishu_api_cache` 中所有 api 的 session，
      并清空缓存；重复 stop 不报错。
- [ ] 渠道让渡/重建（`_prune_built_apps` 判定 app 不在活渠道）时，该 app 缓存的 api
      被关闭并移出缓存。
- [ ] 装配覆盖（`_drop_cached_ctx`）时旧 api 被关闭。
- [ ] api.close 抛异常 → 只产生 WARNING（含 app_id），stop/prune 正常完成。
- [ ] `_default_endpoint_fetcher` 在 close 失败时仍返回原 `(url, code)` / 抛原异常
      （两条路径均有用例：close 失败不覆盖返回值、get_ws_endpoint 抛错时 close 失败不覆盖原异常）。
- [ ] 新增回归套件接入 CI：`.github/workflows/lint.yml` 的 lint job 跑
      `test_issue_3735_feishu_session_lifecycle.py` + `test_issue_3897_feishu_endpoint_session.py`。
- [ ] 真实 `aiohttp.ClientSession` 断言：`stop()` 后 `api._session.closed is True`。
- [ ] 现有 `tests/runtime/im/` 全绿；`make lint` 通过。

## 风险

| 风险 | 影响 | 缓解 |
|------|------|------|
| 关闭正在被 in-flight handler 使用的 session | 低（该渠道已断连/让渡） | 关闭以 task 异步执行；`_get_session` 对已关闭 session 会重建 |
| 跨事件循环 close（UI 线程/无 loop） | 中 | 仅在运行中的 loop 上调度；无 loop 时挂进 `_pending_api_closes` 保留引用，由 stop() 收尾（不丢弃） |
| 关闭 task 未保留引用被 GC（关闭静默丢失） | 低 | `_api_close_tasks` 持引用 + `add_done_callback(discard)`，同时作为测试的确定性等待点 |
| stop 清空进程级 `_feishu_api_cache` 波及通知侧 | 低 | 使用方约定只经 `feishu_api()` 取用、不跨引擎生命周期持有实例；清空后懒建绑定当前 loop 的新客户端（回归用例覆盖） |
| 误关注入的共享 session | 高 | 依赖 `FeishuAsync._own_session` 语义，只关自有 session |