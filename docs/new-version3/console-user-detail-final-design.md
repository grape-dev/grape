# 工作台用户系统设计方案（最终版 V3）

> 分支：`feat/admin-style-optimization`
> 最后更新：2026-08-29

## 一、工作台用户列表页（ConsoleUsers.vue）

### 表格列

| ID | 用户名 | 昵称 | GitCode ID | 登录状态 | 凭证配置 | 创建时间 | 最后活跃 | 操作 |

- 创建时间显示格式：YYYY-MM-DD HH:MM:SS
- 排序：`ORDER BY last_active_at DESC, id DESC`（在线优先，最新活跃排前面）
- 行点击跳转详情页 `/sys/console/:id`
- 分页：10/20/50（默认10）

### 统计卡

- 用户总数 / 在线 / 离线

### 徽章筛选行

- 全部 / 在线 / 离线 / GitCode登录 / 其他

### 在线状态判断

SQL 计算（不用 Java 时间）：
```sql
(last_active_at IS NOT NULL AND last_active_at >= NOW() - INTERVAL 30 MINUTE) as is_online
```

## 二、工作台用户详情页（ConsoleUserDetail.vue）

### 布局

```
┌─────────────────────────────────────────────────────────────┐
│ ← 返回  xiaowu2119  [在线/离线]  最后活跃 2026-08-29 18:03  [强制下线] │
├─────────────────────────────────────────────────────────────┤
│ 基本信息                              凭证 X / 4            │
│ 用户名  昵称  来源  创建时间  最后场景  发布仓库              │
│ [GitCode] [华为云] [飞书] [CodeArts]  ← 凭证横排 chip        │
├──────────────────────┬──────────────────────────────────────┤
│ 活动统计              │ 最近活动                              │
│ [任务统计|仓库与Issue]│ [最近执行|进行中|最近失败]            │
│                      │                                      │
│ 任务统计 tab:        │ 时间线（最近5条任务）                 │
│ 任务总数/已完成/      │ ● 仓库 Issue [状态]                  │
│ 进行中/失败/成功率   │   Agent · Skill  时间                 │
│                      │                                      │
│ 仓库与Issue tab:     │                                      │
│ [下拉框选仓库]       │                                      │
│ 仓库统计6卡          │                                      │
│ Issue分页表格        │                                      │
└──────────────────────┴──────────────────────────────────────┘
```

### 统计口径（核心规则）

**所有统计只统计 Issue 任务（有 issue_id 的任务），排除对话任务（无 issue_id）。**

| 统计项 | SQL 条件 |
|--------|---------|
| 任务总数 | `issue_id IS NOT NULL AND issue_id<>''` |
| 已完成 | `exit_code=0 AND completed_at IS NOT NULL AND issue_id非空` |
| 进行中 | `(exit_code IS NULL OR completed_at IS NULL) AND issue_id非空` |
| 失败 | `exit_code<>0 AND exit_code IS NOT NULL AND issue_id非空` |
| Issue 数 | `COUNT(DISTINCT CASE WHEN issue_id非空 THEN issue_id END)` |
| 对话任务 | 单独统计 `chatTaskCount`，不纳入主统计 |

### 失败判断

`exit_code<>0 AND exit_code IS NOT NULL`（不是 `exit_code>0`）

原因：exit_code 有负值（-1），表示被取消/超时/中断的任务。

### 关联方式

通过 `repo_owner` 三表 JOIN：
```sql
FROM agent_tasks t
INNER JOIN managed_repos m ON t.repo_key = m.repo_key
INNER JOIN user_accounts u ON u.username = m.repo_owner
WHERE u.id = ?
```

### repoKey 匹配

前端传 `gitcode:owner/repo` 格式，后端精确匹配 `m.repo_key=?`。

### 任务统计 tab

5 个统计卡：任务总数 / 已完成 / 进行中 / 失败 / 成功率

### 仓库与 Issue tab

1. 下拉框选仓库（只显示仓库名）
2. 仓库总体统计 6 卡：任务总数 / Issue 总数 / 已完成 / 进行中 / 失败 / 成功率
3. Issue 分页表格：Issue / 任务 / 完成 / 进行中 / 失败 / 成功率 / 最近活动
   - Issue 号可点击跳转 GitCode
   - 按 `MAX(started_at) DESC` 排序
   - 后端分页（5/10，默认5）
   - repoKey 用 query 参数（含 `/`，不能用路径参数）
   - total count 用 `COUNT(DISTINCT t.issue_id)`（joinSql 已过滤 issue_id）

### 最近活动 — 三个 tab

最近执行 / 进行中 / 最近失败，各最近 5 条，只显示有 issue_id 的任务。

## 三、总览页注册趋势图（Overview.vue）

### 图表

- 标题：工作台用户 · 注册趋势
- 图表类型：平滑折线图（line chart + area gradient + circle markers）
- 维度切换：年/月/日 三个按钮（默认月）
- X轴时间，Y轴注册人数
- tooltip：注册 X 人
- 标签超过6个时X轴旋转30度

### 后端 API

`GET /api/admin/stats/user-registration?granularity=month`

- year: `DATE_FORMAT(created_at, '%Y')`
- month: `DATE_FORMAT(created_at, '%Y-%m')`
- day: `DATE_FORMAT(created_at, '%Y-%m-%d')`
- 排除异常 created_at（`REGEXP '^[0-9]{4}'`）
- 返回 `[{label, count}]`

## 四、在线状态机制

### 原理

grape 主服务每次 API 请求 → `get_current_user` → `_pending_active.add(uid)` → 后台每 30 秒批量 flush → `UPDATE user_accounts SET last_active_at=datetime('now') WHERE id IN (...)`

