---
name: implementation
version: 1.0.0
description: |
  自主开发实现流程。当 Issue 标签被更新时触发，根据标签类型自动进入修复或方案开发流程。
  适用于：bug → 自主修复；feature/suggestion → 问询责任人后启动开发。
  不适用于：未打标签的 Issue、仅评论变更的事件。
triggers:
  - event_type: issue_labeled
tools:
  - git
  - curl
---

# 自主实现流程

收到 `issue_labeled` 事件时，读取 Issue 当前状态和标签，按以下流程处理。

## 1. 读取 Issue 状态

```bash
curl -s -H "Authorization: Bearer $GITCODE_API_TOKEN" \
  https://api.gitcode.com/api/v5/repos/{owner}/{repo}/issues/{number}
```

获取当前 labels、assignee、state。

## 2. 判断入口

| Issue 状况 | 行为 |
|-----------|------|
| 标签含 `bug` 且含 `PATCH` | → 直接进入修复流程 |
| 标签含 `feature` 且含 `PATCH` | → 直接进入开发流程 |
| 标签含 `bug`/`feature` 但不含 `PATCH` | → **跳过**（等待飞书用户确认，由 chat skill 处理） |
| 其他情况 | → 忽略，不做处理 |

## 3. 修复 / 开发流程

### 3.0 创建开发分支

所有开发工作从独立分支进行，不直接在主分支上操作：

```bash
git checkout -b feat/issue-{number}-{简短描述}
git push origin feat/issue-{number}
```

整个 3.1-3.5 的产出（文档 + 代码 + 测试）都在此分支上提交。

### 3.1 理解需求
读取并执行 `skills/agent/spec-driven-development/SKILL.md`
- 编写 PRD：Objective、Commands、Structure、Style、Testing、Boundaries
- 将需求文档保存到仓库 `docs/spec.md`
- 在 Issue 中评论 `📋 需求文档已归档到 [docs/spec.md]({仓库链接})`

### 3.2 方案设计与拆解
读取并执行 `skills/agent/planning-and-task-breakdown/SKILL.md`
- 绘制依赖图，确定实施顺序
- 切分垂直切片，每个切片可独立测试
- 输出任务到 `tasks/plan.md` 和 `tasks/todo.md`
- 在 Issue 中评论 `📋 方案设计已归档到 [tasks/plan.md]({仓库链接})`

### 3.3 代码实现
1. **测试先行** — 读取并执行 `skills/agent/test-driven-development/SKILL.md`
   - RED：先写测试（测试失败）
   - GREEN：写最小代码让测试通过
   - REFACTOR：清理代码，确保测试仍通过
2. **增量实现** — 读取并执行 `skills/agent/incremental-implementation/SKILL.md`
   - 薄垂直切片开发，每个切片实现后提交
   - 每次提交后保持构建绿色

```bash
# 增量提交，每个提交一个原子变更
git add -A && git commit -m "feat: #{number} 描述"
git push origin feat/issue-{number}
```

完成后在 Issue 中评论：
```
📋 实现代码已提交到分支 feat/issue-{number}
```

### 3.4 自审查
读取并执行 `skills/agent/code-review-and-quality/SKILL.md`
- 五轴审查：正确性、可读性、架构、安全、性能
- 按严重度标注发现（Critical / Required / Nit）
- 修复发现的问题后提交修正

### 3.5 创建 PR 并发飞书通知

**1. 创建 PR：**

```bash
curl -sL -X POST -H "Authorization: Bearer $GITCODE_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "feat: #{number} 标题",
    "head": "feat/issue-{number}",
    "base": "main",
    "description": "## 概述\n\n自动实现 Issue #{number}",
    "labels": ["auto-pr"],
    "assignees": [8091690],
    "testers": [8091690],
    "inner_issue_nums": ["#Jason7602/devops-bot#{number}"],
    "close_related_issue": true
  }' \
  https://api.gitcode.com/api/v5/repos/{owner}/{repo}/pulls
```

> **注意**：`assignees` 和 `testers` 使用 GitCode 数字用户 ID。REST API **不支持**审批人设置，需在 Web 界面操作。

**2. 飞书通知 PR 检视人 + Issue 责任人：**

PR 创建成功后，根据 Prompt 中的 Feishu Notification Capability，向以下人员发送通知：
- PR 的 assignees
- 关联 Issue 的责任人（assignee）

通知中引用用户时必须使用 Collaborators 列表中的 `nick_name`（中文真名），禁止使用 `username`/`login`。

Card 内容：
```
PR [#{N}]({PR链接}): {PR标题} 已创建
请 {assignee_nick_name} 检视合入。
```
如有多人，用顿号连接：`请廖堉哲、吴扬检视合入`

**3. 评论回复：**

在 Issue 中评论：
```
✅ 已完成 [bug/feature] 实现，请审查 PR [#{number}]。
```

## 4. 问询责任人流程（Issue 评论兜底）

Feature 的启动确认主要通过飞书处理（由 issue skill 发送飞书通知，chat skill 处理回复）。以下 Issue 评论问询为兜底逻辑，仅在飞书不可用时触发：

当标签为 feature 且不含 PATCH 时推送到此 skill（兜底路径），在 Issue 中评论询问：

```bash
POST /repos/{owner}/{repo}/issues/{number}/comments
Authorization: Bearer $GITCODE_API_TOKEN
Content-Type: application/json
{"body": "此需求是否启动开发？回复「启动」或「暂缓」。如需调整方案，请一并说明。"}
```

之后等待责任人回复。**仅接受 Issue 分配的责任人（assignee）本人的回复。**

| 谁回复了 | 回复内容 | 行为 |
|---------|---------|------|
| 责任人 | "启动" / "同意" / "可以" | 添加 PATCH 标签 → 进入开发流程 |
| 责任人 | "暂缓" / "先不做" | 评论记录，不开发 |
| 责任人 | 附带建议 | 纳入方案设计后进入开发 |
| 非责任人 | 任意内容 | 回复 "请 @责任人 本人确认是否启动开发" |

## 硬规则

- ❌ 禁止合并 PR（仅创建，等待人工审批）
- ❌ 禁止删除仓库代码或文件
- ❌ 禁止修改非 Issue 相关的代码
- ✅ 仅实现当前 Issue 描述的内容
- ✅ 每个提交一个原子变更
- ✅ 创建 PR 后在 Issue 评论链接

## 参考

需要调用 GitCode API 时，读取 `references/gitcode-api-reference.md`。
