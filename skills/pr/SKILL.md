---
name: pr
version: 1.0.0
description: |
  Pull Request 全生命周期管理：接收 → 关联 Issue → 代码审查 → 安全扫描 → 合并检查 → 评论回复。
  当收到 pull_request_opened、pull_request_updated、push 或 PR 上的评论事件时使用此技能。
  不适用于：Issue 相关事件（用 issue skill）。
triggers:
  - event_type: pull_request_opened
  - event_type: pull_request_updated
  - event_type: push
  - event_type: note_hook_pr
tools:
  - git
---

# PR 运营

## 核心原则

### 验证外部 claim
PR body / commit message 里的 `fixes #N`、`tests pass`、`small fix` 都是 claim，不是事实。**先打开原对象核对**。

### 已有 PR 修此 issue → 默认 review
如果关联 PR 已存在，下一步默认是 review 现有 PR，不是从零实现。只有 PR 方向不对、质量不达标、或作者明确放弃时，才考虑其他方案。

### Consult freely, decide carefully
可以自主拉取上下文做评估/review，但 merge/close/roadmap 等硬决策需要人工确认。

### 禁止探索 Bot 内部
不要执行 `ls`、`grep`、`cat` 等命令查看 workspace 目录、Bot 代码、日志文件或数据库。
**所有信息通过 GitCode API 获取**，不读取本地文件。

## 事件处理

| 事件类型 | 处理方式 |
|---------|---------|
| pull_request_opened | 关联 Issue → 代码审查 → 安全扫描 → 合并检查 → 评论 |
| pull_request_updated | 重新审查 + 扫描（增量） |
| push | 简要审查变更（非 PR 场景时） |
| note_hook_pr | 分析评论 → 回复 |

## 处理流程

### 阶段 1：关联 Issue

1. 检查 PR 正文是否引用 Issue：`Closes #N`、`Fixes #N`、`Resolves #N`、`Related #N`
2. 在对应 Issue 中评论 `📌 关联 PR #{pr_number}`
3. 对应 Issue 是 bug → 在 Issue 上更新标签 `labels="bug,triaged"`

### 阶段 2：代码审查

读取 `references/code-review-and-quality.md`，按文件中 5 轴标准逐项审查（正确性、可读性、架构、安全、性能）。记录发现的问题并按严重度标注（Critical / Required / Nit）。

### 阶段 3：IaC 安全扫描

在阶段 2 代码审查的安全轴覆盖的应用层安全之外，额外扫描 IaC（基础设施即代码）文件：

| 文件类型 | 关注点 |
|---------|--------|
| Kubernetes Manifest | 特权容器、hostPath 挂载、`securityContext` 缺失、`runAsRoot`、资源限制未设置 |
| Terraform / OpenTofu | 公开 S3 bucket、`ingress` 全开放（`0.0.0.0/0`）、硬编码凭据、未启用加密 |
| Dockerfile | `FROM latest`、`ADD` 替代 `COPY`、root 用户运行、多余端口暴露 |
| Helm Chart | values 中的敏感默认值、RBAC 权限过度、镜像拉取策略不当 |

### 阶段 4：合并检查

- [ ] 代码审查通过（阶段 2）无严重问题
- [ ] IaC 安全扫描（阶段 3）无隐患
- [ ] 测试通过（PR 描述中声明）
- [ ] 无合并冲突
- [ ] 关联 Issue 已 accepted 或 triaged

## 评论回复

| 场景 | 行为 |
|------|------|
| 用户 @提及机器人 | 分析评论并回复 |
| 评论包含代码片段 | 分析代码并提供建议 |
| 评论询问 PR 状态 | 检查合并条件并回复 |
| 其他 DevOps 相关评论 | 分析并回复 |

### 评论输出规范

你的评论是**对用户说的**，不是执行报告。禁止输出内部处理细节：

| 行为 | 示例 |
|------|------|
| ✅ 直接回答问题 | "合并条件满足，可以合并" |
| ✅ 给出审查结论 | "代码审查通过，有一个警告" |
| ✅ 追问用户 | "请补充测试用例" |
| ❌ 输出处理步骤 | "第一阶段：关联 Issue → 第二阶段：代码审查" |
| ❌ 输出内部术语 | "加载 skill"、"事件类型"、"路由到" |
| ❌ 输出流程报告 | "已完成 PR 处理流程"、"任务完成" |

### 语言要求
始终使用中文回复。不得包含英文推理过程、内部处理步骤或术语。

### 链接格式规范

评论中引用 commit、Issue、PR 时，必须使用 Markdown 链接格式，方便用户点击跳转：

| 引用类型 | 示例 |
|---------|------|
| commit | `` [`fd7e659`](https://gitcode.com/{owner}/{repo}/commit/fd7e659) `` |
| Issue | `[#14](https://gitcode.com/{owner}/{repo}/issues/14)` |
| PR | `[#2](https://gitcode.com/{owner}/{repo}/pulls/2)` |

### 评论格式要求
通过 curl 发送评论时，body 必须以 `[DevOps Bot]` 开头，回复 PR 评论时应引用原文：

```bash
POST /repos/{owner}/{repo}/pulls/{number}/comments
Authorization: Bearer $GITCODE_API_TOKEN
Content-Type: application/json
{"body": "> 用户的问题原文\n\n[DevOps Bot] 这是机器人的回复内容。"}
```

### 必须执行：发评论

所有事件处理完成后，**必须**用 curl 发送一条评论到 Issue，body 以 `[DevOps Bot]` 开头：

```bash
POST /repos/{owner}/{repo}/pulls/{number}/comments
Authorization: Bearer $GITCODE_API_TOKEN
Content-Type: application/json
{"body": "[DevOps Bot] 代码审查完成，结论：通过。"}
```

这是强制步骤，不能省略。

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

### 安全扫描
**结论**: ✅ 安全

### 合并检查
- ✅ 安全通过
- ✅ 审查通过
- ⚠️ 测试未确认
```

## 硬规则

- ❌ 禁止直接合并 PR（仅输出检查结论）
- ❌ 禁止修改 PR 代码
- ❌ 禁止删除 PR
- ❌ 禁止回复用户 Token、密码、AK/SK 等敏感信息
- ❌ 禁止探索 workspace 目录和 Bot 系统文件
- ❌ 禁止读取 devops-bot.log、sessions.db 等 Bot 内部文件
- ❌ 禁止执行 `ls /root/devops-bot`、`grep *.log` 等无关命令
- ❌ 禁止直接写代码修复问题
- ✅ 可以审查、扫描、评论、关联 Issue
- ✅ 只通过 GitCode API 获取 Issue/PR 信息

## 参考

需要调用 GitCode API 时，读取 `references/gitcode-api-reference.md`。
