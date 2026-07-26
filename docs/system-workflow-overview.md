# DevOps Bot 系统使用场景与工作流设计

## 一、系统概述

DevOps Bot 是一个基于 GitCode 事件驱动的多 Agent 协作 DevOps 平台。

**核心理念：**
- **Bot** — 事件编排器，负责监听事件、路由分发、会话管理
- **Agent** — 大脑，负责理解任务、执行操作、生成结果
- **A2A 协议** — Agent 间通过 API 链式协作，形成完整工作流

**一句话定位：** 从 Issue 提出到代码发布的全流程自动化，通过多 Agent 协作降低开发者的重复劳动。

---

## 二、用户旅程（端到端）

### 2.1 首次登录

```
开发者通过 GitCode OAuth 登录 DevOps Bot
  │
  ├── 1. 选择要加入的仓库（Repo）
  │     用自己的 GitCode token 拉取所有成员仓库列表
  │     选择仓库 → 「加入」（成为该项目的 Bot 参与者）
  │     加入后可查看该仓库的事件流和 Agent 处理记录
  │
  ├── 2. 托管仓库（仅限项目维护者/管理员）
  │     对于自己管理的仓库，点击「托管」
  │     系统自动创建 GitCode Webhook
  │     → DevOps Bot 开始监听该仓库的所有事件
  │     → 事件将按照路由规则自动分发给对应的 Agent
  │
  ├── 3. 配置消息通道（Channel）
  │     选择 Chat Agent 接入方式：
  │       ├── 方式 A：使用平台默认的 Chat Agent
  │       │     直接在 Web 面板或通过默认 IM 交互
  │       │
  │       └── 方式 B：接入自有 Chat Agent
  │             提供自己的 Agent Backend（如 OpenClaw、Hermes）
  │             自有 Chat Agent 可对接飞书、企业微信等 IM
  │             作为个人专用的对话引擎
  │
  ├── 4. 配置华为云凭据（可选）
  │     填入华为云 AK/SK
  │     → Coding Agent 可使用华为云 CodeArts 云端开发环境
  │     → 获得免费云端编码能力
  │
  └── 5. 进入主工作流看板
        展示当前参与的所有仓库
        每个仓库的 Agent 工作流状态
        待处理事项和最近活动
```

### 2.2 日常使用

```
日常开发流程：
  │
  ├── 提交 Issue → Issue Agent 自动分析处理
  │     ├── 分类打标签、指派负责人
  │     └── 自动触发 Coding Agent（或等待用户确认）
  │
  ├── 编码实现 → Coding Agent 执行
  │     ├── 分析需求 → 设计方案 → 编写代码
  │     ├── 自我审查 → 提交 Pull Request
  │     └── 触发 QA Agent
  │
  ├── 代码评审 → QA Agent 执行
  │     ├── Code Review（代码规范、安全扫描、架构检查）
  │     ├── 自动化测试（单元测试、集成测试）
  │     ├── 功能验证（端到端验证）
  │     └── 通过 → Release Agent / 不通过 → 回 Coding Agent
  │
  ├── 应用发布 → Release Agent 执行
  │     ├── 变更日志生成（CHANGELOG）
  │     ├── 构建打包
  │     └── 发布部署
  │
  ├── 应用运维 → SRE Agent 执行
  │     ├── 部署监控与健康检查
  │     ├── 故障排查与修复建议
  │     ├── 合规检查与安全扫描
  │     └── 构建失败诊断
  │
  ├── 对话交互 → Chat Agent 处理
  │     ├── 用户通过 IM 或 Web 面板与 Bot 对话
  │     ├── 查询状态、触发操作、审批确认
  │     └── 对接飞书、企业微信等 IM 平台
  │
  └── 声音提炼 → VOD Agent 后台运行
        ├── 收集开发者反馈（来源待扩展）
        ├── 分析共性问题与改进点
        └── 提炼价值信息输出报告
```

---

## 三、7 大 Agent 工作流详解

### 3.1 Issue Agent

