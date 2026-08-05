# Grape — Multi-Agent DevOps 使用场景与工作流设计

## 一、系统概述

Grape 是一个多 Agent 协作的 DevOps 平台，致力于实现 Multi-Agent DevOps。

**核心理念：**
- **Bot** — 事件编排器，负责监听事件、路由分发、会话管理
- **Agent** — 大脑，负责理解任务、执行操作、生成结果
- **A2A 协议** — Agent 间通过 API 链式协作，形成完整工作流

**Agent 工作流逻辑关系：**

```
VOD Agent ──→ Issue Agent ──→ Coding Agent ──→ QA Agent ──→ SRE Agent
 收集声音      分类需求        编码实现         审查验证       构建部署
 分析提炼      定义开发项     PR 提交          测试验证       发布运维
                                                                    ↑
                                                            ┌───────┘
                                                            │ 每个用户
                                                         Chat Agent（个人助手）
```

**一句话定位：** 从开发者声音收集到代码发布的全流程自动化，通过多 Agent 协作降低开发者的重复劳动。

---

## 二、用户旅程（端到端）

### 2.1 首次登录

登录页面提供 GitCode OAuth 一键登录和密码登录两种方式：

```
┌─ Login Page ──────────────────────────────────────┐
│                                                     │
│                   [Logo Image]                       │
│                    Grape                             │
│              Multi-Agent DevOps                      │
│                                                     │
│  ┌─────────────────────────────────────────────┐   │
│  │  [▦ 使用 GitCode 登录]                      │   │
│  └─────────────────────────────────────────────┘   │
│                                                     │
│        ← 用户名密码登录                               │
│                                                     │
│        （点击用户名密码登录后，GitCode 按钮消失，       │
│         展开用户名/密码输入框，底部显示返回链接）        │
│                                                     │
└─────────────────────────────────────────────────────┘
```

登录后的完整流程：
开发者通过 GitCode OAuth 登录 Agentic DevOps
  │
  ├── 1. 选择要加入的仓库（Repo）
  │     用自己的 GitCode token 拉取仓库列表，仓库分为两种类型：
  │       ├── 类型 A：自己是维护员或管理员的**未托管仓库**
  │       │     点击「加入并托管」→ 系统自动创建 Webhook + 加入仓库
  │       │     → Agentic DevOps 开始监听该仓库的所有事件
  │       │
  │       └── 类型 B：自己是成员，同时**已托管的仓库**
  │             点击「加入」→ 成为该项目的 Bot 参与者
  │             加入后可查看该仓库的事件流和 Agent 处理记录
  │
  ├── 2. 配置消息通道（Channel）
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
  └── 5. 进入 Dashboard
         选择一个已托管的 Repo 仓库 → 进入该仓库的 Dashboard
         展示该仓库的 Agent 工作流状态、待处理事项、最近活动
```

### 2.1.1 选择仓库页面（登录后首屏）

> 此页面仅在用户首次登录且尚未加入任何仓库时展示。如果用户已有已加入的仓库，登录后直接进入该仓库的 Dashboard。

用户首次登录后，进入仓库选择页面。每个仓库以卡片形式展示，支持搜索和角色过滤：

| 类型 | 用户角色 | 按钮 | 操作 |
|------|---------|------|------|
| **类型 A：未托管仓库** | Admin / Maintainer | `加入并托管` | 创建 Webhook + 加入仓库，开始监听事件 |
| **类型 B：已托管仓库** | Member（有人已托管）| `加入` | 仅加入仓库，可查看事件流和 Agent 记录 |

```
┌─ Top Bar ────────────────────────────────────────────┐
│  Welcome, Zhang Wei · 选择要加入的仓库                │
│  [🔍 搜索仓库...]        [全部 ▼] [成员 ▼] [管理 ▼]  │
│                          [↻ Sync from GitCode] [退出] │
├───────────────────────────────────────────────────────┤
│                                                        │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐ │
│  │ ■ ＋ 新增仓库  │  │ ■ team/      │  │ ■ team/      │ │
│  │ 创建一个新的   │  │ backend-srv  │  │ web-frontend │ │
│  │ GitCode 项目   │  │ Maintainer   │  │ Maintainer   │ │
│  │ 🆕 New Project │  │ Owner: team  │  │ Owner: team  │ │
│  │ [Create Now]  │  │ [加入并托管] │  │ [加入并托管] │ │
│  └──────────────┘  └──────────────┘  └──────────────┘ │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐ │
│  │ ■ org/       │  │ ■ team/      │  │ ■ org/       │ │
│  │ devops-tklt  │  │ notification │  │ data-pipeline│ │
│  │ Admin        │  │ Member       │  │ Member       │ │
│  │ Owner: org   │  │ Owner: team  │  │ Owner: org   │ │
│  │ 🚧 Not added │  │ 🚧 Not added │  │ 🚧 Not added │ │
│  │ [加入并托管] │  │ [加入]       │  │ [加入]       │ │
│  └──────────────┘  └──────────────┘  └──────────────┘ │
└─────────────────────────────────────────────────────────┘
```

每个仓库卡片包含：

| 卡片元素 | 说明 |
|---------|------|
| **新增仓库卡片（首个）** | 始终展示在首位，点击跳转到 `https://gitcode.com/create` 创建新项目 |
| **仓库名称** | `owner/repo` 格式，左侧显示仓库图标 |
| **角色标签** | Member / Maintainer / Admin（用户在 GitCode 中的角色） |
| **Owner 信息** | 仓库所属组织/用户 |
| **状态指示** | 🚧 未加入（所有卡片统一状态） |
| **操作按钮** | 管理员/维护员 → `加入并托管`（创建 Webhook + 加入仓库）；成员 → `加入`（仅加入） |
| **搜索过滤** | 顶部搜索框可按名称搜索；下拉过滤按角色：全部 / 成员 / 管理 |
| **Sync from GitCode** | 手动从 GitCode 重新拉取仓库列表，新创建的仓库立即出现（无需等待 5 分钟自动刷新） |

### 2.2 Dashboard（按仓库维度）

Dashboard 以**单个已托管仓库**为维度展示，左侧导航栏可下拉切换仓库：

```
┌─ Sidebar ──────────────────────────────────────────┐
│  [Logo] Grape                                           │
│  Multi-Agent DevOps                                     │
│                                                     │
│  [team/backend-service ▼]        [Maintainer ▼]       │
│       ┌─────────────────────┐                       │
│       │ team/backend-service│                       │
│       │ team/web-frontend   │                       │
│       │ team/mobile-app     │                       │
│       └─────────────────────┘                       │
│                                                     │
│  Workspace                                          │
│    ■ Dashboard                                      │
│    ▶ Pipeline                                       │
│                                                     │
│  DevOps Agents                                      │
│    ♫ VOD Agent                                      │
│    ⚠ Issue Agent                                    │
│    ⚙ Coding Agent                                   │
│    ✔ QA Agent                                        │
│    ♡ SRE Agent                                      │
│                                                     │
│  DevOps Skills                                       │
│    ◈ Skills Library                                  │
│                                                     │
│  Account                                            │
│    ☎ Chat Agent                                     │
│                                                     │
│  Settings                                           │
│    ⚙ General Settings                                │
│    ⎘ GitCode Repo                                    │
│    ⎘ Backend Runtimes                                   │
│                                                     │
│    ↩ Logout                                          │
└─────────────────────────────────────────────────────┘
```

