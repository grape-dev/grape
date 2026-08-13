# Grape — Multi-Agent DevOps 使用场景与工作流设计（new-version2）

> 版本：v2.0（对齐 `new-version2` 分支最新实现）
> 日期：2026-08-10
> 说明：本文基于 `new-version2` 分支的**实际代码实现**重新梳理（前端 `src/api/static/`、后端 `src/api/routes/`、`event_pipeline.py`、`runtime/service.py` 等），对 v1 文档（`docs/new-version/user-workflow-overview.md`）做整体更新。只描述现状，不含未落地设计。

---

## 一、系统概述

Grape 是一个多 Agent 协作的 DevOps 平台，致力于实现 Multi-Agent DevOps。

**核心理念：**
- **Bot** — 事件编排器，负责监听事件（GitCode Webhook / 飞书 IM / 用户操作）、路由分发、会话管理
- **Agent** — 大脑，负责理解任务、执行操作、生成结果
- **Runtime（Agent Runtime）** — 执行引擎（opencode / codearts / cursor / codex 等 CLI）与其承载设备（Worker）。术语上已从旧版 `backend` 全面更名为 `runtime`
- **A2A 协议** — Agent 间通过 API 链式协作，形成完整工作流

**核心 Agent 工作流逻辑关系：**

```
VOD Agent ──→ Issue Agent ──→ Coding Agent ──→ QA Agent ──→ SRE Agent
 收集声音      分类需求        编码实现         审查验证       构建部署
 引导澄清      定义开发项     PR 提交          测试验证       发布运维
    ↑                                                     
    └────── 每个用户通过 Chat Agent（个人助手）横向接入 ──────┘
```

**执行模式（Execution Mode，`meta.execution_mode`，new-version2 关键机制）：**

| 模式 | 含义 | 说明 |
|------|------|------|
| `direct`（默认） | 事件/chat 直接就地执行 | 线程 `stream_agent` 在 bot 进程内执行 |
| `queue_local` | 事件/chat 入队，bot 进程内本地 worker 消费 | 任务队列 `agent_tasks` + 本地线程池 |
| `queue_remote` | 入队后由远端（或本机独立进程）RuntimeAgent 边端 claim 执行 | 通过 `/api/runtime/*` 协议 |

> 运维可在 Agent Runtimes 页切换执行模式；`direct` 一键回退。三种模式共用同一条任务队列。

**一句话定位：** 从开发者声音收集到代码发布的全流程自动化，通过多 Agent 协作 + 可插拔执行引擎（本地/远端 Worker）降低开发者的重复劳动。

---

## 二、用户旅程（端到端）

### 2.1 登录

登录页面提供 **GitCode OAuth 一键登录**和**密码登录**两种方式。OAuth 支持白名单控制（`configs/login-whitelist.json`，未在白名单的新用户无法登录）。

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
│        （点击后 GitCode 按钮消失，展开用户名/密码输入框， │
│         底部显示返回链接）                            │
└─────────────────────────────────────────────────────┘
```

**登录后行为：**
- **首次登录（无任何已托管仓库）**：OAuth 回调带 `first_login=1`，清除上次选择的仓库，直接进入「仓库选择大堂（Lobby）」。
- **再次登录（已有已托管仓库）**：`first_login=0`，**恢复上次退出时选择的仓库**，直接进入该仓库的 Dashboard。
- 登录后自动拉取 GitCode 仓库列表（带个人 Token），OAuth 成功即把 GitCode API Token 存入用户账户（`user_accounts.gitcode_api_token`），并在 Settings → Credentials 中展示。

---

### 2.2 仓库选择大堂（Lobby）与项目模板

> 登录后首屏。已加入仓库的用户点击侧边栏 Logo 或「＋ 新增仓库托管」也会回到此页。

大堂为全屏卡片式布局，顶部包含品牌区、两个页签（**工作项目 / 项目模板**）、用户信息与退出登录：

```
┌─ Lobby ─────────────────────────────────────────────────┐
│  [Grape · Agentic DevOps]    [工作项目] [项目模板]   [👤 用户 ↩] │
├──────────────────────────────────────────────────────────┤
│                                                           │
│   WELCOME TO GRAPE                                        │
│   你好，<name> 👋                                         │
│   选择一个工作项目，开启您的 AI for AI DevOps 之旅。          │
│   ◧ 10 工作项目   ✓ 3 已托管   ◇ 7 已加入                   │
│                                                           │
│  ┌───────────┐  ┌───────────┐  ┌───────────┐             │
│  │    ＋      │  │   BE      │  │   WE      │             │
│  │ 创建新项目  │  │ backend-srv│  │ web-frontend│            │
│  │ 在 GitCode │  │ 描述...    │  │ 描述...    │            │
│  │ 新建项目   │  │ owner · 角色│  │ owner · 角色│           │
│  │            │  │ [进入项目]  │  │ [加入并托管]│            │
│  └───────────┘  └───────────┘  └───────────┘             │
│  ┌───────────┐  ┌───────────┐  ┌───────────┐             │
│  │   DT      │  │   NF      │  │   DP      │             │
│  │ devops-tklt│  │ notification│ │ data-pipeline│          │
│  │ 描述...    │  │ 描述...    │  │ 描述...    │            │
│  │ org · Admin│  │ team · Member│ │ org · Member│          │
│  │ [加入并托管]│  │ [加入]     │  │ [加入]     │            │
│  └───────────┘  └───────────┘  └───────────┘             │
└──────────────────────────────────────────────────────────┘
```

**仓库卡片状态与按钮：**

| 状态 | 角色 | 按钮 | 行为 |
|------|------|------|------|
| 已托管 / 已加入 | 任意 | `进入项目` | 选中仓库，进入 Dashboard |
| 未托管 / 未加入 | Admin / Maintainer | `加入并托管` | 创建 Webhook + Bot 入仓 → 进入项目 |
| 未托管 / 未加入 | Member | `加入` | 仅加入（不建 Webhook）→ 进入项目 |

- **创建新项目卡片**：首位固定，点击跳转 `https://gitcode.com/create`。
- 进入「工作项目」页签时自动 `POST /repos/sync` 强制从 GitCode 拉取最新列表（新创建的项目立即可见）。

**项目模板页签（`templates.js`）：**

```
┌─ Lobby · 项目模板 ──────────────────────────────────────┐
│  TEMPLATE GALLERY                                       │
│  ☁️ 云原生项目模板                                        │
│  选择一个模板，快速创建你的 DevOps 项目。                    │
│  ◧ 总数  ◇ 当前分类  ◧ 分类数                              │
│  [☁️ 云原生项目模板 5] [🤖 AI Office 7] [🧩 Skills 1] [🏗️ IaC 1] │
│                                                          │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐   │
│  │ 🛍️          │  │ 🚪          │  │ 🔌          │   │
│  │ 730harmony   │  │ Best Practice│  │ CodeArts MCP │   │
│  │ 电商应用     │  │ Gateway      │  │ 服务         │   │
│  │ [▶ 视频]     │  │              │  │              │   │
│  │ [一键创建 →]  │  │ [一键创建 →]  │  │ [一键创建 →]  │   │
│  └──────────────┘  └──────────────┘  └──────────────┘   │
└──────────────────────────────────────────────────────────┘
```

- 模板按分类组织（云原生 / AI Office / Skills / IaC），每张卡片含图标、名称、描述、标签、统计与操作按钮。
- **一键创建**：弹窗输入仓库名 → 自动「建仓（GitCode 个人账号下）→ 写入模板代码 → 自动托管（复用 `/repos/host` 全链路）→ 应用开发场景」；创建成功后直接进入该仓库。首次由模板创建的项目还会异步触发「首次托管引导」（Onboarding），由 issue agent 生成「快速部署体验」与「探索方向引导」两个 Issue。
- 模板数据源：`configs/project-templates.json`（git 模板克隆自 `source_repo`；local 模板复制 `examples/{template_id}/` 本地目录）。

---

### 2.3 侧边栏与仓库切换

进入某个仓库后，主界面由**侧边栏 + 顶部栏 + 内容区**组成。侧边栏仓库下拉**仅显示已加入/已托管仓库**，底部带「＋ 新增仓库托管」入口——选择后与点击 Grape Logo 一致，进入**全屏仓库选择大堂**（隐藏侧边栏）：