| 属性 | 说明 |
|------|------|
| **职责** | Issue 全生命周期管理 |
| **触发方式** | `issue_opened` / `issue_comment` / 用户主动触发 |
| **关键能力** | Issue 分类与打标签、责任人指派、A2A 触发 Coding Agent |
| **工作流** | 新 Issue 到达 → 分析内容 → 分类打标签 → 判断是否需要开发 → 用户确认/自动 → A2A 通知 Coding Agent |
| **预制 Skill** | `skills/issue/` |

### 3.2 Coding Agent

| 属性 | 说明 |
|------|------|
| **职责** | 代码实现全流程 |
| **触发方式** | A2A 触发（来自 Issue Agent / QA Agent） |
| **关键能力** | 需求分析→技术方案→编码→自测→提交 PR |
| **工作流** | 收到 A2A 请求 → 理解需求 → 设计方案 → 编写实现代码 → 自我审查 → 编写测试 → 提交 PR → A2A 通知 QA Agent |
| **预制 Skill** | `skills/implementation/` |
| **云端能力** | 可配置华为云 CodeArts 作为云端开发环境 |

### 3.3 QA Agent

| 属性 | 说明 |
|------|------|
| **职责** | 代码质量保障 |
| **触发方式** | `pull_request_opened/updated` / A2A（来自 Coding Agent） |
| **关键能力** | Code Review、自动化测试、功能验证 |
| **工作流** | PR 提交/更新 → 代码规范检查 → 安全扫描 → 架构评审 → 自动化测试执行 → 功能验证 → 通过/不通过 |
| **不通过时** | A2A 回 Coding Agent，附评审意见和修复建议 |
| **预制 Skill** | `skills/pr/`（扩展中） |

### 3.4 Release Agent

| 属性 | 说明 |
|------|------|
| **职责** | 应用发布管理 |
| **触发方式** | `pull_request_merged` / 手动触发 |
| **关键能力** | 变更日志生成、构建打包、发布部署 |
| **工作流** | PR 合并 → 从 commit 生成 CHANGELOG → 触发构建 → 部署到目标环境 → 通知相关人员 |
| **预制 Skill** | `skills/changelog-gen/`、`skills/cloud-deploy/` |

### 3.5 SRE Agent

| 属性 | 说明 |
|------|------|
| **职责** | 应用运维保障 |
| **触发方式** | `deploy_start/failure` / `build_failure` / 定期巡检 |
| **关键能力** | 部署监控、故障诊断、合规检查、构建调试 |
| **工作流** | 事件到达 → 分析日志/状态 → 诊断问题 → 输出修复建议或自动修复 → 通知相关人员 |
| **预制 Skill** | `skills/deploy-guard/`、`skills/build-debug/`、`skills/incident-triage/` |

### 3.6 Chat Agent

| 属性 | 说明 |
|------|------|
| **职责** | 用户与 DevOps Bot 的交互入口 |
| **触发方式** | 用户消息（IM / Web 面板） |
| **关键能力** | 对话理解、命令路由、结果反馈、审批确认 |
| **工作流** | 用户发送消息 → 理解意图 → 执行或转发 → 返回结果 |
| **Backend 接入** | 两种模式： |
|  | **默认模式**：使用平台内置的 Chat Agent |
|  | **自有模式**：用户接入自己的 Agent Backend（OpenClaw、Hermes 等） |
|  | 自有 Chat Agent 可作为个人对话引擎，对接飞书、企微等 IM |
| **预制 Skill** | `skills/chat/`、`skills/im-notify/` |

### 3.7 VOD Agent

| 属性 | 说明 |
|------|------|
| **职责** | 开发者声音收集与分析 |
| **触发方式** | 后台定期 / 事件驱动 |
| **关键能力** | 声音收集（来源待定）、共性问题分析、价值提炼 |
| **工作流** | 收集开发者反馈 → 聚类分析 → 识别共性问题 → 提炼价值信息 → 输出报告 |
| **预制 Skill** | 待开发 |

> VOD Agent 的数据来源非常广泛（Issue 讨论、IM 消息、代码 Review 评论、用户反馈等），当前阶段先作为后端数据清理和提炼的 Agent，后续逐步扩展声音收集技能。

---

## 四、Agent 间协作（A2A 端到端工作流）

### 4.1 典型 Issue → PR 全流程

