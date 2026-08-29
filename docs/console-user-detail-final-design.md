# 工作台用户详情页设计方案（最终版 V2）

> 分支：`feat/admin-style-optimization`
> 最后更新：2026-08-29（审查修复后）

## 页面结构

```
┌─────────────────────────────────────────────────────────────┐
│ ← 返回  xiaowu2119  [在线/离线]  最后活跃 2026-08-29 18:03  [强制下线] │
├─────────────────────────────────────────────────────────────┤
│ ┌─────────────────────────────────────────────────────────┐ │
│ │ 基本信息                              凭证 X / 4        │ │
│ │ 用户名  昵称  来源  创建时间  最后场景  发布仓库        │ │
│ │ ─────────────────────────────────────────────────────── │ │
│ │ [GitCode] [华为云] [飞书] [CodeArts]  ← 凭证横排 chip    │ │
│ └─────────────────────────────────────────────────────────┘ │
│ ┌──────────────────────┐ ┌────────────────────────────────┐│
│ │ 活动统计              │ │ 最近活动                        ││
│ │ [任务统计|仓库与Issue]│ │ [最近执行|进行中|最近失败]      ││
│ │                      │ │                                ││
│ │ 任务统计 tab:        │ │ 时间线（最近5条任务）           ││
│ │ 任务总数/已完成/      │ │ ● 仓库 Issue [状态]            ││
│ │ 进行中/失败/成功率   │ │   Agent · Skill  时间           ││
│ │                      │ │                                ││
│ │ 仓库与Issue tab:     │ │                                ││
│ │ [下拉框选仓库]       │ │                                ││
│ │ 仓库统计6卡          │ │                                ││
│ │ Issue分页表格        │ │                                ││
│ └──────────────────────┘ └────────────────────────────────┘│
└─────────────────────────────────────────────────────────────┘
```

## 统计口径（核心规则）

**所有统计只统计 Issue 任务（有 issue_id 的任务），排除对话任务（无 issue_id）。**

| 统计项 | SQL 条件 |
|--------|---------|
| 任务总数 | `issue_id IS NOT NULL AND issue_id<>''` |
| 已完成 | `exit_code=0 AND completed_at IS NOT NULL AND issue_id非空` |
| 进行中 | `(exit_code IS NULL OR completed_at IS NULL) AND issue_id非空` |
| 失败 | `exit_code<>0 AND exit_code IS NOT NULL AND issue_id非空` |
| Issue 数 | `COUNT(DISTINCT CASE WHEN issue_id非空 THEN issue_id END)` |
| 对话任务 | 单独统计 `chatTaskCount`，不纳入主统计 |

### 失败判断（审查修复）

`exit_code<>0 AND exit_code IS NOT NULL`（不是 `exit_code>0`）

原因：`agent_tasks.exit_code` 有负值（-1），表示 A2A 链路中被取消/超时/中断的任务。`exit_code>0` 会漏掉这些。

### 关联方式

通过 `repo_owner` 三表 JOIN（非 `user_id`，因为 `agent_tasks.user_id` 大量为 0）：

```sql
FROM agent_tasks t
INNER JOIN managed_repos m ON t.repo_key = m.repo_key
INNER JOIN user_accounts u ON u.username = m.repo_owner
WHERE u.id = ?
```

### repoKey 匹配（审查修复）

前端传 `gitcode:owner/repo` 格式的 repoKey，后端精确匹配 `m.repo_key=?`（不再三路 OR）。

## 在线状态（审查修复）

用 MySQL SQL 计算，不用 Java 时间判断：

```sql
(last_active_at IS NOT NULL AND last_active_at >= NOW() - INTERVAL 30 MINUTE) as is_online
```

原因：Java `LocalDateTime.now()` 和 MySQL `NOW()` 时区可能不一致（容器化部署后 JVM 默认 UTC），用 SQL 计算保证一致。

## 页面分区

### 1. Page Header

- 返回按钮（← 返回列表页）
- 用户名 + 在线/离线标签 + 最后活跃时间
- 强制下线按钮（右侧，token_version+1 + 清 last_active_at）

### 2. 基本信息 + 凭证配置（同一卡片）

