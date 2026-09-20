# Grape 自动化任务（Autopilot）页面设计文档

> 基于 multica Autopilot 设计 + grape 现有架构，适配 grape 的场景/流水线/A2A 机制。
> 前端风格完全参照 multica 的 AutopilotsPage / AutopilotDetailPage / AutopilotDialog 设计。

## 1. 概念映射

| Multica 概念 | Grape 对应 | 说明 |
|--------------|-----------|------|
| Assignee（Agent/Squad） | 场景（Scene） | grape 用场景定义 Agent 链（vod→issue→coding→qa→sre） |
| Runbook（prompt） | 任务描述 | 作为 Issue body 传入，触发 VOD 分析 |
| Trigger: Schedule | 时间表（cron） | 本期只支持 cron 时间表触发 |
| Trigger: Webhook | — | 本期不支持 |
| Execution Mode: Create Issue | 创建任务 | 本期唯一模式：每次触发创建新 Issue |
| Execution Mode: Run Only | — | 本期不支持 |
| AutopilotRun | autopilot_executions | 已有表结构 |

## 2. 页面结构总览

Grape 的 Autopilot 功能由三个核心页面组成：

| 页面 | 路由 | 说明 |
|------|------|------|
| 列表页 | 侧边栏「自动化任务」导航 | 任务列表 + 工具栏 + 批量操作 |
| 详情页 | 点击任务行跳转 | 任务配置 + 时间表 + 执行历史 |
| 创建/编辑对话框 | 浮层 | 新建/编辑任务表单 |

---

## 3. 列表页设计

### 3.1 页面布局

```
┌─────────────────────────────────────────────────────────┐
│ CollectionPageHeader                                     │
│   ⏰ 自动化任务 (N)                        [+ 新建任务]  │
├─────────────────────────────────────────────────────────┤
│ ListToolbar                                              │
│   [全部|启用|禁用]  [筛选▼] [排序▼] [列▼]  N/total       │
├─────────────────────────────────────────────────────────┤
│ ListGrid                                                 │
│   ☐ | Name        | Scene    | Cron        | LastRun  | ... │
│   ☐ | 每日新闻    | 通用开发  | 🕒0 9 * * * | 2h ago ✅|   │
│   ☐ | PR审查提醒  | 通用开发  | 🕒0 10 * * 1| 1d ago ❌|   │
│   ☐ | 依赖审计    | 云原生   | 🕒0 8 * * 1 | —        |   │
│   ...                                                    │
├─────────────────────────────────────────────────────────┤
│ BatchToolbar（选中行时显示）                              │
│   2 selected  [启用] [禁用] [删除]  [清除]              │
└─────────────────────────────────────────────────────────┘
```

### 3.2 空状态设计

当没有自动化任务时，页面展示**模板卡片网格**（6 个预设模板）：

| 模板 | 图标 | 调度 | 说明 |
|------|------|------|------|
| 每日新闻摘要 | 📰 | 每天 09:00 | 搜索并汇总今日相关技术新闻 |
| PR审查提醒 | 🔀 | 工作日 10:00 | 标记需要 Review 的过期 PR |
| Bug 分拣 | 🐛 | 工作日 09:00 | 评估并优先排序新 Bug 报告 |
| 周进度报告 | 📊 | 周一 17:00 | 编写团队周报 |
| 依赖审计 | 🛡 | 周一 08:00 | 依赖安全漏洞审计 |
| 文档检查 | 🔍 | 周一 14:00 | 检查文档覆盖完整性 |

每个模板卡片可点击直接进入创建对话框，预填标题和任务描述。底部还有"从空白创建"按钮。

### 3.3 表格列定义