```
用户提交 Issue
  │
  ▼
Issue Agent
  ├── 分析 Issue 内容
  ├── 分类、打标签、指派
  └── 自动/经用户确认 → A2A 触发 Coding Agent
        │
        ▼
      Coding Agent
        ├── 理解需求（从 Issue 中提取）
        ├── 设计方案（技术方案文档）
        ├── 编写代码实现
        ├── 自我审查 & 编写测试
        └── 提交 Pull Request
              │
              ▼
            QA Agent
              ├── Code Review
              ├── 自动化测试执行
              ├── 功能验证
              ├── 通过 → 合并 PR → A2A 触发 Release Agent
              │         │
              │         ▼
              │       Release Agent
              │         ├── 生成 CHANGELOG
              │         ├── 构建打包
              │         └── 发布部署 → SRE Agent 跟踪
              │
              └── 不通过 → A2A 回 Coding Agent
                           附评审意见和修复建议 → 修复后重新提交
```

### 4.2 用户对话触发流程

```
用户在飞书：帮我查一下线上故障
  │
  ▼
Chat Agent（用户自有或默认）
  ├── 理解用户意图
  ├── A2A 触发 SRE Agent
  │     ├── 查询部署状态
  │     ├── 分析日志
  │     └── 返回诊断结果
  └── Chat Agent 将结果回复给用户
```

### 4.3 构建失败自动处理

```
构建失败事件
  │
  ▼
SRE Agent
  ├── 分析构建日志
  ├── 识别错误模式
  ├── 输出诊断报告
  └── A2A 触发 Coding Agent（如需修复）
        └── 自动修复 → 提交修复 PR → QA Agent 验证
```

### 4.4 A2A 协议规则

| 规则 | 说明 |
|------|------|
| **调用方式** | `POST /api/a2a`（localhost:8080） |
| **最大链深度** | 5 层（防止无限循环） |
| **上下文传递** | 前序 Agent 的摘要自动注入下一 Agent Prompt 顶部 |
| **Agent 注册** | 所有 Agent 在 `configs/agent-description.json` 中注册 |
| **调用权限** | 仅限内部 localhost 调用 |

---

## 五、用户可配置项

| 配置项 | 说明 | 配置入口 |
|--------|------|---------|
| **已加入 Repo 列表** | 用户参与的所有仓库，展示角色（成员/维护者/管理员） | 用户面板 |
| **已托管 Repo 列表** | 被 DevOps Bot 监听的项目，显示 Webhook 状态 | Admin 面板 |
| **Channel 配置** | Chat Agent 选择（默认/自有），IM 绑定 | 用户面板 |
| **云平台凭据** | 华为云 AK/SK，为 Coding Agent 提供云端开发环境 | 用户面板 |
| **GitCode Token** | 用户 API Token，用于拉取仓库列表和身份认证 | 用户面板 |
| **Agent Skill 自定义** | 项目维护者/管理员可上传自定义 SKILL.md 覆盖预置 Skill | 用户面板 |
| **Repo-Skill 映射** | 指定仓库的特定事件使用自定义 Skill | 用户面板 |
| **通知偏好** | 哪些事件需要推送到 IM | 用户面板 |

---

## 六、技能系统（Skills）

### 6.1 预制技能

当前系统已预置以下技能，直接关联到对应 Agent：

| Agent | 预制 Skill 目录 | 说明 |
|-------|----------------|------|
| Issue Agent | `skills/issue/` | Issue 分类、标签、指派、A2A 触发 |
| Coding Agent | `skills/implementation/` | 需求分析→编码→自测→PR |
| QA Agent | `skills/pr/`（扩展中） | Code Review、测试验证 |
| Release Agent | `skills/changelog-gen/`、`skills/cloud-deploy/` | 变更日志、构建部署 |
| SRE Agent | `skills/deploy-guard/`、`skills/build-debug/`、`skills/incident-triage/` | 运维、调试、故障 |
| Chat Agent | `skills/chat/`、`skills/im-notify/` | 对话、通知推送 |
| VOD Agent | 待开发 | 声音分析 |

### 6.2 自定义技能

- 项目的维护者和管理员可以上传自定义 Skill
- 自定义 Skill 通过 `repo_skill_mapping` 绑定到特定仓库和事件类型
- 自定义 Skill 的优先级高于系统预制 Skill

### 6.3 事件路由优先级

