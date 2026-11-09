# Spec: Issue #442 — ChatAgent 消息撤回（二次确认删除 + 终止执行 + 重新编辑追加）

> 目标分支：`weekly-XX`（场景基线权威值，覆盖先前 Spec 标注的 weekly-08）

## Objective
- 构建什么：ChatAgent 对话「消息撤回」能力——用户对自己已发送消息发起撤回，二次确认后软删除该消息并终止其触发的该轮 agent 执行；原位置保留撤回提示，提示上提供「重新编辑」入口，将原文**追加**到输入框现有内容之后。
- 为什么：发错消息可即时止损（节省执行资源、避免错误对话记录），并可快速改写重发。
- 用户：ChatAgent 对话页面的登录用户。

## Scope
- In Scope：前端撤回入口/二次确认/撤回提示/重新编辑追加（`src/api/static/js/pages/chat.js` + i18n）；后端撤回端点（`src/api/routes/chat.py`）；任务终止（`src/api/chat_task_manager.py` 取消机制 + 复用 #136 `service.cancel_task` 终止远端排队任务）；消息软删除字段（`chat_messages.deleted`）。
- Out of Scope：撤回他人消息、撤回 agent 回复、批量撤回、撤回时间窗口。

## Acceptance Criteria
- [ ] 已发送消息可发起撤回，弹确认「撤回会删除该消息，并停止 agent 的执行」，支持确认/取消；取消零变更
- [ ] 确认后原消息软删除，原位置显示撤回提示
- [ ] 确认后该轮 agent 执行被终止（direct：ChatTaskManager.cancel；remote：service.cancel_task），不再输出状态更新
- [ ] 撤回提示提供「重新编辑」，原文追加到输入框现有内容之后，不覆盖
- [ ] 仅本人 user 消息可撤回，他人消息/agent 消息被端点拒绝
- [ ] 存量数据兼容（deleted 默认 0，无需迁移脚本）；新增测试覆盖端点权限、状态流转与终止联动