说明：
- **Repo 下拉**：仅显示已加入/已托管的仓库，底部有「+ 新增仓库托管」选项；**记住上次退出时选择的仓库**——重新登录（非首次）后自动恢复该仓库，而非每次默认第一个；若上次仓库已解除托管则回退到列表第一个
- **DevOps Agents**：核心工作流的 5 个 Agent，点击进入对应 Agent 详情
- **Account**：用户个人信息、在线状态
- **Settings**：仓库级别的通用设置（凭据、通知、自定义 Skill 等），与 Account 并列
- **Backend Runtimes**：检测本地 Agent 引擎安装状态（OpenCode/CodeArts/Hermes），配置连接凭据
- **Chat Agent 设置**：集成在 Chat Agent 页面内部，不在侧边栏展示

切换仓库后，Dashboard、Pipeline 等页面内容跟随切换为该仓库的数据。

Dashboard 主内容区域展示所选仓库的完整工作流看板：

```
┌─ Top Bar ────────────────────────────────────────────┐
│  Dashboard · team/backend-service                     │
│  [🔍 Search events, repos, agents...]                 │
│                                    [☁ 云开发环境]       │
├───────────────────────────────────────────────────────┤
│                                                        │
│  ┌─ Repo Header ──────────────────────────────────┐  │
│  │  ■ team/backend-service   🌐 Webhook OK     [Maintainer] │  │
│  │  Core backend API service · ★ main             │  │
│  │   98.5% Auto-Reso. │ 47 Events/Week │ 12s Avg  │  │
│  └────────────────────────────────────────────────┘  │
│                                                        │
│  Agent Workflow · [开发场景: 通用开发 ▼] [应用]         │
│                                                        │
 │  ┌──────┐  ┌──────┐  ┌──────┐  ┌──────┐  ┌──────┐   │
 │  │♫ VOD │→│⚠ Issue│→│⚙Coding│→│✔ QA  │→│♡ SRE │   │
 │  │OpenCode│  │OpenCode│  │OpenCode│  │OpenCode│  │OpenCode│   │
 │  │▶ Signal→│  │✓ Issue#342│  │✓ A2A:cod│  │▶ PR#1278│  │✓ Release│   │
 │  │   Issue│  │  done  │  │  done  │  │ review  │  │  done  │   │
 │  └──────┘  └──────┘  └──────┘  └──────┘  └──────┘   │
│                                                        │
│  ┌─ Chat Agent (Personal Assistant) ──────────────┐  │
│  │ ☎ Your personal agent connected to workflow    │  │
│  │ [↔ VOD] [↔ Issue] [↔ Coding] [↔ QA] [↔ SRE]   │  │
│  └────────────────────────────────────────────────┘  │
│                                                        │
│  Recent Activity (Last 24h · team/backend-service)     │
│  ├─ 14:32 QA Approved PR #1278 · feat: webhook retry  │
│  ├─ 14:15 Coding Submitted PR #1278                    │
│  ├─ 13:50 Issue Assigned #342 to @alice               │
│  ├─ 13:22 SRE Deployed v2.3.1 to production ✓         │
│  └─ 12:05 VOD Signal #47 → created issue #345         │
└─────────────────────────────────────────────────────────┘
```

Dashboard 主要区域说明：

| 区域 | 内容 |
|------|------|
| **Repo Header** | 当前仓库名称、状态（Webhook OK）、指标（自动解决率、事件量、平均响应时间） |
| **☁ 云开发环境** | 顶部按钮，点击跳转华为云 DevStation AI Shell（`https://devstation.connect.huaweicloud.com/aishell2?utm_source=huawei&utm_medium=floating_ball&utm_campaign=space`）|
| **Agent Flow** | 5 个 Agent 横向流水线卡片，每个显示 Backend 引擎 + 状态 + **最近 2 个处理事件**（title + status，与 Pipeline 页一致，非假数据）；右上角提供开发场景下拉选择器（可切换 Agent-Skill 映射） |
| **Chat Lane** | 个人 Chat Agent 横贯连接条，点击进入 Chat 页面 |
| **Recent Activity** | 最近 24 小时事件流，点击事件可跳转到 Event Detail 详情页 |

### 2.3 Pipeline 页面

Pipeline 页面作为**每个 Issue 状态跟进的展示页面**，列出项目下所有 Issue 的进展信息：
- 每个 Issue 当前进行到哪个阶段（VOD / Issue / Coding / QA / SRE）
- 当前处理责任人（机器人 Agent 或项目组成员）
- 点击某个 Issue，上方 Agent 卡片展示该 Issue 下每个 Agent 的处理 Event 的 title 和 status

```
┌─ Top Bar ────────────────────────────────────────────┐
│  Pipeline · team/backend-service                      │
│  Active pipeline: 1  |  Completed today: 3            │
├───────────────────────────────────────────────────────┤
│                                                        │
│  Active: Issue #342 - Login timeout                    │
│  👤 当前责任人: QA Agent                                │
│                                                        │
│  ┌────────┐  ┌────────┐  ┌────────┐  ┌────────┐  ┌───────┐ │
│  │ ● VOD  │→│ ● Issue│→│ ● Code │→│ ● QA  │→│ ● SRE │ │
│  │Signal #│  │Classify│  │ PR #   │  │Review  │  │Await  │ │
│  │47>#345 │  │bug @ali│  │1278 sub│  │ing...  │  │QA OK  │ │
│  │✅Done  │  │✅Done  │  │✅Done  │  │🔄InProg│  │⏳Pend │ │
│  └────────┘  └────────┘  └────────┘  └────────┘  └───────┘ │
│                                                        │
│  ──────────────────────────────────────────────────   │
│                                                        │
│  Pipeline History（点击 Issue 切换上方 Agent 卡片）      │
│  ┌─────────┬──────────┬──────────────┬───────┬────────┐│
│  │Issue    │ Duration │ Agents       │Result │责任人   ││
│  ├─────────┼──────────┼──────────────┼───────┼────────┤│
│  │Issue#342│ 28m 12s  │ V I C Q S   │Processing│QA Agent││
│  │Issue#338│ 1h 05m   │ V I C Q S   │Released│SRE Agent││
│  │Issue#331│ 42m 18s  │ I C Q       │Rollback│Jason7602││
│  └─────────┴──────────┴──────────────┴───────┴────────┘│
└─────────────────────────────────────────────────────────┘
```

- **Issue 链接**：上方 "Active: Issue #342" 为可点击链接，点击跳转到 GitCode 对应的 Issue 页面（`https://gitcode.com/{owner}/{repo}/issues/{number}`）；下方历史表的 Issue 号为纯文本（点击行用于切换上方 Agent 卡片）。
- **责任人列**：历史表的"责任人"列显示该 Issue 当前阶段的处理责任人（机器人 Agent 名或项目组成员用户名）。

**Agent 卡片**展示每个 Agent 对该 Issue 的处理 Event：

| Agent 卡片元素 | 内容 |
|---------------|------|
| **Agent 名称** | VOD / Issue / Coding / QA / SRE |
| **事件描述** | 该 Agent 对该 Issue 最新一条 Event 的 title（如 `issue_opened`、`A2A: coding`）|
| **状态** | ✅ Done / 🔄 In Progress / ❌ Failed / ⏳ Pending |
| **当前责任人** | 当前阶段 Agent 的最新 Event 的 actor：若是机器人 → Agent 名（如 QA Agent）；若是项目成员 → 用户名（如 Jason7602）|

