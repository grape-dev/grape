# 任务状态统计方案——对齐 grape 主程序 observability

> 最后更新：2026-08-29

## grape 主程序 observability 的做法

### 后端（dashboard.py）

```python
# 6个标准 status 值
task_status_counts = {"queued": 0, "dispatched": 0, "running": 0,
                      "completed": 0, "failed": 0, "cancelled": 0}

# 直接用 status 字段 GROUP BY
SELECT status, COUNT(*) FROM agent_tasks GROUP BY status

# 只认标准值，旧版数字状态码（'0'/'1'/None/'-9'）被忽略
if status in task_status_counts:
    task_status_counts[status] = count

# in_progress = dispatched + running
in_progress = dispatched + running

# stuck = status='running' AND started_at < 22分钟前
stuck_count = SELECT COUNT(*) WHERE status='running' AND started_at < cutoff
```

### 前端（observability.js）8个 KPI

| KPI | 数据来源 | 说明 |
|-----|---------|------|
| 排队中 | status_counts.queued | status='queued' |
| 已派发 | status_counts.dispatched | status='dispatched' |
| 运行中 | status_counts.running | status='running' |
| 已完成 | status_counts.completed | status='completed' |
| 失败 | status_counts.failed | status='failed' |
| 已取消 | status_counts.cancelled | status='cancelled' |
| 进行中 | in_progress（dispatched+running） | 计算值 |
| 卡住 | stuck_count | status='running' 且超22分钟 |

### 旧版数字状态码处理

grape 主程序**直接忽略**——`if status in task_status_counts` 只认6个标准值。
旧版 '0'/'1'/None/'-9'/'-15'/'-1' 不在字典里，被跳过。

## 我们 admin 的调整方案

### 统计逻辑（对齐 observability）

| 分类 | SQL | 对应状态机 |
|------|-----|-----------|
| 排队中 | `status='queued'` | queued |
| 已派发 | `status='dispatched'` | dispatched |
| 运行中 | `status='running'` | running |
| 已完成 | `status='completed'` | completed（终态） |
| 失败 | `status='failed'` | failed（终态） |
| 已取消 | `status='cancelled'` | cancelled（终态） |
| 进行中 | `status IN ('dispatched','running')` | dispatched+running |
| 卡住 | `status='running' AND started_at < NOW()-INTERVAL 22 MINUTE` | 计算值 |

### 旧版数字状态码处理

和 grape 主程序一致——**直接忽略**，不纳入统计。
旧版 '0'/'1'/None/'-9'/'-15'/'-1' 不在任何分类里。

这会导致 taskCount < 实际总行数——但和 observability 行为一致。

### 统计卡展示

**任务总览 tab**：8 个统计卡
- 排队中 / 已派发 / 运行中 / 已完成 / 失败 / 已取消 / 进行中 / 卡住

**仓库统计卡**：7 个
- 任务总数 / Issue总数 / 已完成 / 进行中 / 已取消 / 失败 / 成功率
- 任务总数 = 6个标准status之和（不含旧版数字状态码）

**Issue 列表表格**：加"已取消"列
- Issue / 任务 / 完成 / 进行中 / 已取消 / 失败 / 成功率 / 最近活动

### 最近活动 tab

- 最近执行：最近5条（不限status）
- 进行中：`status IN ('queued','dispatched','running')` 最近5条
- 最近失败：`status='failed'` 最近5条

### 成功率

`completed / (queued+dispatched+running+completed+failed+cancelled) * 100%`

## 改动文件

| # | 文件 | 改动 |
|---|------|------|
| 1 | ConsoleUserController.java | detail/repoMatrix/repo-issues/recentTasks 全部改为 status 字段判断 |
| 2 | ConsoleUserDetail.vue | 统计卡改为8个（加排队中/已派发/运行中/卡住） |
