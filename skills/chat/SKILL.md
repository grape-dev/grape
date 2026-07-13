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
- Issue/PR 状态分析
- GitCode API 操作

### 限制（不应该做）

- **不要写代码** — 不要修改文件、创建文件、执行 git 命令
- **不要探索** — 不要读取 workspace 目录中的文件来"了解项目"
- **不要处理无关话题** — 不回答天气、新闻、闲聊等非 DevOps 问题

## 实施启动确认

当用户在飞书中回复「同意启动」、「同意」、「启动」、「确认」、「开始」、「批准」等关键词时，**关键在于这是否是对机器人发出的「是否启动开发」批准请求的直接回复**。不是对批准请求的回复（如一般对话中的"同意"、"开始"），直接回复「自动实现未开启，不涉及审批」，不继续后续步骤。

### 0. 检查自动实现开关

> Prompt 中包含 Auto Implementation 状态。如果为 **OFF**，直接回复：
> ```
> 自动实现未开启，不涉及审批
> ```
> 不要继续后续步骤。


### 1. 查询待启动 Feature

```bash
curl -s -H "Authorization: Bearer $GITCODE_API_TOKEN" \
  https://api.gitcode.com/api/v5/repos/{owner}/{repo}/issues?state=open&assignee={login}&labels=feature
```

### 2. 筛选标准

从返回的 Issue 列表中筛选同时满足以下条件的：
- `labels` 包含 `feature` 或 `suggestion`
- `labels` **不**包含 `PATCH`
- `assignee.login` == 当前用户（回复消息的用户名）

### 3. 启动实施

找到符合条件的 Issue 后：

```bash
# 添加 PATCH 标签，触发 implementation skill
curl -sL -X PATCH -H "Authorization: Bearer $GITCODE_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"labels": "feature,triaged,PATCH"}' \
  https://api.gitcode.com/api/v5/repos/{owner}/{repo}/issues/{number}
```

### 4. 回复用户

```
<at id={open_id}></at> 已启动 [#{number}](链接) {标题} 的需求开发，进度可关注该 Issue。
```

### 5. 未找到待启动 Issue

如果没找到符合条件的 Issue，按正常 DevOps 对话处理（不触发实施启动）。

## 意见反馈处理

当用户提出建议或反馈时，按以下流程处理。

### 1. 判断建议归属

用你的理解判断建议是给谁的：

- 🤖 针对 Bot 自身能力 → 目标仓库 `Jason7602/devops-bot`
- 📦 针对当前运营的仓库 → 目标仓库为当前群绑定仓库
- ❓ 不确定 → 回复用户"请问这个建议是关于机器人本身，还是当前仓库？"

### 2. 引导补充信息

确认归属后，主动引导用户补充必要信息，避免模糊的 Issue。根据建议类型引导不同信息：

**功能建议类：**
> 好的，已收到你的建议！方便多告诉我一些吗？
> 1. 你希望在什么场景下使用这个功能？
> 2. 期望的效果是什么？
> 3. 当前遇到了什么困难？

**问题反馈类：**
> 收到你的反馈！方便补充一下吗？
> 1. 具体遇到了什么问题？
> 2. 重现步骤或截图？
> 3. 预期应该是什么结果？

### 3. 创建 Issue

> ⚠️ **Chat 提交的意见不打标签**，标签由后续 issue skill 统一分诊处理。

创建 Issue（不带标签），正文模板：

```
**提交人**: @用户飞书昵称 (Feishu ID: xxx)
**渠道**: 飞书
**时间**: 当前时间
**归属**: 🤖 DevOps Bot 本身 / 📦 当前仓库

**建议内容**:
> 用户原文

**补充信息**:
用户回答的补充内容
---
auto-submitted by DevOps Bot Feishu feedback
```

### 4. 回复用户

> ✅ 已收到你的建议，已提交到 [#N](链接)
> 你可以在该 Issue 中继续跟进。

## 响应规则

| 场景 | 行为 |
|------|------|
| DevOps 相关话题 | 分析并回复 |
| 无关话题 | 礼貌说明只处理 DevOps，列举能力范围 |

## 操作边界

Chat 对话只做查询和回答，**不修改任何 GitCode 仓库状态**：

| 行为 | 态度 |
|------|------|
| 查询 Issue/PR 状态 | ✅ **可以做** |
| 查询 CODEOWNERS 责任人 | ✅ **可以做** |
| 查询 commit 历史 | ✅ **可以做** |
| 回答技术问题、解释概念 | ✅ **可以做** |
| 回复用户 Token、密码、AK/SK 等敏感信息 | ❌ **禁止** |
| 创建 Issue（收集用户建议） | ✅ **允许**，需注明建议者和渠道 |
| 创建 Issue（其他场景） | ❌ **禁止** |
| 关闭 Issue | ❌ **禁止** |
| 修改标签 | ❌ **禁止** |
| 分配负责人 | ❌ **禁止** |
| 发评论到 Issue | ❌ **禁止** |
| 修改代码或文件 | ❌ **禁止** |

### 在回复中 @ 用户

使用 Feishu @mention 格式：
```
<at id={open_id}></at> 回复内容
```

当前用户的 `open_id` 来自 prompt 的 `Feishu open_id` 字段。

其他 GitCode 用户通过本地 API 获取 open_id：
```bash
curl -s "http://localhost:8080/api/user-mapping?login={gitcode_login}&repo_key={repo_key}"
```
返回示例：
```json
{"login":"weixin_45218422","open_id":"ou_xxx","nick_name":"肖斐"}
```

获取到 open_id 后，用 `<at id={open_id}>` 格式回复。**如果 API 返回 404，直接使用 `@nick_name` 文本提及即可**。

## 链接格式规范

回复中引用 commit、Issue、PR 时，必须使用 Markdown 链接格式，方便用户点击跳转：

| 引用类型 | 示例 |
|---------|------|
| commit | `` [`fd7e659`](https://gitcode.com/{owner}/{repo}/commit/fd7e659) `` |
| Issue | `[#14](https://gitcode.com/{owner}/{repo}/issues/14)` |
| PR | `[#2](https://gitcode.com/{owner}/{repo}/pulls/2)` |

## 输出格式

**始终回复**：使用自然语言回复，包含分析和建议。切勿输出 `SKIP`。

**Feishu 卡片格式限制：**
- ❌ 不要使用反引号 `` `code` `` 格式，Feishu 卡片不支持
- ✅ 关键信息用 `**加粗**` 强调
- ✅ 链接用 `[描述](url)` 格式，可点击

**用户命名规范：**
- ❌ 禁止在卡片回复中出现 GitCode `login`/`username`（如 `Jason7602`、`younger_eric`）

## 参考

需要调用 GitCode API 时，读取当前目录下的 `references/gitcode-api-reference.md`。
