# 工作台用户详情页设计方案（最终版）

> 分支：`feat/admin-style-optimization`
> 最后更新：2026-08-29

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
| 失败 | `exit_code>0 AND issue_id非空` |
| Issue 数 | `COUNT(DISTINCT CASE WHEN issue_id非空 THEN issue_id END)` |
| 对话任务 | 单独统计 `chatTaskCount`，不纳入主统计 |

### 关联方式

通过 `repo_owner` 三表 JOIN（非 `user_id`，因为 `agent_tasks.user_id` 大量为 0）：

```sql
FROM agent_tasks t
INNER JOIN managed_repos m ON t.repo_key = m.repo_key
INNER JOIN user_accounts u ON u.username = m.repo_owner
WHERE u.id = ?
```

## 页面分区

### 1. Page Header

- 返回按钮（← 返回列表页）
- 用户名 + 在线/离线标签 + 最后活跃时间
- 强制下线按钮（右侧，token_version+1 + 清 last_active_at）

### 2. 基本信息 + 凭证配置（同一卡片）

**基本信息**：紧凑横排，一行展示
- 用户名 / 昵称 / 来源（GitCode登录/其他）/ 创建时间 / 最后场景 / 发布仓库

**凭证配置**：4 个横排 chip
- GitCode / 华为云 / 飞书 / CodeArts
- 每个 chip 显示"已配置"/"未配置"
- 右上角汇总"已配置 X / 4"

### 3. 活动统计（左卡片）+ 最近活动（右卡片）

#### 活动统计 — 两个 tab

**任务统计 tab**（默认）— 5 个统计卡：
- 任务总数 / 已完成 / 进行中 / 失败 / 成功率
- 全局统计（所有仓库合计，只统计 Issue 任务）

**仓库与 Issue tab**：
1. 下拉框选仓库（只显示仓库名，不显示任务数）
2. 选中后显示仓库总体统计 — 6 个卡：
   - 任务总数 / Issue 总数 / 已完成 / 进行中 / 失败 / 成功率
3. 下方 Issue 分页表格：
   - 列：Issue / 任务 / 完成 / 进行中 / 失败 / 成功率 / 最近活动
   - Issue 号可点击跳转 GitCode（`https://gitcode.com/{owner}/{repo}/issues/{num}`）
   - 按 `MAX(started_at) DESC` 排序（最近活动的 Issue 排前面）
   - 后端分页（`GET /repo-issues?repoKey=xxx&page=0&size=5`）
   - 分页选项：5 / 10（默认 5）

#### 最近活动 — 三个 tab

**最近执行**（默认）— 最近 5 条 Issue 任务（按时间倒序）
**进行中** — 最近 5 条进行中 Issue 任务（`exit_code IS NULL OR completed_at IS NULL`）
**最近失败** — 最近 5 条失败 Issue 任务（`exit_code>0`）

每条任务显示：
- 状态圆点（绿=完成 红=失败 橙=进行中）
- 仓库名
- Issue 号（可点击跳转 GitCode）
- 状态标签
- Agent · Skill
- 时间

## 后端 API

### `GET /api/admin/console-users/{id}` — 用户详情

返回：
```
{
  id, username, nickname, gitcodeId, online, lastActiveAt, createdAt, lastScene,
  gitcodePublishRepo, githubPublishRepo,
  hasGitcodeToken, hasHuaweiAk, hasHuaweiSk, hasFeishu, hasFeishuSecret,
  hasCodeartsAk, hasCodeartsSk,
  
  // 任务总览统计（只统计 Issue 任务）
  taskCount, completedCount, pendingCount, failedCount, successRate,
  chatTaskCount,  // 对话任务数（单独统计）
  
  // 仓库 × Issue 矩阵
  repoMatrix: [{ fullName, repoKey, taskCount, issueCount, completed, failed, pending }],
  issueDetails: [{ repoKey, fullName, issueId, taskCount, completed, failed, pending }],
  repoCount,
  
  // 最近活动（只统计 Issue 任务）
  recentTasks: [{ id, repoName, issueId, exitCode, agentType, skill, startedAt, completedAt, status }],
  recentPendingTasks: [...],
  recentFailedTasks: [...]
}
```