```
┌─ Sidebar ──────────────────────────────────────────┐
│  [Grape]（点击返回仓库选择大堂）                        │
│  Multi-Agent DevOps                                 │
│                                                     │
│  [team/backend-service ▼]        [Maintainer ▼]     │
│       ┌─────────────────────┐                       │
│       │ team/backend-service│                       │
│       │ team/web-frontend   │                       │
│       │ team/mobile-app     │                       │
│       │ ─────────────────── │                       │
│       │ ＋ 新增仓库托管       │                       │
│       └─────────────────────┘                       │
│                                                     │
│  Workspace                                          │
│    ■ Dashboard                                      │
│    ▶ Pipeline                                       │
│                                                     │
│  DevOps Agents                                      │
│    ● VOD Agent                                      │
│    ● Issue Agent                                    │
│    ● Coding Agent                                   │
│    ● QA Agent                                       │
│    ● SRE Agent                                      │
│                                                     │
│  DevOps Skills                                      │
│    ◈ Skills Library                                 │
│                                                     │
│  Account                                            │
│    📋 Task Center                                   │
│    ☎ Chat Agent                                     │
│                                                     │
│  Settings                                           │
│    ⚙ General Settings                                │
│    ⎘ GitCode Repo                                    │
│    ⚡ Agent Runtimes                                 │
│                                                     │
│    [👤 user] [↩ 登出]                               │
└─────────────────────────────────────────────────────┘

┌─ Top Bar ──────────────────────────────────────────┐
│  Dashboard · team/backend-service ↗                │
│  [☁ 云开发环境]      [⌕ 搜索...]                    │
└─────────────────────────────────────────────────────┘
```

- **仓库切换**：下拉切换后 Dashboard / Pipeline / Agent 详情等页面数据跟随切换；**记住上次退出时选择的仓库**（`localStorage.currentRepo`），重新登录自动恢复；若上次仓库已解除托管则回退到列表第一个。
- **☁ 云开发环境**：顶部按钮，跳转华为云 DevStation AI Shell（`https://devstation.connect.huaweicloud.com/aishell2?utm_source=huawei&utm_medium=floating_ball&utm_campaign=space`）。
- **全局搜索**：顶部搜索框，按 Enter 检索 Events / Repos / Agents 并跳转结果。

---

### 2.4 Dashboard（按仓库维度）

Dashboard 以**单个已托管仓库**为维度，每 30 秒自动刷新：

```
┌─ Top Bar ────────────────────────────────────────────┐
│  Dashboard · team/backend-service ↗                   │
├───────────────────────────────────────────────────────┤
│  ┌─ Repo Header ──────────────────────────────────┐  │
│  │  ■ team/backend-service [Maintainer]           │  │
│  │  Maintainer · ★ main · Webhook OK              │  │
│  │  98.5% Auto-Resolution│ 47 Events/Week│ 12s Avg │  │
│  └────────────────────────────────────────────────┘  │
│                                                        │
│  ┌─ DevOps Agent Workflow ─────────────────────────┐  │
│  │  开发场景: [通用开发 ▼] [应用]                     │  │
│  │   ┌──────┐  ┌──────┐  ┌──────┐  ┌──────┐  ┌──────┐│
│  │   │● VOD │→│● Issue│→│● Code │→│● QA  │→│● SRE ││
│  │   │🔍 VOD │  │📋 Issue│  │💻 Code │  │🧪 QA  │  │⚙ SRE ││
│  │   │✓ 开启 │  │✓ 开启 │  │✓ 开启 │  │✓ 开启 │  │○ 停用││
│  │   │Backend │  │Backend │  │Backend │  │Backend │  │Backend ││
│  │   │opencode│  │opencode│  │opencode│  │opencode│  │opencode││
│  │   │● Active│  │● Active│  │● Idle  │  │● Active│  │已停用  ││
│  │   │最近事件...│  │最近事件...│  │最近事件...│  │最近事件...│  │      ││
│  │   └──────┘  └──────┘  └──────┘  └──────┘  └──────┘│
│  │                                                    │
│  │  ☎ Chat Agent · Personal Assistant                │
│  │  Your personal agent connected to the workflow    │
│  │  [↔ VOD] [↔ Issue] [↔ Coding] [↔ QA] [↔ SRE]      │
│  └────────────────────────────────────────────────┘  │
│                                                        │
│  ┌─ Recent Activity ────────────────────────────────┐  │
│  │  team/backend-service · Last 24h                  │  │
│  │  14:32 [QA] QA Approved PR #1278 ...              │  │
│  │  14:15 [COD] Coding Submitted PR #1278 ...        │  │
│  │  13:50 [ISS] Issue Assigned #342 to @alice ...    │  │
│  │  13:22 [SRE] SRE Deployed v2.3.1 to production ...│  │
│  │  12:05 [VOD] VOD Signal #47 → created issue #345  │  │
│  └────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────┘
```

Dashboard 区域说明：

| 区域 | 内容 |
|------|------|
| **Top Bar** | 页面标题 + 仓库名（**点击跳转 GitCode 项目页 ↗**，可查看仓库/Commit/Issues/PRs）；右侧 ☁ 云开发环境 + 全局搜索 |
| **Repo Header** | 仓库名 + 角色徽标 + 分支（★ main）+ Webhook 状态；指标：自动解决率 / 周事件量 / 平均响应 |
| **Agent Workflow** | 5 个 Agent 流水线卡片，每张显示：图标+名称、**启用开关**、Backend 引擎名、状态点、**最近 2 条真实事件**（title + status，来自 events 表，非假数据）；右上角**开发场景选择器**（切换 Agent-Skill 映射） |
| **Chat Lane** | 个人 Chat Agent 横贯连接条，点击进入 Chat 页面 |
| **Recent Activity** | 最近 24 小时事件流（最近 5 条），点击事件跳转 Event Detail |

**Agent 启用开关（new-version2 新增）：**
- 每个 Agent 卡片头部有开关按钮（`✓ 开启` / `○ 停用`），**仅 Maintainer/Admin 可切换**，写入仓库级 `devops_agent_session.enable`。
- 全局默认来自 `configs/agent-description.json` 的 `enable` 字段：**SRE 默认关闭**，其余默认开启。
- 关闭效果：该 Agent 不再被事件路由触发（跳过并记录 `event_skipped(agent_disabled)`）；A2A 调用也被拒绝；已排队的任务不受影响。
- 停用态卡片整体降透明度并显示「已停用」。

**开发场景**：`configs/agent-skill-mapping.json` 定义了 通用开发 / 云原生开发 / IaC开发 / Agent开发 / Skill开发 等场景，每个场景含 `{vod,issue,coding,qa,sre}` → skill 的映射。选择场景点「应用」后，`POST /api/scenes/apply` 为仓库写入 `devops_agent_session` 的技能映射（不覆盖仓库级 enable 开关）。

---

### 2.5 Pipeline 页面（Issue 工作流跟踪）

Pipeline 按 **Issue** 维度聚合展示工作流进展：

```
┌─ Top Bar ────────────────────────────────────────────┐
│  Pipeline · team/backend-service                      │
│  Active: 1  |  Today: 3                               │
├───────────────────────────────────────────────────────┤
│  Active: Issue #342 (链接→GitCode)                    │
│  👤 当前责任人: QA Agent   ⚡执行中 / 空闲             │
│                                                        │
│  ┌────────┐  ┌────────┐  ┌────────┐  ┌────────┐  ┌───────┐ │
│  │ ● VOD  │→│ ● Issue│→│ ● Code │→│ ● QA  │→│ ● SRE │ │
│  │ issue_o│  │ A2A    │  │ PR #   │  │Review  │  │Await  │ │
│  │ @vod   │  │ @issue │  │ @coder │  │ing...  │  │QA OK  │ │
│  │✅ Done │  │✅ Done │  │✅ Done │  │🔄InProg│  │⏳Pend │ │
│  └────────┘  └────────┘  └────────┘  └────────┘  └───────┘ │
│                                                        │
│  Pipeline History（点击 Issue 切换上方 Agent 卡片）      │
│  ┌─────────┬──────────┬──────────────┬────────┬────────┐│
│  │Issue    │ Duration │ Agents       │阶段     │责任人   ││
│  ├─────────┼──────────┼──────────────┼────────┼────────┤│
│  │Issue#342│ 28m 12s  │ V I C Q S   │Coding   │grape_bot││
│  │Issue#338│ 1h 05m   │ V I C Q S   │QA       │grape_bot││
│  │Issue#331│ 42m 18s  │ I C Q       │Done     │Jason7602││
│  └─────────┴──────────┴──────────────┴────────┴────────┘│
└─────────────────────────────────────────────────────────┘
```

