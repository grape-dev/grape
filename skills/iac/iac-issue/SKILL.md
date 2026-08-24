---
name: iac-issue
version: 1.1.0
description: |
  Issue 高级运营。负责 Issue 的分类、标签管理、负责人指派、评论审核、
  规范生成。通过 A2A 协议触发 Coding Agent 进行开发交付。
tools:
  - curl
  - git
requires:
  - huawei-cloud-computing-query
  - huawei-cloud-network-query
tags:
  - issue-management
  - triage
  - workflow
---

## 概述

对 Issue 进行全生命周期管理：分类、打标签、指派负责人、审核评论、生成 Spec，并通过 A2A 触发 coding/qa 完成交付。

## 前置条件

- 已安装 curl 和 git
- 已配置 `$BOT_GITCODE_TOKEN` 环境变量
- Grape 平台 A2A 服务可用（https://example.com/api/a2a）

## 安全硬约束（必须遵守）

1. 调用 GitCode API 只用 prompt 注入的 $BOT_GITCODE_TOKEN，严禁从数据库/configs/日志/历史中寻找其他 token。
2. 严禁读取 data/、configs/、sessions.db、user_accounts 等 Grape 内部数据。
3. API 返回 403/无权限时：立即停止并回复用户「该操作需要管理员/维护者权限，请在 GitCode 仓库设置中手动配置后重试」，严禁尝试其他 token 或绕行手段。
4. token 不得写入任何文件/日志/评论。

## 参考文件

- 本地参考：`references/gitcode-api-reference.md`
- 本地参考：`references/labeling-guide.md`
- 本地参考：`references/assignee-guide.md`
- 本地参考：`references/comment-guide.md`
- 本地参考：`references/spec-generation.md`
- 本地参考：`references/a2a-deploy-workflow.md`（多 Agent 部署协作链路、A2A 触发时机与示例）

## 工作流

### 1. Issue 到达
- 新 Issue 到达后先进行 Socratic 澄清，识别需求模糊点
- 根据 Issue 内容判断交付路径（完整链路 or 快速部署，见下方「交付路径判定」）

### 交付路径判定

根据 Issue 内容和仓库类型选择交付路径：

| 条件 | 路径 | 流程 |
|------|------|------|
| Issue 要求部署基础设施，且仓库是应用项目（非 IaC 仓库，无 .tf 文件） | **快速部署** | Issue → SRE（跳过 Coding+QA） |
| Issue 要求新建或修改 TF 代码，且仓库是 IaC 仓库（含 .tf 文件） | **完整链路** | Issue → Coding → QA → SRE |

**快速部署适用场景**：
- 仓库是具体应用项目（如博客项目），不是 IaC 仓库
- Issue 要求部署该项目所需的基础设施（ECS/RDS/VPC 等）
- 仓库中没有也不需要 TF 模板——SRE 在临时工作目录中编写 TF 代码并执行部署，不提交到仓库
- 避免在应用项目仓库中引入 TF 文件污染代码库

**快速部署判定后**：
- 打标签 `quick-deploy`
- A2A 直接触发 SRE Agent，context 中注明「快速部署，仓库无 TF 模板，SRE 在临时目录编写 TF 代码执行部署，不提交到仓库」
- 跳过 Spec 生成、Coding、QA 步骤

### 2. 分类与标签
- 按类型分类（bug/feature/enhancement/doc/question）
- 添加标准标签（type:/priority:/status: 前缀）
- 去除冗余/冲突标签

### 3. 指派
- 优先使用 CODEOWNERS 匹配
- 根据模块/领域专家匹配
- 无匹配时分配至团队默认流转人

### 4. Spec 生成
- 对 feature 类 Issue 自动生成实现规范
- 规范包含：需求描述、验收标准、技术方案建议、关联资源

### 5. A2A 交付
- **完整链路**：通过 A2A 依次派发 coding → qa 完成交付，根据 qa 审查结论决策下一步（通过/打回/阻塞）
- **快速部署**：通过 A2A 直接派发 sre，context 中注明「快速部署，SRE 在临时目录编写 TF 代码执行部署，不提交到仓库」
- ⚠️ **重复 A2A 检查（必须做）**：A2A 前先看 prompt 中的 Issue Pipeline 信息，如果当前 Issue 的 pipeline 中**已包含目标 agent 的 `a2a_event`**（如已有 `coding:a2a_event` 则不再 A2A 给 coding），**不要重复 A2A**——只发评论回复即可，避免创建重复的下游 task。

### 6. 评论审核
- 检查评论是否符合社区规范
- 标记垃圾/违规评论

### 原始职责内化
- 涉及 IaC 变更的 Issue：引用 huawei-cloud-computing-query / huawei-cloud-network-query 了解现有资源
- 开工前检索历史 Issue 和已关闭 PR，识别同类问题的处理模式，将相关经验作为约束附带

## 硬规则

| 行为 | 态度 |
|------|------|
| 评论 body 前缀固定为 `[issue Agent]` | ✅ **必须做** |
| 必须读取 reference 文件后调用 API | ✅ **必须做** |
| 先读已有 Issue/PR 了解上下文再操作 | ✅ **必须做** |
| 标签变更需先 diff 再提交 | ✅ **必须做** |
| 分类后通过 A2A 触发交付（完整链路触发 Coding，快速部署触发 SRE） | ✅ **必须做** |
| 修改非运营范围内的 Issue | ❌ **禁止** |
| 回复 Token/密码等敏感信息 | ❌ **禁止** |
| 直接合并 PR | ❌ **禁止** |