| 列 | 说明 | 默认显示 | 可排序 |
|----|------|----------|--------|
| ☐ | 多选 checkbox | ✓ | — |
| Name | 任务名称 + 禁用标记 | ✓ | ✓ |
| Scene | 场景名称（通用开发/云原生/IaC 等） | ✓ | — |
| Cron | cron 表达式 + 人类可读描述 | ✓（宽屏） | — |
| Last Run | 上次运行状态点 + 相对时间 | ✓（宽屏） | ✓ |
| Next Run | 下次运行时间（按时区格式化） | ✓（宽屏） | ✓ |
| Max Retry | 最大重试次数 | 默认隐藏 | — |
| Creator | 创建者 | 默认隐藏 | — |
| Created | 创建日期 | 默认隐藏 | — |
| ⋯ | 行操作菜单 | ✓ | — |

**响应式设计**：
- 宽屏（≥ 672px）：显示所有用户启用的列，水平滚动
- 窄屏（< 672px）：只显示 Name + Scene，无水平滚动

**列可配置**：用户可通过工具栏的"列"下拉菜单显示/隐藏列，配置持久化。

### 3.4 工具栏（ListToolbar）

- **Scope 切换**：全部 / 启用 / 禁用（显示各 scope 计数）
- **筛选器**（下拉多选）：
  - 场景筛选（通用开发/云原生/IaC 等）
  - 创建者筛选
- **排序**：字段选择（Name / Last Run / Next Run / Created）+ 方向（升序/降序）
- **列配置**：Checkbox 列表显示/隐藏列
- **结果计数**：显示 "N / total"

### 3.5 行操作（RowActions）

每行右侧 ⋯ 菜单：
- 立即触发（Run Now）
- 启用 / 禁用（Enable / Disable）
- 编辑（Edit）
- 删除（Delete）

### 3.6 批量操作（BatchToolbar）

选中多行时底部浮动显示：
- 选中计数
- 启用全部 / 禁用全部 / 删除全部
- 清除（取消选择）

---

## 4. 详情页设计

### 4.1 页面布局

```
┌─────────────────────────────────────────────────────────┐
│ BreadcrumbHeader                                        │
│   自动化任务 > [任务名称]          [编辑] [立即触发]    │
├─────────────────────────────────────────────────────────┤
│ ┌─ 任务信息 ──────────────────────────────────────────┐ │
│ │ 名称: [任务名称]                                     │ │
│ │ 任务描述: [文本内容预览]                              │ │
│ │ 场景: [场景名称]  仓库: [仓库名称]                   │ │
│ │ 状态: [启用/禁用 开关]  最大重试: [3]               │ │
│ └──────────────────────────────────────────────────────┘ │
│                                                          │
│ ┌─ 时间表（Schedule）────────────────────────────────┐ │
│ │ 🕒 cron: "0 9 * * 1-5"  时区: Asia/Shanghai        │ │
│ │ 人类可读: 工作日 09:00                                │ │
│ │ 下次运行: 2026-09-17 09:00                           │ │
│ │ [启用开关]  [编辑时间表]                             │ │
│ └──────────────────────────────────────────────────────┘ │
│                                                          │
│ ┌─ 执行历史（Run History）────────────────────────────┐ │
│ │ ✅ 成功     schedule  Issue #42 已创建     2h ago   │ │
│ │ ❌ 失败     manual    a2a request timeout  5h ago   │ │
│ │ 🔄 运行中   schedule  —                     just now│ │
│ │ ⏭ 跳过     schedule  runtime offline      1d ago   │ │
│ │ [加载更多]                                           │ │
│ └──────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────┘
```

### 4.2 执行历史行（RunRow）

每行显示：
- **状态图标**：✅ 成功（绿）/ ❌ 失败（红）/ 🔄 运行中（蓝+旋转）/ ⏭ 跳过（灰）/ 📋 Issue已创建（蓝）
- **状态文案**：本地化状态名称
- **触发来源**：schedule（定时）/ manual（手动触发）
- **结果/错误**：关联 Issue 链接（点击跳转 Issue 详情） 或 失败原因
- **时间**：按读者时区格式化
- **重试信息**：当前重试次数 / 最大重试次数