**未涉及任务的 Agent 框图调暗**：不涉及该 Issue（或还未到处理阶段，状态为 Pending）的 Agent 卡片，**整体色调调暗**（灰色背景 + 降饱和 + 弱化文字 + 降低透明度），以区分已处理/活跃阶段的卡片；已处理（Done）或正在处理（In Progress）的 Agent 卡片保持正常色调。

点击历史表中的不同 Issue，上方的活跃流水线状态和 Agent 卡片会对应切换为该 Issue 的处理事件：

| 选中 Issue | VOD | Issue | Coding | QA | SRE | 当前责任人 |
|-----------|-----|-------|--------|-----|-----|-----------|
| #342 Login timeout | ✅ Signal #47→#345 | ✅ Classified bug, @alice | ✅ PR #1278 | 🔄 Reviewing... | ⏳ Await QA | QA Agent |
| #338 Webhook retry | ✅ Signal #12→#338 | ✅ Enhancement, @bob | ✅ PR #1275 | ✅ 156 tests OK | ✅ Deployed v2.3.1 | SRE Agent |
| #335 Refactor auth | ✅ Signal #8→#335 | ✅ Refactor task | ✅ PR #1272 | ✅ Approved | ✅ Deployed v2.3.0 | SRE Agent |
| #331 API rate limit | ❌ Skipped | ✅ Bug confirmed | ✅ PR #1268 | ✅ 2 issues found | ✅ Rolled back | Jason7602 |

> 责任人判定：取当前阶段 Agent 最新 Event 的 `actor`，若为机器人（bot 用户名）则显示该 Agent 的显示名；若为项目组成员则显示其 GitCode 用户名。


### 2.4 DevOps Agent 详情页面（以 VOD Agent 为例）

```
┌─ Top Bar ────────────────────────────────────────────┐
│  VOD Agent · team/backend-service                     │
├───────────────────────────────────────────────────────┤
│                                                        │
│  ┌─ Agent Header ─────────────────────────────────┐  │
│  │  ♫ VOD Agent                    [Config] [Events]  │  │
│  │  Voice of Developer                            │  │
│  │  ● Active | Today: 5 | Signals: 47/Week        │  │
│  └────────────────────────────────────────────────┘  │
│                                                        │
│  ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐               │
│  │  47  │ │   8  │ │  92% │ │  2m  │               │
│  │Signals│ │Issues│ │Quality│ │  Avg │               │
│  └──────┘ └──────┘ └──────┘ └──────┘               │
│                                                        │
│  Event History (Last 24h)                             │
│  ├─ 12:05 Signal: CI too slow → Issue #345            │
│  ├─ 10:30 Analysis: 3 signals cluster                 │
│  ├─ 08:00 Weekly report: 8 key signals                │
│  └─ Yest. Collected 12 feedback entries               │
│                                                        │
│  ──────────────────────────────────────────────────   │
│                                                        │
│  Skill Configuration                                   │
│  ★ Default: skills/vod/SKILL.md                       │
│  ✎ Custom: team/backend-service (uploaded)             │
└─────────────────────────────────────────────────────────┘
```

点击 [Config] 按钮后，展开该 Agent 的配置面板：

```
┌─ Agent Config (下拉面板或在页内展开) ──────────────────┐
│                                                        │
│  Backend Engine                                        │
│  [OpenCode ▼]  ← 选择该 Agent 使用的后端引擎            │
│                                                        │
│  Skill（最多选一个，默认官方自带）                       │
│  ┌──────────────────────────────────────────────────┐  │
│  │ [Default · skills/vod/SKILL.md ▼]                │  │
│  │ Options:                                         │  │
│  │   Default · skills/vod/SKILL.md                  │  │
│  │   Custom · custom-vod-analysis                   │  │
│  │   Custom · team/backend-service uploaded skill    │  │
│  └──────────────────────────────────────────────────┘  │
│                                                        │
│  [Save] [Cancel]                                       │
│                                                        │
│  ──────────────────────────────────────────────────   │
│  🧹 Clean Session                                      │
│  清空该 Agent 的 backend_session_id。下一次事件到来     │
│  时会创建全新的 Agent 上下文（不延续历史对话）。        │
└────────────────────────────────────────────────────────┘
```

Agent Detail 页面说明：

| 区域 | 内容 |
|------|------|
| **Agent Header** | Agent 名称、状态、[Config] 和 [Events] 按钮 |
| **指标卡** | 该 Agent 的关键统计数据 |
| **Event History** | 最近 24h 的操作记录 |
| **Config 面板** | 点击展开：选择 Backend Engine、勾选启用的 Skills |
| **🧹 Clean Session** | 清空该 Agent 会话的 `backend_session_id`（置空），下次事件到来时创建全新会话上下文，不延续历史对话 |

Clean Session 行为说明：

- 每个 DevOps Agent（issue / coding / qa / sre / vod）均可清理自己的会话
- 清理动作：`UPDATE devops_agent_session SET backend_session_id='' WHERE repo_key=? AND agent_type=?`
- 效果：下一次事件触发该 Agent 时，由于 `backend_session_id` 为空，Agent 会创建新的后端会话（`oc_session_id` 全新），不继承此前的多轮上下文
- 适用场景：Agent 上下文被污染/走偏、需要重新开始、或排查 Agent 行为异常

### 2.5 Event Detail 页面

点击 Agent 详情页中的任意 Event，进入该 Event 的详细视图。左栏展示事件上下文和 Agent 执行日志，右栏展示事件元信息和反馈入口：

```
┌─ Top Bar ────────────────────────────────────────────┐
│  Event Detail · VOD Agent · team/backend-service      │
├───────────────────────────────────────────────────────┤
│  [← 返回 VOD Agent]                                   │
│                                                        │
│  ┌─ Context & Logs ───────────┐  ┌─ Event Info ────┐ │
│  │                              │                    │ │
│  │ ## Event Context             │  Backend: OpenCode │ │
│  │ Trigger: pull_request_opened │  Skill: skills/vod/│ │
│  │ Repository: team/backend-srv │  Issue: #345       │ │
│  │ Branch: main                 │  Duration: 12s     │ │
│  │                              │  Status: ✅ Success │ │
│  │ ## Agent Execution Log       │                    │ │
│  │ [12:05:01] Agent started     │  ───────────────── │ │
│  │ [12:05:03] Analyzing signals │                    │ │
│  │ [12:05:08] Cluster: 3 similar│  💬 Feedback       │ │
│  │ [12:05:11] Creating issue... │  Found an issue?   │ │
│  │ [12:05:13] Issue #345 created│  ┌──────────────┐ │ │
│  │ [12:05:13] Agent completed   │  │Title:        │ │ │
│  │                              │  │Description:  │ │ │
│  │ ──────────────────────────── │  │              │ │ │
│  │ 💡 Have a question about     │  └──────────────┘ │ │
│  │ these logs? Submit to →      │  [Submit Issue to  │ │
│  │ DevOps Bot project issues    │   DevOps Bot]      │ │
│  └──────────────────────────────┘  └────────────────┘ │
└─────────────────────────────────────────────────────────┘
```

