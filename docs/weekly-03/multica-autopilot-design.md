# Multica 自动化任务（Autopilot）功能设计文档

> 基于 multica 源码分析（`packages/views/autopilots/`）+ 官方文档（https://multica.ai/docs/autopilots），
> 输出完整的自动化任务功能设计供 grape 参考。

## 1. 页面结构总览

Multica 的 Autopilot 功能由三个核心页面组成：

| 页面 | 路由 | 组件 | 说明 |
|------|------|------|------|
| 列表页 | `/[workspace]/autopilots` | `AutopilotsPage` | 任务列表 + 工具栏 + 批量操作 |
| 详情页 | `/[workspace]/autopilots/[id]` | `AutopilotDetailPage` | 任务配置 + 触发器 + 执行历史 |
| 创建/编辑对话框 | （浮层） | `AutopilotDialog` | 新建/编辑任务表单 |

技术栈：React + Next.js App Router + TanStack Query + Tailwind CSS + shadcn/ui。

---

## 2. 列表页设计（AutopilotsPage）

### 2.1 页面布局

```
┌─────────────────────────────────────────────────────────┐
│ CollectionPageHeader                                     │
│   ⚡ Autopilot (count: N)                    [+ New]    │
├─────────────────────────────────────────────────────────┤
│ AutopilotListToolbar                                     │
│   [All|Active|Paused]  [筛选▼] [排序▼] [列▼]  N/total   │
├─────────────────────────────────────────────────────────┤
│ ListGrid（虚拟滚动）                                      │
│   ☐ | Name        | Assignee | Trigger | LastRun | ... │
│   ☐ | Daily News  | @agent1  | 🕒Schedule | 2h ago |   │
│   ☐ | PR Review   | @squad1  | 🕒Schedule | 1d ago |   │
│   ☐ | Bug Triage  | @agent2  | 🔗Webhook  | —      |   │
│   ...                                                    │
├─────────────────────────────────────────────────────────┤
│ AutopilotBatchToolbar（选中行时显示）                      │
│   2 selected  [Pause] [Resume] [Delete]  [Clear]        │
└─────────────────────────────────────────────────────────┘
```

### 2.2 空状态设计

当没有自动化任务时，页面展示**模板卡片网格**（6 个预设模板）：

| 模板 | 图标 | 调度 | 说明 |
|------|------|------|------|
| Daily news digest | 📰 Newspaper | 每天 09:00 | 搜索并汇总今日新闻 |
| PR review reminder | 🔀 GitPullRequest | 工作日 10:00 | 标记需要 Review 的过期 PR |
| Bug triage | 🐛 Bug | 工作日 09:00 | 评估并优先排序新 Bug |
| Weekly progress report | 📊 BarChart3 | 周一 17:00 | 编写周报 |
| Dependency audit | 🛡 Shield | 周一 08:00 | 依赖安全审计 |
| Documentation check | 🔍 FileSearch | 周一 14:00 | 文档覆盖检查 |

每个模板卡片可点击直接进入创建对话框，预填标题和 prompt。底部还有"Start from scratch"按钮。

### 2.3 表格列定义

| 列 | 说明 | 默认显示 | 可排序 |
|----|------|----------|--------|
| ☐ | 多选 checkbox | ✓ | — |
| Name | 任务名称 + 暂停标记 | ✓ | ✓ |
| Assignee | 负责人（Agent/Squad）头像+名称 | ✓ | — |
| Trigger | 触发器类型（Schedule/Webhook/API） | ✓（≥2xl） | — |
| Last Run | 上次运行状态点 + 相对时间 | ✓（≥2xl） | ✓ |
| Next Run | 下次运行时间（按时区格式化） | ✓（≥2xl） | ✓ |
| Mode | 执行模式（Create Issue / Run Only） | 默认隐藏 | — |
| Creator | 创建者头像+名称 | 默认隐藏 | — |
| Created | 创建日期 | 默认隐藏 | — |
| ⋯ | 行操作菜单 | ✓ | — |

**响应式设计**：
- 容器 ≥ 672px（`@2xl`）：显示所有用户启用的列，水平滚动
- 容器 < 672px：只显示 Name + Assignee，无水平滚动