跳过的执行默认隐藏，底部可展开查看。

### 4.3 时间表管理

时间表以独立卡片展示：
- cron 表达式 + 人类可读描述
- 时区
- 下次运行时间
- 启用/禁用开关
- 编辑时间表（打开可视化调度编辑器）

> 本期只支持一个时间表触发器，不支持多触发器。

---

## 5. 创建/编辑对话框（AutopilotDialog）

### 5.1 表单字段

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| 任务名称 | 文本输入 | ✓ | 自动化任务名称；触发时拼接时间戳作为 Issue 标题（如"每日新闻摘要 20260917-0900"） |
| 场景 | 下拉选择 | ✓ | 当前仓库可用场景列表（通用开发/云原生/IaC 等） |
| 任务描述 | 多行文本 | ✓ | 每次触发时作为 Issue body，传入 VOD Agent 分析 |
| 时间表 | 调度编辑器 | ✓ | 可视化 cron 配置（每天/每周/自定义） |
| 最大重试 | 数字输入 | 否 | 0-10，默认 3，失败后按指数退避重试 |

> 仓库 = 当前选中仓库，不作为表单字段。

### 5.2 调度编辑器（ScheduleEditor）

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

## 6. 可视化时间表编辑器

### 6.1 三种快捷模式

**每天**：
- 时间选择：HH:MM
- 生成 cron：`M H * * *`

**每周**：
- 周几选择：日/一/二/三/四/五/六（多选）
- 时间选择：HH:MM
- 生成 cron：`M H * * <days>`

**自定义**：
- 直接输入 5 段制 cron 表达式
- 显示人类可读描述（如"每 30 分钟"）
- 实时预览下 3 次运行时间

### 6.2 时区

- 默认使用浏览器时区（Asia/Shanghai）
- cron 表达式存储时标注时区
- 下次运行时间按用户时区显示

---

## 7. 触发执行流程

```
定时调度器扫描到期任务
  → 调用 POST /api/issues/{owner}/{repo} 创建新 Issue
    （title=任务名称+时间戳, body=任务描述, scene_id=场景ID）
  → grape 自动触发场景流水线（无需手动调 webhook）
    （VOD 分析 → Issue 细化 → Coding 编码 → QA 审查 → SRE 部署）
  → 记录 autopilot_execution（关联 Issue 编号 + A2A seq）
  → 失败按指数退避重试（base_delay * 2^retry_count，上限 300s）
```

**关键变化**：当前 autopilot 通过 A2A 直接触发已有 Issue 的 Agent 链。新设计改为**先创建新 Issue**，grape 自动触发场景流水线。这样：
- 每次自动化执行都有独立的 Issue 记录
- 流水线从 VOD 开始完整执行
- 执行历史可关联到具体 Issue
- 无需手动调 webhook，grape 内部自动联动

---

## 8. 数据模型变更

### 8.1 autopilot_tasks（新增字段）

```sql
-- 新增字段
scene_id     TEXT NOT NULL DEFAULT 'default'  -- 场景ID
description  TEXT NOT NULL DEFAULT ''         -- 任务描述（Issue body）
-- 保留字段
id, name, repo_key, cron, max_retry, enabled, last_run_at, next_run_at, created_at, updated_at
-- 废弃字段（不再使用，保留兼容）
-- agent_chain: 改为场景驱动，不再手动配置
-- issue_number: 每次创建新 Issue，不再绑定固定 Issue
```

### 8.2 autopilot_executions（新增字段）

```sql
-- 新增字段
issue_number  TEXT DEFAULT ''     -- 创建的 Issue 编号
-- 保留字段
id, task_id, status, retry_count, result, error, a2a_seq, started_at, completed_at, created_at
```

---

## 9. API 设计

