# 新开发流程

从 issue skill 通过 A2A 触发的新功能/fix 开发。

## 分析（按顺序执行，上一步未完成不得进入下一步）

### 1. 需求理解

获取 Issue 的标题、正文、标签、评论等完整信息，判断需求的目标、范围、关键功能点。

⚠️ A2A context 可能只包含摘要。必须通过 GitCode API 获取 Issue 完整内容：

```bash
curl -s -H "Authorization: Bearer $BOT_GITCODE_TOKEN" \
  "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/issues/{number}"
```

如有不明确之处，先通过评论向用户确认，确认后再继续。

### 2. 技术可行性

实现该需求是否需要调用外部 API 或第三方服务？（如 VOD、OBS、IAM 等）
- 需要 → 先获取对应 API 文档，确认接口是否存在、参数是否明确
  - API 文档可获取且信息完整 → 可行，继续
  - API 文档不存在或不完整 → **评论说明缺少必要信息，暂缓实施，等待用户补充后继续**
- 不需要 → 跳过此步

### 3. 任务类型

| 判断条件 | 任务类型 |
|---------|---------|
| context 包含 `CODEOWNERS`、`模块责任人`、`codeowner 分支` | 生成 CODEOWNERS |
| context 包含 `启动开发`、`实现` | feature 开发 |
| context 包含 `修复`、`bug` | bug 修复 |
| Issue 标签含 `bug`/`feature` 且含 `PATCH` | bug fix / feature dev |
| 非以上情况 | 不做处理，结束 |

### 4. 复杂度评估

| 级别 | 判定标准 | 必须执行的能力 |
|------|---------|--------------|
| 简单 | 单文件改动、< 50 行，不涉及外部依赖 | 需求文档 + 方案设计 + 编码 + 测试 + 自审查 + PR + A2A 通知 QA |
| 中等 | 多文件改动、涉及外部 API 或已有模块接口 | 需求文档 + 方案设计 + 编码 + 测试 + 自审查 + PR + A2A 通知 QA |
| 大型 | 跨模块改动、架构调整、新增外部服务集成（如 OAuth） | 需求文档 + 方案设计 + 编码 + 测试 + 自审查 + PR + A2A 通知 QA |

⚠️ **涉及架构变更或新增外部服务集成的，必须归为「大型」，不得低估**

### 分析结论示例

```
分析结论：
- 场景：新开发（从 issue skill 触发）
- 任务类型：feature 开发
- 复杂度：大型（涉及 OAuth 外部服务集成）
- 必须执行：需求文档 → 方案设计 → 编码 → 测试 → 自审查 → 创建 PR → A2A 通知 QA 审查
```

## 执行能力

### 能力一：问询责任人（仅兜底）

仅当无 A2A context 且通过 issue_labeled + PATCH 标签触发时执行。

```bash
POST /repos/{owner}/{repo}/issues/{number}/comments
Authorization: Bearer $BOT_GITCODE_TOKEN
Content-Type: application/json
{"body": "此需求是否启动开发？回复「启动」或「暂缓」。如需调整方案，请一并说明。"}
```

### 能力二：CODEOWNERS 生成

当 context 中包含 CODEOWNERS 模块责任人信息时执行。从 context 中提取模块定义，生成 CODEOWNERS 文件并推送到 `codeowner` 分支。在 Issue 中评论告知完成。

### 能力三：创建开发分支

```bash
git checkout -b feat/issue-{number}-{简短描述}
git push origin feat/issue-{number}
```

### 能力四：需求文档

所有 feature 开发都必须执行。

读取 `skills/agent/spec-driven-development/SKILL.md`，编写 PRD 保存到 `docs/spec.md`，评论告知。

### 能力五：方案设计

所有 feature 开发都必须执行。

读取 `skills/agent/planning-and-task-breakdown/SKILL.md`，输出任务到 `tasks/plan.md` 和 `tasks/todo.md`，评论告知。

### 能力六：代码实现

所有开发任务都执行。

