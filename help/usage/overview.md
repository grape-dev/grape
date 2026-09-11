# Grape 使用指导文档

Grape 是一个多 Agent 协作的 DevOps 平台，通过 VOD/Issue/Coding/QA/SRE 五类 Agent 覆盖从需求分析到部署运维的全生命周期。平台对接 GitCode 代码托管，通过 Webhook 自动触发流水线，由 AI Agent 自主完成任务并提交 PR。

> 💡 **核心概念**
>
> - **仓库（Repo）**：GitCode 上的代码仓库，是所有任务的工作空间
> - **Agent**：AI 驱动的自主代理，按职责分为 VOD/Issue/Coding/QA/SRE 五类
> - **技能（Skill）**：Agent 的能力包，定义了执行特定任务的指令和工具
> - **运行时（Runtime）**：执行 Agent 的引擎实例（opencode/hermes/codearts），部署在 Worker 机器上
> - **流水线（Pipeline）**：Issue 从创建到关闭的全流程，Agent 按 VOD→Issue→Coding→QA→SRE 链路协作
