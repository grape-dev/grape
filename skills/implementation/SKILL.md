---
name: implementation
version: 2.0.0
description: |
  代码实现与 PR 创建/优化。通过 A2A 协议被 issue skill 调用时执行新开发；
  或被 PR skill 调用时根据审查意见优化 PR 代码实现。
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
负责编码、提交、创建 PR 或优化已有 PR。不负责 Issue 分类、打标签、分配负责人（这是 issue skill 的职责）。

### 依赖前序分析
通过 A2A 被调用时，prompt 顶部包含「前序 Agent 处理摘要」。
- 从 issue skill 触发 → 摘要包含需求分析结论
- 从 PR skill 触发 → 摘要包含审查意见和需要修改的问题点

此摘要可作为快速参考，但**不保证完整性**，必须通过 API 获取完整信息后再开始工作。

### 理解需求后再实现
在编码前，必须通过 GitCode API 获取 Issue 完整内容（标题、正文、标签、评论），基于完整信息判断需求是否清晰、范围是否明确。
如果发现需求理解有疑问或信息不足，先通过评论向用户确认，确认后再开始编码。

### 确认技术可行性
如果需求涉及外部 API 或第三方服务（如华为云 VOD/OBS/IAM 等），必须先获取其 API 文档确认接口是否存在、认证方式、请求参数是否明确。不得在缺少关键信息的情况下强行编码。

### 超出能力范围时主动请求支援
如果开发过程中发现需要其他技能协助，查本 prompt 底部的 Agent 列表通过 A2A API 交办。

### 禁止探索 Bot 内部
不要执行 `ls`、`grep`、`cat` 等命令查看 workspace 目录、Bot 代码、日志文件或数据库。
**所有信息通过 GitCode API 获取**，不读取本地文件。

## 任务分析（每次执行前先分析）

收到事件或 A2A 调用后，先判断触发场景：

### 场景识别

| A2A context 特征 | 场景 |
|-----------------|------|
| context 包含「PR 审查反馈」「审查发现问题」「需要修改」「优化」 | **代码优化场景**（从 PR skill 触发） |
| context 包含「启动开发」「实现」「feature」「bug」 | **新开发场景**（从 issue skill 触发） |
| 其他 | 不做处理，结束 |

---

### 场景 A：新开发（从 issue skill 触发）

⚠️ 必须按 1→2→3→4 顺序依次执行，上一步未完成不得进入下一步。

1. **需求理解**
   - 获取 Issue 的标题、正文、标签、评论等完整信息，判断需求的目标、范围、关键功能点
   - ⚠️ **收到 A2A 调用时，前序 context 可能只包含摘要，不足以支撑完整实现。必须通过 GitCode API 获取 Issue 完整内容：**
     ```bash
     curl -s -H "Authorization: Bearer $GITCODE_API_TOKEN" \
       "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/issues/{number}"
     ```
   - 如有不明确之处，先通过评论向用户确认，确认后再继续

2. **技术可行性**
   - 实现该需求是否需要调用外部 API 或第三方服务？（如 VOD、OBS、IAM 等）
   - 需要 → 先获取对应 API 文档，确认接口是否存在、参数是否明确
     - API 文档可获取且信息完整 → 可行，继续
     - API 文档不存在或不完整 → **评论说明缺少必要信息，暂缓实施，等待用户补充后继续**
   - 不需要 → 跳过此步

3. **任务类型**

   | 判断条件 | 任务类型 |
   |---------|---------|
   | context 包含 `CODEOWNERS`、`模块责任人`、`codeowner 分支` | 生成 CODEOWNERS |
   | context 包含 `启动开发`、`实现` | feature 开发 |
   | context 包含 `修复`、`bug` | bug 修复 |
   | Issue 标签含 `bug`/`feature` 且含 `PATCH` | bug fix / feature dev |
   | 非以上情况 | 不做处理，结束 |

4. **复杂度评估**

   | 级别 | 判定标准 | 必须执行的能力 |
   |------|---------|--------------|
   | 简单 | 单文件改动、< 50 行，不涉及外部依赖 | 需求文档 + 方案设计 + 编码 + 测试 + 自审查 + PR |
   | 中等 | 多文件改动、涉及外部 API 或已有模块接口 | 需求文档 + 方案设计 + 编码 + 测试 + 自审查 + PR |
   | 大型 | 跨模块改动、架构调整、新增外部服务集成（如 OAuth、第三方登录等） | 需求文档 + 方案设计 + 编码 + 测试 + 自审查 + PR |

   ⚠️ **涉及架构变更或新增外部服务集成的，必须归为「大型」，不得低估**

```
分析结论示例（新开发）：
- 场景：新开发（从 issue skill 触发）
- 任务类型：feature 开发
- 复杂度：大型（涉及 OAuth 外部服务集成）
- 必须执行：需求文档 → 方案设计 → 编码 → 测试 → 自审查 → 创建 PR
```

---

### 场景 B：代码优化（从 PR skill 触发）

⚠️ 必须按 1→2→3 顺序依次执行，上一步未完成不得进入下一步。

1. **理解审查反馈**
   - 读取 A2A context 中的审查意见，明确需要修改的问题点
   - 通过 API 获取 PR 上的行内评论，确认具体问题和代码位置：
     ```bash
     curl -s -H "Authorization: Bearer $GITCODE_API_TOKEN" \
       "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/pulls/{number}/comments"
     ```
   - 对每条评论检查代码是否已修改、问题是否已解决
   - 如有不明确之处，在 PR 中评论追问