**基本信息**：紧凑横排
- 用户名 / 昵称 / 来源（GitCode登录/其他）/ 创建时间 / 最后场景 / 发布仓库

**凭证配置**：4 个横排 chip（按平台统计 X/4）
- GitCode / 华为云 / 飞书 / CodeArts

### 3. 活动统计（左）+ 最近活动（右）

#### 活动统计 — 两个 tab

**任务统计 tab**（默认）— 5 个统计卡：
任务总数 / 已完成 / 进行中 / 失败 / 成功率

**仓库与 Issue tab**：
1. 下拉框选仓库（只显示仓库名）
2. 仓库总体统计 6 卡：任务总数 / Issue 总数 / 已完成 / 进行中 / 失败 / 成功率
3. Issue 分页表格：Issue / 任务 / 完成 / 进行中 / 失败 / 成功率 / 最近活动
   - Issue 号可点击跳转 GitCode
   - 按 `MAX(started_at) DESC` 排序
   - 后端分页（5/10，默认5）

#### 最近活动 — 三个 tab

**最近执行**（默认）/ **进行中** / **最近失败**

每条最近 5 条，只显示有 issue_id 的任务。

## 后端 API

### `GET /api/admin/console-users/{id}` — 用户详情

返回用户信息 + 凭证状态 + 任务统计（排除对话任务）+ repoMatrix + 最近活动（3组×5条）

### `GET /api/admin/console-users/{id}/repo-issues?repoKey=xxx&page=0&size=5` — 仓库 Issue 分页

- `repoKey` 用 query 参数（含 `/`，不能用路径参数）
- 精确匹配 `m.repo_key=?`
- total count 用 `COUNT(DISTINCT t.issue_id)`（joinSql 已过滤 issue_id）
- 按 `MAX(started_at) DESC` 排序

### `POST /api/admin/console-users/{id}/reset-token` — 强制下线

## 审查修复的 6 个问题

| # | 问题 | 修复 |
|---|------|------|
| 1 | exit_code=-1 被漏统计（49个任务） | `exit_code>0` → `exit_code<>0 AND exit_code IS NOT NULL`（6处SQL） |
| 2 | issueDetails 后端查询但前端不用 | 删除后端 issueDetails 全量查询 |
| 3 | repo-issues total SQL 条件重复 | `COUNT(DISTINCT CASE WHEN...)` → `COUNT(DISTINCT t.issue_id)` |
| 4 | isOnline Java时区不一致风险 | 改为 MySQL SQL 计算 `NOW() - INTERVAL 30 MINUTE` |
| 5 | 前端死代码 | 删除 expandedRepos/toggleRepoExpand/issuesByRepo |
| 6 | repoKey 三路OR匹配效率低 | 改为精确匹配 `m.repo_key=?` |

## 已删除的冗余代码

- 后端 `issueDetails` 全量查询（前端改后端分页后不再使用）
- 后端 `isOnline` Java 方法（改为 SQL 计算）
- 前端 `expandedRepos` / `toggleRepoExpand` / `issuesByRepo`

## 其他修复

- 点击"Grape运营管理系统"标题跳转到 `/overview`（之前跳 `/runtime/devices`）

## 改动文件

| # | 文件 | 改动 |
|---|------|------|
| 1 | `ConsoleUserDetail.vue` | 详情页完整重写 |
| 2 | `ConsoleUsers.vue` | 列表页行点击跳转详情页 |
| 3 | `ConsoleUserController.java` | detail/repo-issues/reset-token API |
| 4 | `router/index.js` | 路由 `/sys/console/:id` |
| 5 | `App.vue` | 标题跳转改为 `/overview` |
| 6 | `src/api/deps.py` | `_pending_active` set + `mark_user_active` |
| 7 | `src/user/db.py` | `batch_update_last_active` + `create_user` 写 `created_at` |
| 8 | `src/api/main.py` | 定时 flush `active_user_tracker` |

## commit 历史（本次审查修复）

- `dc3c776d` 6个问题修复——exit_code负值+死代码清理+SQL优化+时区一致性
- `3470bd92` 点击Grape运营管理系统标题跳转到总览页面
