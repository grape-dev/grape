# Task — Issue #3735 实施拆解

| # | 任务 | 文件 | 验收 |
|---|------|------|------|
| 1 | 装配层收尾关闭原语：`_close_api_quietly`（失败仅 WARNING 带 app_id）、`_schedule_api_close`（运行中 loop 上异步关，不阻塞装配） | `src/domains/runtime/im/engine_wiring.py` | 单测：close 抛异常 → 只 WARNING；无 loop 不炸 |
| 2 | `ImEngine._drop_cached_ctx(app_id)`：pop 缓存并关闭旧 api；替换工厂/覆盖路径的裸 `_ctx_cache.pop` | 同上 | 单测：覆盖重建后旧 api `closed=True` 且缓存已清 |
| 3 | `ImEngine._close_cached_apis()` + `stop()` 接入：逐个 await 关闭 `_ctx_cache` 与进程级 `_feishu_api_cache`，并清空 | 同上 | 单测：真实 aiohttp session `closed is True`；重复 stop 幂等 |
| 4 | `_prune_built_apps()`：app 不在活渠道（让渡/重建/抢租约失败）时同时关闭其缓存 api | 同上 | 单测：prune 后旧 api 被关、缓存移除 |
| 5 | `_default_endpoint_fetcher` 的 `finally` 关闭失败仅 WARNING（带 app_id），不覆盖返回值/原异常 | `src/domains/runtime/im/adapters/feishu/channel.py` | 单测：close 失败仍返回原 `(url, code)` |
| 6 | 回归守卫：`FeishuChannel` 实例不持有 `FeishuAsync`（session 只属于短生命周期客户端/装配层） | `tests/runtime/im/test_issue_3735_feishu_session_lifecycle.py` | 单测断言 |
| 7 | 回归：现有 IM 测试 + lint + 架构检查 | — | `pytest tests/runtime/im -q` / `make lint` / `check-arch.py` |

## 顺序（TDD）

1. 先落测试（RED）：`test_issue_3735_feishu_session_lifecycle.py` 全量断言新契约。
2. 实现 1→5（GREEN），逐步跑测试。
3. 收尾回归：`tests/runtime/im` 全量 + `make lint` + `python3 scripts/check-arch.py`。

## 不做什么

- 不碰 #3698/#3897 的异常归一与退避，不改其他渠道适配器。
- 不做方案 B（进程级共享 session）改造。
- 不改 Supervisor/Channel 公开契约。