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
| 标签含 `bug` | → 直接进入修复流程（无论分配给谁） |
| 标签含 `feature`/`suggestion`，分配给人 | → 评论问询责任人是否启动开发 |
| 标签含 `feature`/`suggestion`，分配人为 bot | → 直接进入开发流程 |
| 其他情况 | → 忽略，不做处理 |

## 3. 修复 / 开发流程

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
# 创建开发分支
git checkout -b fix/issue-{number}-{简短描述}

# 增量提交，每个提交一个原子变更
git add -A && git commit -m "fix: #{number} 描述"
git push origin fix/issue-{number}
```

完成后在 Issue 中评论：
```
📋 实现代码已提交到分支 fix/issue-{number}
```

### 3.4 自审查
读取并执行 `skills/agent/code-review-and-quality/SKILL.md`
- 五轴审查：正确性、可读性、架构、安全、性能
- 按严重度标注发现（Critical / Required / Nit）
- 修复发现的问题后提交修正

### 3.5 创建 PR

```bash
curl -s -X POST -H "Authorization: Bearer $GITCODE_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"title": "fix: #{number} 标题", "head": "fix/issue-{number}", "base": "main"}' \
  https://api.gitcode.com/api/v5/repos/{owner}/{repo}/pulls
```

### 3.6 评论回复

在 Issue 中评论：

```
✅ 已完成 [bug/feature] 实现，请审查 PR #[number]。
```

## 4. 问询责任人流程

当标签为 feature/suggestion 且分配给某人时：

```bash
POST /repos/{owner}/{repo}/issues/{number}/comments
Authorization: Bearer $GITCODE_API_TOKEN
Content-Type: application/json
{"body": "此需求是否启动开发？回复「启动」或「暂缓」。如需调整方案，请一并说明。"}
```

之后等待责任人回复：

| 责任人回复 | 行为 |
|-----------|------|
| "启动" / "同意" / "可以" | 进入开发流程，将责任人建议纳入方案 |
| "暂缓" / "先不做" | 评论记录，不开发 |
| 附带建议 | 纳入方案设计后进入开发 |

## 硬规则

- ❌ 禁止合并 PR（仅创建，等待人工审批）
- ❌ 禁止删除仓库代码或文件
- ❌ 禁止修改非 Issue 相关的代码
- ✅ 仅实现当前 Issue 描述的内容
- ✅ 每个提交一个原子变更
- ✅ 创建 PR 后在 Issue 评论链接

## 参考

需要调用 GitCode API 时，读取 `references/gitcode-api-reference.md`。
