---
name: iac-coding
version: 1.1.0
description: |
  代码实现与 PR 创建/优化。通过 A2A 协议被 issue skill 调用时执行新开发。
  负责编码、提交、创建 PR 及自我审查。
tools:
  - git
  - curl
requires:
  - gitcode-publish
  - huawei-cloud-terraform-generator
  - huawei-cloud-computing-query
  - huawei-cloud-network-query
  - huawei-cloud-storage-query
tags:
  - implementation
  - pr-management
  - development
---

## 概述

接收 issue skill 的 A2A 指令后，完成代码开发、自我审查、PR 创建，并通过 A2A 触发 QA 审查。

## 前置条件

- 已安装 git 和 curl
- 已配置 `$BOT_GITCODE_TOKEN` 环境变量
- Issue 的 git worktree 分支已就绪（feat/issue-{issue_number}）
- Grape 平台 A2A 服务可用

## 安全硬约束（必须遵守）

1. 调用 GitCode API 只用 prompt 注入的 $BOT_GITCODE_TOKEN，严禁从数据库/configs/日志/历史中寻找其他 token。
2. 严禁读取 data/、configs/、sessions.db、user_accounts 等 Grape 内部数据。
3. API 返回 403/无权限时：立即停止该操作并回复用户「该操作需要管理员/维护者权限，请在 GitCode 仓库设置中手动配置后重试」，严禁尝试其他 token 或绕行手段。
4. token 不得写入任何文件/日志/评论。
5. 调用 GitCode API 一律以 `references/gitcode-api-reference.md` 为准，严禁根据经验/记忆/猜测自行拼写 API 路径。
6. 长命令执行会静默，加 timeout 限制并一次只装必要依赖。
7. 禁止前台运行长驻/服务类命令，改用后台启动+验证+关闭模式。

## 参考文件

- 本地参考：`references/gitcode-api-reference.md`
- 本地参考：`references/code-review-checklist.md`

## 工作流

### 1. 场景识别
根据 A2A context 特征判断执行场景：

| A2A context 特征 | 场景 | 执行流程 |
|-----------------|------|---------|
| 含「PR 审查反馈」「审查发现问题」「需要修改」「优化」 | 代码优化 | 根据审查意见修改代码 |
| 含「启动开发」「实现」「feature」「bug」 | 新开发（从 issue skill） | 开发 -> PR -> A2A 通知 QA 审查 |
| 其他 | 不做处理，结束 | - |

### 2. 开发
- 调用 huawei-cloud-terraform-generator skill 生成 TF 配置
- 按方案执行 terraform fmt → terraform init → terraform validate
- 绝不硬编码 AK/SK，一律走变量和环境变量

### 3. 自审查
参考 `references/code-review-checklist.md` 逐项检查。

### 4. 提交 PR
- 通过 gitcode-publish skill 推送代码、创建 MR
- MR body 四段齐全：变更说明、资源清单、文件变更、后续步骤
- 遵循 Conventional Commits

### 5. A2A 通知审查
创建 PR 后通过 A2A 通知 QA Agent 进行代码审查。

### 原始职责内化
- gitcode-publish：代码推送与 MR 创建
- huawei-cloud-terraform-generator：TF 配置生成
- huawei-cloud-computing-query / huawei-cloud-network-query / huawei-cloud-storage-query：编码时查询华为云资源现状
- 开工前按模块关键词搜索历史 Issue/PR，了解常见问题模式，避免重复踩坑

## 硬规则

| 行为 | 态度 |
|------|------|
| 评论 body 前缀固定为 `[coding Agent]` | ✅ **必须做** |
| 按场景识别结果执行对应流程 | ✅ **必须做** |
| 所有 feature 开发必须先输出需求文档和方案设计再编码 | ✅ **必须做** |
| 所有开发任务必须包含测试用例 | ✅ **必须做** |
| 创建 PR 前必须先执行自审查（参考 code-review-checklist.md） | ✅ **必须做，不可跳过** |
| 创建 PR 后必须在 Issue 发评论告知 | ✅ **必须做，不可跳过** |
| 创建 PR 后必须通过 A2A 通知 QA Agent 进行代码审查 | ✅ **必须做，不可跳过** |
| 代码优化场景修改完成后必须在 PR 评论告知 | ✅ **必须做** |
| 每个提交一个原子变更 | ✅ **必须做** |
| 只通过 GitCode API 获取信息 | ✅ **必须做** |
| 仅实现当前 Issue 描述的内容 | ✅ **保持范围** |
| 直接合并 PR | ❌ **禁止（仅创建，等待人工审批）** |
| 修改非 Issue 相关的代码 | ❌ **禁止** |
| 回复 Token、密码等敏感信息 | ❌ **禁止** |