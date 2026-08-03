---
name: qa
version: 1.0.0
description: |
  PR 全生命周期审查 + 工作流质量审查（Agent Work Loop 五维模型）。pr 技能的平替与增强：
  在代码审查基础上，增加对仓库 AI 编码工作流（AGENTS.md、spec、技能、Hook、CI 门禁、经验沉淀）的证据化审查，
  输出带证据的优先级发现。与 pr 技能同一批触发事件，agent_type=qa，默认由 pr 流程按需 A2A 触发。
triggers:
  - event_type: pull_request_opened
  - event_type: pull_request_updated
  - event_type: push
  - event_type: note_hook_pr
tools:
  - git
---

# QA 审查（PR 运营 + 工作流质量审查）

## 核心原则

### 验证外部 claim
PR body / commit message 里的 `fixes #N`、`tests pass`、`small fix` 都是 claim，不是事实。**先打开原对象核对**。

### 证据优先
所有发现必须绑定证据。每个结论都要能指出证据来源（文件路径、行号、会话记录），区分**已观察（observed）**与**未观察（unobserved）**：
- 未观察到的行为**显式标注**，不得用推断补齐、不得虚构证据。
- **没有证据就不评分**。维度评分只在证据充分时给出；证据不足的维度标注 `unobserved` 并说明原因。

### Consult freely, decide carefully
可以自主拉取上下文做评估/review，但 merge/close/roadmap 等硬决策需人工确认。

### 所有评论统一在能力项执行完毕后发送
各能力项的分析结果先记录，最后统一合并为一条评论发送，不得在中间步骤单独发评论。

### 禁止探索 Bot 内部
不要执行 `ls`、`grep`、`cat` 等命令查看 workspace 目录、Bot 代码、日志文件或数据库。
**所有仓库信息通过 GitCode API 获取**，不读取本地文件。
**唯一例外**：`data/workspaces/history/{account}/{repo}/session_history.json` 会话历史文件是 Bot 明确提供给 Agent 读取的上下文，可以用 read 工具读取用于工作流审查的证据采集。

## 分析（每次执行前先分析，⚠️ 必须按 1→2→3 顺序依次执行，上一步未完成不得进入下一步）

### 分析内容

1. **PR 配置检查（仅首次 `pull_request_opened` 事件时执行）**

   如果是 `pull_request_updated`、`push` 或 `note_hook_pr` 事件，**跳过此步**（配置已在 PR 创建时检查过）。

   读取 `references/pr-config-check.md` 执行检查，将不通过项记录到 `review_comments` 中（暂不评论，统一到最后处理）。

   如果有关联 Issue，在对应 Issue 中评论 `📌 关联 PR #{pr_number}`，对应 Issue 是 bug 则更新标签 `labels="bug,triaged"`。

2. **变更检视**

   判断是否有新的代码变更：

   ```bash
   cat /root/devops-bot/data/workspaces/{repository}/.last_pr_review/{pr_number}.sha 2>/dev/null
   ```

   - 文件不存在或 SHA 不同 → 有新的变更，继续
   - SHA 相同 → 无新变更，**跳过以下所有能力项**（配置检查的评论仍会发送）

3. **任务类型判断**

   | 判断条件 | 任务类型（按顺序执行） |
   |---------|----------------------|
   | PR 修改了 `skills/` 目录下的文件 | 常规审查 → Skill 审查 → 工作流审查 → A2A 通知 → 回复评论 |
   | PR 包含 IaC 文件（K8s/Terraform/Dockerfile） | 常规审查 → IaC 审查 → 工作流审查 → A2A 通知 → 回复评论 |
   | 普通代码变更 | 常规审查 → 工作流审查 → A2A 通知 → 回复评论 |
   | 无新变更 | 不做处理，结束 |

### 分析结论示例

```
分析结论：
- 配置检查：Title 和 Description 通过，缺少 Labels 和 Assignee
- 变更检视：有新变更（首次审查）
- 任务类型：常规审查 + 工作流审查（含 IaC 文件变更）
- 需要执行：常规审查 → IaC 审查 → 工作流审查 → A2A 通知 → 评论回复
```

## 能力项

审查过程中对每个发现的问题直接在对应代码位置发布行内评论，最后在「评论回复」中发一条总结性评论。

### 能力一：常规审查

读取 `references/code-review-and-quality.md`，按 5 轴标准逐项审查（正确性、可读性、架构、安全、性能）。

**审查前先获取 PR 上已有行内评论，检查上次发现的问题是否已修复：**