- **Issue 链接**：上方 "Issue #342" 为可点击链接（跳转 `https://gitcode.com/{owner}/{repo}/issues/{n}`）；历史表行点击仅用于切换上方 Agent 卡片。
- **阶段列**：显示 Issue 生命周期当前阶段（VOD / Issue / Coding / QA / SRE / Done），取 `issue_state.stage`。
- **排序**：Pipeline History 按 Issue 最新活动**倒序**（最新的 issue 排最前，原为 issue 首次出现顺序）。
- **执行态**：由 `agent_tasks` 中 `dispatched/running` 任务实时派生，执行中的 Issue 显示 `⚡执行中` 徽标（空闲显示「空闲」）。
- **Agent 卡片**：每阶段展示该 Agent 对该 Issue 最新一条 Event 的 title、状态（✅ Done / 🔄 In Progress / ❌ Failed / ⏳ Pending）与 actor（@用户名）；**未涉及任务的阶段（Pending）卡片整体调暗**（灰色背景 + 降饱和 + 弱化文字 + 降低透明度）。
- **责任人判定**：优先取 **`issue_state.assignee`**（GitCode Issue 真实指派，可为 `grape_bot` 或项目成员用户名）；缺失时回退取当前阶段 Agent 最新 Event 的 `actor`，若为 bot 则显示 Agent 显示名，若为项目成员则显示其用户名。

> new-version2 说明：事件按 **Issue 生命周期状态机**路由（`issue_state` 表，阶段 `vod→issue→coding→qa→sre→done`）。`issue_opened` 恒进 VOD 审视；Issue 类事件按阶段归 VOD 或 Issue Agent；PR 类事件按阶段归 coding/qa/sre。同时执行采用 **Issue 级会话/工作目录隔离**（`{workspace}/{owner}/{repo}/{agent}/issues/{n}/` + issue 级执行会话），支持同仓库多 Issue 并行，同 Issue 内串行。

---

### 2.6 DevOps Agent 详情页面（以 VOD Agent 为例）

```
┌─ Top Bar ────────────────────────────────────────────┐
│  VOD Agent · team/backend-service                     │
├───────────────────────────────────────────────────────┤
│  ┌─ Agent Header ─────────────────────────────────┐  │
│  │  🔍 VOD Agent              [Configure] [Events] │  │
│  │  VOD Agent 描述文字...                          │  │
│  └────────────────────────────────────────────────┘  │
│                                                        │
│  [Configure] 点击展开配置面板：                        │
│  ┌─ Agent Configuration ──────────────────────────┐  │
│  │  Backend Engine                                │  │
│  │  [opencode-worker-23249 ▾] ← 引擎实例唯一名      │  │
│  │  Skill                                         │  │
│  │  [默认官方技能（自动）▾]                          │  │
│  │      - 默认官方技能（自动）                       │  │
│  │      - 开发者声音分析 (官方)                      │  │
│  │      - 自定义技能 (仓库) / (个人)                 │  │
│  │  ──────────────────────────                    │  │
│  │  事件订阅  〔仅 SRE Agent〕                      │  │
│  │  ☑ PR 合入（pull_request_merged）               │  │
│  │  合入分支 [main, dev…（留空=不限）]               │  │
│  │     当 PR 合入到上述分支时，SRE Agent 自动收到任务  │  │
│  │  ──────────────────────────                    │  │
│  │  IM 配置  〔仅 VOD Agent〕                       │  │
│  │  Bot App ID [cli_xxxxxxxx       ]               │  │
│  │  Bot Secret [********          ]               │  │
│  │  [扫码配置]                                     │  │
│  │  ──────────────────────────                    │  │
│  │  [Save] [Cancel]                               │  │
│  │  ──────────────────────────                    │  │
│  │  🧹 Clean Session                               │  │
│  │  清空会话后，下一次事件将创建全新的 Agent 上下文     │  │
│  └────────────────────────────────────────────────┘  │
│  ┌─ 指标卡 ───────────────────────────────────────┐  │
│  │  5 事件总量 │  3 已完成 │  1 进行中             │  │
│  └────────────────────────────────────────────────┘  │
│                                                        │
│  ┌─ Event History ────────────────────────────────┐  │
│  │  #503 #345 12:05 Signal: CI too slow [Done]     │  │
│  │  #499 #345 10:30 issue_opened @alice 12s [Done]  │  │
│  └────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────┘
```

Agent 详情说明：

| 区域 | 内容 |
|------|------|
| **Agent Header** | 图标、显示名、描述、[Configure] 和 [Events] 按钮 |
| **Backend Engine（运行时引擎）** | 下拉直接选**引擎实例唯一名**（如 `opencode-worker-23249`，格式 `{引擎类型}-{设备短名}`）；**DevOps Agent（vod/issue/coding/qa/sre）仅可选 仓库/全局 范围引擎**（个人引擎仅供 Chat Agent，见 Agent Runtimes 一节）；在线实例优先、离线置灰；保存即绑定该实例的 `runtime_id` |
| **Skill** | 下拉按 `scope:name` 精确匹配（官方/仓库/个人），"默认官方技能（自动）"表示由 Agent-Skill 映射自动决定 |
| **SRE Agent 专属：事件订阅** | PR 合入（`pull_request_merged`）勾选 + 合入分支（逗号分隔，留空=不限）；命中订阅即触发任务（`devops_agent_session.subscribe_events`，见 `agent-event-subscription-design.md`）；**仅 SRE Agent 展示** |
| **VOD Agent 专属：IM 配置** | 绑定飞书 Bot App ID / Secret，展示已绑定群列表；类型独占（同一凭证不可同时用于 VOD 与 Chat） |
| **🧹 Clean Session** | 将 `runtime_session_id` 置空，下一次事件到来时创建全新会话上下文（不延续历史对话） |
| **指标卡** | 事件总量 / 已完成 / 进行中（来自 events 表统计） |
| **Event History** | 最近 10 条事件（时间、摘要、@actor、耗时、引擎、状态），点击进入 Event Detail |

---

### 2.7 Event Detail 页面（含与 Agent 对话）

> 本页的「💬 对话」为 **Event 对话窗口设计**（详见 `event-conversation-design.md`），是对原「Feedback 表单」的重构，**待实施**；其余（结构化产出、Event Info）为现状。

点击任意 Event（Dashboard 活动流、Agent 详情事件、全局搜索）进入事件详情：

```
┌─ Top Bar ─────────────────────────────────────────────────┐
│  Event Detail · VOD Agent · team/backend-service           │
├────────────────────────────────────────────────────────────┤
│  [← 返回 VOD Agent]                                        │
│                                                             │
│  ┌─ Event Context ──────────────┐  ┌─ Event Info ────────┐ │
│  │  Trigger: issue_opened       │  │ Seq: #1234          │ │
│  │  Repository: team/backend-   │  │ Runtime: opencode   │ │
│  │  Issue: #345                 │  │ Skill: vod          │ │
│  │  Actor: @alice               │  │ Issue: #345         │ │
│  │  Time: ...                   │  │ Actor: @alice       │ │
│  │                              │  │ Agent: vod          │ │
│  │  ┌─ Agent 处理记录（结构化）─┐  │ Duration: 12s       │ │
│  │  │ 结论: 已确认该反馈为...   │  │ Status: ✅ Success  │ │
│  │  │ 动作: • 分析 · 聚类      │  │ ────────────────     │ │
│  │  │ 产出: [artifact-1]       │  │ 门禁: ✔ 评论开放中   │ │
│  │  │ 下一步: A2A 交办 Issue   │  │                      │ │
│  │  └─────────────────────────┘  │  ┌─ 💬 与 VOD 对话 ──┐ │
│  │  ┌─ Agent 答复结论 ──────────┐ │  │ ┌──────────────┐ │ │
│  │  │（Agent 结论文本，等宽）    │  │  │ 用户          │ │ │
│  │  └─────────────────────────┘  │  │ 为什么这样处理？ │ │ │
│  │  ┌─ 💬 对话产出（结果摘要）───┐ │  │ ────────────── │ │ │
│  │  │ 知识沉淀:                  │  │  │ agent         │ │ │
│  │  │  · 仓库级: VOD 不删原文    │  │  │ 该 issue 描述  │ │ │
│  │  │ 流程操作:                  │  │  │ 缺验收标准，故  │ │ │
│  │  │  · 回退 vod · 暂停 coding  │  │  │ 先澄清…       │ │ │
│  │  │ 结论: 已与用户澄清验收标准  │  │  └──────────────┘ │ │
│  │  └─────────────────────────┘  │  ├────────────────┤ │ │
│  │                                │  │ [输入消息...]   │ │ │
│  │                                │  │      [发送]      │ │ │
│  │                                │  └────────────────┘ │ │
│  └────────────────────────────────┘  └───────────────────┘ │
└────────────────────────────────────────────────────────────┘
```

