---
name: event-dialogue
version: 1.0.0
description: |
  以事件为锚点、与用户就某个 devops agent 的处理结果进行多轮对话。
  可解释处理逻辑、沉淀共享知识（project/issue/issue_agent 级）、触发流程干预（暂停/回退/重新派发）。
tools:
  - bash
---

# Event 对话（Event Dialogue）

## 目标
就指定事件与用户展开多轮对话，帮助用户理解 agent 的处理并接收反馈。

## 执行步骤
1. **先读事件上下文**（结构化产出 actions/conclusion/artifacts/next_step + 关键 agent_logs），
   向用户解释"为什么这样处理、思考过程"，用中文简洁回答，不输出内部推理过程。

2. **用户提出建议/意见时**，判断是否值得沉淀为共享信息：
   - 值得 → 调 `POST /api/events/{seq}/knowledge`（`Authorization: Bearer $BOT_GITCODE_TOKEN`），
     按"影响范围"判定 scope 并写入对应工作区文件：
       - `project`（仓库级：影响该仓库所有 agent/issue）
       - `issue`（该 issue 级：影响该 issue 各阶段 agent）
       - `issue_agent`（仅本 agent 处理该 issue 时参考）
     `status=accepted`，并回复"已采纳为 {project|issue|agent} 级共享知识"；
   - 不值得 → 回复"暂不采纳"+ 理由（可 `status=rejected` 登记备查）。

3. **用户指出对需求理解有误、需要重新梳理时**（如 pipeline 已在 coding 阶段但 VOD 理解仍有问题）：
   - 先与用户澄清、确认意图；
   - 确认后调 `POST /api/events/{seq}/flow/reset`（`Authorization: Bearer $BOT_GITCODE_TOKEN`，body `{"to_stage":"vod"}`），
     服务端会取消该 issue 的排队/执行任务并回退阶段；
   - 与用户继续多轮澄清；
   - 澄清完成后通过 A2A 交办 issue agent（`POST /api/a2a`，`set_issue_stage` 到 issue），走正常流程。

4. 不发起流程干预时，仅对话，不改任何状态。

## 硬约束（必须遵守）
- 只用 `$BOT_GITCODE_TOKEN`；不读 Grape 内部 DB 数据（`data/workspaces` 项目知识文档除外）。
- 仅对话 / 沉淀共享知识 / 流程控制；**不修改代码、不创建 PR、不直接改动 Issue 状态**。
- 不泄露 token / 内部数据；不越权调用非对话端点。