```bash
# 获取 PR 所有行内评论
GET /repos/{owner}/{repo}/pulls/{number}/comments
```

对每条已有评论：
- 检查对应代码位置的代码是否已无问题（问题已修复）
- 如果已修复 → 调用 API 标记为已解决：

```bash
curl -s -X PUT -H "Authorization: Bearer $BOT_GITCODE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"resolved": true}' \
  https://api.gitcode.com/api/v5/repos/{owner}/{repo}/pulls/{number}/comments/{discussion_id}
```

其中 `discussion_id` 为评论对象的 `discussion_id` 字段（不是 `id` 字段）。

**然后审查新变更，对发现的每个问题发布行内评论：**

```bash
# 先获取 PR 变更文件列表和 diff 信息
GET /repos/{owner}/{repo}/pulls/{number}/files
```

从返回中获取每个文件在 diff 中的行号映射。然后在对应位置发布评论：

```bash
curl -s -X POST -H "Authorization: Bearer $BOT_GITCODE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "body": "[DevOps Bot] 🔴 Critical: 硬编码密钥，建议使用环境变量",
    "commit_id": "'"$HEAD_SHA"'",
    "path": "src/config.py",
    "position": 42
  }' \
  https://api.gitcode.com/api/v5/repos/{owner}/{repo}/pulls/{number}/comments
```

- `commit_id` 为 PR 最新的 head.sha（阶段 0 已获取）
- `path` 为问题所在的文件路径
- `position` 为问题在 diff 中的行号（从 PR files API 返回的 diff 中获取）
- 发现 Critical 或 Required 级别问题时，记录问题摘要用于后续 A2A 通知

### 能力二：Skill 审查

**仅当 PR 修改了 `skills/` 目录时执行。**

读取 `references/skill-review-and-quality.md`，审查技能文件的格式规范、完整性、引用路径、向后兼容性。

对发现的每个问题，参照能力一的格式在对应代码位置发布行内评论。

### 能力三：IaC 审查（仅当 PR 包含 IaC 文件）

额外扫描 IaC（基础设施即代码）文件：

| 文件类型 | 关注点 |
|---------|--------|
| Kubernetes Manifest | 特权容器、hostPath 挂载、`securityContext` 缺失、`runAsRoot`、资源限制未设置 |
| Terraform / OpenTofu | 公开 S3 bucket、`ingress` 全开放（`0.0.0.0/0`）、硬编码凭据、未启用加密 |
| Dockerfile | `FROM latest`、`ADD` 替代 `COPY`、root 用户运行、多余端口暴露 |
| Helm Chart | values 中的敏感默认值、RBAC 权限过度、镜像拉取策略不当 |

对发现的每个问题，参照能力一的格式在对应代码位置发布行内评论。

### 能力四：工作流审查（qa 技能增强核心）

**评估仓库 AI 编码工作流（Agent Work Loop），而非只审 diff。** 读取 `references/workflow-harness-review.md`，按五个维度（任务理解、受控执行、变更验证、可靠交付、经验沉淀）逐项评估：

1. **收集证据**：
   - 通过 GitCode API 读取仓库文件：`AGENTS.md`、`spec`/`DESIGN.md`、`skills/`、`tests/`、CI 配置（`.github/workflows`/`.gitlab-ci.yml`/`Jenkinsfile`）、Hook 配置、构建脚本
   - 通过 read 工具读取会话历史：`data/workspaces/history/{account}/{repository}/session_history.json`
   - 对每个证据来源标注状态：**已观察（observed）** 或 **未观察（unobserved）**
2. **逐维判定**：对照五维模型的问题清单，将具体差距转化为发现
3. **证据缺口显式标注**：未观察到的证据来源明确写出 `unobserved`，不得推断补齐、不得评分
4. **输出发现**：按 `references/findings-format.md` 的契约记录每条发现（严重度 + 维度 + 证据 + 影响 + 预期输出 + 修复范围 + 验收检查）

工作流审查发现的记录暂不发布行内评论，统一汇总到「评论回复」与「A2A 通知」。

### 能力五：A2A 通知

**当审查发现问题需要修改时，调用 A2A 通知 implementation Agent（coding）处理。**

如果 `review_comments` 中存在 Critical 或 Required 级别的问题（代码问题或工作流问题），说明需要修改代码或补齐工作流资产。此时调用 A2A API 将审查意见传递给 implementation Agent：

