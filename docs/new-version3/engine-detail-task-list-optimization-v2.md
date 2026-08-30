# 引擎详情任务列表优化方案（V2 最终版）

> 分支：`feat/admin-optimization`

## 范围声明

本方案只改 admin 运营管理台（Java 后端 + Vue 前端），不耦合健康中心，独立 API。

## 需求

引擎详情任务列表点击 Issue 行，内联展开（不弹窗），展示：
1. 📊 时序图（mermaid sequenceDiagram）
2. 📋 事件列表（Seq/事件/Agent/Skill/状态/耗时/时间）
3. 📝 任务列表（ID/Agent/Skill/状态/耗时/Exit/创建时间，点击行打开日志弹窗）

效果和健康中心一致——但用独立 API，不耦合健康中心代码。

## 交互方式

1. 任务列表按 Issue 分组（有 issueId 的 Task 归到同一个 Issue 下，无 issueId 的归到「独立任务」组）
2. 点击 Issue 行 → 内联展开（和健康中心一样，不是弹窗）
3. 展开后有三个 tab：📊 时序图 / 📋 事件列表 / 📝 任务列表
4. 事件列表里点击有 taskId 的行 → 打开日志弹窗
5. 任务列表里点击行 → 打开日志弹窗
6. 筛选/分页/排序保留（在分组前生效）

## 后端独立 API（3 个，在 RuntimeController 里）

### API 1：按 Issue 获取事件列表

```
GET /api/admin/runtime/issues/{issueId}/events
```

查询逻辑（查 grape 业务库 events + agent_tasks 表）：
- events 表：`SELECT seq, event_type, issue_id, agent_type, skill, status, duration, created_at, actor, repo_key FROM events WHERE issue_id=? ORDER BY created_at`
- agent_tasks 表建立 (issue_id, event_seq) → task_id 映射
- 返回：`{ issueId, events: [{ seq, eventType, agentType, skill, status, duration, createdAt, actor, taskId }] }`

### API 2：任务日志消息

```
GET /api/admin/runtime/tasks/{taskId}/messages
```

查询 task_messages 表，返回：`[{ seq, type, tool, content, createdAt }]`

### API 3：任务详情

```
GET /api/admin/runtime/tasks/{taskId}/info
```

查询 agent_tasks 表，返回：`{ id, repoKey, agentType, status, issueId, createdAt, completedAt, startedAt, exitCode, resultText, errors, eventSeq, skill }`

## 前端改动（EngineDetail.vue）

### 任务列表改为 Issue 分组渲染

1. 将 `detail.tasks.content` 按 `issueId` 分组（computed）
2. 有 issueId 的 Task 归到对应 Issue 组
3. 无 issueId 的 Task 归到「独立任务」组

### Issue 行（可点击展开）

```html
<div class="tl-issue">
  <div class="tl-issue-head" @click="toggleIssue(issueId)">
    <span class="tl-arrow">▾/▸</span>
    <b>{{ issueId }}</b>
    <span class="t3">{{ tasks.length }} 个任务</span>
    <span class="t3">{{ lastTime }}</span>
  </div>
  <div v-if="expandedIssues.has(issueId)" class="tl-issue-body">
    <div class="tl-view-tabs">
      <button>📊 时序图</button>
      <button>📋 事件列表</button>
      <button>📝 任务列表</button>
    </div>
    <!-- 时序图（mermaid v-html） -->
    <!-- 事件列表（表格，点击行打开日志） -->
    <!-- 任务列表（表格，点击行打开日志） -->
  </div>
</div>
```

### 独立任务组（无 issueId）

直接显示任务表格，点击行打开日志弹窗。

### mermaid 时序图渲染

从 HealthCenter 复制 buildSequence / renderMermaidForIssue / applySequenceColors 逻辑，改为调独立 API `/api/admin/runtime/issues/{issueId}/events` 获取事件。

### 日志弹窗

保留已有日志弹窗，改为调独立 API：
- `/api/admin/runtime/tasks/{taskId}/messages`
- `/api/admin/runtime/tasks/{taskId}/info`

## 改动清单

| # | 文件 | 改动 | 类型 |
|---|------|------|------|
| 1 | RuntimeController.java | 新增 3 个 API | 后端 |
| 2 | EngineDetail.vue | 任务列表改为 Issue 分组内联展开 | 前端 |
| 3 | EngineDetail.vue | mermaid 时序图渲染 | 前端 |
| 4 | EngineDetail.vue | 日志弹窗改用独立 API | 前端 |

### 不改的

- 健康中心代码（独立 API 不耦合）
- 数据库（查的是 events / task_messages / agent_tasks 表，已有）
- 筛选/分页/排序（保留，在分组前生效）

## 保留的现有功能

- 筛选（status/skill/agentType）
- 分页（page/size）
- 排序（创建时间 asc/desc）
- 状态颜色标签
- 耗时/退出码列
