# 分配负责人指南

## 执行前提

当分析结论为「负责人缺失，且 Issue 在仓库主题范围内」时执行。

## 流程

### 1. 读取 Issue

读取 Issue 标题和正文，理解该 Issue 所属的核心领域。

### 2. 获取 CODEOWNERS 文件

优先从 `codeowner` 分支获取：

```bash
curl -s -H "Authorization: Bearer $BOT_GITCODE_TOKEN" \
  "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/contents/CODEOWNERS?ref=codeowner"
```

如果返回 404，尝试默认分支：

```bash
curl -s -H "Authorization: Bearer $BOT_GITCODE_TOKEN" \
  "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/contents/CODEOWNERS"
```

返回 JSON，`content` 字段是 Base64 编码，需解码后读取：

```python
import base64, json
data = json.loads(response_text)
text = base64.b64decode(data['content']).decode('utf-8')
```

### 3. 未找到 CODEOWNERS → 引导用户创建

如果以上两个分支都没有 CODEOWNERS 文件：
- 读取 `references/codeowners-guide.md` 中的引导 Issue 模板
- 用该模板创建一个新的引导 Issue
- 在当前 Issue 回复：本仓库尚未配置模块负责人，已创建引导 Issue [#N]
- 跳过此能力项（等待用户回复引导 Issue）

### 4. 逐模块匹配

按以下字段匹配：
- **触发词** → 快速预筛选：Issue 内容命中触发词的模块标记为候选
- **描述** → 语义确认：对比 Issue 核心领域与模块描述

### 5. 选择最匹配的模块

从候选模块中选择描述最贴近的那个。如命中多个模块，选择描述匹配度最高的。

### 6. 未命中任何模块

判断 Issue 是否与仓库项目范围完全无关：
- **完全无关** → 不分配，直接关闭：
  ① `PATCH /issues/{number}` `{"labels": "invalid"}`
  ② `PATCH /issues/{number}` `{"state": "close"}`
  ③ 评论说明该 Issue 与项目无关，已关闭 → 结束处理
- **可能相关但无法确认** → 走兜底负责人，让人工判断

### 7. 能确定负责人

立即调用 API 分配（必须先执行，再评论）：

```bash
curl -s -X PATCH -H "Authorization: Bearer $BOT_GITCODE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"assignee": "younger_eric"}' \
  https://api.gitcode.com/api/v5/repos/{owner}/{repo}/issues/{number}
```

### 7.1 通知责任人 / 确保任务存在（必做）

**在分配动作完成后（处理结束、责任人最终确定时），调用 `POST /api/tasks/notify`** 创建任务（接口幂等，换人时自动关闭原责任人任务）：

```bash
curl -X POST http://localhost:8080/api/tasks/notify \
  -H "Content-Type: application/json" \
  -d '{
    "assignee": "younger_eric",
    "repo_key": "'"$REPO_FULL_NAME"'",
    "issue_number": "'"$ISSUE_NUMBER"'",
    "task_description": "Issue #'"$ISSUE_NUMBER"' 已分配给你，请处理：<简要任务描述>"
  }'
```

- assignee 必须是仓库有效成员（GitCode 用户名）
- 未注册平台 → 返回 `{ok:false}`，在 Issue 评论说明
- 已注册未绑飞书 → 任务仍创建，仅跳过飞书通知

### 8. 检查跨仓同步

该负责人的模块定义中是否有 `# 仓库：` 字段？
- **有** → 在目标仓库创建 Issue，标题和内容与原 Issue 一致，body 末尾注明 `源自 [owner/repo#N](链接)`
- **无** → 跳过同步

### 9. 不能确定

评论中 @ 项目创建者（Issue 作者），说明未找到对应模块负责人。

## CODEOWNERS 格式说明

| 字段 | 格式 | 说明 |
|------|------|------|
| 责任人 | `# 责任人：@用户名` | 模块负责人 |
| 模块 | `# 模块：模块名称` | 模块名称和作用域 |
| 触发词 | `# 触发词：keyword1, keyword2` | 快速预筛选关键词 |
| 描述 | `# 描述：功能说明` | 语义匹配的描述信息 |
| 仓库 | `# 仓库：owner/repo` | 跨仓同步目标（可选） |
| 兜底 | `* @用户名` | 未匹配任何模块时使用 |
