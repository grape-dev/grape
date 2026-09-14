# 3. DevOps Agent 详解

## VOD Agent — 需求分析

> **职责**：接收用户需求（Issue 评论或直接输入），分析需求类型，路由到合适的下游 Agent。
>
> **触发方式**：GitCode Issue 创建/评论事件，或控制台手动输入
>
> **典型场景**：
> - 用户创建 Issue "优化前端UI样式" → VOD 分析为开发类需求 → 路由到 Issue Agent
> - 用户创建 Issue "销毁云资源" → VOD 分析为运维类需求 → 路由到 SRE Agent
> - 用户在评论区补充需求 → VOD 重新分析并路由

## Issue Agent — 任务拆解

> **职责**：将 VOD 分析后的需求细化为可执行的 Spec 文档，打标签、分配负责人，交办给 Coding/QA/SRE Agent。
>
> **输出**：Issue 评论中的 Spec 文档（含技术方案、文件清单、验收标准）

## Coding Agent — 代码实现

> **职责**：根据 Spec 文档编写代码，本地构建验证，自审查后提交 PR。
>
> **引擎**：opencode 或 hermes（在 Worker 上执行）
>
> **工作目录**：`/opt/grape-workspaces/<owner>/<repo>/issues/<n>/coding`
>
> **典型流程**：克隆仓库 → 阅读项目知识基线 → 按需求编码 → 构建+测试 → 代码自审查 → 推送分支 → 创建 PR

## QA Agent — 质量审查

> **职责**：审查 Coding Agent 提交的 PR，检查代码质量、安全性、测试覆盖，给出 Approve/Request Changes 意见。
>
> **触发**：PR 创建事件自动触发，或 Coding Agent 完成后 A2A 交办

## SRE Agent — 运维部署

> **职责**：云资源管理（创建/查询/销毁）、CI/CD 配置、部署执行、故障排查。
>
> **能力**：华为云 ECS/VPC/EIP/安全组/RDS/OBS 管理，Terraform IaC，部署脚本执行
>
> **安全机制**：销毁类操作需用户在 Issue 评论区确认后才执行