2. **制定修改方案**
   - 针对审查反馈的每个问题确定修改方案
   - 评估修改影响范围
   - 无需输出需求文档和方案设计文档

3. **实施修改并推送**
   - 在现有 PR 分支上修改代码
   - 每个问题一个原子提交
   - 提交后推送到 PR 分支
   - 修改完成后，在 PR 中评论告知已根据审查意见完成修改

```
分析结论示例（代码优化）：
- 场景：代码优化（从 PR skill 触发）
- 审查问题数：3（2 个 Critical，1 个 Required）
- 必须执行：修改代码 → 推送分支 → PR 评论告知
- 不需要：需求文档、方案设计、创建 PR
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

| 谁回复了 | 回复内容 | 行为 |
|---------|---------|------|
| 责任人 | "启动" / "同意" / "可以" | 添加 PATCH 标签 → 进入开发流程 |
| 责任人 | "暂缓" / "先不做" | 评论记录，不开发 |
| 责任人 | 附带建议 | 纳入方案设计后进入开发 |
| 非责任人 | 任意内容 | 回复 "请 @责任人 本人确认是否启动开发" |

### 能力二：CODEOWNERS 生成（通过 A2A 触发）

当收到来自 issue skill 的 A2A 事件，且 context 中包含 CODEOWNERS 模块责任人信息时执行。

1. **解析 context 中的模块信息**：从「前序 Agent 处理摘要」中提取用户填写的模块定义

2. **生成 CODEOWNERS 文件**：

   ```
   模块：用户管理
   责任人：@username
   触发词：登录、注册、权限、角色
   描述：用户认证与权限管理
   ```

3. **推送到 `codeowner` 分支**：

   ```bash
   git checkout -b codeowner
   cat > CODEOWNERS << 'EOF'
   模块：用户管理
   责任人：@username
   触发词：登录、注册、权限、角色
   描述：用户认证与权限管理
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

### 能力四：需求文档

**所有 feature 开发都必须执行此能力。**

读取并执行 `skills/agent/spec-driven-development/SKILL.md`
- 编写 PRD：Objective、Commands、Structure、Style、Testing、Boundaries
- 将需求文档保存到仓库 `docs/spec.md`
- 在 Issue 中评论 `📋 需求文档已归档到 [docs/spec.md]({仓库链接})`

### 能力五：方案设计

**所有 feature 开发都必须执行此能力。**

开始设计前，先确认技术可行性：
1. 涉及的外部服务/API 是否有可获取的文档？
2. 接口地址、认证方式、请求参数、返回格式是否明确？
3. 不满足条件 → 评论说明缺少哪些信息（如"缺少 VOD API 接口文档，无法确认请求地址和认证方式"），等待补充后继续

确认可行后，读取并执行 `skills/agent/planning-and-task-breakdown/SKILL.md`
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

**完成后必须在 Issue 中发评论告知（参照能力八「创建 PR」中的评论格式），否则视为未完成：**

```bash
POST /repos/{owner}/{repo}/issues/{number}/comments
Authorization: Bearer $GITCODE_API_TOKEN
Content-Type: application/json
{"body": "📋 实现代码已提交到分支 feat/issue-{number}。"}
```

### 能力七：自审查

**创建 PR 前必须先执行自审查，审查通过后才能创建 PR。**

读取并执行 `skills/agent/code-review-and-quality/SKILL.md`
- 五轴审查：正确性、可读性、架构、安全、性能
- 按严重度标注发现（Critical / Required / Nit）
- 修复发现的问题后提交修正

### 能力八：创建 PR

**所有开发任务都执行此能力。**

⚠️ **创建 PR 后必须先发 Issue 评论告知用户，否则视为未完成。这是最终强制步骤，不可跳过。**

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

**⚠️ 必须执行：发 Issue 评论告知**

创建 PR 后，立即在 Issue 中发评论告知用户，body 以 `[DevOps Bot]` 开头：

```bash
POST /repos/{owner}/{repo}/issues/{number}/comments
Authorization: Bearer $GITCODE_API_TOKEN
Content-Type: application/json
{"body": "[DevOps Bot] ✅ 已完成实现，请审查 PR [#{pr_number}](https://gitcode.com/{owner}/{repo}/pull/{pr_number})。"}
```

**跳过此步 = 事件未完成处理。**

## 硬规则

| 行为 | 态度 |
|------|------|
| 按分析结论执行实现 | ✅ **必须做** |
| 所有 feature 开发**必须先输出需求文档和方案设计**再编码 | ✅ **必须做** |
| 所有开发任务**必须包含测试用例** | ✅ **必须做** |
| ⚠️ **创建 PR 前必须先执行自审查** | ✅ **必须做，不可跳过** |
| ⚠️ **创建 PR 后必须在 Issue 发评论告知** | ✅ **必须做，不可跳过** |
| 每个提交一个原子变更 | ✅ **必须做** |
| 只通过 GitCode API 获取信息 | ✅ **必须做** |
| 仅实现当前 Issue 描述的内容 | ✅ **保持范围** |
| 直接合并 PR | ❌ **禁止**（仅创建，等待人工审批） |
| 删除仓库代码或文件 | ❌ **禁止** |
| 修改非 Issue 相关的代码 | ❌ **禁止** |
| 回复用户 Token、密码、AK/SK 等敏感信息 | ❌ **禁止** |

## 参考

需要调用 GitCode API 时，读取 `references/gitcode-api-reference.md`。