- **结构化产出卡片（new-version2 新增）**：读取该 Issue 的 `event.json` 事件档案对应 pipeline 段，展示 Agent 处理记录的 `actions / conclusion / artifacts / next_step / issue_summary` 五字段。
- **Event Info**：事件元信息（Runtime 引擎、Skill、Issue、Actor、Agent、耗时、状态），并标注对话门禁状态。
- **💬 与 VOD 对话（右栏 · 会话本体/过程）**：以该事件为锚点、与**处理该事件的 agent** 的完整消息线程——用户/agent 每一轮消息 + 输入框 + 发送 + 流式回复。**所有原始对话消息都在这里**，取代原 Feedback 表单。支持：
  1. **解释**：问"为什么这样处理 / 思考过程"，agent 基于事件上下文作答；
  2. **建议沉淀**：用户提出建议，agent 判断是否沉淀为 **agent 级 / repo 级共享知识**（`agent_shared_knowledge`），后续任务自动参考；
  3. **流程干预**：用户指出需求理解有误（如 pipeline 已在 coding 但 VOD 理解仍偏差）→ agent 确认后执行 **暂停下游 → 回退阶段（重置回 vod）→ 重新澄清 → 重新派发（A2A 交办 issue）**，回到正常流程；
  4. 对话**复用该 agent 的 issue 级会话**（`issue_agent_session`，上下文跨事件延续），由 `event-dialogue` 对话 skill 约束行为。
- **💬 对话产出（左栏 · 结果摘要）**：与右栏**不重复**——右栏是对话"过程"（每一条消息），左栏是对话"结果"摘要，随对话实时更新：
  - **知识沉淀**：本次对话新增的共享知识（scope agent/repo + 内容 + 采纳状态）；
  - **流程操作**：对话触发的 `flow/reset` 回退、任务取消（暂停 coding）、A2A 重新派发等动作留痕；
  - **结论**：agent 在对话中与用户达成的明确结论。
- **门禁**：关联 Issue 的 pipeline 结束（`issue_state.stage='done'` 或 Issue 关闭）后，**关闭评论**——右栏输入框隐藏、后端拒绝（409）；左栏对话产出保留可查。

---

### 2.8 Skills Library（技能库）

技能库按 Agent 分区展示，顶部提供**官方技能 / 仓库技能 / 个人技能**三个页签与搜索框：

```
┌─ Top Bar ────────────────────────────────────────────┐
│  DevOps Skills                                       │
│  [🔍 搜索技能...] [官方技能] [仓库技能] [个人技能] [＋新增技能] │
├───────────────────────────────────────────────────────┤
│  🔍 VOD Agent Skills                                  │
│  ┌──────────────┐  ┌──────────────┐                  │
│  │ 🔍 开发者声音分析│  │ (个人技能)    │                  │
│  │ 官方技能      │  │ 用户上传      │                  │
│  │ 开发者声音分析… │  │ 描述...      │                  │
│  │ [View]       │  │ [View] [Delete] [转为仓库技能] │  │
│  └──────────────┘  └──────────────┘                  │
│  📋 Issue Agent Skills                                │
│  ┌──────────────┐  ┌──────────────┐                  │
│  │ 📋 Issue 管理 │  │ ✨ Issue 高级运营│                │
│  │ 官方技能      │  │ 官方技能      │                  │
│  │ ...          │  │ ...          │                  │
│  └──────────────┘  └──────────────┘                  │
│  ... Coding / QA / SRE / Chat Agent Skills ...        │
└─────────────────────────────────────────────────────────┘
```

- 页签过滤：官方技能（只读 View）/ 仓库技能（绑定当前仓库，`scope=repo`）/ 个人技能（当前用户上传，`scope=user`）；**仓库/个人页签提供 [＋新增技能]**。
- 打开官方技能页签时自动调用 `/skills/sync-official` 从 `configs/skill-description.json` 同步新增官方技能。
- **技能卡片**：图标 + 名称 + 范围标签 + 描述 + [View]；非官方技能可 [Delete]；个人技能在有当前仓库时可 [转为仓库技能]（复制到仓库，原个人技能保留）。

**上传技能（ZIP）弹窗**：

```
┌─ 上传技能 (ZIP) ────────────────────────────────────────┐
│  类型:     ● 仓库技能  /  ○ 个人技能                     │
│  仓库:     team/backend-service      (仓库技能时自动带入) │
│  技能类型* [VOD Agent (🔍 声音分析)      ▾]              │
│            ├─ VOD Agent   (🔍 声音分析)                 │
│            ├─ Issue Agent (📋 需求管理)                 │
│            ├─ Coding Agent(💻 代码实现)                 │
│            ├─ QA Agent    (🧪 审查验证)                 │
│            ├─ SRE Agent   (⚙ 构建部署)                 │
│            └─ Chat Agent  (☎ 对话助手)                 │
│  选择 ZIP 文件（目录名 = 技能名，内含 SKILL.md）：        │
│  [📁 选择文件]  skill.zip                               │
│  格式要求：[📄 下载技能模板 ZIP]                         │
│  上传中...  ▓▓▓▓▓▓▓▓░░░░ 65%                           │
│                              [取消]  [上传]              │
└─────────────────────────────────────────────────────────┘
```

- **技能类型必填**（vod/issue/coding/qa/sre/chat），未选或非法后端 400 拒绝。
- 上传过程显示真实进度条（XHR onprogress），完成后自动关闭；校验失败时后端返回结构化错误列表，前端多行展示（如 name 不一致、description 为空、含可执行文件等）。
- 仓库技能存储于 `skills/repo/{repo_key}/{skill_name}`；个人技能存储于 `skills/custom/{user_id}/{skill_name}`。
- 技能详情页展示 SKILL.md 全文与元信息。

**官方技能一览（`configs/skill-description.json`）：**

| Agent | 官方技能 |
|-------|---------|
| VOD | 开发者声音分析（`vod`） |
| Issue | Issue 管理（`issue`）、Issue 高级运营（`issue-advance`） |
| Coding | 代码实现（`implementation`） |
| QA | PR 管理（`pr`）、PR 审查与工作流质量审查（`qa`）、技能仓库 PR 审查（`skill-pr`） |
| SRE | 构建调试（`build-debug`）、云部署（`cloud-deploy`）、部署安全审查（`deploy-guard`）、变更日志生成（`changelog-gen`）、事件分诊（`incident-triage`） |
| Chat | IM 通知（`im-notify`）、对话助手（`chat`） |
| Onboarding | 首次托管引导（`repo-onboarding`） |

---

### 2.9 Chat Agent（个人助手）

Chat Agent 为**三栏布局**：左栏对话列表、中栏对话区、右栏固定配置面板。

```
┌─ Top Bar ───────────────────────────────────────────────────────┐
│  Chat Agent · Personal Assistant                                 │
├──────────────────────────────────────────────────────────────────┤
│  ┌── Conversations ─┐  ┌──────── Main Chat ─────────┐  ┌ Config┐│
│  │ ＋ 新对话        │  │                            │  │Backend││
│  │━━━━━━━━━━━━━━━━━│  │  Hello! I'm your assistant │  │[opencode││
│  │ 新对话           │  │                            │  │ -worker││
│  │ 2026-07-28 17:30 │  │  ┌───────────────────────┐│  │ -23249 ││
│  │  ✕               │  │  │Check issue #342       ││  │ ▼]     ││
│  │ 需求讨论         │  │  └───────────────────────┘│  │        ││
│  │ 2026-07-27 10:15 │  │                            │  │ Skill  ││
│  │  ✕               │  │  ✅ Issue #342 is with QA ││  │[... ▼] ││
│  │ 问题反馈         │  │  Agent, ETA ~15 min       ││  │        ││
│  │ 2026-07-26 09:00 │  │                            │  │ IM      ││
│  │  ✕               │  │  ┌─[Type message...]───┐  │  │[Feishu ▼]││
│  │                  │  │  │                [Send]│  │  │ App ID ││
│  │                  │  │  └──────────────────────┘  │  │[cli...] ││
│  └──────────────────┘  └────────────────────────────┘  │ Secret  ││
│                                                        │[******] ││
│                                                        │ [Save]  ││
│                                                        │ Agents  ││
│                                                        │ ● VOD 47││
│                                                        │ ○ Iss 3 ││
│                                                        │ ○ Cod 1 ││
│                                                        │ ○ QA 2  ││
│                                                        │ ○ SRE v ││
│                                                        └────────┘│
└──────────────────────────────────────────────────────────────────┘
```

| 区域 | 内容 |
|------|------|
| **Conversations（左栏）** | 用户所有对话列表，「＋ 新对话」、切换、删除；首次输入自动创建对话并生成标题（取消息前 30 字） |
| **Main Chat（中栏）** | 消息历史（Markdown 渲染）+ 输入框 + Send |
| **Config（右栏）** | Backend（**仅个人范围引擎**，Chat Agent 不可选仓库/全局引擎）、Skill（chat 官方/个人/仓库技能）、IM（飞书/企业微信/钉钉，App ID + Secret + [扫码配置] + Save）、5 个 DevOps Agent 事件统计 |