1. 测试先行 — 读取 `skills/agent/test-driven-development/SKILL.md`（RED→GREEN→REFACTOR）
2. 增量实现 — 读取 `skills/agent/incremental-implementation/SKILL.md`

```bash
git add -A && git commit -m "feat: #{number} 描述"
git push origin feat/issue-{number}
```

完成后在 Issue 中评论（参照能力八的评论格式）。

### 能力七：自审查

创建 PR 前必须先执行，审查通过后才能创建 PR。

读取 `skills/agent/code-review-and-quality/SKILL.md`，按五轴审查，修复发现的问题。

### 能力八：创建 PR

⚠️ 创建 PR 后必须先发 Issue 评论告知用户，否则视为未完成。

```bash
# 创建 PR
curl -sL -X POST -H "Authorization: Bearer $BOT_GITCODE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "feat: #{number} 标题",
    "head": "feat/issue-{number}", "base": "main",
    "inner_issue_nums": "#{owner}/{repo}#{number}",
    "close_related_issue": true
  }' \
  https://api.gitcode.com/api/v5/repos/{owner}/{repo}/pulls

# PATCH 补充 body
curl -sL -X PATCH -H "Authorization: Bearer $BOT_GITCODE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"body": "## 概述\n\n自动实现 Issue #{number}", "labels": "auto-pr", "prune_branch": true}' \
  https://api.gitcode.com/api/v5/repos/{owner}/{repo}/pulls/{pr_number}
```

### ⚠️ 必须执行：发 Issue 评论告知

```bash
POST /repos/{owner}/{repo}/issues/{number}/comments
Authorization: Bearer $BOT_GITCODE_TOKEN
Content-Type: application/json
{"body": "[Coding Agent] ✅ 已完成实现，请审查 PR [#{pr_number}](https://gitcode.com/{owner}/{repo}/pull/{pr_number})。"}
```

**跳过此步 = 事件未完成处理。**

> 注：`[$AGENT_NAME]` 中的 `$AGENT_NAME` 是当前 Agent 名称的环境变量（如 Issue Agent / QA Agent / Coding Agent），构造 curl body 时需用双引号包裹以触发 shell 展开（单引号内不展开）。

### 能力九：A2A 通知 QA Agent 代码审查

⚠️ 创建 PR 后必须执行，否则 QA Agent 不会收到审查请求。

```bash
curl -X POST {{SERVER_URL}}/api/a2a \
  -H "Content-Type: application/json" \
  -d '{
    "agent": "qa",
    "payload": {
      "repository": "'"$REPO_FULL_NAME"'",
      "account": "'"$ACCOUNT"'",
      "issue_number": "'"$ISSUE_NUMBER"'",
      "pr_number": "'"$PR_NUMBER"'",
      "context": "Issue #'"$ISSUE_NUMBER"' 开发已完成，PR #'"$PR_NUMBER"' 已创建。请对该 PR 进行代码审查。原始需求：[简要描述]"
    }
  }'
```

> ⚠️ **字段规则（A2A 服务端强制校验）：** `issue_number` 必填（当前开发任务的 Issue 真实编号）。**若无法确定 issue_number，则不要调用 A2A**，在最终评论中说明 PR 已创建但未通知 QA 的原因。`pr_number` 有则填（PR 编号）、无则不填，严禁把 PR 编号填入 issue_number。

#### context 构造规则

context 应包含：
1. **PR 编号**：`PR #N 已创建`（QA Agent 据此定位 PR）
2. **原始需求摘要**：Issue 标题和关键功能点
3. **待审查要点**：如有特别关注的安全/性能/架构风险，一并说明

#### 注意

- 仅「新开发」场景执行此步；代码优化场景（pr-optimization-flow）无新 PR，不触发。
- `$PR_NUMBER` 来自能力八创建 PR 的 API 返回值。
- `$ISSUE_NUMBER` 来自原始 Issue 编号。

**跳过此步 = 事件未完成处理。**
