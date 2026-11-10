# Issue #870 需求文档（spec）

目标：新 IM 引擎 `/health/realtime` im 段补齐 5 个 legacy 对齐健康指标：
`total_messages_received / total_messages_sent / total_messages_failed / reconnects / flap_by_cause`。

- 完整 Spec 见 Issue #870 评论（issue agent 生成，评级 ready）。
- legacy 对照实现：`src/im/feishu_async.py:1907-1911`（BotManager.health_check）。
- 新引擎快照点：`src/im/engine_wiring.py:787`（ImEngine.health）。

验收：
1. engine=on/off 两态 im 段字段集合一致（新引擎额外 running/channels 保留）；
2. 一次收发后 received/sent 计数 +1（失败路径 failed +1）；
3. 一次重连后 reconnects +1，flap_by_cause 按原因累计；
4. 缺失场景字段值为 0 而非缺键；
5. `pytest tests/im` 全绿。