Event Detail 页面说明：

| 区域 | 内容 |
|------|------|
| **Context & Logs（左栏）** | 事件触发上下文（仓库、分支、触发类型）+ Agent 执行过程日志 |
| **Event Info（右栏上）** | 事件元信息：Backend 引擎、Skill、所属 Issue、执行耗时、状态 |
| **Feedback（右栏下）** | 对日志/执行结果有疑问时，提交 Issue 到 DevOps Bot 项目 |

按 DevOps Agent 分类的技能库页面，展示官方自带技能和用户上传的自定义技能，顶部提供三个页签切换：

| 页签 | 内容 | 操作 |
|------|------|------|
| **官方技能** | 系统内置的官方技能（scope=official） | 只读（View） |
| **仓库技能** | 绑定到当前仓库的技能（scope=repo，owner_id=仓库） | View / Delete / **＋新增技能** |
| **个人技能** | 当前用户上传的技能（scope=user，owner_id=用户） | View / Delete / **＋新增技能** |

```
┌─ Top Bar ────────────────────────────────────────────┐
│  DevOps Skills                                       │
│  [🔍 搜索技能...]  [官方技能] [仓库技能] [个人技能] [＋新增技能]│
├───────────────────────────────────────────────────────┤
│  VOD Agent Skills                                     │
│  ┌──────────────┐  ┌──────────────┐                 │
│  │ ♫ 声音分析   │  │ ♫ 反馈收集   │                 │
│  │ 官方技能     │  │ 个人技能     │                 │
│  │ 收集、聚类、 │  │ 从 IM 和 PR  │                 │
│  │ 提炼开发者声 │  │ 收集反馈信息 │                 │
│  │ 音中的共性问题│  │              │                 │
│  │ [View] [Edit]│  │ [View] [Edit]│                 │
│  └──────────────┘  └──────────────┘                 │
│                                                        │
│  Issue Agent Skills                                    │
│  ┌──────────────┐  ┌──────────────┐                 │
│  │ ⚠ Issue 管理 │  │ ⚠ 自定义Issue│                 │
│  │ 官方技能     │  │ 用户上传     │                 │
│  │ 分类、标签、 │  │ 包含特定项目 │                 │
│  │ 指派、A2A触发│  │ 的 Issue 处理 │                 │
│  │ [View] [Edit]│  │ [View] [Edit]│                 │
│  └──────────────┘  └──────────────┘                 │
│                                                        │
│  Coding Agent Skills                                   │
│  ┌──────────────┐                                    │
│  │ ⚙ 代码实现   │                                    │
│  │ 官方技能     │                                    │
│  │ 需求分析→方案 │                                    │
│  │ 设计→编码→PR │                                    │
│  │ [View] [Edit]│                                    │
│  └──────────────┘                                    │
│                                                        │
│  QA Agent Skills                                       │
│  ┌──────────────┐  ┌──────────────┐                 │
│  │ ✔ PR Review  │  │ ✔ 自定义审查 │                 │
│  │ 官方技能     │  │ 用户上传     │                 │
│  │ Code Review、 │  │ 特定项目代码 │                 │
│  │ 自动化测试、 │  │ 审查规则     │                 │
│  │ 功能验证     │  │              │                 │
│  │ [View] [Edit]│  │ [View] [Edit]│                 │
│  └──────────────┘  └──────────────┘                 │
│                                                        │
│  SRE Agent Skills                                      │
│  ┌──────────────┐  ┌──────────────┐                 │
│  │ ♡ 部署发布   │  │ ♡ 合规检查   │                 │
│  │ 官方技能     │  │ 官方技能     │                 │
│  │ CHANGELOG→构│  │ 部署前安全与 │                 │
│  │ 建→部署→监控 │  │ 合规门禁     │                 │
│  │ [View] [Edit]│  │ [View] [Edit]│                 │
│  └──────────────┘  └──────────────┘                 │
│                                                        │
└─────────────────────────────────────────────────────────┘
```

每个技能卡片包含：

| 元素 | 说明 |
|------|------|
| **图标 + 名称** | 所属 Agent 的图标 + 技能名称 |
| **类型标签** | 官方技能 / 仓库技能 / 个人技能（对应三个页签） |
| **描述** | 技能的简要功能说明 |
| **操作按钮** | [View] 查看详情、[Delete] 删除（仅非官方技能）；仓库/个人页签提供 [＋新增技能] 上传 ZIP |
| **分类** | 按 VOD / Issue / Coding / QA / SRE / Chat 六个 Agent 分区展示 |

点击 [＋新增技能] 弹出上传弹窗，**必须手动选择技能类型（所属 Agent）**，后端校验为合法值（vod / issue / coding / qa / sre / chat）：

```
┌─ 上传技能 (ZIP) ────────────────────────────────────────┐
│  类型:     [● 个人技能]  [○ 仓库技能]                   │
│  仓库:     owner/repo                      (仓库技能时)  │
│  技能类型* [VOD Agent (♫ 声音分析)      ▾]              │
│            ├─ VOD Agent   (♫ 声音分析)                 │
│            ├─ Issue Agent (⚠ 需求管理)                 │
│            ├─ Coding Agent(⚙ 代码实现)                 │
│            ├─ QA Agent    (✔ 审查验证)                 │
│            ├─ SRE Agent   (♡ 构建部署)                 │
│            └─ Chat Agent  (☎ 对话助手)                 │
│  选择 ZIP 文件（目录名 = 技能名，内含 SKILL.md）：        │
│  [📁 选择文件]  skill.zip                               │
│  格式要求：[📄 下载技能模板 ZIP]                         │
│  上传中...  ▓▓▓▓▓▓▓▓░░░░ 65%                           │
│                              [取消]  [上传]              │
└─────────────────────────────────────────────────────────┘
```

- **技能类型必填**：未选择或传入非法值时，后端返回 400 拒绝上传。
- **上传进度**：选择文件并点击上传后，显示真实上传进度条（XHR 监听 onprogress），完成后自动关闭。
- **格式要求**：ZIP 内第一个目录为技能目录（目录名 = 技能名，须与 SKILL.md 的 `name` 字段一致），SKILL.md 需含 YAML Front Matter（name / version / description），可点击「下载技能模板 ZIP」获取示例。不强制特定正文章节，与官方技能格式一致。
- **校验失败提示**：后端返回结构化的校验错误列表，前端多行展示（如 name 不一致、description 为空、可执行文件等）。
- 上传成功后，技能按所选 Agent 类型分组展示在对应分区下（如选择 SRE Agent 则显示在"SRE Agent Skills"）。
- 选择"仓库技能"时需指定当前仓库（取自页面当前仓库），存储为 `skills/repo/{repo_key}/{skill_name}`。

Chat Agent 是每个用户专属的个人对话助手，包含聊天区、Agent 连接状态、以及自身的配置面板（Backend 选择、IM 集成）。

