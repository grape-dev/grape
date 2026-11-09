# Spec: Issue #431 — Chat 配置保存静默清空飞书 IM 凭证修复

## 背景
`PUT /api/chat/config`（`src/api/routes/chat.py` `update_chat_config`）中，body 未携带 `im` 部分时走 else 分支，
将 `im_app_id` / `im_app_secret` / `im_provider` 显式写为空字符串；叠加
`set_chat_config → _write_chat_config_row`（`src/session_manager.py`）的 INSERT OR REPLACE 整行覆盖，
导致一次普通保存（换引擎/换技能）即清空 `chat_agent_session` 行的三个 IM 字段。

## 目标行为
1. body 无 `im` 键（或 `im` 无 `feishu` 键且非显式解绑意图）时，不向 `set_chat_config` 传任何 `im_` 字段，
   现有 IM 配置保持不变（含 BotManager 不摘除 bot）。
2. 仅当显式携带 `im` 且 `app_id` 为空时视为解绑：清三字段 + 沿用 `_purge_chat_im_binding` 清理链路
   + BotManager 摘除 bot（Issue #294 既有行为，保持不变）。
3. 其他写库入口排查：`agents.py`（VOD 走 `devops_agent_session`，已有显式 `im_unbind` 标记，无静默清空）、
   `settings.py`（无 chat 配置写库路径）、`feishu_onboard.py`（仅显式绑定写值）——均无需改动。

## 范围
- In：`src/api/routes/chat.py` 调用侧修复；回归测试；历史数据恢复脚本。
- Out：`_write_chat_config_row` 语义改造、前端表单、机器人收发逻辑（`global_im_bot_config` 全局注册不受影响）。

## 验收标准
- 不带 `im` 字段保存后，`chat_agent_session` 行的 `im_app_id` / `im_app_secret` / `im_provider` 保持原值不变。
- 显式携带 `im` 且 `app_id` 为空时，解绑清理逻辑（`_purge_chat_im_binding`）正常执行。
- 其他写库入口无同类静默清空路径（排查结论落地）。
- 历史受损数据可从 `bak_chat_cfg_migrate_20261018_*` 备份表恢复（提供恢复脚本 + user_id=1 恢复操作）。