### 9.1 任务 CRUD（已有，需调整）

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/autopilot/tasks` | GET | 任务列表 |
| `/api/autopilot/tasks` | POST | 创建任务（字段调整为 name/repo_key/scene_id/description/cron/max_retry） |
| `/api/autopilot/tasks/{id}` | GET | 任务详情 |
| `/api/autopilot/tasks/{id}` | PUT | 更新任务 |
| `/api/autopilot/tasks/{id}` | DELETE | 删除任务 |
| `/api/autopilot/tasks/{id}/trigger` | POST | 立即触发 |
| `/api/autopilot/executions` | GET | 执行日志列表 |

### 9.2 仓库场景列表（新增）

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/autopilot/scenes?repo_key=xxx` | GET | 返回仓库可用场景列表 |

---

## 10. 与 Multica 的差异说明

| 维度 | Multica | Grape 本期 |
|------|---------|-----------|
| 执行者 | Agent/Squad | 场景（Scene） |
| 触发方式 | Schedule + Webhook + API | 仅 Schedule |
| 执行模式 | Create Issue / Run Only | 仅 Create Issue |
| Runbook | Markdown prompt | 任务描述（Issue body） |
| 场景配置 | Agent 链手动配置 | 场景选择（场景内置 Agent 链） |
| 权限 | 协作者管理 | 仅登录用户 |
| 批量操作 | Pause/Resume/Delete | 启用/禁用/删除 |
| 虚拟滚动 | 是 | 是（参照 multica） |
| Webhook | 完整支持 | 不支持 |
| 自动暂停 | 7天/50次/90% | 不支持（后续可加） |
| 多触发器 | 是（Schedule + Webhook 混合） | 否（仅单个 Schedule） |
| 详情页 | 有 | 有（参照 multica） |
| 调度编辑器 | 可视化（时间+日期+时区+cron预览+高级模式） | 同 multica |
| 空状态模板 | 6 个模板卡片 | 同 multica（6 个模板） |
| 列可配置 | 是 | 是（参照 multica） |
| 响应式 | 双区域 + 虚拟滚动 | 同 multica |

---

## 11. 需要解决的问题

### 11.1 已解决
- **Issue 创建**：grape 已有 `POST /api/issues/{owner}/{repo}`，支持 title + body + scene_id
- **场景列表**：grape 已有 `devops_pipeline_scene` 表，可查询仓库可用场景
- **cron 调度**：已有 `src/runtime/scheduler.py`，支持 cron 扫描 + 重试

### 11.2 已确认
1. **Issue 标题**：使用「任务名称 + 时间戳」格式（如"每日新闻摘要 20260917-0900"），避免同标题冲突
2. **仓库选择**：不设仓库选择字段，使用当前选中仓库
3. **触发流程**：通过 grape API 创建 Issue 后，grape 自动触发场景流水线，无需手动调 webhook

### 11.3 待实现时处理
4. **调度器启动**：`src/runtime/scheduler.py` 的 `start_scheduler()` 未在 `main.py` 中调用，实现时需在 lifespan 中注册启动

---

## 12. 实现计划

### Phase 1：基础功能（本期）
1. 数据模型调整（autopilot_tasks 加 scene_id/description，去 agent_chain/issue_number 依赖）
2. 后端 API 调整（CreateTaskBody 字段更新）
3. scheduler 改为创建 Issue 触发（调 POST /api/issues）
4. scheduler 在 main.py 启动时注册
5. 前端列表页（CollectionPageHeader + ListToolbar + ListGrid + 空状态模板 + 批量操作 + 行操作）
6. 前端详情页（任务信息 + 时间表卡片 + 执行历史列表）
7. 前端创建/编辑对话框（场景选择 + 任务描述 + 可视化调度编辑器）
8. 前端列可配置 + 排序 + 筛选

### Phase 2：增强（后续）
9. 可视化 cron 编辑器增强（cron 预览 + 下次运行时间预览 + 高级模式）
10. 自动暂停机制
11. Webhook 触发器
12. 多触发器支持