```
┌─ Top Bar ────────────────────────────────────────────┐
│  Chat Agent · Personal Assistant                      │
├───────────────────────────────────────────────────────┤
│                                                        │
│  ┌────────── Main Chat ──────────┐  ┌── Config ─────┐ │
│  │                                │  │ ★ Agents      │ │
│  │  Hello! I'm your personal     │  │ ♫ VOD  47     │ │
│  │  DevOps assistant.            │  │ ⚠ Issue 3     │ │
│  │                                │  │ ⚙ Coding 1    │ │
│  │  ┌───────────────────────────┐│  │ ✔ QA    2     │ │
│  │  │Check status of issue #342 ││  │ ♡ SRE v2.3    │ │
│  │  └───────────────────────────┘│  │               │ │
│  │                                │  │ ★ Backend     │ │
│  │  ✅ Issue #342 is with QA     │  │ [Default ▼]   │ │
│  │  Agent (PR #1278 under review)│  │ URL: localhost │ │
│  │  ETA: ~15 min.                │  │ Key: ********  │ │
│  │                                │  │               │ │
│  │  ┌─[Type your message...]───┐ │  │ ★ IM          │ │
│  │  │                    [Send]│ │  │ Feishu        │ │
│  │  └──────────────────────────┘ │  │ App ID:       │ │
│  └────────────────────────────────┘  │ cli_xxxxxxxx  │ │
│                                       │ App Secret:   │ │
│                                       │ ************  │ │
│                                       │ 💡 Scan QR to | │
│  └────────────────────────────────┘  │ ○ WeCom       │ │
│                                       │ ○ DingTalk    │ │
│                                       │ [Custom IM +] │ │
│                                       └──────────────┘ │
└─────────────────────────────────────────────────────────┘
```

### 2.7 Chat Agent 页面

Chat Agent 是三栏布局：左侧对话列表、中间对话区、右侧固定配置面板。

```
┌─ Top Bar ───────────────────────────────────────────────────────┐
│  Chat Agent · Personal Assistant                                 │
├──────────────────────────────────────────────────────────────────┤
│                                                                   │
│  ┌── Conversations ─┐  ┌──────── Main Chat ─────────┐  ┌ Config┐│
│  │ ＋ 新对话        │  │                            │  │Backend││
│  │━━━━━━━━━━━━━━━━━│  │  Hello! I'm your assistant │  │[opencode││
│  │ 新对话           │  │                            │  │ ▼]     ││
│  │ 2026-07-28 17:30 │  │  ┌───────────────────────┐│  │        ││
│  │                  │  │  │Check issue #342       ││  │ IM     ││
│  │ 需求讨论         │  │  └───────────────────────┘│  │● Feishu││
│  │ 2026-07-27 10:15 │  │                            │  │ App ID ││
│  │                  │  │  ✅ Issue #342 is with QA ││  │[cli...]││
│  │ 问题反馈         │  │  Agent, ETA ~15 min       ││  │Secret  ││
│  │ 2026-07-26 09:00 │  │                            │  │[******]││
│  │                  │  │  ┌─[Type message...]───┐  │  │ [Save] ││
│  │                  │  │  │                [Send]│  │  │        ││
│  │                  │  │  └──────────────────────┘  │  │ Agents ││
│  │                  │  │                            │  │ ● VOD 47││
│  └──────────────────┘  └────────────────────────────┘  │ ○ Iss 3││
│                                                        │ ○ Cod 1││
│                                                        │ ○ QA 2 ││
│                                                        │ ○ SRE v││
│                                                        └────────┘│
└──────────────────────────────────────────────────────────────────┘
```

| 区域 | 内容 |
|------|------|
| **Conversations（左栏）** | 用户所有对话列表，「＋ 新对话」按钮，点击切换 |
| **Main Chat（中栏）** | 消息历史 + 输入框 + Send 按钮 |
| **Config（右栏）** | Backend 引擎选择、Feishu IM 配置（App ID/Secret）、5 个 DevOps Agent 事件统计 |

### 2.8 Repositories 页面

```
┌─ Top Bar ────────────────────────────────────────────┐
│  Repositories                    [↻ Sync from GitCode]│
├───────────────────────────────────────────────────────┤
│                                                        │
│  Hosted (3)                                           │
│  ┌────────────────────────────────────────────────┐  │
│  │ ■ team/backend-service        Webhook OK  Active│  │
│  │ ■ team/web-frontend           Webhook OK  Active│  │
│  │ ■ team/mobile-app             Webhook OK  Joined│  │
│  └────────────────────────────────────────────────┘  │
│                                                        │
│  Available to Join (5)                                 │
│  ┌────────────────────────────────────────────────┐  │
│  │ Repository        Role     Description       Action│  │
│  │ org/docs-site     Member   Documentation     Join │  │
│  │ org/devops-toolkit Member   DevOps Tools      Join │  │
│  │ team/notification Member   Notification      Join │  │
│  │ org/data-pipeline  Admin   ETL pipeline    Host Now│  │
│  │ team/analytics    Maintainer Analytics    Host Now│  │
│  └────────────────────────────────────────────────┘  │
│  │ org/data-pipeline  Admin   ETL pipeline    Join │  │
│  │ team/analytics    Maintain Analytics       Join │  │
│  └────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────┘
```

### 2.9 General Settings 页面

仓库级别的通用配置，包含凭据、通知偏好：

```
┌─ Top Bar ────────────────────────────────────────────┐
│  General Settings · team/backend-service              │
├───────────────────────────────────────────────────────┤
│                                                        │
│  [Credentials] [Notifications]                        │
│                                                        │
│  ┌─ GitCode API（OAuth 登录自动获取）──────────────┐  │
│  │ API Token  [ghp_zhangwei_token_xxxxxxxxxxxx]     │  │
│  │ Account    zhangwei · 登录时自动填充               │  │
│  └────────────────────────────────────────────────┘  │
│                                                        │
│  ┌─ Huawei Cloud ──────────────────────────────────┐  │
│  │ Access Key [AK-demo-access-key-xxxx]             │  │
│  │ Secret Key [SK-demo-secret-key-xxxx]             │  │
│  └────────────────────────────────────────────────┘  │
│                                                        │
│  ┌─ Notifications ─────────────────────────────────┐  │
│  │ ☑ Issue events  ☑ PR events  ☐ Build failures   │  │
│  └────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────┘
```

> **用户个人 Token 说明：** 此处的 GitCode API Token 是**用户的个人凭证**，通过 OAuth 登录自动获取并填充。该 Token 由 **Chat Agent** 代表你在 GitCode 上执行操作（提交 Issue、查询状态、审批流程等）。DevOps Agents 使用独立的 **Bot Token**，不受此页影响。

### 2.10 GitCode Repo 页面

仓库的 GitCode 专属配置，包含 API Token、Webhook 和机器人设置：

```
┌─ Top Bar ────────────────────────────────────────────┐
│  GitCode Repo · team/backend-service                  │
├───────────────────────────────────────────────────────┤
│                                                        │
│  ┌─ DevOps Bot Token ───────────────────────────────┐  │
│  │ Bot Token  [ghp_devopsbot_xxxxxxxxxxxxxxxxxxxx]    │  │
│  │ Scope      read_repo, write_repo, manage_webhook    │  │
│  └────────────────────────────────────────────────┘  │
│                                                        │
│  ┌─ Webhook Bot ───────────────────────────────────┐  │
│  │ Webhook Token  [xxxxxxxxxxxxxxxxxxxxxxxxxxxx]    │  │
│  │ Webhook URL    http://203.0.113.2:8080/...     │  │
│  │ Bot Username   [devops-bot]                      │  │
│  └────────────────────────────────────────────────┘  │
│                                                        │
│  ┌─ 解除托管 ──────────────────────────────────────┐  │
│  │  点击「解除托管」后：                              │  │
│  │  1. 删除 GitCode Webhook                          │  │
│  │  2. 从托管列表中移除                              │  │
│  │  3. 跳转到仓库选择页                              │  │
│  │  [解除托管]  ← 红色按钮，含确认提示                │  │
│  └────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────┘
```