### 判断

- `last_active_at >= NOW() - INTERVAL 30 MINUTE` → 在线
- 否则 → 离线
- 用 MySQL SQL 计算（不用 Java 时间，避免时区不一致）

### 集群安全

- 多实例各自 flush，`datetime('now')` 幂等无冲突
- 内存 set 只存活跃用户 uid

## 五、其他修复

- 点击"Grape运营管理系统"标题跳转到 `/overview`（之前跳 `/runtime/devices`）

## 六、后端 API 清单

| API | 说明 |
|-----|------|
| `GET /api/admin/console-users` | 列表（分页+筛选+统计+is_online） |
| `GET /api/admin/console-users/{id}` | 详情（凭证+统计+repoMatrix+最近活动3组） |
| `GET /api/admin/console-users/{id}/repo-issues?repoKey=xxx&page=0&size=5` | 仓库Issue分页 |
| `POST /api/admin/console-users/{id}/reset-token` | 强制下线 |
| `GET /api/admin/stats/user-registration?granularity=month` | 注册趋势 |

## 七、审查修复的 6 个问题

| # | 问题 | 修复 |
|---|------|------|
| 1 | exit_code=-1 被漏统计（49个任务） | `exit_code>0` → `exit_code<>0 AND exit_code IS NOT NULL`（6处SQL） |
| 2 | issueDetails 后端查询但前端不用 | 删除后端 issueDetails 全量查询 |
| 3 | repo-issues total SQL 条件重复 | `COUNT(DISTINCT CASE WHEN...)` → `COUNT(DISTINCT t.issue_id)` |
| 4 | isOnline Java时区不一致风险 | 改为 MySQL SQL 计算 `NOW() - INTERVAL 30 MINUTE` |
| 5 | 前端死代码 | 删除 expandedRepos/toggleRepoExpand/issuesByRepo |
| 6 | repoKey 三路OR匹配效率低 | 改为精确匹配 `m.repo_key=?` |

## 八、改动文件

| # | 文件 | 改动 |
|---|------|------|
| 1 | `ConsoleUserDetail.vue` | 详情页完整重写 |
| 2 | `ConsoleUsers.vue` | 列表页+创建时间列+跳转详情页 |
| 3 | `ConsoleUserController.java` | detail/repo-issues/reset-token API |
| 4 | `StatsController.java` | user-registration API |
| 5 | `Overview.vue` | 注册趋势折线图（年/月/日切换） |
| 6 | `router/index.js` | 路由 `/sys/console/:id` |
| 7 | `App.vue` | 标题跳转改为 `/overview` |
| 8 | `src/api/deps.py` | `_pending_active` set + `mark_user_active` |
| 9 | `src/user/db.py` | `batch_update_last_active` + `create_user` 写 `created_at` |
| 10 | `src/api/main.py` | 定时 flush `active_user_tracker` |

## 九、性能优化

### 索引

- `agent_tasks.started_at` 加前缀索引 `idx_started_at(19)`——覆盖 YYYY-MM-DD HH:MM:SS
- 注意：TEXT 类型前缀索引只对 `LIKE 'prefix%'` 有效，`>=` 范围查询不走索引（MySQL 限制）
- 当前 1441 行全表扫描 6ms，性能足够；未来 10 万行需改字段类型为 DATETIME

### SQL 优化

- 注册趋势：`REGEXP '^[0-9]{4}'` → `LIKE '20%'`（LIKE 比 REGEXP CPU 开销低）
- 活跃用户：保持 `t.started_at >= DATE_SUB(NOW(), INTERVAL ? DAY)`（尝试 LEFT()/FORCE INDEX 均不改善）

### 性能验证

| 查询 | 数据量 | 耗时 |
|------|--------|------|
| 注册趋势(月) | 18 行 | 50ms（含网络） |
| 活跃用户(30天) | 1441 行 | 29ms（含网络） |
| 活跃用户(90天) | 1441 行 | 29ms（含网络） |

## 十、验证结果

- mvn compile: OK ✅
- vite build: OK ✅
- admin health: ok ✅
- hermes verify: 71 failed / 1230 passed（比基线121/1180好50个，0新增failed）
- 统计口径一致：taskCount=291, completed+failed+pending=291, 差异=0 ✅
- 注册趋势 API：年/月/日三个维度正常 ✅

## 十一、commit 历史

| commit | 说明 |
|--------|------|
| `1f99659d` | 来源筛选改为来源维度 |
| `898bd747` | create_user 写入 created_at |
| `1617eae8` | 仓库与Issue改为下拉框+后端分页 |
| `e9ab519e` | 下拉框只显示仓库名+统计卡加Issue总数+分页改为5/10 |
| `acd4a6c4` | 仓库统计和Issue列表加进行中状态 |
| `44790bc2` | 最近活动改为三个tab |
| `45bade03` | Issue数量统计排除空字符串 |
| `c4700c39` | 仓库统计pending排除对话任务 |
| `e78f14da` | 全量排除对话任务 |
| `72f6689c` | repoMatrix也排除对话任务 |
| `dc3c776d` | 6个问题修复——exit_code负值+死代码+SQL优化+时区 |
| `3470bd92` | 标题跳转/overview |
| `008724d9` | 注册趋势图（年/月/日切换） |
| `d630dd63` | 列表加创建时间列（时分秒） |
| `fad39a17` | 注册趋势改为平滑折线图 |
| `31edd29b` | 总览页加tab切换——注册趋势/活跃用户 |
| `a170040e` | 活跃用户查询性能优化——加索引+REGEXP改LIKE |
