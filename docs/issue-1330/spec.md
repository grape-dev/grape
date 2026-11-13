# Issue #1330 需求规格：Slack 渠道接入 / 发送 / 卡片三段适配

> 目标合入分支：`weekly-09-IM`
> 设计依据：`docs/weekly-09/im-slack-adapter-design.md`
> 实现来源：复用 PR #1000（`feat/issue-1309` @ `9fa99aa2`）的完整实现，按当前基线复核后落地。

## 1. 目标

为 IM 渠道补齐 Slack 适配：Socket Mode 接入、Web API 发送、Block Kit 卡片与真回调，
并在 `engine_wiring` 完成注册与 renderer/validator 映射，前端提供双令牌凭据入口。
引擎侧（Supervisor / 租约 / 去重 / 指标 / handler）零改动。

## 2. 范围

### In

1. **接入**：Socket Mode 长连接；3 秒内先 ack；`event_id` 去重；显式忽略
   `bot_message` / `message_changed` / `message_deleted`；envelope `payload` 字符串容错。
2. **发送**：Markdown → mrkdwn；38000 字符分块且不切多字节字符；`thread_ts` 线程回复；
   `chat.update` 原地更新；`reactions.add` 表情回执；三步文件上传；429 `Retry-After` 退避。
3. **卡片**：Block Kit 渲染；`block_actions` 真回调复用同一份 `_handle_selection`；
   25 元素上限降级为文本提示并记录降级原因。
4. **解析与装配**：`resolvers.py` 频道/用户映射；`engine_wiring` 注册 `Type.SLACK`、
   `_card_renderer_for("slack")`、`_msg_id_validator_for("slack")`。
5. **前端**：`im_channel.js` 新增 Slack 平台卡与 `slack.svg`；Bot Token（`xoxb-`）+
   App Token（`xapp-`）双令牌表单、状态回显、启停与解绑写穿。
6. **单测**：`tests/im/test_slack_{channel,inbound,outbound,card}.py`。

### Out

- 首批仅 user 轨（Chat Agent 页），repo 轨后置。
- 不做斜杠命令（设计批 5）。
- 不做 OAuth 安装，凭据只手填 `xoxb-` + `xapp-`。

## 3. 能力位

`TEXT | RICH_CARD | CARD_ACTION | THREAD_REPLY | MESSAGE_EDIT | ATTACHMENT | REACTION`

## 4. 验收标准

- 频道 @ 机器人有回复；私聊可用。
- 回复落入线程（`thread_ts`）。
- 卡片按钮可换仓，且通过 `chat.update` 原地更新。
- 发图能读取，产物能回传。
- 429 按 `Retry-After` 退避，不退化为中断。
- 前端配置 / 启停 / 解绑 / 状态四项真实可用。
- `tests/im` 全绿且飞书 / 钉钉 / 企微三渠道零回归。

## 5. 前端修正点

与原设计文档的差异以代码为准：当前 `im_channel.js` 平台表**没有** Slack 卡（非仅禁用），
因此本实现新增 Slack 平台卡与图标；`chat.py` 的 `_IM_PROVIDERS` 追加 `"slack"`。