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

## 场景

开发者创建/更新 Pull Request 或提交代码时，Agent 需要执行完整 PR 运营流程：
关联 Issue、代码审查、安全扫描、合并检查、回复评论。

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

#### 审查维度

1. **正确性** — 逻辑是否正确，边界条件是否处理
2. **安全性** — 是否存在注入、硬编码密钥、不安全的依赖
3. **可维护性** — 命名清晰、函数长度适中、无重复代码
4. **性能** — 无 N+1 查询、不必要的内存分配、阻塞操作
5. **测试覆盖** — 变更是否附带测试，测试覆盖关键路径

#### 常见问题清单

- [ ] 硬编码的 IP、密钥、Token
- [ ] 未处理的 error 返回值
- [ ] 全局变量或可变共享状态
- [ ] 超过 50 行的函数
- [ ] 嵌套超过 3 层的条件逻辑
- [ ] 缺少输入校验
- [ ] SQL/命令拼接而非参数化
- [ ] 未关闭的资源（文件、连接、channel）
- [ ] 缺少并发保护（竞态条件）
- [ ] TODO/FIXME/HACK 注释未跟进

#### 语言特定关注点

| 语言 | 关注点 |
|------|--------|
| Go | goroutine 泄漏、context 传递、defer 顺序 |
| Python | 异常吞没（bare except）、可变默认参数 |
| Java | 资源泄漏（未 close）、线程安全 |
| TypeScript | any 类型滥用、未处理的 Promise |

### 阶段 3：安全扫描

#### 扫描维度

1. **密钥与凭据泄露** — AWS Key（`AKIA[0-9A-Z]{16}`）、GitHub Token（`ghp_`）、私钥、JWT、密码硬编码
2. **不安全代码模式** — SQL 拼接、命令注入、路径遍历、SSRF、反序列化
3. **依赖漏洞** — 检查 go.sum、package-lock.json、requirements.txt 中的已知漏洞
4. **IaC 安全** — Kubernetes 特权容器、hostPath、Terraform 公开 S3 bucket

#### 严重程度

| 级别 | 处理要求 |
|------|---------|
| Critical | 阻止合并，立即修复 |
| High | 阻止合并，24h 内修复 |
| Medium | 警告，下个迭代修复 |
| Low | 记录，择时修复 |

### 阶段 4：合并检查

- [ ] 无 Critical/High 级别安全问题
- [ ] 代码审查通过或无严重问题
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
- ❌ 禁止探索 workspace 目录和 Bot 系统文件
- ❌ 禁止读取 devops-bot.log、sessions.db 等 Bot 内部文件
- ❌ 禁止执行 `ls /root/devops-bot`、`grep *.log` 等无关命令
- ❌ 禁止直接写代码修复问题
- ✅ 可以审查、扫描、评论、关联 Issue
- ✅ 只通过 GitCode API 获取 Issue/PR 信息

## 参考

需要调用 GitCode API 时，读取 `devops-event-ops/references/gitcode-api-reference.md`。
