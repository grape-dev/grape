# Spec: Issue #825 — IM 新引擎接线（main.py 开关 + Router 六注入点 + 迁移对账）

> **A2 收尾（2026-11-10，Phase B）— 历史设计说明**：下文"legacy BotManager 可秒级切回"
> 的链路已不存在。M1 删传输层、M2 删双轨桥与 `PUT /api/im/engine-mode`、M3 租约迁
> `channel_lease`；Phase A/B 后 `chat_agent_session` / `devops_agent_session` 的 `im_*`
> 三列与 `global_im_bot_config` 整表已 DROP（一次性迁移
> `scripts/migrations/002_drop_legacy_im.py`）。现仅保留总开关 `data/im_engine_mode`（on/off）。

> 本文档为 Issue #825 已发布 Spec（见 Issue 评论）的落地副本，开发以 Issue Spec 为权威。

## 目标
把 IM 渠道生产运行时从 legacy BotManager 切换到新引擎链路
（Supervisor + SqliteInstallationProvider + FeishuChannel + Router 九步流水线），
legacy BotManager 降级为可秒级切回的回滚开关。

## 交付
1. main.py 引擎开关（GRAPE_IM_ENGINE，默认 on；data/im_engine_mode 文件覆盖实现秒级切回）；
2. Router 六注入点真实实现（installations/identity/bindings/append_message/parse_command+apply_command/agent_run/add_reaction），集中在独立装配层 src/im/engine_wiring.py；
3. 启动幂等执行 scripts/migrations/001_channel_tables.py + reconcile_installations 定时对账告警。

## 依赖铁律
- im_engine 只 import im_channel（适配器注册进 Registry）；
- 核心永不读写 InboundMessage.raw（test_raw_sentinel.py 守卫）；
- 装配层（engine_wiring，非引擎核心）允许读 raw 与 import 适配器/legacy handler；
- 新旧 lease 互斥：LegacyCompatLeaseStore 把 lease_key(installation) 翻译回 legacy bot_app_id 键。
