# async 上下文持 db_lock 清单（A6）

`scripts/check_async_blocking.py` 的 A6 规则：`async def` 里直接 `with db_lock:`。
这把锁是 SQLite 时代的进程级串行化手段；MySQL 后端上只要持锁做同步查询，
就把整个事件循环占住 —— 实测表现为心跳停发、引擎被判失联、端点 nginx 504，
而不是“变慢”。

当前共 **62 处**，已冻结进基线（`configs/async_blocking_baseline.json`），
新增会直接让 `make lint` 失败。下面是按文件聚合的收敛清单。

| 文件 | 处数 |
| --- | --- |
| `src/domains/runtime/routes/issues.py` | 11 |
| `src/domains/runtime/im/feishu_async.py` | 7 |
| `src/domains/runtime/im/handler_async.py` | 6 |
| `src/domains/runtime/im/vod_handler_async.py` | 6 |
| `src/domains/mgmt/knowledge.py` | 5 |
| `src/domains/mgmt/routes/agents.py` | 5 |
| `src/domains/mgmt/routes/scenes.py` | 5 |
| `src/domains/runtime/routes/webhook.py` | 3 |
| `src/domains/runtime/engine/scheduler.py` | 2 |
| `src/domains/runtime/routes/events.py` | 2 |
| `src/api/health_endpoints.py` | 1 |
| `src/api/server_common.py` | 1 |
| `src/domains/insight/routes/dashboard.py` | 1 |
| `src/domains/insight/routes/latency.py` | 1 |
| `src/domains/mgmt/routes/autopilot.py` | 1 |
| `src/domains/mgmt/routes/feishu_onboard.py` | 1 |
| `src/domains/mgmt/routes/skills.py` | 1 |
| `src/domains/runtime/engine/service.py` | 1 |
| `src/domains/runtime/routes/chat.py` | 1 |
| `src/domains/runtime/routes/tasks.py` | 1 |

## 明细