**IM 飞书扫码配置弹窗（规划中，对应 `feishu-qr-app-onboarding-plan.md`）：**

IM 区域在 App ID / Secret 输入框下新增 **[扫码配置]** 按钮，点击后弹出二维码弹层，替代手动粘贴凭据：

```
点击 [扫码配置] 后弹出：

┌─ 飞书扫码配置 ─────────────────────────────────┐
│  🔗 连接飞书 Bot                                 │
│                                                  │
│  ┌──────────────────────────────┐               │
│  │        ██▌ ██ ██▌ ██ ██      │               │
│  │       ██ ██████ ██ ██████    │               │
│  │        ████ ██▌██ ██████     │  ← 二维码      │
│  │        ████████ ██▌ ██▌█     │    (QR Code)   │
│  │        ██▌█ ██ ██▌█████      │               │
│  │       ██████ ██████████      │               │
│  └──────────────────────────────┘               │
│                                                  │
│  1. 打开飞书 App，扫描左侧二维码                    │
│  2. 在飞书内确认「创建应用/授权」页面                 │
│                                                  │
│  ⏳ 等待确认…   二维码将在 04:32 后过期             │
│                                                  │
│                              [取消]              │
└──────────────────────────────────────────────────┘

配置成功（自动关闭弹层并刷新 Config）：

┌─ 飞书扫码配置 ─────────────────────────────────┐
│  ✅ 配置成功                                    │
│  App ID      cli_axxxxxxxxxxxxxxxxxxxxx        │
│  App Secret  已保存（回显为 ******）              │
│  Bot 状态     🟢 已连接 · 在线                    │
│                              [关闭]            │
└──────────────────────────────────────────────────┘

配置失败（按异常类型展示对应文案）：

┌─ 飞书扫码配置 ─────────────────────────────────┐
│  ❌ 配置失败                                    │
│  原因: 二维码已过期，请重新扫码                    │
│                [重新扫码]  [取消]                │
└──────────────────────────────────────────────────┘
```

**扫码弹窗交互说明：**

| 状态 | 弹窗内容 | 说明 |
|------|---------|------|
| `pending` | 转圈 + 「正在生成二维码…」 | 后端 `POST /api/feishu/onboard/start` 返回 task_id 后进入轮询 |
| `waiting_confirm` | 二维码图片 + 剩余有效期倒计时 | 每 2s 轮询 `GET /api/feishu/onboard/status?task_id=xx`；倒计时为 0 自动切换「已过期，请重新扫码」 |
| `connected` | ✅ 成功卡片 + App ID 回显 | 自动关闭弹层，Config 面板回填 App ID / 显示「🟢 已连接」，无需再手动粘贴 Secret |
| `failed` | ❌ 失败卡片 + 原因 | 用户拒绝（`AppAccessDeniedError`）/ 二维码过期（`AppExpiredError`）/ 其他（`RegisterAppError` 含 code）分别映射不同文案 |

- **过期重扫**：二维码过期后弹层内提供 [重新扫码]（重新调 start 生成新 task_id），不关闭弹层。
- **并发限制**：同用户已有进行中的扫码任务时，start 返回 409，弹层直接展示「已有配置正在进行，请稍候」。
- **取消**：调 `POST /api/feishu/onboard/cancel`，弹层关闭；已 connected 后取消为幂等，不影响已保存配置。
- **适用面**：同一弹窗组件复用于 Chat Agent（`bot_type=chat_bot`）与 VOD Agent 详情页 IM 配置（`bot_type=vod_bot`），仅 bot_type / repo_key 参数不同。

**发送交互（new-version2 变更）：**
- `direct` 模式：走 SSE 流式（`POST /api/chat/send/stream`），打字机实时输出。
- 队列模式（`queue_local` / `queue_remote`）：`send/stream` 返回 `{task_id}` → 前端以 1s 间隔轮询 `GET /api/runtime/tasks/{id}/status`，显示「⏳ 任务执行中… (Ns)」，终态展示 `result_text`（打字机效果降级）。

> Chat Agent 不完全属于 DevOps 工程循环的关键一环，它是每个用户接入工作流的个人对话入口。核心工作流仍为 VOD → Issue → Coding → QA → SRE。Chat Agent 可代表用户在 GitCode 上操作（提交 Issue、查询状态、任务对话等），并使用**用户的个人 GitCode Token**（与 DevOps Agents 使用的 Bot Token 隔离）。

---

### 2.10 Task Center（任务中心，Account 下新增）

Issue Agent 在分配/换人时通过 `/api/tasks/notify` 为用户创建任务（并自动创建对应的 Web 对话 + 飞书通知）；Issue 关闭时 `/api/tasks/close` 将相关 active 任务置为已完成。

```
┌─ Task Center ─────────────────────────────────────────────┐
│  [全部 5] [待处理 2] [已完成 3]        [🔍 搜索任务...]      │
├────────────────────────────────────────────────────────────┤
│  ┌─ 待处理 (2) ────────────────┐  ┌─ 任务详情 ────────────┐ │
│  │ # ──────────────── 待处理    │  │ # ────────────────   │ │
│  │ Issue #342 · 登录超时        │  │ Issue #342           │ │
│  │ ⏱ 2026-08-01 · team/be...   │  │ 登录超时              │ │
│  │ ────────────────            │  │ 状态     ● 待处理     │ │
│  │ # ──────────────── 待处理    │  │ 仓库     team/backend-│ │
│  │ Issue #338 · Webhook 重试    │  │ 创建时间 ...          │ │
│  │ ⏱ 2026-08-01 · team/be...   │  │ 完成时间 —            │ │
│  │ ────────────────            │  │                      │ │
│  │ ✓ ──────────────── 已完成    │  │ 描述: ...            │ │
│  │ Issue #331 · API 限流        │  │ [去对话] [查看 Issue ↗]│ │
│  │ ⏱ ... · team/be...          │  └──────────────────────┘ │
│  │ ────────────────            │                           │
│  │ ✓ ──────────────── 已完成    │                           │
│  │ Issue #335 · Refactor auth   │                           │
│  └──────────────────────────────┘                           │
└────────────────────────────────────────────────────────────┘
```

- 页签：全部 / 待处理 / 已完成（含计数）；支持关键字搜索（标题、描述、Issue 号、仓库）。
- 列表按「待处理在前、已完成在后」分组；右侧详情展示状态（含完成原因：Issue 已关闭 / 已转派）、仓库、创建/完成时间、任务描述。
- 操作：**[去对话]** 跳转任务对应的 Chat 对话；**[查看 Issue ↗]** 打开 GitCode Issue 页。

---

### 2.11 仓库管理页（Repositories）

侧边栏仓库下拉选「＋ 新增仓库托管」后，与点击 Grape Logo 行为一致，进入**全屏仓库选择大堂**（工作项目页签卡片视图）。此处的 **Hosted / Available to Join 管理表格**（顶部带 **[↻ Sync from GitCode]**）用于集中管理，在**解除托管后仍有剩余托管仓库**时展示；日常的加入、托管、进入项目操作均在全屏大堂中完成：

```
┌─ Repositories ───────────────────────────────────────┐
│  Repositories                   [↻ Sync from GitCode] │
├───────────────────────────────────────────────────────┤
│  Hosted (3)                                           │
│  ┌────────────────────────────────────────────────┐  │
│  │ ■ team/backend-service   Maintainer  Webhook OK │  │
│  │                          · ★ backend-service   │  │
│  │                          Active      [解除托管] │  │
│  │ ■ team/web-frontend      Member  ...  Joined   │  │
│  │ ■ team/mobile-app        ...                   │  │
│  └────────────────────────────────────────────────┘  │
│                                                        │
│  Available to Join (5)                                 │
│  ┌────────────────────────────────────────────────┐  │
│  │ Repository       Role        Description   Action│  │
│  │ org/docs-site    Member      Documentation  加入 │  │
│  │ org/devops-toolkit Member    DevOps Tools   加入 │  │
│  │ org/data-pipeline Admin      ETL pipeline 加入并托管 │  │
│  │ team/analytics   Maintainer  Analytics  加入并托管 │  │
│  └────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────┘
```

- **Hosted**：已托管/已加入仓库列表（仓库名、角色、Webhook 状态、Active/Joined 状态），点击仓库名进入；提供**解除托管**按钮（含数据清理确认：事件、会话、Agent 配置、仓库技能、工作区文件全部删除，不可恢复）。
- **Available to Join**：可加入仓库表格，Admin/Maintainer → `加入并托管`，Member → `加入`。
- **Sync from GitCode**：手动 `POST /repos/sync` 重新拉取缓存；另有 30 分钟自动过期机制（超过 TTL 后访问自动刷新）。

---

### 2.12 Settings（通用设置）

Settings 包含 **Credentials** 与 **Notifications** 两个页签：