**列可配置**：用户可通过工具栏的"列"下拉菜单显示/隐藏列，配置持久化到 store。

### 2.4 工具栏（AutopilotListToolbar）

- **Scope 切换**：All / Active / Paused（显示各 scope 计数）
- **筛选器**（Popover 多选）：
  - Assignee 筛选（Agent/Squad/Member）
  - Execution Mode 筛选（Create Issue / Run Only）
  - Trigger Kind 筛选（Schedule / Webhook / API）
  - Creator 筛选
- **排序**：字段选择（Name / Last Run / Next Run / Created）+ 方向（ASC/DESC）
- **列配置**：Checkbox 列表显示/隐藏列
- **结果计数**：显示 "N / total"

### 2.5 行操作（AutopilotRowActions）

每行右侧 ⋯ 菜单：
- Run Now（立即触发）
- Pause / Resume（暂停/恢复）
- Edit（编辑）
- Delete（删除）

### 2.6 批量操作（AutopilotBatchToolbar）

选中多行时底部浮动显示：
- 选中计数
- Pause All / Resume All / Delete All
- Clear（取消选择）

### 2.7 虚拟滚动

使用 `@tanstack/react-virtual` 实现行虚拟化：
- 固定行高 48px
- overscan 10 行
- 上下 padding 模拟未渲染行

---

## 3. 详情页设计（AutopilotDetailPage）

### 3.1 页面布局

```
┌─────────────────────────────────────────────────────────┐
│ BreadcrumbHeader                                        │
│   Autopilots > [Title]              [Edit] [Run Now]    │
├─────────────────────────────────────────────────────────┤
│ ┌─ 任务信息 ──────────────────────────────────────────┐ │
│ │ Title: [可编辑标题]                                  │ │
│ │ Description: [Markdown 内容预览]                     │ │
│ │ Assignee: [Agent 头像 + 名称]  Mode: [Create Issue] │ │
│ │ Project: [项目链接]  Status: [Active/Paused 开关]    │ │
│ └──────────────────────────────────────────────────────┘ │
│                                                          │
│ ┌─ 触发器（Triggers）─────────────────────────────────┐ │
│ │ + Add Trigger                                        │ │
│ │ ┌─ Trigger 1 ──────────────────────────────────────┐ │ │
│ │ │ 🕒 Schedule  cron: "0 9 * * 1-5"  tz: Asia/Shanghai│ │ │
│ │ │ Next run: 2026-09-17 09:00  [Enabled Switch]     │ │ │
│ │ │                              [Rotate] [Delete]    │ │ │
│ │ └──────────────────────────────────────────────────┘ │ │
│ │ ┌─ Trigger 2 ──────────────────────────────────────┐ │ │
│ │ │ 🔗 Webhook  URL: https://.../webhooks/autopilots/│ │ │
│ │ │ Event Filters: [push, pull_request]              │ │ │
│ │ │ Last fired: 2h ago  [Rotate Token] [Delete]      │ │ │
│ │ └──────────────────────────────────────────────────┘ │ │
│ └──────────────────────────────────────────────────────┘ │
│                                                          │
│ ┌─ 执行历史（Run History）────────────────────────────┐ │
│ │ ✅ Completed  schedule  Issue #42 linked  2h ago    │ │
│ │ ❌ Failed     webhook   timeout (30s)    5h ago    │ │
│ │ 🔄 Running    manual    —                just now  │ │
│ │ ⏭ Skipped    schedule  runtime offline   1d ago   │ │
│ │ [Load More]                                          │ │
│ └──────────────────────────────────────────────────────┘ │
│                                                          │
│ ┌─ Webhook Deliveries（仅 webhook 触发器）────────────┐ │
│ │ queued → dispatched → completed  attempt: 1        │ │
│ │ rejected (event filter mismatch)                    │ │
│ └──────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────┘
```

### 3.2 执行历史行（RunRow）