> **DevOps Bot Token 说明：** 此 Token 是 DevOps 系统的**全局凭证**，所有 DevOps Agents（VOD/Issue/Coding/QA/SRE）在执行 GitCode 操作时自动使用此 Token。Bot Token 需要有仓库的读写权限（`read_repo, write_repo, manage_webhook`），建议使用单独的 Bot 账号创建，与用户个人 Token 隔离。  
> **回退规则：** 若当前仓库未配置 Bot Token，系统自动使用 `default` 账户下的默认 Token。管理员可在 GitCode Repo 页中编辑 `default` 条目来配置系统默认 Token，确保新仓库零配置即可运行。

### 2.11 仓库列表缓存与同步

**目的：** 避免每次访问仓库选择页或管理页时都调用 GitCode API，减少加载时间。

**设计方案：**

```
用户 GitCode Token 首次拉取仓库列表
        │
        ▼
┌──────────────────────────────────────────────┐
│              user_repos 表                    │
│──────────────────────────────────────────────│
│ user_id │ full_name │ owner │ name           │
│ role    │ description │ is_admin            │
│ created_at (缓存时间戳)                      │
└──────────────────────────────────────────────┘
        │
        ├── GET /api/repos/member → 从 user_repos 表读取
        │    如果表为空 → 自动调 GitCode API 拉取并写入
        │
        └── POST /api/repos/sync → 手动强制从 GitCode 刷新
              清空该用户的 user_repos → 重新拉取 GitCode API → 写入
```

**页面交互：**

仓库管理页（侧边栏「＋ 新增仓库托管」）顶部增加 **[↻ Sync from GitCode]** 按钮：

```
┌─ Repositories ───────────────────────────────────┐
│  Repositories                   [↻ Sync from GitCode]  │
├────────────────────────────────────────────────────┤
│  Hosted (n)  ← 从 managed_repos 读取               │
│  ...                                               │
│  Available to Join (n)  ← 从 user_repos 表读取      │
│  ...                                               │
└────────────────────────────────────────────────────┘
```

**缓存刷新时机：**

| 操作 | 缓存行为 |
|------|---------|
| 首次登录/访问选择页 | user_repos 为空 → 自动从 GitCode 拉取并缓存 |
| 点击「Sync from GitCode」 | 强制重新拉取，更新缓存 |
| 加入仓库 | 仅更新 managed_repos，不影响 user_repos |
| 解除托管 | 仅更新 managed_repos，不影响 user_repos |
| 自动过期 | 无过期时间，由用户手动刷新 |

**API 接口：**

```python
# 获取可加入仓库列表（从 user_repos 表读取，自动按 is_admin + managed_repos 过滤）
GET /api/repos/member → {"repos": [...]}

# 手动从 GitCode 同步
POST /api/repos/sync → 调 GitCode API 拉取 → 写入 user_repos → 返回 {"repos": [...]}
```

### 2.12 Backend Runtimes 页面

管理和检测本地已安装的 Agent 引擎（OpenCode、CodeArts、Hermes 等），检查运行状态和配置连接凭据。此页面不与具体的 DevOps Agent 绑定，属于系统级 Agent 引擎管理：

```
┌─ Top Bar ────────────────────────────────────────────┐
│  Backend Runtimes                                      │
├───────────────────────────────────────────────────────┤
│                                                        │
│  ┌─ OpenCode ──────────────────────────────────────┐  │
│  │  ● Running         Version: 1.5.2               │  │
│  │  Installed ✓       Path: /usr/local/bin/opencode│  │
│  │  [Test Connection] [View Logs]                  │  │
│  └────────────────────────────────────────────────┘  │
│                                                        │
│  ┌─ CodeArts ──────────────────────────────────────┐  │
│  │  ● Running         Version: 2.1.0               │  │
│  │  Installed ✓       Path: /usr/local/bin/codearts │  │
│  │  AK  [AK-demo-access-key-xxxx]                  │  │
│  │  SK  [SK-demo-secret-key-xxxx]                  │  │
│  │  [Test Connection] [Save AK/SK]                  │  │
│  └────────────────────────────────────────────────┘  │
│                                                        │
│  ┌─ Hermes ────────────────────────────────────────┐  │
│  │  ● Running         Version: 0.9.1               │  │
│  │  Installed ✓       Path: /usr/local/bin/hermes  │  │
│  │  [Test Connection] [View Logs]                  │  │
│  └────────────────────────────────────────────────┘  │
│                                                        │
│  ┌─ Cursor ────────────────────────────────────────┐  │
│  │  ○ Not Installed                                │  │
│  │  [Install Guide]                                │  │
│  └────────────────────────────────────────────────┘  │
│                                                        │
│  [⟳ Refresh All]                                      │
└─────────────────────────────────────────────────────────┘
```

Backend Runtimes 页面说明：

| 元素 | 说明 |
|------|------|
| **引擎列表** | 每个 Agent 引擎一行，展示安装状态、版本、运行状态 |
| **安装检测** | 自动检测本地是否安装该引擎，显示路径和版本 |
| **运行状态** | ● Running / ○ Stopped / ● Error |
| **连接测试** | 点击 Test Connection 验证引擎是否可正常工作 |
| **CodeArts 特殊配置** | CodeArts 需要 AK/SK 连接华为云，提供独立配置区 |
| **刷新** | Refresh All 重新检测所有引擎状态 |

### 2.12 日常使用

```
日常开发流程：
  │
  ├── VOD Agent 收集声音
  │     ├── 从 Issue、PR 评论、IM 等渠道收集反馈
  │     ├── 聚类分析共性问题
  │     └── 创建 Issue 提交给 Issue Agent
  │
  ├── Issue Agent 处理
  │     ├── 分类打标签、提炼为可开发的需求
  │     ├── 指派负责人
  │     └── 触发 Coding Agent
  │
  ├── Coding Agent 编码实现
  │     ├── 分析需求 → 设计方案 → 编写代码
  │     ├── 自我审查 → 提交 Pull Request
  │     └── 触发 QA Agent
  │
  ├── QA Agent 审查验证
  │     ├── Code Review（代码规范、安全扫描、架构检查）
  │     ├── 自动化测试（单元测试、集成测试）
  │     ├── 功能验证（端到端验证）
  │     └── 通过 → 合并 PR，触发 SRE Agent / 不通过 → 回 Coding Agent
  │
  ├── SRE Agent 发布运维
  │     ├── 变更日志生成（CHANGELOG）
  │     ├── 构建打包、发布部署
  │     ├── 部署监控与健康检查
  │     ├── 故障排查与修复建议
  │     ├── 合规检查与安全扫描
  │     └── 构建失败诊断
  │
  └── Chat Agent（每个用户个人专属）
        ├── 与上述任意 Agent 对话交互
        ├── 查询工作流状态、事件详情
        ├── 提交 Issue、审批流程
        ├── 对接飞书、企业微信等 IM 平台
        └── 每个用户拥有自己的 Chat Agent 实例
```

