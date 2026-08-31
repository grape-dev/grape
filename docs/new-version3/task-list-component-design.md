# 任务列表统一组件方案（最终版）

> 分支：`feat/admin-optimization`

## 设计

抽出一个 `TaskListSection.vue` 组件，设备详情和引擎详情共用。基于 props 配置列显示，通用可复用。

## 组件设计

### Props

| Prop | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| tasks | Object | — | 分页数据 `{ content, totalElements, totalPages, filterOptions }` |
| loading | Boolean | false | 加载状态 |
| columns | Object | 见下 | 可配置列显示 |

### columns 默认值

```javascript
{
  id: true,        // Task ID
  issue: true,     // Issue 号 + GitCode链接 + 📊执行详情按钮
  agent: true,     // Agent 类型
  skill: true,     // 技能
  status: true,    // 状态（颜色标签）
  duration: true,  // 耗时
  engine: false,   // 引擎名（设备详情开启，引擎详情关闭）
  createdAt: true, // 创建时间
}
```

### Emit

| Event | 参数 | 说明 |
|-------|------|------|
| reload | `{ taskPage, taskSize, sortOrder, taskStatus, taskSkill, taskType }` | 筛选/排序/分页变化时通知父组件重新加载 |

### 父组件用法

```vue
<!-- 设备详情：显示引擎列 -->
<TaskListSection :tasks="detail.tasks" :loading="taskLoading" :columns="{ engine: true }" @reload="onReload" />

<!-- 引擎详情：默认不显示引擎列 -->
<TaskListSection :tasks="detail.tasks" :loading="taskLoading" @reload="onReload" />
```

## 组件结构

```
TaskListSection.vue
├── template
│   ├── 统计卡（任务总数 + 各状态计数）
│   ├── 任务状态分布饼图（echarts）
│   ├── 工具栏（清除筛选 + 筛选 chip）
│   ├── 任务表格
│   │   ├── 列头（v-if 控制 each column）
│   │   │   ├── ID
│   │   │   ├── Issue（链接 + 📊按钮）
│   │   │   ├── Agent（筛选下拉）
│   │   │   ├── 技能（筛选下拉）
│   │   │   ├── 状态（筛选下拉）
│   │   │   ├── 耗时
│   │   │   ├── 引擎（v-if="columns.engine"）
│   │   │   └── 创建时间（排序）
│   │   ├── 数据行（v-if 控制 each column）
│   │   └── 空状态
│   ├── 分页栏
│   ├── Issue 详情弹窗（时序图 + 事件列表）
│   └── 任务日志弹窗（时间线 + 日志消息）
├── script
│   ├── props: tasks, loading, columns
│   ├── emit: reload
│   ├── 状态: taskPage/taskSize/taskStatus/taskSkill/taskType/sortOrder/filterDropdown/taskLoading
│   ├── computed: visibleColumnCount, filterOptions, hasAnyFilter, filterCount
│   ├── 函数: load/toggleFilter/setFilter/clearFilters/toggleSort/formatTime/formatDuration
│   ├── 日志弹窗: openLogModal/closeLogModal/logTypeIcon
│   ├── Issue弹窗: openIssueDetail/closeIssueModal
│   ├── mermaid: getMermaid/buildSequence/applySequenceColors/mermaidId/actionText/renderIssueMermaid
│   ├── eventTypeText/eventTypeClass/issueGitCodeUrl
│   └── 饼图: renderTaskChart
└── style scoped
    ├── 统计卡样式
    ├── 筛选/排序样式
    ├── 表格样式
    ├── 日志弹窗样式
    ├── Issue弹窗样式
    └── mermaid 容器样式
```

## 后端改动

### toTaskMap 补充 engineName

当前 `toTaskMap` 返回 `runtimeId` 但没有引擎名。设备详情需要显示引擎列，加一个 `engineName` 字段：

```java
m.put("engineName", t.getRuntimeId() == null ? null
    : runtimeRepository.findById(t.getRuntimeId())
        .map(r -> r.getName() + (r.getDisplayName() != null ? "(" + r.getDisplayName() + ")" : ""))
        .orElse(null));
```

但因为 `toTaskMap` 是 private 方法不方便查 repository，改为在 `deviceDetail` 方法里构建 `runtimeId → name` 的 Map 传入。

## 改动清单

| # | 文件 | 改动 |
|---|------|------|
| 1 | 新建 `admin/frontend/src/components/TaskListSection.vue` | 统一组件 |
| 2 | RuntimeDeviceService.java | toTaskMap 补 engineName（设备详情场景） |
| 3 | EngineDetail.vue | 删除任务列表部分，改为 `<TaskListSection>` |
| 4 | DeviceDetail.vue | 删除任务列表部分，改为 `<TaskListSection :columns="{ engine: true }">` |

### 不改的

- 后端 API 路径
- 路由
- 其他页面
- 数据库

## 收益

- 消除两份重复代码（约 400 行 × 2 → 1 个组件 + 2 行引用）
- DeviceDetail 自动获得 Issue 列 / 耗时 / 状态颜色 / 日志弹窗 / Issue 时序图 / 引擎列
- 后续任务列表改动只需改一处
- columns 配置化，可扩展到其他页面复用