每行显示：
- **状态图标**：✅ Completed（绿）/ ❌ Failed（红）/ 🔄 Running（蓝+旋转）/ ⏭ Skipped（灰）/ 📋 Issue Created（蓝）
- **状态文案**：本地化状态名称
- **触发来源**：schedule / manual / webhook / api
- **结果/错误**：关联 Issue 链接 或 失败原因
- **时间**：按读者时区格式化
- **Transcript 按钮**：点击查看 Agent 执行记录（run_only 模式）
- **Webhook Payload**：点击查看触发 payload 详情

跳过的执行默认隐藏，底部可展开查看。

### 3.3 触发器管理

每个触发器独立卡片：
- **Schedule 触发器**：cron 表达式 + 时区 + 下次运行时间 + 启用开关 + 编辑 + 删除
- **Webhook 触发器**：Webhook URL + 事件筛选器 + 最近触发时间 + 轮换 Token + 删除

添加触发器对话框支持选择类型（Schedule / Webhook）：
- Schedule：使用可视化调度编辑器
- Webhook：自动生成 URL + 事件筛选配置

---

## 4. 创建/编辑对话框（AutopilotDialog）

### 4.1 表单字段

| 字段 | 类型 | 说明 |
|------|------|------|
| Title | 文本编辑器 | 任务名称 |
| Description | Markdown 编辑器 | 任务 prompt（Agent 执行指令） |
| Assignee | Agent/Squad 选择器 | 负责执行的 Agent 或 Squad |
| Project | 项目选择器 | 关联项目（可选） |
| Execution Mode | 分段切换 | Create Issue（创建 Issue）/ Run Only（仅执行） |
| Schedule | 调度编辑器 | cron 调度配置 |
| Subscribers | 多选成员 | 通知订阅者 |
| Access | 权限管理 | 协作者管理 |

### 4.2 调度编辑器（ScheduleEditor）

**可视化 cron 编辑器**，不要求用户直接写 cron 表达式：

**时间模式（TimePattern）**：
- **At**：固定时间点 `"HH:MM"`（如 09:00）
- **Every**：间隔模式
  - 每 N 分钟（1-59）
  - 每 N 小时（1-23）
  - 可选时间窗口（如 09:00-18:00 之间每 2 小时）

**日期模式（DayPattern）**：
- **Every**：每天
- **Weekly**：按周几选择（0=Sun ~ 6=Sat，多选）
- **Monthly**：每月几号（1-31）

**时区选择**：IANA 时区列表，默认浏览器时区

**cron 预览**：实时显示等效 cron 表达式 + 下几次运行时间预览

**高级模式**：当结构化模型无法表达时（如复杂 cron），切换为原始 cron 输入框

---

## 5. 数据模型

### 5.1 Autopilot（任务）

```typescript
interface Autopilot {
  id: string;
  title: string;
  description: string | null;       // Agent prompt
  project_id?: string | null;
  assignee_type: "agent" | "squad";
  assignee_id: string;
  status: "active" | "paused" | "archived";
  pause_reason?: string | null;     // "agent_runtime_required" 等
  execution_mode: "create_issue" | "run_only";
  issue_title_template: string | null;
  created_by_type: string;
  created_by_id: string;
  last_run_at: string | null;
  last_run_status?: string | null;  // "completed" | "failed" | "running" | "skipped" | "issue_created"
  next_run_at?: string | null;
  trigger_kinds?: string[];         // ["schedule", "webhook", "api"]
  can_write?: boolean;              // 权限
  can_manage_access?: boolean;
  subscribers?: AutopilotSubscriber[];
}
```

### 5.2 AutopilotTrigger（触发器）

```typescript
interface AutopilotTrigger {
  id: string;
  autopilot_id: string;
  kind: "schedule" | "webhook" | "api";
  enabled: boolean;
  cron_expression: string | null;
  timezone: string | null;
  next_run_at: string | null;
  webhook_token: string | null;
  webhook_url?: string | null;
  label: string | null;
  event_filters?: WebhookEventFilter[] | null;
  last_fired_at: string | null;
}
```

### 5.3 AutopilotRun（执行记录）

```typescript
interface AutopilotRun {
  id: string;
  autopilot_id: string;
  trigger_id: string | null;
  source: "schedule" | "manual" | "webhook" | "api";
  status: "issue_created" | "running" | "completed" | "failed" | "skipped";
  issue_id: string | null;          // 关联的 Issue
  task_id: string | null;           // 关联的 Agent Task
  triggered_at: string;
  completed_at: string | null;
  failure_reason: string | null;
  reason_code?: string;             // 本地化失败原因码
  trigger_payload: unknown;         // webhook payload
  result: unknown;
}
```