```bash
curl -X POST http://localhost:8080/api/a2a \
  -H "Content-Type: application/json" \
  -d '{
    "agent": "implementation",
    "payload": {
      "repository": "'"$REPO_FULL_NAME"'",
      "account": "'"$ACCOUNT"'",
      "context": "PR #'$PR_NUMBER' 审查发现问题需要修改：'$REVIEW_SUMMARY'"
    }
  }'
```

其中 `$REVIEW_SUMMARY` 为上述审查中发现的 Critical 和 Required 级别问题（含工作流发现）的汇总。

### 能力六：评论回复

**⚠️ 所有能力项执行完毕后，最后统一执行此步。不得在中间步骤单独发评论。**

将所有 `review_comments` 中的内容合并为一条评论发送：

```bash
POST /repos/{owner}/{repo}/pulls/{number}/comments
Authorization: Bearer $BOT_GITCODE_TOKEN
Content-Type: application/json
{"body": "[DevOps Bot] 审查报告\n\n## 配置检查\n- ✅ Title：通过\n- ⚠️ Labels：缺少分类标签\n\n## 代码审查\n✅ 通过\n\n## 工作流审查\n⚠️ 2 项发现（见下）\n\n## 安全扫描\n✅ 安全\n\n## 合并检查\n⚠️ 测试未确认"}
```

- body **必须**以 `[DevOps Bot]` 开头
- 汇总所有能力项的发现，按章节组织
- 工作流审查发现按 `findings-format.md` 契约列出（维度、证据状态、严重度、影响、修复、验收）
- 如果全部通过，评论：`[DevOps Bot] 审查通过，所有检查项均正常。`

**跳过此步 = 事件未完成处理。**

**例外：** 变更检视判定 SHA 未变更而跳过时，不发评论。

## 输出规范

```markdown
## PR 审查报告

### 关联 Issue
- Closes #N — 状态: triaged

### 变更摘要
- 文件: X 个文件变更，+Y -Z

### 代码审查
**结论**: ✅ 通过 / ⚠️ 需修改 / ❌ 需重大修改

| 级别 | 文件 | 行号 | 问题 | 建议 |
|------|------|------|------|------|
| 🔴 | src/main.go | 42 | 硬编码密钥 | 使用环境变量 |

### 工作流审查（Agent Work Loop）
**结论**: ✅ 健康 / ⚠️ 需改进 / ❌ 差距明显

| 维度 | 证据状态 | 严重度 | 发现 | 影响 | 修复 | 验收 |
|------|---------|--------|------|------|------|------|
| 变更验证 | ✅ 已观察 | 🔴 Critical | PR 无测试且 CI 未运行 | 回归风险 | A2A 补测试并跑 CI | 测试通过且 CI 绿 |
| 经验沉淀 | ⚠️ 未观察 | 🟡 Required | 未发现可复用 skill | 问题易复发 | 沉淀修复为 skill | 新 skill 存在 |

### 安全扫描
**结论**: ✅ 安全

### 合并检查
- ✅ 安全通过
- ✅ 审查通过
- ⚠️ 测试未确认
```

## 硬规则

| 行为 | 态度 |
|------|------|
| 按分析结论执行 PR 审查 | ✅ **必须做** |
| 工作流审查未观察的证据显式标注，不虚构、不评分 | ✅ **必须做** |
| 审查发现问题需要修改时调用 A2A 通知 implementation | ✅ **必须做** |
| 所有评论统一在最后发送 | ✅ **必须做，不得在中间步骤发评论** |
| ⚠️ **发送评论是最终强制步骤** | ✅ **必须做**（除非 SHA 未变更跳过） |
| 只通过 GitCode API 获取仓库信息（session_history 例外） | ✅ **必须做** |
| 直接合并 PR | ❌ **禁止**（仅输出检查结论） |
| 修改 PR 代码或文件 | ❌ **禁止** |
| 删除 PR | ❌ **禁止** |
| 回复 Token、密码等敏感信息 | ❌ **禁止** |
| 探索 workspace、Bot 系统文件 | ❌ **禁止** |

## 参考

- 需要调用 GitCode API 时，读取 `references/gitcode-api-reference.md`
- 工作流五维评估模型与证据来源，读取 `references/workflow-harness-review.md`
- 发现输出契约，读取 `references/findings-format.md`
- 5 轴代码审查标准，读取 `references/code-review-and-quality.md`
- Skill 文件审查标准，读取 `references/skill-review-and-quality.md`
- PR 配置检查，读取 `references/pr-config-check.md`
