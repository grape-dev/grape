# Issue #870 方案设计（plan）

## 字段对齐决策

legacy `BotManager.health_check`（feishu_async.py:1902-1913）实际输出键为
`total_messages_received / total_messages_sent / total_messages_failed /
total_reconnects / flap_by_cause`。Spec 命名第 4 字段为 `reconnects`——
为同时满足"与 legacy 完全对齐"与 Spec 命名，新引擎快照同时输出
`reconnects` 与 `total_reconnects`（同值别名），flap_by_cause 默认
`{"lease": 0, "ws_transport": 0, "shutdown": 0}`（与 legacy 初始化一致）。

## 计数点（全部落既有钩子，不改消息链路语义）

| 指标 | 钩子位置 | 说明 |
|------|---------|------|
| received | `_feishu_factory` 组装的 `cfg.inbound_handler`（Router.handle 外包一层） | 适配器 seen 去重后的分发点，等价 legacy run_bot L1384 位置 |
| sent / failed | `FeishuSender` 外包 `CountingSender`（发送器结果钩子） | SendResult.ok → sent，否则 failed |
| reconnects | Supervisor `_run` 重连循环 `except ChannelError` 分支 | 链路断开 → 退避重连 |
| flap_by_cause | Supervisor `_run` 异常分类 | ws_transport / lease / shutdown 三类，语义对齐 legacy disconnect_reasons |

## 改动文件

- `src/im/engine/metrics.py`（新增）：`EngineMetrics` 计数器 + `CountingSender` 包装器 + `snapshot()`
- `src/im/engine/supervisor.py`：`metrics` 可选注入，`_run` 循环分类记录
- `src/im/engine_wiring.py`：`ImEngine._metrics`，Supervisor 注入，`health()` 补字段，`_feishu_factory` 包 handler/sender
- `tests/im/test_health_metrics.py`（新增）：字段集合两态一致、计数递增、重连/抖动、缺失给 0

## 风险

- 计数均为 O(1) 内存累加，不落库，不影响消息链路；
- 两态字段集合一致性测试兜底（用 `object.__new__(BotManager)` 空 bot 状态取 legacy 键集合对照）。
