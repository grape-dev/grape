# Task: Issue #442 — 任务拆解

| # | 任务 | 验证 |
|---|------|------|
| T1 | 数据层：`chat_messages.deleted` DDL+迁移；session_manager 撤回 CRUD（get_message/recall_message/recall_agent_replies_by_task/is_task_recalled）；get_messages 带 deleted | 单测：字段存在、默认 0、撤回后置 1 |
| T2 | ChatTaskManager：ChatTask.cancelled/cancel()；push_event 丢弃；_run break+gen.close()；publish 丢弃；cancel(task_id) | 单测：cancel 后状态 cancelled、后续事件不入缓冲 |
| T3 | chat.py：recall 端点（权限/幂等/终止联动）；submit 响应加 message_id；agent_fn/remote_agent_fn 撤回感知跳过落库 | 单测：权限 404/400、终止联动、落库跳过 |
| T4 | 前端：撤回入口+确认+提示+重新编辑追加；i18n zh/en；CSS hover 按钮 | 语法检查 node --check |
| T5 | help/api/04-chat.md 端点块 | test_api_docs_sync 通过 |
| T6 | 测试文件 tests/test_chat_message_recall_442.py + 回归相关 chat 测试 | pytest 全绿 |
