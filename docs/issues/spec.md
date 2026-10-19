# Spec: Issue #284 — chat agent 会话标题优化

## 目标
chat agent 中 Issue 任务会话标题从「Issue #N」升级为「Issue #N · 需求标题」，与需求中心看板卡片标题风格一致。

## 改动范围
- `src/api/issues_center.py` `_ensure_task_conversation`：标题生成 / 复用匹配 / 存量升级
- `tests/test_issue_284_conversation_title.py`：新增四类单测

## 设计决策
| 决策 | 选择 | 理由 |
|------|------|------|
| 标题格式 | `Issue #N · {title}` | 与看板卡片一致；保留「Issue #N」前缀使前端链接化正则继续命中 |
| 复用匹配键 | `conv_type='task' + issue_number` 列 | 标题含可变需求文本，字符串匹配会失效；旧格式作回退 |
| 存量升级 | 复用时顺带刷新 | 影响面小，无需迁移脚本 |
| 标题来源 | events 表 `_get_issue_title` | 零 API 调用 |
