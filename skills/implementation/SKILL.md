---
name: implementation
version: 2.0.0
description: |
  代码实现与 PR 创建。通过 A2A 协议被 issue skill 调用时执行开发；
  也响应 issue_labeled 事件（带 PATCH 标签时进入修复/开发流程）。
triggers:
  - event_type: issue_labeled
tools:
  - git
  - curl
---

# 自主实现流程

## 核心原则

### 专注实现
负责编码、提交、创建 PR。不负责 Issue 分类、打标签、分配负责人（这是 issue skill 的职责）。

### 依赖前序分析
通过 A2A 被调用时，prompt 顶部包含「前序 Agent 处理摘要」，其中已包含了 issue skill 的分析结论。优先依据此上下文执行。

### 超出能力范围时主动请求支援
如果开发过程中发现需要其他技能协助，查本 prompt 底部的 Agent 列表通过 A2A API 交办。

### 禁止探索 Bot 内部
不要执行 `ls`、`grep`、`cat` 等命令查看 workspace 目录、Bot 代码、日志文件或数据库。
**所有信息通过 GitCode API 获取**，不读取本地文件。

## 任务分析（每次执行前先分析）

收到事件或 A2A 调用后，先确定当前要处理的任务类型：

### 分析内容

1. **触发来源**
   - A2A context 中有「前序 Agent 处理摘要」→ 优先按 context 执行
   - 无 A2A context → 读取 Issue 标签判断

2. **任务类型**

   | 判断条件 | 任务类型 |
   |---------|---------|
   | context 包含 `CODEOWNERS`、`模块责任人`、`codeowner 分支` | 生成 CODEOWNERS |
   | context 包含 `启动开发`、`实现` | feature 开发 |
   | context 包含 `修复`、`bug` | bug 修复 |
   | Issue 标签含 `bug` 且含 `PATCH` | bug 修复（传统入口） |
   | Issue 标签含 `feature` 且含 `PATCH` | feature 开发（传统入口） |
   | 非以上情况 | 不做处理，结束 |

3. **复杂度评估**
   - 简单修复（单文件改动、< 50 行）→ 跳过文档和方案设计
   - 中等 feature（多文件、有明确需求）→ 执行方案设计
   - 大型 feature（跨模块、架构调整）→ 执行文档 + 方案设计

### 分析结论示例

```
分析结论：
- 任务类型：feature 开发（通过 A2A 触发）
- 复杂度：中等
- 需要执行：方案设计 → 编码 → 自审查 → 创建 PR
- 不需要：问询责任人（已在 issue skill 完成）
```

## 能力项

### 能力一：问询责任人（仅兜底）

**仅当无 A2A context 且通过 issue_labeled + PATCH 标签触发时执行。**

```bash
POST /repos/{owner}/{repo}/issues/{number}/comments
Authorization: Bearer $GITCODE_API_TOKEN
Content-Type: application/json
{"body": "此需求是否启动开发？回复「启动」或「暂缓」。如需调整方案，请一并说明。"}
```

等待责任人回复。仅接受 Issue 分配的责任人（assignee）本人的回复。

### 能力二：CODEOWNERS 生成（通过 A2A 触发）

当收到来自 issue skill 的 A2A 事件，且 context 中包含 CODEOWNERS 模块责任人信息时执行。

1. **解析 context 中的模块信息**：从「前序 Agent 处理摘要」中提取用户填写的模块定义

2. **生成 CODEOWNERS 文件**：

   ```
   # 项目：xxx
   # 描述：xxx

   # 模块：用户管理
   # 责任人：@username
   # 触发词：登录、注册、权限、角色
   # 描述：用户认证与权限管理
   * @username
   ```

3. **推送到 `codeowner` 分支**：

   ```bash
   git checkout -b codeowner
   cat > CODEOWNERS << 'EOF'
   # 模块：用户管理
   # 责任人：@username
   # 触发词：登录、注册、权限、角色
   # 描述：用户认证与权限管理
   * @username
   EOF
   git add CODEOWNERS
   git commit -m "docs: add CODEOWNERS for module ownership"
   git push origin codeowner
   ```

4. **在触发 Issue 中评论告知完成**：

   ```bash
   POST /repos/{owner}/{repo}/issues/{number}/comments
   Authorization: Bearer $GITCODE_API_TOKEN
   Content-Type: application/json
   {"body": "[DevOps Bot] CODEOWNERS 已生成并推送到 `codeowner` 分支。"}
   ```

### 能力三：创建开发分支

