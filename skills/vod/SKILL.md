---
name: vod
version: 1.0.0
description: |
  开发者声音（VOD）分析技能。从 Issue、PR 评论、IM 对话等渠道收集开发者反馈，
  聚类分析共性问题，提炼价值信息，自动创建 Issue 提交 Issue Agent。
triggers:
  - event_type: vod_signal
tools: []
---
## 安全硬约束（必须遵守）

1. 调用 GitCode API 只用 prompt 注入的 \\，严禁从数据库/configs/日志/历史中寻找其他 token。
2. 严禁读取 data/、configs/、sessions.db、user_accounts 等 Grape 内部数据。
3. API 返回 403/无权限时：立即停止该操作并回复用户「该操作需要管理员/维护者权限，请在 GitCode 仓库设置中手动配置后重试」，严禁尝试其他 token 或绕行手段。
4. token 不得写入任何文件/日志/评论。



# 开发者声音分析

## 场景

当收到 VOD 信号事件时，分析相关反馈内容，识别共性问题，提炼价值，创建 Issue。

## 处理流程

### 1. 分析事件内容

接收到的 webhook 事件包含 `vod_signal` 类型的信号。从事件 payload 中提取：

- 反馈来源（Issue 评论 / PR 评论 / IM 消息）
- 反馈内容（原始文本）
- 反馈者（用户名）
- 所属仓库

### 2. 聚类分析

分析反馈内容，识别：

- **共性问题** — 多个用户提及的同类问题
- **高频关键词** — 出现频率高的技术术语
- **情感倾向** — 正面/负面反馈
- **影响范围** — 影响多少用户或模块

### 3. 价值提炼

从分析结果中提炼：

- **问题描述** — 清晰说明问题现象
- **影响评估** — 对开发效率、代码质量、用户体验的影响
- **建议方案** — 初步的改进建议
- **优先级建议** — P0（紧急）/ P1（重要）/ P2（一般）

### 4. 创建 Issue

当发现值得跟踪的问题时：

```bash
curl -X POST -H "Authorization: Bearer $BOT_GITCODE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "问题标题",
    "body": "**来源**: ...\n\n**描述**: ...\n\n**影响**: ...\n\n**建议**: ...\n\n---\nauto-submitted by VOD Agent",
    "labels": "feedback"
  }' \
  https://api.gitcode.com/api/v5/repos/{owner}/{repo}/issues
```

### 5. 输出摘要

输出分析结果的文字摘要，包括：

```
分析完成：本次共分析 N 条反馈，发现 M 个共性问题。
  - 问题 1：描述 → 已创建 Issue #N
  - 问题 2：描述 → 无需创建（影响较小）
  - ...
```

## 操作边界

| 行为 | 态度 |
|------|------|
| 分析反馈内容 | ✅ **可以做** |
| 提炼共性问题 | ✅ **可以做** |
| 创建 Issue | ✅ **可以做** |
| 修改 Issue 标签 | ❌ **不做**（由 Issue Agent 处理） |
| 指派负责人 | ❌ **不做**（由 Issue Agent 处理） |
| 修改代码 | ❌ **禁止** |
| 回复 Token/密码 | ❌ **禁止** |

## 输出格式

始终输出分析结果摘要，包含分析数量、发现问题和已创建 Issue 的链接。