```
事件到达
  → 检查 repo_skill_mapping（用户自定义配置）
    → 有 → 使用用户自定义 Skill
    → 无 → 检查 event-routing.json repo_routing（全局仓库覆盖）
      → 有 → 使用全局指定 Skill
      → 无 → 检查 event-routing.json routing（全局默认路由）
```

---

## 七、系统架构

```
┌─────────────────────────────────────────────────────────┐
│                    GitCode                              │
│             Webhook / API / OAuth                       │
└────────────────────┬────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────┐
│              事件处理器（端口 8080）                      │
│                                                         │
│  webhook_handler.py → event_processor.py                │
│    ├── 事件归一化（VCS Adapter）                          │
│    ├── 反循环过滤（跳过 Bot 自身事件）                     │
│    ├── 仓库级锁（串行化每个 Repo 的事件处理）               │
│    ├── 路由解析（自定义 > 全局覆盖 > 默认路由）             │
│    ├── A2A 深度追踪（最大 5 层）                          │
│    └── Feishu 上下文预取                                 │
└────────────────────┬────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────┐
│               Prompt 构建器                              │
│                                                         │
│  prompt_builder.py                                       │
│    ├── A2A 上下文注入                                    │
│    ├── 事件描述 + 历史记录                                │
│    ├── Skill 指令注入（SKILL.md）                         │
│    ├── 角色定义 + 行为规则                                │
│    └── Agent 注册表 + A2A 调用指令                        │
└────────────────────┬────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────┐
│               Agent 执行器                               │
│                                                         │
│  agent/executor.py                                       │
│    ├── Backend 选择（Opencode / CodeArts）                │
│    ├── 环境变量注入（Repo、Token、凭据等）                 │
│    ├── NDJSON 流式输出                                   │
│    └── 超时控制（120s）                                   │
└────────────────────┬────────────────────────────────────┘
                     │
         ┌───────────┼───────────┐
         ▼           ▼           ▼
   ┌──────────┐┌──────────┐┌──────────┐
   │ A2A 协议  ││ 会话管理 ││ 历史持久化│
   │ /api/a2a  ││session  ││ workspace│
   └──────────┘└──────────┘└──────────┘
```

### 7.1 端口与协议

| 端口 | 用途 | 协议 |
|------|------|------|
| **80** | Admin/User Web 面板、OAuth、JSON API | HTTP |
| **8080** | GitCode Webhook、A2A、Health、Session API | HTTP |
| **WebSocket** | 飞书 IM 长连接 | WSS |

### 7.2 数据存储

| 存储 | 内容 | 说明 |
|------|------|------|
| `data/sessions.db` | 用户、账户、Session、VM、Skill、映射 | SQLite |
| `data/workspaces/` | Agent 执行历史和工作目录 | 文件系统 |
| `skills/` | 预制 Skill + 自定义 Skill | 文件系统 |
| `configs/` | 事件路由、Agent 路由、Agent 注册 | JSON 文件 |

---

## 八、安全措施

| 措施 | 说明 |
|------|------|
| **OAuth 联邦登录** | GitCode 账号一键登录，state 防 CSRF |
| **Session Token** | 24 小时过期，内存存储 |
| **Webhook Token** | 每个账户独立的 webhook token 验证 |
| **A2A 内部调用** | 仅限 localhost:8080，不对外暴露 |
| **反循环保护** | Bot 用户名的评论事件自动跳过 |
| **凭据隔离** | 每个用户的 GitCode Token、云平台凭据独立存储 |
| **自定义 Skill 校验** | 上传时校验格式、安全限制 |

---

## 九、后续规划

### 短期（当前迭代）

- [ ] 用户首次登录选 Repo 流程
- [ ] 仓库托管流程优化
- [ ] Chat Agent 自有 Backend 接入能力
- [ ] QA Agent 自动化测试集成

### 中期

- [ ] VOD Agent 声音收集技能开发
- [ ] Release Agent 统一发布流水线
- [ ] SRE Agent 统一运维看板
- [ ] 更多 IM 渠道接入（企业微信、钉钉）

### 长期

- [ ] 自定义 Skill 市场/分享
- [ ] Agent 可视化编排
- [ ] 多租户隔离增强
