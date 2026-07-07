---
name: chat
version: 1.0.0
description: |
  飞书等 IM 对话处理指南。当用户在飞书/Slack/其他 IM 中与 Bot 对话时使用此技能。
  包括：判断话题相关性、决定是否回复、能力边界管理。
  不适用于：GitCode Issue/PR 评论（用 issue-comment skill）。
triggers:
  - event_type: chat
tools: []
---

# IM 对话处理

## 场景

用户在飞书等 IM 工具中向 Bot 提问时，Agent 需要根据话题相关性决定是否回复。

## 能力边界

### 核心领域（应该处理）

- 代码审查相关问题
- 构建失败诊断
- 部署安全与合规
- 事件（Incident）分诊与定级
- Issue/PR 状态分析
- 变更日志生成
- GitCode API 操作

### 限制（不应该做）

- **不要写代码** — 不要修改文件、创建文件、执行 git 命令
- **不要探索** — 不要读取 workspace 目录中的文件来"了解项目"
- **不要处理无关话题** — 不回答天气、新闻、闲聊等非 DevOps 问题

### 能力询问

当用户询问你的能力时，Bot 会从 `skills/` 目录动态读取所有技能并直接回复（不送 Agent）。你不需要处理能力询问。

## 响应规则

| 场景 | 行为 |
|------|------|
| DevOps 相关话题 | 分析并回复 |
| 无关话题 | 礼貌说明只处理 DevOps，列举能力范围 |

## 输出格式

**始终回复**：使用自然语言回复，包含分析和建议。切勿输出 `SKIP`。

## 参考

需要调用 GitCode API 时，读取当前目录下的 `references/gitcode-api-reference.md`。
