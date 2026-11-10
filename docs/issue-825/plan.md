# Plan: Issue #825 — 引擎装配层设计

> **A2 收尾（2026-11-10，Phase B）— 历史设计说明**：下文"legacy BotManager 可秒级切回"
> 的链路已不存在。M1 删传输层、M2 删双轨桥与 `PUT /api/im/engine-mode`、M3 租约迁
> `channel_lease`；Phase A/B 后 `chat_agent_session` / `devops_agent_session` 的 `im_*`
> 三列与 `global_im_bot_config` 整表已 DROP（一次性迁移
> `scripts/migrations/002_drop_legacy_im.py`）。现仅保留总开关 `data/im_engine_mode`（on/off）。

## 组件（src/im/engine_wiring.py，新增）
- `engine_mode()`：开关读取。优先 `GRAPE_IM_ENGINE_MODE_FILE`（默认 `<repo>/data/im_engine_mode`）
  文件值 on/off（运行期可改 → 秒级切回），回落 env `GRAPE_IM_ENGINE`（默认 on）。
- `run_startup_migration(db_path)`：幂等执行 001 ensure_channel_tables + backfill_installations
  （worker 线程内新连接），另建 wiring 自有 `channel_inbound_message` 表（append_message 持久化用）。
- `SqliteDedupStore` / `SqliteBindingStore`：channel_inbound_dedup / channel_chat_session_binding
  的同步 sqlite 实现（每次操作新连接，满足线程亲和）。
- `LegacyCompatLeaseStore`：lease_key("feishu:<installation_id>") → 查 channel_installation
  config.app_id → BotLeaseStore 以 legacy bot_app_id 键抢锁（新旧互斥防双回复）。
- `ImEngine`：装配 + 生命周期。start()：迁移 → Registry 注册 Feishu 工厂 →
  Supervisor(installation_provider=SqliteInstallationProvider) 起 scan → reconcile 定时任务。
- Feishu 工厂（per installation）：建 BotContext/FeishuAsync + per-installation Router
  （六注入点闭包捕获 installation 上下文）+ FeishuChannel（cfg.inbound_handler = router.handle）。
  repo 轨 app 共享时按 app_id 去重只建一条连接（单 bot 灰度语义）。

## Router 六注入点实现
| 注入点 | 实现 |
|--------|------|
| installations | 工厂闭包返回本安装的 Installation 视图（来源 channel_installation 枚举） |
| identity | channel_sender_identity 查 (installation, sender) → 回退 unified_user_mapping.feishu_open_id；未绑定走绑定卡 |
| bindings | SqliteBindingStore get/upsert（落 channel_chat_session_binding） |
| append_message | 单事务：INSERT channel_inbound_message + dedup state→marked |
| parse_command | "/xxx" → command；纯数字 → selection（pending 判定在 apply） |
| apply_command | 命令→ legacy process_command_async（含卡片选择 _handle_selection；无 pending 回落 agent） |
| agent_run | InboundMessage → parsed dict → ctx.bot_type 双轨 dispatch process_chat_async / process_vod_async |
| add_reaction | api.add_reaction(ctx, message_id, "OK") |

## main.py 接线
- lifespan：mode=on → `engine_wiring.start_im_engine()`；off → 既有 BotManager 路径（原代码搬入 `_start_legacy_im/_stop_legacy_im`）。
- `_im_mode_watcher`：每 5s 重读 mode 文件，翻转时 stop 旧链路 start 新链路（秒级切回）。
- shutdown：watcher/engine/legacy 全收口；/health IM 段兼容两态。

## 测试（tests/im/test_engine_wiring.py）
- 开关两态 + 回滚路径（mode 翻转 stop/start）；
- 六注入点集成（临时 sqlite：安装回填→入站→绑定落库/消息持久化+dedup 同事务/命令/agent/reaction）；
- LegacyCompatLeaseStore 键翻译互斥；迁移幂等重跑。