- `src/api/health_endpoints.py:78  [A6 db_lock in async] with db_lock`
- `src/api/server_common.py:438  [A6 db_lock in async] with db_lock`
- `src/domains/insight/routes/dashboard.py:936  [A6 db_lock in async] with db_lock`
- `src/domains/insight/routes/latency.py:77  [A6 db_lock in async] with db_lock`
- `src/domains/mgmt/knowledge.py:595  [A6 db_lock in async] with db_lock`
- `src/domains/mgmt/knowledge.py:897  [A6 db_lock in async] with db_lock`
- `src/domains/mgmt/knowledge.py:953  [A6 db_lock in async] with db_lock`
- `src/domains/mgmt/knowledge.py:975  [A6 db_lock in async] with db_lock`
- `src/domains/mgmt/knowledge.py:991  [A6 db_lock in async] with db_lock`
- `src/domains/mgmt/routes/agents.py:624  [A6 db_lock in async] with db_lock`
- `src/domains/mgmt/routes/agents.py:647  [A6 db_lock in async] with db_lock`
- `src/domains/mgmt/routes/agents.py:653  [A6 db_lock in async] with db_lock`
- `src/domains/mgmt/routes/agents.py:666  [A6 db_lock in async] with db_lock`
- `src/domains/mgmt/routes/agents.py:686  [A6 db_lock in async] with db_lock`
- `src/domains/mgmt/routes/autopilot.py:672  [A6 db_lock in async] with db_lock`
- `src/domains/mgmt/routes/feishu_onboard.py:336  [A6 db_lock in async] with db_lock`
- `src/domains/mgmt/routes/scenes.py:778  [A6 db_lock in async] with db_lock`
- `src/domains/mgmt/routes/scenes.py:895  [A6 db_lock in async] with db_lock`
- `src/domains/mgmt/routes/scenes.py:961  [A6 db_lock in async] with db_lock`
- `src/domains/mgmt/routes/scenes.py:981  [A6 db_lock in async] with db_lock`
- `src/domains/mgmt/routes/scenes.py:1010  [A6 db_lock in async] with db_lock`
- `src/domains/mgmt/routes/skills.py:785  [A6 db_lock in async] with db_lock`
- `src/domains/runtime/engine/scheduler.py:1173  [A6 db_lock in async] with db_lock`
- `src/domains/runtime/engine/scheduler.py:1284  [A6 db_lock in async] with db_lock`
- `src/domains/runtime/engine/service.py:4010  [A6 db_lock in async] with db_lock`
- `src/domains/runtime/im/feishu_async.py:1126  [A6 db_lock in async] with db_lock`
- `src/domains/runtime/im/feishu_async.py:1234  [A6 db_lock in async] with db_lock`
- `src/domains/runtime/im/feishu_async.py:1592  [A6 db_lock in async] with db_lock`
- `src/domains/runtime/im/feishu_async.py:1608  [A6 db_lock in async] with db_lock`
- `src/domains/runtime/im/feishu_async.py:1620  [A6 db_lock in async] with db_lock`
- `src/domains/runtime/im/feishu_async.py:1629  [A6 db_lock in async] with db_lock`
- `src/domains/runtime/im/feishu_async.py:1639  [A6 db_lock in async] with db_lock`
- `src/domains/runtime/im/handler_async.py:803  [A6 db_lock in async] with db_lock`
- `src/domains/runtime/im/handler_async.py:825  [A6 db_lock in async] with db_lock`
- `src/domains/runtime/im/handler_async.py:866  [A6 db_lock in async] with db_lock`
- `src/domains/runtime/im/handler_async.py:1053  [A6 db_lock in async] with db_lock`
- `src/domains/runtime/im/handler_async.py:1139  [A6 db_lock in async] with db_lock`
- `src/domains/runtime/im/handler_async.py:1171  [A6 db_lock in async] with db_lock`
- `src/domains/runtime/im/vod_handler_async.py:179  [A6 db_lock in async] with db_lock`
- `src/domains/runtime/im/vod_handler_async.py:438  [A6 db_lock in async] with db_lock`
- `src/domains/runtime/im/vod_handler_async.py:537  [A6 db_lock in async] with db_lock`
- `src/domains/runtime/im/vod_handler_async.py:574  [A6 db_lock in async] with db_lock`
- `src/domains/runtime/im/vod_handler_async.py:584  [A6 db_lock in async] with db_lock`
- `src/domains/runtime/im/vod_handler_async.py:608  [A6 db_lock in async] with db_lock`
- `src/domains/runtime/routes/chat.py:1710  [A6 db_lock in async] with db_lock`
- `src/domains/runtime/routes/events.py:1901  [A6 db_lock in async] with db_lock`
- `src/domains/runtime/routes/events.py:2502  [A6 db_lock in async] with db_lock`
- `src/domains/runtime/routes/issues.py:653  [A6 db_lock in async] with db_lock`
- `src/domains/runtime/routes/issues.py:967  [A6 db_lock in async] with db_lock`
- `src/domains/runtime/routes/issues.py:1055  [A6 db_lock in async] with db_lock`
- `src/domains/runtime/routes/issues.py:1082  [A6 db_lock in async] with db_lock`
- `src/domains/runtime/routes/issues.py:1120  [A6 db_lock in async] with db_lock`
- `src/domains/runtime/routes/issues.py:1157  [A6 db_lock in async] with db_lock`
- `src/domains/runtime/routes/issues.py:1246  [A6 db_lock in async] with db_lock`
- `src/domains/runtime/routes/issues.py:1453  [A6 db_lock in async] with db_lock`
- `src/domains/runtime/routes/issues.py:1825  [A6 db_lock in async] with db_lock`
- `src/domains/runtime/routes/issues.py:2474  [A6 db_lock in async] with db_lock`
- `src/domains/runtime/routes/issues.py:2493  [A6 db_lock in async] with db_lock`
- `src/domains/runtime/routes/tasks.py:275  [A6 db_lock in async] with db_lock`
- `src/domains/runtime/routes/webhook.py:1422  [A6 db_lock in async] with db_lock`
- `src/domains/runtime/routes/webhook.py:2014  [A6 db_lock in async] with db_lock`
- `src/domains/runtime/routes/webhook.py:2307  [A6 db_lock in async] with db_lock`