所有开发工作从独立分支进行：

```bash
git checkout -b feat/issue-{number}-{简短描述}
git push origin feat/issue-{number}
```

### 能力四：需求文档（仅大型 feature）

**仅当分析结论为「大型 feature」时执行。**

读取并执行 `skills/agent/spec-driven-development/SKILL.md`
- 编写 PRD：Objective、Commands、Structure、Style、Testing、Boundaries
- 将需求文档保存到仓库 `docs/spec.md`
- 在 Issue 中评论 `📋 需求文档已归档到 [docs/spec.md]({仓库链接})`

### 能力五：方案设计（中等及以上 feature）

**仅当分析结论需要方案设计时执行。**

读取并执行 `skills/agent/planning-and-task-breakdown/SKILL.md`
- 绘制依赖图，确定实施顺序
- 切分垂直切片，每个切片可独立测试
- 输出任务到 `tasks/plan.md` 和 `tasks/todo.md`
- 在 Issue 中评论 `📋 方案设计已归档到 [tasks/plan.md]({仓库链接})`

### 能力六：代码实现

**所有开发任务都执行此能力。**

1. **测试先行** — 读取并执行 `skills/agent/test-driven-development/SKILL.md`
   - RED：先写测试（测试失败）
   - GREEN：写最小代码让测试通过
   - REFACTOR：清理代码，确保测试仍通过
2. **增量实现** — 读取并执行 `skills/agent/incremental-implementation/SKILL.md`
   - 薄垂直切片开发，每个切片实现后提交
   - 每次提交后保持构建绿色

```bash
git add -A && git commit -m "feat: #{number} 描述"
git push origin feat/issue-{number}
```

完成后在 Issue 中评论：
```
📋 实现代码已提交到分支 feat/issue-{number}
```

### 能力七：自审查

**可选，根据时间充裕度决定是否执行。**

读取并执行 `skills/agent/code-review-and-quality/SKILL.md`
- 五轴审查：正确性、可读性、架构、安全、性能
- 按严重度标注发现（Critical / Required / Nit）
- 修复发现的问题后提交修正

### 能力八：创建 PR

**所有开发任务都执行此能力。**

> **重要**：不要依赖 `git push` 自动创建 PR。必须使用以下 curl 手动创建并补充字段。

```bash
# 创建 PR（关联 Issue）
curl -sL -X POST -H "Authorization: Bearer $GITCODE_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "feat: #{number} 标题",
    "head": "feat/issue-{number}",
    "base": "main",
    "inner_issue_nums": "#{owner}/{repo}#{number}",
    "close_related_issue": true
  }' \
  https://api.gitcode.com/api/v5/repos/{owner}/{repo}/pulls

# 从 response 提取 pr_number，PATCH 补充 body 和 labels
curl -sL -X PATCH -H "Authorization: Bearer $GITCODE_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "body": "## 概述\n\n自动实现 Issue #{number}",
    "labels": "auto-pr",
    "prune_branch": true
  }' \
  https://api.gitcode.com/api/v5/repos/{owner}/{repo}/pulls/{pr_number}
```

**注意事项：**
- `inner_issue_nums` 值为字符串格式 `"#owner/repo#number"`（不是 JSON 数组）
- `close_related_issue: true` 表示 PR 合入时自动关闭关联 Issue
- `body`（不是 `description`）用于设置 PR 描述

**在 Issue 中评论告知：**

```bash
POST /repos/{owner}/{repo}/issues/{number}/comments
Authorization: Bearer $GITCODE_API_TOKEN
Content-Type: application/json
{"body": "✅ 已完成实现，请审查 PR [#{pr_number}]。"}
```

## 硬规则

| 行为 | 态度 |
|------|------|
| 按分析结论执行实现 | ✅ **必须做** |
| 创建 PR 后在 Issue 评论链接 | ✅ **必须做** |
| 每个提交一个原子变更 | ✅ **必须做** |
| 只通过 GitCode API 获取信息 | ✅ **必须做** |
| 仅实现当前 Issue 描述的内容 | ✅ **保持范围** |
| 直接合并 PR | ❌ **禁止**（仅创建，等待人工审批） |
| 删除仓库代码或文件 | ❌ **禁止** |
| 修改非 Issue 相关的代码 | ❌ **禁止** |
| 回复用户 Token、密码、AK/SK 等敏感信息 | ❌ **禁止** |

## 参考

需要调用 GitCode API 时，读取 `references/gitcode-api-reference.md`。
