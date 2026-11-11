# Issue #1330 任务拆解

- [x] 从 `origin/feat/issue-1309`（`9fa99aa2`）取 Slack 适配实现与设计文档
- [x] 接入层：`channel.py`（Socket Mode / ack / 心跳 / 重连）+ `inbound.py`
- [x] 发送层：`outbound.py`（postMessage / 分块 / 线程 / update / reactions / files / 429）
- [x] 文本层：`markdown.py`（mrkdwn + 38000 分块）
- [x] 卡片层：`cards.py` + `renderers/slack.py`（Block Kit + 上限降级）
- [x] 解析层：`resolvers.py`
- [x] 装配：`engine_wiring.py` 注册 Slack / renderer / validator / `extra_config`
- [x] 前端：`im_channel.js` 平台卡 + 双令牌表单 + `slack.svg`
- [x] 后端：`chat.py` provider 白名单 + 脱敏回显 + 双令牌写穿
- [x] 单测：`test_slack_{channel,inbound,outbound,card}.py`
- [x] 回归：`tests/im` 全绿（472 passed）
- [x] 提交并创建 PR 到 `weekly-09-IM`