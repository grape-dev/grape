# IM 引擎新老逻辑差异对照 + 待办（2026-11-03）

对象：legacy BotManager（feishu_async.py 1913 + handler_async.py 2022 +
vod_handler_async.py 613 + bot_lease.py 374 ≈ 4900 行） vs 新引擎
（src/im/channel 契约 + src/im/engine 9 模块 + feishu 适配器 + engine_wiring 装配层 ≈ 2800 行）。

结论先说：**业务逻辑不变**——Router 处理完 ⑦ 持久化的消息，仍然交给同一批 legacy
handler（process_chat_async / process_vod_async / process_command_async）；
变动集中在"装、连、去重、路由、落库"这一层。

## 13 项差异

| # | 维度 | legacy | 新引擎 |
|---|---|---|---|
| 1 | 安装来源 | 现场查 chat_agent_session（用户轨按 app_id 去重）+ devops_agent_session（仓库轨）+ global_im_bot_config 兜底，每次启动/增删都重算 | 只枚举 channel_installation 的 active 行；启动时从 legacy 双轨回填一次，之后靠写穿桥同步 |
| 2 | 多主控分片 | 一致哈希环 50 vnodes，实例取自 grape_im_instance 心跳存活列表，真分片 | Supervisor.instances 目前只有 [自己]，环预筛失效；靠租约互斥避免双连，不做负载分摊（**能力回退**） |
| 3 | 租约 | BotLeaseStore 以 bot_app_id 抢 global_im_bot_config 的 lease 三态 | 同一把锁，键是 installation_id，经 LegacyCompatLeaseStore 翻译回 app_id（灰度互斥） |
| 4 | 去重 | bot_seen_messages 已见集合（7 天窗口），只有见过/没见过 | 两阶段 claim→mark + channel_inbound_dedup + channel_inbound_message 同事务；崩溃重投不丢不重 |
| 5 | 身份 | handler 内查 unified_user_mapping | channel_sender_identity 优先，回退 unified_user_mapping；未绑定发绑定引导卡 |
| 6 | 会话绑定 | repo_im_group_binding + vod_conversations + handler 内存缓存，三处分散 | 统一 channel_chat_session_binding（BindingKey = chat_id 或 chat_id#thread_root） |
| 7 | 群 @ 裁决顺序 | 先身份后 @ | @ 裁决（④）先于身份（⑤）→ 群内未 @ 的消息不再误发引导卡 |
| 8 | 媒体理解 | handler 协程内同步调 VLM | MediaWorkQueue 异步 worker，流水线只入队 MediaRef（#760）；首轮回复可能先于描述就绪 |
| 9 | 下行 | 直接拼富卡 | FeishuSender + Replier 能力降级（无富卡→纯文本） |
| 10 | 连接生命周期 | run_bot 内（ping 策略/空闲重连/ack/永久错误一起处理） | 迁入 FeishuChannel.connect()；永久错误码 10002/10003/10014 → PermanentChannelError → 该安装置 revoked 不再重试 |
| 11 | 运行开关 | 无 | GRAPE_IM_ENGINE + data/im_engine_mode 文件，5s 轮询切换 |
| 12 | 健康观测 | total_bots/connected/disconnected/messages_*/reconnects/flap_by_cause | 只有 engine/instance_id/running/channels（**指标变少**） |
| 13 | 数据权威 | legacy 两表即真源 | channel_installation 是派生表：UI 写 legacy → 写穿桥同步；绕过接口直改 legacy 不被感知 |

## 待办

- **A｜多主控分片（唯一影响正确性的）**：Supervisor 从 grape_im_instance 拉存活实例填充
  instances（约 20 行），否则多开实例只抢租约、不分摊。
- **B｜健康指标补齐**：新引擎补 total_messages_received/sent/failed、reconnect/flap 计数，
  避免切链路后看板/告警读不到字段。
- **C｜配置权威收尾**：保存/解绑改为直接写 channel_installation，再反向兼容 legacy
  （现在反着来：legacy 写、新表跟）。

## 已知已修（本次会话）

- MySQL 方言适配（0d4c47361）
- per-app WS 去重黑名单（dc2a4fc5f）
- UI 保存/解绑写穿（6a13d13ea）+ 扫码 onboard 写穿（59b4f283d）
- 卡片按钮回复用 context.open_message_id（d47f84501，修 99992354）
- IM 渠道页新引擎面板 + /api/im/*（377f92fd5）