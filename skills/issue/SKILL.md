---
name: issue
version: 1.0.0
description: |
  Issue 全生命周期管理：接收 → 分类 → 打标签 → 分配负责人 → 设置里程碑 → 关联 PR → 回复评论。
  当收到 issue_opened 或 issue_comment 事件时使用此技能。
  不适用于：PR 相关事件（用 pr skill）、非 Issue 的仓库评论。
triggers:
  - event_type: issue_opened
  - event_type: issue_comment
tools: []
---

# Issue 运营

## 场景

用户在 GitCode 上创建 Issue 或发表评论时，Agent 需要完成 Issue 的完整运营流程：分类、打标签、分配、关联、回复。

## 事件处理

| 事件类型 | 处理方式 |
|---------|---------|
| issue_opened | 分类 → 打标签 → 分配 → 里程碑 → 评论回复 |
| issue_comment | 分析评论 → 更新标签/状态 → 回复 |

## 能力

### 1. 分类与标签管理

**标签体系：**

| 标签 | 适用场景 | 判定线索（标题/正文关键词） |
|------|---------|---------------------------|
| `feature` | 新功能请求 | 建议、需求、新增、支持、实现、希望、能不能加 |
| `bug` | 缺陷报告 | bug、故障、错误、不生效、失败、白屏、异常、报错、500 |
| `need-info` | 信息不足 | 描述过短（<30字）、缺少复现步骤、缺少日志/截图 |
| `triaged` | 已分诊完成 | 已确认分类并分配处理人（收到此事件时与主标签一起加） |

**执行步骤：**

1. 读取 Issue 标题和正文
2. 按关键词判定分类
3. 信息不足以判断 → `labels="need-info"`，评论追问具体信息
4. 可判断 → `labels="bug,triaged"` 或 `labels="feature,triaged"`
5. 同时符合多个 → 逗号分隔多个标签

### 2. 分配负责人

1. 检查 Issue 正文中的 @mention（优先作为分配目标）
2. 检查仓库 CODEOWNERS 文件（如有）
3. 判断所属领域：前端/后端/DevOps/文档
4. 通过 `GET /user` 或 `GET /repos/:owner/:repo/collaborators` 获取用户 ID
5. `PATCH /issues/:number` body: `{"assignee_ids": [id]}`
6. 评论中 @ 通知该用户

> 不确定时不强制分配，只在评论中建议负责人。

### 3. 里程碑设置

1. 检查 Issue 是否提及版本号（如 v1.2.0, v2.0）
2. `GET /repos/:owner/:repo/milestones?state=all` 获取可用里程碑
3. 匹配到对应里程碑 → `PATCH /issues/:number` body: `{"milestone_id": id}`
4. 评论说明已关联里程碑

> 仅 Issue 明确提及版本号时设置，里程碑不存在时不创建。

### 4. PR 关联

当收到 PR 事件时由 pr skill 触发：

1. PR body 引用 `Closes #N` / `Fixes #N` → 在 Issue #N 评论 `📌 关联 PR #{pr_number}`
2. 原 Issue 是 bug → 更新标签为 `bug,triaged`

### 5. 评论回复

| 场景 | 行为 |
|------|------|
| 用户 @提及机器人 | 分析评论并回复 |
| 评论补充了信息（原为 need-info） | 重新分类，更新标签 |
| 评论请求关闭 Issue | 验证后关闭或说明原因 |
| 其他 DevOps 相关评论 | 分析并回复 |

## API 调用参考

```
更新 Issue（标签/负责人/里程碑）:
  PATCH /repos/:owner/:repo/issues/:number
  Content-Type: application/json
  Body: {
    "labels": "bug,triaged",        // 逗号分隔字符串
    "assignee_ids": [12345],        // 用户数字 ID 数组
    "milestone_id": 1               // 里程碑数字 ID
  }

清除标签:
  DELETE /repos/:owner/:repo/issues/:number/labels

查看可用里程碑:
  GET /repos/:owner/:repo/milestones?state=all

查看用户信息:
  GET /user
  GET /repos/:owner/:repo/collaborators
```

## 硬规则

- ❌ 禁止直接修改仓库代码
- ❌ 禁止合并 PR
- ❌ 禁止删除 Issue
- ✅ 可以打标签、分配、评论、关闭 Issue
- ✅ 优先使用 Issue 正文中的 @mention 确定负责人

## 参考

需要调用更多 GitCode API 时，读取 `devops-event-ops/references/gitcode-api-reference.md`。