### `GET /api/admin/console-users/{id}/repo-issues?repoKey=xxx&page=0&size=5` — 仓库 Issue 分页

返回：
```
{
  content: [{ issueId, taskCount, completed, failed, pending, lastTaskAt }],
  totalElements, totalPages, page, size
}
```

- `repoKey` 用 query 参数（不用路径参数，因为 repoKey 含 `/`）
- 按 `MAX(started_at) DESC` 排序
- 只统计有 issue_id 的任务

### `POST /api/admin/console-users/{id}/reset-token` — 强制下线

- `token_version + 1`（旧 token 失效）
- 清空 `last_active_at`（状态变离线）

## 在线状态机制

### 原理

grape 主服务每次 API 请求 → `get_current_user` → `_pending_active.add(uid)` → 后台每 30 秒批量 flush → `UPDATE user_accounts SET last_active_at=datetime('now') WHERE id IN (...)`

### 判断

- `last_active_at >= NOW() - 30分钟` → 在线
- 否则 → 离线

### 集群安全

- 多实例各自 flush，`last_active_at=NOW()` 幂等无冲突
- 内存 set 只存活跃用户 uid（18 用户 = 72 字节）

## 改动文件

| # | 文件 | 改动 |
|---|------|------|
| 1 | `ConsoleUserDetail.vue` | 详情页完整重写 |
| 2 | `ConsoleUsers.vue` | 列表页行点击跳转详情页（去掉弹窗） |
| 3 | `ConsoleUserController.java` | detail API + repo-issues API + reset-token API |
| 4 | `router/index.js` | 路由 `/sys/console/:id` |
| 5 | `src/api/deps.py` | `_pending_active` set + `mark_user_active` |
| 6 | `src/user/db.py` | `batch_update_last_active` + `create_user` 写 `created_at` |
| 7 | `src/api/main.py` | 定时 flush `active_user_tracker` |

## 已解决的问题

| 问题 | 根因 | 修复 |
|------|------|------|
| Issue 数量对不上 | `COUNT(DISTINCT issue_id)` 包含空字符串 | 加 `CASE WHEN issue_id非空` 条件 |
| 进行中数量对不上 | 仓库统计含对话任务，Issue 列表不含 | 全部统计加 `issue_id非空` 过滤 |
| 任务总数对不上 | repoMatrix 的 taskCount/completed/failed 含对话任务 | COUNT/SUM 都加 `issue_id非空` 条件 |
| 关联查不到数据 | `agent_tasks.user_id` 大量为 0 | 改用 `repo_owner` 三表 JOIN |
| repo-issues API 404 | repoKey 含 `/` 被当路径分隔符 | 改用 query 参数 |
| create_user 导致测试失败 | `NOW()` 在 SQLite 不支持 | 改为 `datetime('now')`，mysql_compat 自动转换 |
| 最近活动看到对话任务 | recentTasks/recentPending/recentFailed 没过滤 | 三个 SQL 都加 `issue_id非空` 条件 |

## commit 历史

- `1f99659d` 来源筛选改为来源维度
- `898bd747` create_user 写入 created_at
- `1617eae8` 仓库与Issue改为下拉框+后端分页
- `e9ab519e` 下拉框只显示仓库名+统计卡加Issue总数+分页改为5/10
- `acd4a6c4` 仓库统计和Issue列表加进行中状态
- `44790bc2` 最近活动改为三个tab——最近执行/进行中/最近失败
- `45bade03` Issue数量统计排除空字符串
- `c4700c39` 仓库统计pending排除对话任务
- `e78f14da` 全量排除对话任务——任务总览+最近活动都只统计Issue任务
- `72f6689c` repoMatrix的taskCount/completed/failed也排除对话任务
