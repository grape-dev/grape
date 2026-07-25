# PR 配置检查

## 执行前提

仅首次 `pull_request_opened` 事件时执行。

## 获取 PR 详情

```bash
curl -s -H "Authorization: Bearer $GITCODE_API_TOKEN" \
  https://api.gitcode.com/api/v5/repos/{owner}/{repo}/pulls/{number}
```

从返回中提取 `title`、`body`、`head.sha`、`labels`、`assignee`、`user.login` 等字段。

## 检查项

| 检查项 | 通过条件 | 不通过记录 |
|--------|---------|-----------|
| **Title** | 非空，清晰描述变更 | PR 标题为空或过于简单 |
| **Description** | 非空，有背景说明 | PR 描述为空 |
| **Labels** | 已有分类标签 | PR 缺少分类标签 |
| **Assignee** | 已设置责任人 | PR 未设置 Assignee |
| **Issue 关联** | body 包含 `Closes #N` / `Fixes #N` / `Related #N` | 未检测到关联 Issue |

不通过项暂不评论，统一到最后的评论回复能力项中处理。

## Issue 关联处理

如果有关联 Issue：
- 在对应 Issue 中评论 `📌 关联 PR #{pr_number}`
- 对应 Issue 是 bug → 在 Issue 上更新标签 `labels="bug,triaged"`
