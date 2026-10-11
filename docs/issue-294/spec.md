# Spec — Issue #294：IM 渠道配置支持解绑

> 来源：Issue #294（@Jason7602）| 已发布 Spec：Issue 评论 193194630（评级 ready）| 开发基线：`weekly-06-lyz`
> 本文档按 coding 走查表 Step 3 落库于仓库，作为实现与验收依据；与 Issue 已发布 Spec 保持一致。

## 目标

为已绑定的 IM 渠道增加显式「解绑」操作，解除绑定后回到「未接入」状态，并清理注册表 / 渠道会话 /
BotManager 实例等残留数据，保证「绑定 → 解绑 → 再绑定」循环无残留。

## 背景（现状）

- IM 渠道配置页（`src/api/static/js/pages/im_channel.js`）只有「扫码配置」「保存」两个操作，**无解绑入口**。
- Chat 侧 `PUT /api/chat/config` 存在 `app_id` 置空分支，但只清 `chat_agent_session` 的凭证，
  **不清理** `global_im_bot_config` 注册行与 `chat_conversations` 会话数据。
- VOD 侧 `PUT /api/agents/{name}/{owner}/{repo}` 的 IM 处理仅在 `body.im_app_id` 非空时执行，
  且 `upsert_agent_session` 的 `im_*` 参数传 `None` = 保留既有值，**VOD 凭证无法解绑**。

## 范围

### In Scope

1. `im_channel.js`：已绑定渠道渲染「解绑」按钮 + `confirm()` 二次确认 + 成功/失败 toast + 重新拉取配置。
2. `chat.py` `update_chat_config`：`app_id` 置空分支补齐清理（注册表 / 渠道会话 / 映射对账）。
3. `agents.py` `update_agent_config`：VOD 侧显式解绑（`im_unbind` 标记）+ 完整清理。
4. `agents.js`：VOD 已绑定渠道的解绑入口。
5. i18n：`locales/{zh,en}/chat.json`、`locales/{zh,en}/agents.json` 解绑文案键。

### Out of Scope

- wecom / dingtalk 平台解绑（当前均未开放绑定，`disabled: true`）。
- 扫码 onboarding 流程改造（绑定路径不变，解绑仅复用其清理语义）。
- 新增独立 DELETE 端点（复用现有 PUT 语义）。
- 批量 / 管理员级解绑。

## 验收标准

- [x] IM 渠道配置页已绑定状态下出现「解绑」按钮，点击有 confirm 确认，确认后渠道状态回到「未接入」
- [x] 解绑后 `chat_agent_session.im_app_id/im_app_secret` 为空，`global_im_bot_config` 该 `chat_bot` 行
      （无其他用户引用时）与 `chat_conversations` 相关行被删除，`GET /api/chat/config` 不再返回该凭证
- [x] 解绑后 BotManager 中该 app_id 的 bot 实例被移除（`remove_bot` 被调用且日志可见）
- [x] VOD Agent 已绑定凭证可通过配置页解绑，`devops_agent_session` 三字段清空、`vod_conversations` 清理、
      `global_im_bot_config` 行删除（无其他引用时）
- [x] 绑定 → 解绑 → 再绑定循环无残留（同一凭证；注册表行删除后凭证可被另一 Agent 类型绑定）
- [x] 普通保存（不带 IM 字段 / 不带解绑标记）不误清既有 IM 绑定（VOD 侧防误触发）
- [x] `unified_user_mapping` 对账：解绑后 open_id/app_id 清空，重绑后需重新 p2p 确认
- [x] zh/en 双语文案齐全（解绑按钮 / 确认弹窗 / 成功 / 失败提示）
- [x] 所有现有测试通过（既有失败用例与本改动无关）；新增测试覆盖 Chat 解绑清理、VOD 解绑清理、重绑循环
