# 引擎详情任务列表优化方案

> 分支：`feat/admin-optimization`

## 现状

引擎详情任务列表当前表头：Task ID | Agent | Skill | 状态 | 创建时间（5 列）
- 有筛选/分页/排序
- 缺少 Issue 编号、耗时、退出码、点击跳转日志

健康中心任务列表有但引擎详情没有的优点：
1. Issue 编号（关联 GitCode Issue）
2. 耗时（完成时间 - 创建时间）
3. 退出码（Exit Code）
4. 点击行跳转日志弹窗（查看任务执行详情）
5. 状态颜色标签（completed 绿/failed 红/running 黄）

## 优化点

### 优化 1：补 Issue 编号列

**位置**：表头在 Task ID 后加 `Issue` 列

**后端**：`toTaskMap` 已有 `repoKey` 字段，需补 `issueId` 和 `eventSeq`：
```java
m.put("issueId", t.getIssueId());
m.put("eventSeq", t.getEventSeq());
```
（`AgentTaskRo` 已有 `issueId` 和 `eventSeq` 字段，只是 `toTaskMap` 没 put）

**前端**：表头加 `<th>Issue</th>`，数据行加：
```html
<td><b>{{ t.issueId || '—' }}</b> <span class="t3" v-if="t.eventSeq">#{{ t.eventSeq }}</span></td>
```

### 优化 2：补耗时列

**前端**：表头在状态后加 `耗时` 列，数据行加：
```html
<td class="t3">{{ formatDuration(t.createdAt, t.completedAt) }}</td>
```

新增函数：
```javascript
function formatDuration(start, end) {
  if (!start || !end) return '—'
  const ms = new Date(end.replace(' ', 'T')) - new Date(start.replace(' ', 'T'))
  if (isNaN(ms)) return '—'
  const s = Math.round(ms / 1000)
  if (s < 60) return s + 's'
  return Math.floor(s / 60) + 'm' + (s % 60) + 's'
}
```

**后端**：`toTaskMap` 已有 `createdAt` 和 `completedAt`，无需改后端。

### 优化 3：补退出码列

**前端**：在耗时后加 `Exit` 列：
```html
<td class="t3">{{ t.exitCode ?? '—' }}</td>
```

**后端**：`toTaskMap` 已有 `exitCode`，无需改后端。

### 优化 4：点击行跳转日志弹窗

**前端**：
- 任务行加 `class="tl-row-clickable"` + `@click="openLogModal(t.id, t)"`
- 新增 `openLogModal` 函数（复用健康中心的逻辑）：
  - 调 `/api/admin/health-center/tasks/{taskId}/messages` 获取日志消息
  - 调 `/api/admin/health-center/tasks/{taskId}/info` 获取任务信息
  - 弹窗显示日志时间线 + 消息列表
- 新增日志弹窗 HTML（从 HealthCenter.vue 复制 log-modal 结构）

**后端**：无需改（健康中心 API 已有 `/health-center/tasks/{id}/messages` 和 `/health-center/tasks/{id}/info`）

### 优化 5：状态颜色标签

**前端**：状态列改为带颜色：
```html
<td><span :class="['status-tag', t.status === 'completed' ? 'success' : t.status === 'failed' ? 'danger' : t.status === 'running' ? 'warning' : 'default']">{{ t.status }}</span></td>
```

## 改动清单

| # | 文件 | 改动 | 类型 |
|---|------|------|------|
| 1 | RuntimeDeviceService.java | toTaskMap 补 issueId + eventSeq | 后端 |
| 2 | EngineDetail.vue | 加 Issue/耗时/退出码列 | 前端 |
| 3 | EngineDetail.vue | 状态列加颜色标签 | 前端 |
| 4 | EngineDetail.vue | 点击行跳转日志弹窗 + 日志弹窗 HTML | 前端 |

### 不改的

- 后端 API 路径（复用健康中心 API）
- 数据库（字段已有）
- 筛选/分页/排序（保留现有功能）

## 优化后表头

| Task ID | Issue | Agent | Skill | 状态 | 耗时 | Exit | 创建时间 |
|---------|-------|-------|-------|------|------|------|---------|
| abc123 | #42 #5 | Coding | code-impl | ✅完成 | 12s | 0 | 08-29 10:30 |

8 列，点击行打开日志弹窗。