---

## 三、5 + 2 Agent 工作流详解

### 3.1 VOD Agent（Voice of Developer）

| 属性 | 说明 |
|------|------|
| **职责** | 开发者声音收集、分析、价值提炼 |
| **触发方式** | 后台定期 / 事件驱动 |
| **关键能力** | 声音收集（来源待扩展）、共性问题分析、价值提炼、自动创建 Issue |
| **工作流** | 收集开发者反馈 → 聚类分析 → 识别共性问题 → 提炼价值信息 → 创建 Issue 提交 Issue Agent |
| **工作流位置** | 工作流最上游，触发后续所有 Agent 的起点 |
| **预制 Skill** | 待开发 |

> VOD Agent 的数据来源非常广泛（Issue 讨论、IM 消息、代码 Review 评论、用户反馈等），未来将成为 Issue 的主要提交来源，形成"声音收集 → 自动创建 Issue → 开发解决"的闭环。

### 3.2 Issue Agent

| 属性 | 说明 |
|------|------|
| **职责** | Issue 全生命周期管理 |
| **触发方式** | VOD Agent 提交（主要来源）/ `issue_opened` / `issue_comment` / 用户主动 |
| **关键能力** | Issue 分类与打标签、责任人指派、需求提炼、A2A 触发 Coding Agent |
| **工作流** | Issue 到达 → 分析内容 → 分类打标签 → 提炼为可开发需求 → A2A 通知 Coding Agent |
| **预制 Skill** | `skills/issue/` |

> 未来绝大部分 Issue 将由 VOD Agent 自动提交，VOD Agent 从开发者声音中提炼共性问题，直接生成 Issue 提交 Issue Agent 处理，形成 "声音收集 → 自动创建 Issue → 开发解决" 的闭环。

### 3.3 Coding Agent

| 属性 | 说明 |
|------|------|
| **职责** | 代码实现全流程 |
| **触发方式** | A2A 触发（来自 Issue Agent / QA Agent 回退） |
| **关键能力** | 需求分析→技术方案→编码→自测→提交 PR |
| **工作流** | 收到 A2A 请求 → 理解需求 → 设计方案 → 编写实现代码 → 自我审查 → 编写测试 → 提交 PR → A2A 通知 QA Agent |
| **预制 Skill** | `skills/implementation/` |
| **云端能力** | 可配置华为云 CodeArts 作为云端开发环境 |

### 3.4 QA Agent

| 属性 | 说明 |
|------|------|
| **职责** | 代码质量保障 |
| **触发方式** | `pull_request_opened/updated` / A2A（来自 Coding Agent） |
| **关键能力** | Code Review、自动化测试、功能验证 |
| **工作流** | PR 提交/更新 → 代码规范检查 → 安全扫描 → 架构评审 → 自动化测试执行 → 功能验证 → 通过/不通过 |
| **不通过时** | A2A 回 Coding Agent，附评审意见和修复建议 |
| **预制 Skill** | `skills/pr/`（扩展中） |

### 3.5 SRE Agent（含 Release 能力）

| 属性 | 说明 |
|------|------|
| **职责** | 发布管理 + 应用运维（Release 与 SRE 合并） |
| **触发方式** | `pull_request_merged` / `deploy_start/failure` / `build_failure` / 定期巡检 |
| **关键能力** | 变更日志生成、构建打包、发布部署、部署监控、故障诊断、合规检查、构建调试 |
| **工作流** | PR 合并 → CHANGELOG → 构建部署 → 健康检查 → 持续监控 → 故障响应 |
| **预制 Skill** | `skills/changelog-gen/`、`skills/cloud-deploy/`、`skills/deploy-guard/`、`skills/build-debug/`、`skills/incident-triage/` |

### 3.6 Agent 对比总览

| Agent | 工作流位置 | 输入 | 输出 | 触发下游 |
|-------|-----------|------|------|---------|
| **VOD Agent** | 最上游 | 开发者声音（Issue/PR/IM） | 分析报告、Issue | Issue Agent |
| **Issue Agent** | 上游 | Issue 事件、VOD 提交 | 分类标签、开发需求 | Coding Agent |
| **Coding Agent** | 中游 | 开发需求、修复 PR | 代码实现、PR | QA Agent |
| **QA Agent** | 中下游 | PR 提交/更新 | 审查结论、测试报告 | Coding(回退) / SRE(通过) |
| **SRE Agent** | 下游 | PR 合并、部署事件 | 版本发布、运维报告 | — |

### 3.7 Chat Agent（个人专属）

| 属性 | 说明 |
|------|------|
| **职责** | 每个用户的个人对话助手，与整个工作流交互 |
| **触发方式** | 用户消息（IM / Web 面板） |
| **关键能力** | 对话理解、查询工作流状态、提交 Issue、审批流程、跨 Agent 交互 |
| **工作流** | 用户发送消息 → 理解意图 → 与对应 Agent 交互 → 返回结果 |
| **特点** | 每个用户拥有自己的 Chat Agent 实例，可接入自有 Backend |
| **与工作流关系** | 横向连接 VOD / Issue / Coding / QA / SRE 所有 Agent |
| **Backend 接入** | **默认模式**：使用平台内置的 Chat Agent |
|  | **自有模式**：用户接入自己的 Agent Backend（OpenClaw、Hermes 等） |
|  | 自有 Chat Agent 可对接飞书、企微等 IM，作为个人专用对话引擎 |
| **预制 Skill** | `skills/chat/`、`skills/im-notify/` |

> Chat Agent 不完全属于 DevOps 工程循环（Engineer Loop）的关键一环，它是每个用户接入工作流的个人对话入口。核心工作流仍为 VOD → Issue → Coding → QA → SRE。

---

## 四、Agent 间协作（A2A 端到端工作流）

### 4.1 完整端到端工作流

```
VOD Agent（声音收集 — 未来主要的 Issue 来源）
  │
  ├── 收集开发者反馈（来源：Issue、PR 评论、IM 对话、用户反馈等）
  ├── 聚类分析，识别共性问题
  └── 创建 Issue → A2A 提交 Issue Agent
        │
        ▼
      Issue Agent
        ├── 分析 Issue 内容（来自 VOD 或用户提交）
        ├── 分类、打标签、提炼需求
        ├── 指派责任人
        └── A2A 触发 Coding Agent
              │
              ▼
            Coding Agent
              ├── 理解需求
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
                    ├── 通过 → 合并 PR → A2A 触发 SRE Agent
                    │         │
                    │         ▼
                    │       SRE Agent
                    │         ├── 生成 CHANGELOG
                    │         ├── 构建打包
                    │         ├── 发布部署
                    │         ├── 健康检查
                    │         └── 持续监控
                    │
                    └── 不通过 → A2A 回 Coding Agent
                                 附评审意见和修复建议 → 修复后重新提交
```

### 4.2 Chat Agent 与工作流的交互