---

## 6. Webhook 与运维机制

### 6.1 Webhook 请求规范

- **请求体**：必须是有效的 JSON object 或 array，最大 256 KiB
- **幂等性**：支持 `Idempotency-Key` / `X-GitHub-Delivery` 请求头去重——没有稳定幂等键时，重复请求无法保证只执行一次
- **签名验证**：触发器可配置 signing secret，请求需带签名（HMAC）；签名不匹配返回 401 `rejected`
- **payload 传递**：webhook payload 保存到投递记录和执行记录中；create-issue 模式下还会追加到 Issue 描述

### 6.2 Webhook 事件推断顺序

当 webhook 收到请求时，按以下顺序推断事件名称：

1. body 中的 `event` 字段（直接使用）
2. `X-GitHub-Event` 请求头 + body 中的 `action` → `github.<event>.<action>`
3. `X-Gitlab-Event` 请求头
4. `X-Event-Type` 请求头
5. body 中的 `event`、`type`、`action` 字段组合
6. 以上均缺失时记录为 `webhook.received`

### 6.3 Webhook 事件筛选

每个 webhook 触发器可配置多行事件筛选规则：
- 每行一个事件名称 + 可选的 action 列表
- 任意一行匹配即触发执行
- 所有行为空时接受所有事件
- 示例：`workflow_run` + actions `completed, failed`

### 6.4 Webhook 响应码

| HTTP | status | 含义 |
|------|--------|------|
| 200 | `accepted` | 已接受并创建执行，返回 delivery 和 run ID |
| 200 | `skipped` | 已接受但跳过（如 runtime 离线），包含原因 |
| 200 | `ignored` | 未创建执行——触发器禁用/autopilot 暂停/事件不匹配，`reason` 字段说明原因 |
| 200 | `duplicate` | 幂等键命中已有投递，返回原投递 ID，不重复执行 |
| 400 | — | 请求体为空、非 JSON、非 object/array |
| 401 | `rejected` | 签名验证失败 |
| 404 | — | URL 中的 token 无效或已轮换 |
| 413 | — | 请求体超过 256 KiB |
| 429 | — | 请求过多，按 `Retry-After` 响应头重试 |
| 500 | — | 服务端内部错误 |

业务级忽略（暂停/归档/事件筛选）返回 200 而非 4xx，避免发送方无限重试。

### 6.5 Webhook 投递追踪与重放

- 每次投递独立记录：状态（queued → dispatched → completed/rejected/failed）、签名验证状态、重试次数、响应状态码、解析后的事件名
- 投递去重：基于 dedupe_key（来自幂等键或 GitHub delivery ID）
- **重放规则**：
  - 只有完全处理（completed/failed/ignored）的投递可重放
  - 签名验证失败或仍在排队中的投递不能重放
  - 重放创建新的投递和执行，不修改原始记录
  - 重放不参与去重

### 6.6 自动暂停机制

- 系统定期检查最近 7 天的执行记录
- 当完成+失败执行 ≥ 50 次且失败率 ≥ 90% 时，**自动暂停**该 autopilot
- 暂停后通知创建者
- 用户修复原因后需**手动恢复**

### 6.7 失败重试规则

- **Run Only 模式**：失败的执行不自动重试，下次调度照常触发
- **Create Issue 模式**：产生常规 Issue 执行，基础设施失败遵循通用执行失败规则

### 6.8 Webhook URL 安全

- URL 中的 token 是调用凭证——不要放在公开仓库/Issue/截图中
- 如果 URL 泄露，点击 **Rotate URL** 按钮轮换，旧 URL 立即失效
- 只有创建者 + Owner/Admin + 协作者可查看完整 URL
- UI 默认隐藏 token，点击 URL 或眼睛图标查看完整地址

### 6.9 CLI 支持