```
┌─ Top Bar ────────────────────────────────────────────┐
│  Settings                                            │
│  [Credentials] [Notifications]                        │
├───────────────────────────────────────────────────────┤
│  GitCode API（OAuth 登录自动填充）                     │
│  API Token  [********（登录时自动填充，不可编辑）]       │
│  Account    <username> · 登录时自动填充                │
│                                                        │
│  Huawei Cloud                                         │
│  Access Key [AK-demo-access-key-xxxx]                 │
│  Secret Key [SK-demo-secret-key-xxxx]                 │
│  Huawei Cloud CodeArts provides free cloud dev         │
│  environment for Coding Agent.                        │
│  [Save]                                              │
└─────────────────────────────────────────────────────────┘
```

- **Credentials**：GitCode API Token（OAuth 登录自动获取，只读展示）、华为云 AK/SK（为 Coding Agent 提供云端开发环境），后端还支持 CodeArts CLI AK/SK 字段。
- **Notifications**：Issue events / PR events / Build failures / Release events / VOD signals 勾选偏好。
- 用户个人 GitCode Token 由 **Chat Agent** 代表用户在 GitCode 上操作（提交 Issue、查询状态、审批）；**DevOps Agents 使用独立的 Bot Token**，不受此页影响。

---

### 2.13 GitCode Repo 页面

仓库的 GitCode 专属配置（Settings → GitCode Repo，需先选择仓库）：

```
┌─ Top Bar ────────────────────────────────────────────┐
│  GitCode Repo · team/backend-service                  │
├───────────────────────────────────────────────────────┤
│  DevOps Bot Token                                     │
│  Bot Token  [ghp_devopsbot_xxxxxxxxxxxxxxxxxxxx]       │
│  Scope      read_repo, write_repo, manage_webhook      │
│  （留空将默认使用 Grape 官方机器人）                     │
│  [Save]                                              │
│                                                        │
│  ┌─ 解除托管 ──────────────────────────────────────┐  │
│  │  ⚠️ 解除托管后，将删除该仓库在 Grape 上的所有       │  │
│  │  资产数据（事件记录、会话、Agent 配置、仓库技能、    │  │
│  │  工作区文件），且不可恢复。                         │  │
│  │  [解除托管]  ← 红色按钮，含确认提示                │  │
│  └────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────┘
```

> **DevOps Bot Token 说明**：DevOps Agents 在执行 GitCode 操作时使用的**仓库级凭证**，需仓库读写权限（`read_repo, write_repo, manage_webhook`），建议使用独立 Bot 账号，与用户个人 Token 隔离。未配置时系统回退使用全局 Bot Token（`configs/global-bot-token.json`）或 Grape 官方机器人。

---

### 2.14 Agent Runtimes（运行时管理，new-version2 重构）

> 原「Backend Runtimes」页重构为 **Engines（引擎）/ Workers（设备）双子页**。背景：多 worker 并存后需要把「用什么引擎干活」（Engine 实例）与「谁在执行」（Worker 设备/凭据）两个维度分开。语义对应关系：一个 **Worker**（设备 = 一张 `rta_` 凭据）可承载多个 **Engine 实例**（某 worker 上安装的某个 CLI）。

**子页一：Engines 引擎（默认）** — 按 scope（全局/仓库/个人）分三张表：

```
[Engines 引擎] [Workers 设备]                          [⟳ Refresh]

■ 全局
┌────────────────────────────────────────────────────────────┐
│  ● 在线     opencode-local · opencode v1.17.7              │
│             worker: grape-server-local   owner: Jason7602  │
└────────────────────────────────────────────────────────────┘
┌────────────────────────────────────────────────────────────┐
│  ● 在线     codearts-local · codearts v26.6.2              │
│             worker: grape-server-local   owner: Jason7602  │
└────────────────────────────────────────────────────────────┘

■ 仓库
┌────────────────────────────────────────────────────────────┐
│  ● 在线     opencode-repo-abc · opencode v1.18.0           │
│             worker: repo-worker-a      owner: RepoOwner    │
└────────────────────────────────────────────────────────────┘

■ 个人
┌────────────────────────────────────────────────────────────┐
│  ● 在线     opencode-worker-23249 · opencode v1.18.15      │
│             worker: worker-123249     owner: Jason7602     │
└────────────────────────────────────────────────────────────┘
```

> 状态徽章：● 在线　◐ 刚失联(<5min)　○ 离线　⚠ 待回收(>6d)。高保真 HTML 原型见 `temp/engine-table-prototypes-c-hifi.html`。

- **engine name（引擎实例唯一名）**：`{引擎类型}-{设备短名}`（如 `opencode-worker-23249`），由服务端派生，是 Agent 配置页 Backend 下拉的选择标识。
- **Engine 的 scope/owner 与其承载 worker（凭据）一致**。
- 可见性：全局引擎所有人可见；个人引擎仅其 owner 可见；**仓库级引擎对该仓库所有成员可见**（不只 owner）。

**子页二：Workers 设备** — 按 scope 分三张表，凭据管理合入「凭据」列：

```
[Engines 引擎] [Workers 设备]  [＋ 生成凭据]          [⟳ Refresh]

■ 个人
┌────────────────────────────────────────────────────────────┐
│  ● 在线 · 8s 前   worker-123249 · 123.249.12.67           │
│             owner: Jason7602  凭据: active [改名][吊销]      │
│             承载引擎: opencode v1.18.15 ✓                  │
└────────────────────────────────────────────────────────────┘
```

> 状态徽章：● 在线　◐ 刚失联(<5min)　○ 离线　⚠ 待回收(>6d)；凭据：active / revoked。高保真 HTML 原型见 `temp/engine-table-prototypes-c-hifi.html`。

| 列 | 说明 |
|----|------|
| **worker name** | = 凭据名称（`runtime_credentials.name`，全局唯一）；边端自报的 `device_name` 作辅助 |
| **ip address** | worker 来源 IP（`last_ip`，经 X-Forwarded-For 采集，仅展示） |
| **心跳** | 健康四态：在线 / 刚失联（<5min）/ 离线 / 待回收（>6d）+ 相对时间 |
| **凭据** | 凭据状态 active/revoked + [改名] / [吊销] |
| **承载引擎** | 该 worker 安装的引擎（`name v版本 ✓/✗`） |
| **任务** | 按凭据聚合查看其名下所有引擎实例的 `agent_tasks`（队列/进行中/完成/失败） |

**生成凭据（Workers 页）**：

```
[＋ 生成凭据] 弹出表单：
  凭据名称:  [CI-Worker-01    ]   ← 全局唯一，可后续改名
  归属用户:  Jason7602（当前登录用户，自动，不可选）
  范围:      [个人 / 仓库 / 全局]   ← 默认个人；仓库范围需填仓库 owner/repo
  [生成]  → 展示 rta_ token（仅一次）+ 启动命令 RUNTIME_TOKEN=<token> python -m runtime.agent
```

- 生成的 `rta_` token 明文**仅展示一次**，DB 只存 sha256；复制按钮 + 启动命令。
- **吊销**后凭据立即失效，其引擎行级联置 `offline`，该设备从在线分组消失。
- **⟳ Refresh**：重新发现本地引擎（`discover_runtimes` + `--version` 探测）并落库。
- 管理员可在该页查看/切换**执行模式**（direct / queue_local / queue_remote）。

> **RuntimeAgent 边端**：远端/本机独立进程通过 `python -m runtime.agent`（环境变量 `RUNTIME_TOKEN` / `RUNTIME_SERVER_URL` / `RUNTIME_POLL_INTERVAL` 等）注册 → 心跳 → claim 任务 → 执行 → 回报。多实例键 `(credential_id, name, scope, owner_id)` 保证多 worker 同名引擎并存、互不覆盖。

---

## 三、5 + 2 Agent 工作流详解

### 3.1 VOD Agent（Voice of Developer）

| 属性 | 说明 |
|------|------|
| **职责** | 开发者声音收集、引导澄清、价值提炼；**接管 `issue_opened` 审视**（new-version2） |
| **触发方式** | `vod_signal`（飞书 VOD Bot 多轮沟通）/ `issue_opened`（新 Issue 一律先进 VOD 审视）/ 事件回退 |
| **关键能力** | 构建项目知识基线（project-info）→ 需求引导漏斗（模糊→多轮澄清→清晰）→ 结构化建 Issue → A2A 交办 Issue Agent |
| **工作流** | 收集/接管 Issue → 分析（知识基线 + 历史反馈去重）→ 与开发者澄清 → 收敛为清晰需求（含验收标准）→ 建/总结 Issue → A2A 交办 Issue Agent |
| **工作流位置** | 工作流最上游，所有新 Issue 的审视入口 |
| **预制 Skill** | `skills/vod/`（开发者声音分析） |