```
Chat Agent（个人专属，每个用户一个）
  │
  ├── 与 VOD Agent 交互
  │     └── "最近有什么重要的开发者反馈？" → 获取分析报告
  │
  ├── 与 Issue Agent 交互
  │     ├── "帮我创建一个 Issue" → 提交 Issue
  │     └── "Issue #342 处理到哪了？" → 查询状态
  │
  ├── 与 Coding Agent 交互
  │     └── "PR #1278 的改动范围是什么？" → 获取变更摘要
  │
  ├── 与 QA Agent 交互
  │     └── "帮我审批 PR #1278" → 执行审批
  │
  └── 与 SRE Agent 交互
        ├── "线上服务正常吗？" → 健康检查报告
        └── "部署一下 v2.3.1" → 触发部署

每个用户有独立的 Chat Agent 实例
Chat Agent 是用户与整个 Agentic DevOps 工作流的统一入口
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
| **开发场景** | 选择场景（通用开发/云原生开发/IaC开发/Agent开发/Skill开发），自动刷新所有 Agent 的 Skill 映射 | Dashboard → Agent Workflow 右上角场景选择器 |
| **已加入 Repo 列表** | 用户参与的所有仓库，展示角色（成员/维护者/管理员） | Repositories 页面 |
| **已托管 Repo 列表** | 被 Grape 监听的项目，显示 Webhook 状态 | Repositories 页面 |
| **Dashboard 仓库切换** | 侧边栏下拉选择已托管仓库，Dashboard 跟随切换 | 侧边栏 Repo Selector |
| **Channel 配置** | Chat Agent 选择（默认/自有），IM 绑定 | Settings → Channel |
| **云平台凭据** | 华为云 AK/SK，为 Coding Agent 提供云端开发环境 | Settings → Credentials |
| **GitCode Token** | 用户 API Token，用于拉取仓库列表和身份认证 | Settings → Credentials |
| **Agent Skill 自定义** | 项目维护者/管理员可上传自定义 SKILL.md 覆盖预置 Skill | Settings → Preferences |
| **Repo-Skill 映射** | 指定仓库的特定事件使用自定义 Skill | Settings → Preferences |
| **通知偏好** | 哪些事件需要推送到 IM | Settings → Preferences |

---

## 六、技能系统（Skills）

### 6.1 预制技能

| Agent | 预制 Skill 目录 | 说明 |
|-------|----------------|------|
| VOD Agent | 待开发 | 声音收集、分析、提炼 |
| Issue Agent | `skills/issue/` | Issue 分类、标签、指派、A2A 触发 |
| Coding Agent | `skills/implementation/` | 需求分析→编码→自测→PR |
| QA Agent | `skills/pr/`（扩展中） | Code Review、测试验证 |
| SRE Agent | `skills/changelog-gen/`、`skills/cloud-deploy/`、`skills/deploy-guard/`、`skills/build-debug/`、`skills/incident-triage/` | 发布、运维、监控、故障 |
| Chat Agent | `skills/chat/`、`skills/im-notify/` | 对话、通知推送 |

### 6.2 自定义技能

- 项目的维护者和管理员可以上传自定义 Skill
- 自定义 Skill 通过 `repo_skill_mapping` 绑定到特定仓库和事件类型
- 自定义 Skill 的优先级高于系统预制 Skill

### 6.3 事件处理流程

```
事件到达 webhook:8080
  → 验证 X-GitCode-Token（从 managed_repos 按 repo 查找预期 token）
  → 提取 repo + 事件类型
  → run_pipeline():
     ├── 1. 归一化（adapter.py — gitcode/github 事件转统一格式）
     ├── 2. 反循环过滤（跳过 Bot 自身事件）
     ├── 3. 路由（routing.py — 查询 agent_type_config.subscribe_events）
     ├── 4. 查找 Agent 会话（agent_sessions 表）
     │     ├── 有配置 → 使用用户配置的 backend + skill
     │     └── 无配置 → 从 agent-skill-mapping.json 读取默认 skill
     └── 5. 存储事件（events 表）→ 返回结果
```

场景选择器通过 `POST /api/scenes/apply` 为仓库生成 `agent_sessions` 行，替换默认映射。

---

## 七、系统架构

```
┌─────────────────────────────────────────────────────────┐
│                    GitCode                              │
│             Webhook / API / OAuth                       │
└────────────────────┬────────────────────────────────────┘
                     │
          ┌──────────┴──────────┐
          ▼                     ▼
┌──────────────────┐  ┌──────────────────────┐
│  Port 80          │  │  Port 8080            │
│  src.api.main     │  │  src.api.run_webhook  │
│  SPA + API + 用户   │  │  Webhook + A2A 端点   │
│  Feishu WS        │  │  仅 webhook 路由      │
└────────┬─────────┘  └──────────┬───────────┘
         │                       │
         └───────────┬───────────┘
                     ▼
┌─────────────────────────────────────────────────────────┐
│              事件处理管道（event_pipeline.py）            │
│                                                         │
│    ├── 1. 事件归一化（VCS Adapter）                       │
│    ├── 2. 反循环过滤（跳过 Bot 自身事件）                  │
│    ├── 3. 路由解析（查询 agent_type_config.subscribe_events）│
│    ├── 4. Agent 会话查找（agent_sessions 表）              │
│    │     ├── 有配置 → 使用用户配置                          │
│    │     └── 无配置 → 从 agent-skill-mapping.json 取默认    │
│    └── 5. 事件存储（events 表）                            │
└────────────────────┬────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────┐
│            Agent 执行器（子进程）                          │
│                                                         │
│  agent/executor.py                                       │
│    ├── Backend 选择（OpenCode / CodeArts）                │
│    ├── 环境变量注入（Repo、Token、Skill 等）               │
│    └── NDJSON 流式输出                                    │
└─────────────────────────────────────────────────────────┘
```

### 7.1 端口与协议

| 端口 | 用途 | 协议 |
|------|------|------|
| **80** | SPA + API + Feishu WS（`uvicorn src.api.main:app`） | HTTP |
| **8080** | Webhook 端点（`uvicorn src.api.run_webhook:app`，仅 webhook + A2A） | HTTP |
| **WebSocket** | 飞书 IM 长连接 | WSS |

### 7.2 数据存储

| 存储 | 内容 | 说明 |
|------|------|------|
| `data/sessions.db` | 用户、账户、Session、VM、Skill、映射 | SQLite |
| `data/workspaces/` | Agent 执行历史和工作目录 | 文件系统 |
| `skills/` | 预制 Skill + 自定义 Skill | 文件系统 |
| `configs/` | 事件路由、Agent 路由、场景映射（agent-skill-mapping.json） | JSON 文件 |

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
| **Chat Agent 隔离** | 每个用户拥有独立的 Chat Agent 实例 |
| **自定义 Skill 校验** | 上传时校验格式、安全限制 |

---

## 九、后续规划

### 短期（当前迭代）

- [ ] 用户首次登录选 Repo 流程
- [ ] 仓库托管流程优化
- [ ] Dashboard 按仓库维度隔离
- [ ] Chat Agent 自有 Backend 接入能力
- [ ] QA Agent 自动化测试集成

### 中期

- [ ] VOD Agent 声音收集技能开发
- [ ] SRE Agent 统一发布+运维流水线
- [ ] Chat Agent 多 IM 渠道接入（企业微信、钉钉）
- [ ] Agent 可视化 Pipeline 页面

### 长期

- [ ] Chat Agent 个人化：每个用户独立实例
- [ ] 自定义 Skill 市场/分享
- [ ] Agent 可视化编排
- [ ] 多租户隔离增强
