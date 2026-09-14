# 2. 快速上手

## 2.1 登录与仓库选择

1. 使用 GitCode 账号登录（OAuth 自动授权）
2. 登录后在左侧选择已授权的仓库
3. 选择仓库后进入工作台，顶部显示仓库名

## 2.2 创建第一个 Issue

1. 在 GitCode 仓库中创建一个 Issue（如"开发一个 Hello World 页面"）
2. Grape 通过 Webhook 自动接收 Issue 事件并启动流水线
3. 在 Grape 控制台的 **流水线** 页面可以看到 Issue 的处理进度
4. VOD Agent 首先分析需求，然后 Issue Agent 细化任务，Coding Agent 编写代码并提交 PR

> 💡 **提示**
>
> 也可以在 Grape 控制台的 **VOD Agent** 页面直接输入需求，手动触发处理流程。VOD Agent 会分析需求并自动路由到合适的下游 Agent。

## 2.3 查看任务进度

- **任务中心**：查看所有任务的执行状态（排队/运行中/已完成/失败）
- **流水线**：按 Issue 查看全链路进度，包含每个 Agent 的执行状态和结果
- **系统监控**：查看 Worker 负载、引擎健康状态、探活延迟
- 点击任意任务可查看详细执行日志（Agent 每一步的思考过程和工具调用）

## 2.4 评审与合并 PR

Coding Agent 完成代码编写后会自动在 GitCode 创建 PR。你可以：

1. 在 GitCode 上查看 PR 的代码变更
2. QA Agent 会自动审查 PR 并给出评审意见
3. 确认无误后在 GitCode 合并 PR
4. Grape 会感知 PR 合并事件并更新 Issue 状态
