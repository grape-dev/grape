# 实施启动确认（implementation-start）

当用户回复「同意启动」、「同意」、「启动」、「确认」、「开始」、「批准」等关键词时，判断是否是针对机器人发出的「是否启动开发」批准请求。**不是对批准请求的回复**（如一般对话中的"同意"、"开始"），直接回复「自动实现未开启，不涉及审批」，不继续后续步骤。

## 0. 检查自动实现开关

> Prompt 中包含 Auto Implementation 状态。如果为 **OFF**，直接回复：
> ```
> 自动实现未开启，不涉及审批
> ```
> 不要继续后续步骤。

## 1. 查询待启动 Feature

```bash
curl -s -H "Authorization: Bearer $USER_GITCODE_TOKEN" \
  https://api.gitcode.com/api/v5/repos/{owner}/{repo}/issues?state=open&assignee={login}&labels=feature
```

## 2. 筛选标准

从返回的 Issue 列表中筛选同时满足以下条件的：
- `labels` 包含 `feature` 或 `suggestion`
- `labels` **不**包含 `PATCH`
- `assignee.login` == 当前用户（回复消息的用户名）

## 3. 启动实施

找到符合条件的 Issue 后：

```bash
# 添加 PATCH 标签，触发 implementation skill
curl -sL -X PATCH -H "Authorization: Bearer $USER_GITCODE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"labels": "feature,triaged,PATCH"}' \
  https://api.gitcode.com/api/v5/repos/{owner}/{repo}/issues/{number}
```

## 4. 回复用户

```
<at id={open_id}></at> 已启动 [#{number}](链接) {标题} 的需求开发，进度可关注该 Issue。
```

## 5. 未找到待启动 Issue

如果没找到符合条件的 Issue，按正常 DevOps 对话处理（不触发实施启动）。