### 3.2 Issue Agent

| 属性 | 说明 |
|------|------|
| **职责** | 清晰需求 → 可开发任务（spec 设计、打标签、排里程碑、分配责任人、任务分派） |
| **触发方式** | VOD A2A 交办（主要）/ `issue_updated`/`issue_comment`（阶段在 issue 及之后）/ 用户主动 |
| **关键能力** | 分类与打标签、责任人指派、任务创建（`/api/tasks/notify` + 飞书通知）、Issue 关闭（`/api/tasks/close`）、A2A 触发 Coding |
| **预制 Skill** | `skills/issue/`、`skills/issue-advance/`、`skills/repo-onboarding/`（首次托管引导） |

### 3.3 Coding Agent

| 属性 | 说明 |
|------|------|
| **职责** | 代码实现全流程 |
| **触发方式** | A2A 触发（Issue Agent 交办 / QA 回退），不直接感知 PR 事件 |
| **关键能力** | 需求分析 → 技术方案 → 编码 → 自测 → 提交 PR → A2A 通知 QA |
| **预制 Skill** | `skills/implementation/` |
| **云端能力** | 可配置华为云 CodeArts AK/SK 作为云端开发环境 |

### 3.4 QA Agent

| 属性 | 说明 |
|------|------|
| **职责** | 代码质量保障 |
| **触发方式** | A2A（来自 Coding Agent）/ `pull_request_opened`/`updated` / `note_hook_pr`（按 issue_state 阶段） |
| **关键能力** | Code Review、安全扫描、自动化测试、功能验证 |
| **不通过时** | A2A 回 Coding Agent，附评审意见和修复建议 |
| **预制 Skill** | `skills/pr/`、`skills/qa/`（PR 审查 + 工作流质量审查）、`skills/skill-pr/`（技能仓库审查） |

### 3.5 SRE Agent（含 Release 能力）

| 属性 | 说明 |
|------|------|
| **职责** | 发布管理 + 应用运维 |
| **触发方式** | A2A（QA 通过后）/ `pull_request_merged` / `build_failure` / `deploy_start|failure` / `release_created` |
| **关键能力** | 变更日志生成、构建打包、发布部署、健康检查、故障诊断、合规检查、构建调试 |
| **注意** | **全局默认关闭**（`agent-description.json` `enable:false`），需在仓库 Dashboard 手动开启后才会接收事件 |
| **预制 Skill** | `skills/changelog-gen/`、`skills/cloud-deploy/`、`skills/deploy-guard/`、`skills/build-debug/`、`skills/incident-triage/` |

### 3.6 Agent 对比总览

| Agent | 工作流位置 | 输入 | 输出 | 触发下游 |
|-------|-----------|------|------|---------|
| **VOD Agent** | 最上游 | 开发者声音、`issue_opened` | 清晰需求、Issue | Issue Agent |
| **Issue Agent** | 上游 | VOD A2A、Issue 事件 | Spec、标签、任务、责任人 | Coding Agent |
| **Coding Agent** | 中游 | 开发需求、修复 PR | 代码实现、PR | QA Agent |
| **QA Agent** | 中下游 | PR 提交/更新 | 审查结论、测试报告 | Coding(回退) / SRE(通过) |
| **SRE Agent** | 下游 | PR 合并、部署事件 | 版本发布、运维报告 | — |

### 3.7 Chat Agent（个人专属）

| 属性 | 说明 |
|------|------|
| **职责** | 每个用户的个人对话助手，与整个工作流交互 |
| **触发方式** | 用户消息（Web 面板 / 飞书 IM） |
| **关键能力** | 对话理解、查询工作流状态、提交 Issue、任务对话、跨 Agent 交互 |
| **执行引擎** | **仅可选个人范围引擎**（`scope=user`）；`direct` 模式 SSE 流式，队列模式任务轮询 |
| **预制 Skill** | `skills/chat/`、`skills/im-notify/` |

---

## 四、Agent 间协作（A2A 端到端工作流）

### 4.1 完整端到端工作流

```
VOD Agent（声音收集 + Issue 审视 — 工作流最上游）
  │
  ├── 来源 A：GitCode Issue → issue_opened 恒进 VOD 审视
  │     └── 审视清晰度 → 不清晰则与开发者沟通澄清（Issue 评论 / 飞书群）
  │          └── 清晰后按模板总结 Issue
  ├── 来源 B：飞书 VOD Bot 多轮引导 → 收敛为清晰需求 → 创建 Issue
  │
  └── A2A 交办 Issue Agent
        │
        ▼
      Issue Agent
        ├── spec 设计、打标签、排里程碑
        ├── 分配责任人（/api/tasks/notify → 建任务 + 飞书通知）
        └── A2A 触发 Coding Agent
              │
              ▼
            Coding Agent
              ├── 理解需求 → 设计方案 → 编码实现 → 自测
              └── 提交 Pull Request → A2A 触发 QA Agent
                    │
                    ▼
                  QA Agent
                    ├── Code Review / 安全扫描 / 自动化测试 / 功能验证
                    ├── 通过 → 合并 PR → A2A 触发 SRE Agent
                    │         │
                    │         ▼
                    │       SRE Agent
                    │         ├── CHANGELOG → 构建打包 → 发布部署
                    │         └── 健康检查 → 持续监控
                    │
                    └── 不通过 → A2A 回 Coding Agent
                                 附评审意见和修复建议 → 修复后重新提交
```

### 4.2 Chat Agent 与工作流的交互

```
Chat Agent（个人专属，每个用户一个，仅个人引擎）
  │
  ├── 与 VOD Agent 交互："最近有什么重要的开发者反馈？" → 分析报告
  ├── 与 Issue Agent 交互："Issue #342 处理到哪了？" → 查询状态
  ├── 与 Coding Agent 交互："PR #1278 的改动范围是什么？" → 变更摘要
  ├── 与 QA Agent 交互："帮我审批 PR #1278" → 审批
  └── 与 SRE Agent 交互："线上服务正常吗？" → 健康检查报告

Chat Agent 是用户与整个 Agentic DevOps 工作流的统一入口。
```

### 4.3 Issue 生命周期状态机（new-version2）

```
issue_opened ──► vod ──A2A──► issue ──A2A──► coding ──A2A──► qa ──A2A──► sre ──► done
                   ▲            ▲              │  ▲            │
                   └──── 回退 ───┘              └──回退─────────┘  （qa 发现问题 A2A 回 coding）
```

- 权威存储：`issue_state` 表（`repo_key, issue_id` 唯一，`stage` / `owner_agent`）。
- 路由：`issue_opened` 恒给 VOD；Issue 类事件（updated/comment/labeled）按阶段归 VOD 或 Issue Agent；PR 类事件按阶段归 coding/qa/sre；`done` 阶段不再分发。
- `event.json`（Issue 档案）为**背景档案**（记录每个 Agent 的处理流程与结构化产出），**不作状态指引**。

### 4.4 事件档案（event.json，new-version2）

每个关联 Issue 在 `data/workspaces/{repo_key}/issues/{n}/event.json` 维护一份事件档案，由任务生命周期驱动（`init → append_log → finalize`）：

```json
{
  "issue": {"number": 345, "title": "...", "status": "open"},
  "status": "completed",
  "pipeline": [{
    "seq": 128, "agent": "vod", "event_type": "issue_opened",
    "runtime": "opencode", "skill": "vod",
    "status": "completed", "exit_code": 0, "duration": 12,
    "input": {"issue_id": "#345", "summary": "..."},
    "output": {
      "actions": ["...", "..."], "conclusion": "...",
      "artifacts": ["..."], "next_step": "A2A: issue",
      "issue_summary": "..."
    },
    "a2a_to": "issue", "logs": [...], "completed_at": "..."
  }],
  "summary": "..."        // 有 issue_summary 才覆盖，否则保持占位
}
```

- 该档案在 Event Detail 中以结构化卡片展示；后续 Agent 执行时由 `build_issue_context()` 生成富上下文注入 Prompt。

---

## 五、用户可配置项

