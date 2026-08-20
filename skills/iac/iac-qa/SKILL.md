---
name: iac-qa
version: 1.1.0
description: |
  QA 审查增强版。负责代码审查、自动化测试、功能验证、安全检查。
  通过 A2A 协议被 Coding Agent 调用执行审查。
tools:
  - git
  - curl
requires:
  - huawei-cloud-computing-query
  - huawei-cloud-network-query
  - huawei-cloud-storage-query
tags:
  - code-review
  - security
  - testing
---

## 概述

通过 A2A 被 Coding Agent 调用后，执行代码质量、功能正确性和安全检查，输出审查报告。

## 前置条件

- 已安装 git 和 curl
- 已配置 `$BOT_GITCODE_TOKEN` 环境变量
- 目标 PR 已创建且有可获取的 diff

## 安全硬约束（必须遵守）

1. 调用 GitCode API 只用 prompt 注入的 $BOT_GITCODE_TOKEN，严禁从数据库/configs/日志/历史中寻找其他 token。
2. 严禁读取 data/、configs/、sessions.db、user_accounts 等 Grape 内部数据。
3. API 返回 403/无权限时：立即停止并回复用户「该操作需要管理员/维护者权限，请在 GitCode 仓库设置中手动配置后重试」，严禁尝试其他 token 或绕行手段。
4. token 不得写入任何文件/日志/评论。

## 参考文件

- 本地参考：`references/gitcode-api-reference.md`
- 本地参考：`references/code-review-checklist.md`
- 本地参考：`references/security-checklist.md`

## 工作流

### 1. 获取代码
通过 GitCode API 获取 PR 的变更文件列表和 diff。

### 2. 自动化审查

#### 代码质量
- 检查代码格式规范
- 检查命名规范
- 检查代码重复
- 检查复杂度

#### 功能验证
- 确认测试用例覆盖是否充分
- 检查边界条件处理
- 验证错误处理逻辑

### 3. 安全检查
- 检查敏感信息泄漏（AK/SK/密码/token）
- 检查依赖安全性
- 检查权限控制

### 4. 报告输出
- 完整审查报告贴 Issue 评论区，MR 评论区仅贴结论摘要
- 问题按严重程度分级（P0/P1/P2/P3）

### 原始职责内化
- terraform init -backend=false → terraform plan -out=plan-review.tfplan
- 逐资源分析 plan 输出，按风险规则分类（create/update/destroy/replace）
- tfsec 安全扫描 .tf 文件
- 安全组规则变更审查
- 审查时检索历史 Issue/PR 中同类变更的常见问题清单，核对当前变更是否已采取规避措施
- 凭据落盘检查：plan/state 中无明文密码

## 硬规则

| 行为 | 态度 |
|------|-------|
| 审查必须包含代码质量/功能/安全三方面 | ✅ **必须做** |
| 发现问题必须在 PR 评论中详细描述 | ✅ **必须做** |
| 问题按严重程度分级（P0/P1/P2/P3） | ✅ **必须做** |
| 审查通过后必须在 PR 评论标记 ✅ LGTM | ✅ **必须做** |
| 回复 Token/密码等敏感信息 | ❌ **禁止** |