```bash
multica autopilot get <id> --output json           # 查看详情
multica autopilot trigger <id>                      # 手动触发
multica autopilot runs <id>                         # 查看执行历史
multica autopilot trigger-rotate-url <id> <trigger-id>  # 轮换 webhook token
```

`autopilot get` 默认返回 `has_webhook_token` + `webhook_token_hint`（不泄露 token）；加 `--show-secrets` 才返回完整凭证。

### 6.10 删除即归档

- 删除 autopilot 实际是**归档**（archived 状态）
- 未来触发器停止触发
- 执行历史和投递记录保留
- 不可恢复（但数据不删）

## 7. 关键设计特点

### 7.1 模板驱动的新建体验
- 空状态展示 6 个预设模板卡片
- 每个模板预填 prompt + schedule
- prompt 保持英文（直接注入 Agent），标题用 i18n 本地化
- 也支持"Start from scratch"空白创建

### 7.2 多触发器支持
- 一个 Autopilot 可绑定多个触发器（Schedule + Webhook 混合）
- 每个触发器独立启用/禁用
- Webhook 触发器支持事件筛选（如只响应 push 事件）
- Webhook Token 可轮换

### 7.3 双执行模式
- **Create Issue**：每次触发创建一个 Issue，Agent 在 Issue 上工作
- **Run Only**：直接执行 Agent Task，不创建 Issue

### 7.4 权限模型
- `can_write`：创建者 + Workspace Owner/Admin + 协作者
- `can_manage_access`：仅创建者 + Workspace Owner/Admin
- 协作者（Collaborator）可编辑但不能管理权限

### 7.5 虚拟滚动 + 响应式
- 列表使用 `@tanstack/react-virtual` 虚拟滚动
- 双区域响应式：宽屏显示所有列 + 水平滚动，窄屏只显示核心列
- 列可配置 + 持久化

### 7.6 国际化
- 完整 i18n 支持（en/zh/ja/ko/fr）
- 时间按读者时区显示
- cron 描述可本地化（"Every weekday at 9:00 AM"）

### 7.7 Webhook 投递追踪
- Webhook 触发器有独立的 Deliveries 列表
- 每次投递记录：状态（queued→dispatched→completed/rejected/failed）、签名验证、重试次数、响应状态码
- 支持投递重放

---

## 8. 与 Grape 当前实现的对比

| 维度 | Multica | Grape 当前 |
|------|---------|------------|
| 列表展示 | 虚拟滚动 + 可配置列 + 模板 | 简单 HTML 表格 |
| 触发器 | 多触发器（Schedule + Webhook + API） | 仅 cron |
| 调度编辑器 | 可视化（时间+日期+时区+cron预览） | 直接输入 cron 字符串 |
| 执行模式 | Create Issue / Run Only | 仅 A2A 触发 |
| 执行历史 | 详细（状态+来源+Issue链接+Transcript） | 基础（状态+重试+错误） |
| 空状态 | 6 个模板卡片 | "暂无任务"文本 |
| 权限 | 细粒度（can_write + can_manage_access + 协作者） | 仅登录用户 |
| Webhook | 完整（URL + 事件筛选 + 投递追踪 + Token轮换） | 无 |
| 批量操作 | Pause/Resume/Delete | 无 |
| 响应式 | 双区域 + 虚拟滚动 | 固定宽度表格 |

---

## 9. Grape 改进建议（优先级排序）

### P0 — 必须改进
1. **调度编辑器**：替换直接输入 cron 为可视化编辑（时间选择 + 日期选择 + 时区 + cron 预览）
2. **空状态模板**：添加预设模板卡片引导用户创建
3. **执行历史增强**：显示触发来源 + 关联 Issue 链接 + 执行 Transcript

### P1 — 应该改进
4. **多触发器支持**：一个任务可绑定多个调度 + Webhook 触发器
5. **批量操作**：选中多行 Pause/Resume/Delete
6. **列配置**：用户可显示/隐藏列 + 排序
7. **执行模式**：支持 Create Issue / Run Only 两种模式

### P2 — 可以改进
8. **Webhook 触发器**：Webhook URL + 事件筛选 + 投递追踪
9. **权限模型**：协作者管理
10. **虚拟滚动**：大量任务时性能优化
11. **响应式设计**：窄屏适配