| 配置项 | 说明 | 配置入口 |
|--------|------|---------|
| **开发场景** | 选择场景（通用/云原生/IaC/Agent/Skill 开发），一键刷新 Agent-Skill 映射 | Dashboard → Agent Workflow 右上角场景选择器 |
| **Agent 启用开关** | 按仓库开启/关闭某个 Agent（SRE 默认关）；关闭后不再路由事件 | Dashboard → Agent 卡片开关（仅 Maintainer） |
| **Agent 运行时引擎** | 绑定具体引擎实例（DevOps Agent 仅仓库/全局；Chat Agent 仅个人） | Agent 详情 → Configure → Backend Engine |
| **Agent 技能** | 为仓库的每个 Agent 指定官方/仓库/个人技能 | Agent 详情 → Configure → Skill |
| **已加入/已托管 Repo** | 查看与操作参与/托管的仓库 | 仓库选择大堂（工作项目页签） |
| **项目模板** | 从模板一键创建项目（建仓+托管+应用场景+首次引导） | 仓库选择大堂 → 项目模板 |
| **GitCode Token** | 用户个人 API Token（OAuth 自动填充，Chat Agent 使用） | Settings → Credentials |
| **云平台凭据** | 华为云 AK/SK（Coding Agent 云端环境）；CodeArts AK/SK | Settings → Credentials |
| **GitCode Bot Token** | 仓库级 DevOps Bot 凭证（DevOps Agents 使用） | Settings → GitCode Repo |
| **通知偏好** | 哪些事件需要推送 IM | Settings → Notifications |
| **IM 配置** | Chat Agent 绑定飞书/企微/钉钉（App ID + Secret） | Chat Agent 页右栏 Config |
| **技能上传/转换** | 上传官方/仓库/个人技能 ZIP；个人技能可转仓库技能 | Skills Library |
| **执行模式** | direct / queue_local / queue_remote | Agent Runtimes 页（管理员） |

---

## 六、技能系统（Skills）

### 6.1 预制技能

| Agent | 预制 Skill 目录 | 说明 |
|-------|----------------|------|
| VOD Agent | `skills/vod/` | 声音分析 + 需求引导 + Issue 审视 |
| Issue Agent | `skills/issue/`、`skills/issue-advance/`、`skills/repo-onboarding/` | 分类、标签、指派、spec、引导 |
| Coding Agent | `skills/implementation/` | 需求分析→编码→自测→PR |
| QA Agent | `skills/pr/`、`skills/qa/`、`skills/skill-pr/` | PR 审查、质量审查 |
| SRE Agent | `skills/changelog-gen/`、`skills/cloud-deploy/`、`skills/deploy-guard/`、`skills/build-debug/`、`skills/incident-triage/` | 发布、运维、监控、故障 |
| Chat Agent | `skills/chat/`、`skills/im-notify/` | 对话、通知推送 |

### 6.2 自定义技能

- 个人技能（`scope=user`）与仓库技能（`scope=repo`，绑定仓库）。
- 个人技能可**转为仓库技能**（复制，原技能保留），供该仓库的 DevOps/Chat Agent 配置使用。
- 技能通过 ZIP 上传（目录名 = 技能名，内含 SKILL.md + YAML Front Matter name/version/description），后端校验格式与安全限制，必须指定所属 Agent 类型。
- 官方技能由 `configs/skill-description.json` 驱动，打开技能库时自动同步新增项。

### 6.3 事件处理流程（new-version2）

```
事件到达 webhook:8080（GitCode 签名校验）
  → run_pipeline():
     ├── 1. 归一化（vcs/adapter.py — gitcode/github 事件转统一格式）
     ├── 2. 反循环过滤（actor 为任一 bot 用户名 → 跳过；纯标签变更回声 → 跳过）
     ├── 3. 领域路由：issue_opened → vod；Issue/PR 事件按 issue_state 阶段矩阵（D9）判定；
     │     非 Issue 事件走静态映射；主路由为空时回退仓库级 subscribe_events（SRE PR 订阅）
     ├── 3.1 Agent 启用检查（devops_agent_session.enable / agent_type.enable）
     ├── 3.2 Issue 生命周期状态机路由（issue_state 阶段 → vod/issue/coding/qa/sre）
     ├── 4. Agent 会话查找（devops_agent_session → runtime_id + skill）
     │     无会话 → 从 agent-skill-mapping.json 读默认 skill
     ├── 5. append_event（events 表，status=running）
     └── 6. 执行分派：
            direct ──► 线程就地 stream_agent 执行 → update_event_complete
            queue_* ──► init_event_json（建档案）→ enqueue_task（agent_tasks，确定路由 runtime_id）
                          └─► 本地 worker / 远端 RuntimeAgent claim → 执行
                                └─► messages/complete/fail 回报
                                      └─► parse_agent_output_json → finalize_event_archive
                                          → update_event_complete（events 表 result）
```

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
│  API :3000        │  │  Webhook :8080        │
│  src.api.main     │  │  src.api.run_webhook  │
│  SPA + API + 用户   │  │  Webhook + A2A 端点   │
│  Feishu WS        │  │  + /api/runtime/*     │
│  (nginx 反代 80/443)│  │  （边端 claim 协议）    │
└────────┬─────────┘  └──────────┬───────────┘
         │                       │
         └───────────┬───────────┘
                     ▼
┌─────────────────────────────────────────────────────────┐
│              事件处理管道（event_pipeline.py）            │
│   归一化 → 反循环 → 领域路由 → enable 检查 →              │
│   阶段状态机路由 → 会话查找 → append_event →              │
│   执行分派（direct / enqueue_task）                      │
└────────────────────┬────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────┐
│     任务队列 + RuntimeAgent（new-version2 核心）          │
│                                                         │
│  agent_tasks 表（queued→dispatched→running→终态）        │
│    ├─ 本地 worker：进程内线程池 claim + stream_runtime    │
│    └─ 远端 RuntimeAgent：/api/runtime/* claim + 执行     │
│        （rta_ 凭据认证、心跳、健康四态、多实例键隔离队列）   │
│                                                         │
│  回报驱动 event.json 档案 + events 表终态                 │
└─────────────────────────────────────────────────────────┘
```

### 7.1 端口与协议

| 端口 | 用途 | 协议 |
|------|------|------|
| **3000** | 主 API + SPA + Feishu WS（`uvicorn src.api.main:app`），nginx 反代对外 80/443 | HTTP/WSS |
| **8080** | Webhook 端点（`uvicorn src.api.run_webhook:app`，webhook + A2A + `/api/runtime/*` 协议端点） | HTTP |

### 7.2 执行引擎与运行时

| 组件 | 说明 |
|------|------|
| **agent_runtimes** | 引擎实例表（`opencode/codearts/cursor/codex`…），多实例键 `(credential_id, name, scope, owner_id)` |
| **runtime_credentials** | Worker 凭据表（`rta_` token，sha256 存储），name 全局唯一，scope 个人/仓库/全局 |
| **agent_tasks** | 任务队列（确定路由 runtime_id + 事件档案快照 + 技能交付 skill_files） |
| **执行模式** | `direct` / `queue_local` / `queue_remote`（`meta.execution_mode`） |
| **RuntimeAgent 边端** | `python -m runtime.agent`，独立进程注册/心跳/claim/执行/回报 |

### 7.3 数据存储

| 存储 | 内容 | 说明 |
|------|------|------|
| `data/sessions.db` | 25 张业务表（用户、仓库、Agent、事件、任务、聊天、技能、运行时等） | SQLite 单库，WAL + busy_timeout |
| `data/workspaces/` | Agent 执行工作目录、event.json 事件档案、project-info 项目知识基线 | 文件系统 |
| `skills/` | 官方技能 + 个人技能（`custom/{uid}/`）+ 仓库技能（`repo/{repo_key}/`） | 文件系统 |
| `configs/` | 事件路由、场景映射（`agent-skill-mapping.json`）、Agent 描述（`agent-description.json`）、官方技能（`skill-description.json`）、项目模板（`project-templates.json`） | JSON 文件 |

---

## 八、安全措施

| 措施 | 说明 |
|------|------|
| **OAuth 联邦登录** | GitCode 账号一键登录，state 防 CSRF；**白名单控制**新用户准入 |
| **登录白名单** | `configs/login-whitelist.json`，未在白名单的新 GitCode 账号拒绝登录 |
| **Session Token** | 登录态 token，登出即失效 |
| **Webhook Token** | webhook 签名/Token 验证（managed_repos 按 repo 存预期 token） |
| **rta_ 凭据隔离（D2）** | `/api/runtime/*` 仅 `rta_` 凭据可过（sha256 比对 active 凭据），用户登录 token 拒收 |
| **A2A 内部调用** | 仅限 localhost:8080，不对外暴露 |
| **反循环保护** | Bot 用户名的事件自动跳过；`issue_updated` 纯标签变更跳过 |
| **Agent 启用门禁** | 仓库级 `enable` 控制事件路由与 A2A 调用 |
| **凭据隔离** | 用户个人 GitCode Token（Chat Agent 用）与 DevOps Bot Token（DevOps Agents 用）隔离；各凭据 sha256 存储 |
| **日志脱敏** | agent_logs / event.json 中 token 统一替换为 `[REDACTED]` |
| **自定义 Skill 校验** | 上传时校验格式、安全限制（含可执行文件等） |
| **路径安全** | 解除托管清理时对 owner/repo 做路径穿越校验 |
