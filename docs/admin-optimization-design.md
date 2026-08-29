# 运营管理台优化方案（最终版）

> 分支：`feat/admin-optimization`（基于 dev `2284241a`）
> 范围：只改 admin 运营管理台（Java 后端 + Vue 前端），不涉及 grape 主服务 Python 代码

---

## 1. Runtime 引擎显示运行 IP

### 问题

EngineManage 引擎列表缺少运行 IP 地址。

### 根因

IP 在 `runtime_credentials.last_ip` 表里，后端 `engineList` 查了凭据但没取 `lastIp`。

### 改动

- 后端 `RuntimeDeviceService.java`：engineList + engineDetail 加 `m.put("lastIp", cred.getLastIp())`
- 前端 `EngineManage.vue`：列表表头/数据行/卡片加 IP 列
- 前端 `EngineDetail.vue`：详情页标题显示 IP

### IP 更新机制

`last_ip` 是动态的——worker 注册/心跳时从 `X-Forwarded-For` 或 `request.client.host` 更新。127.0.0.1 表示 worker 和服务在同一台机器上。

---

## 2. Issue 跳转 GitCode

### 改动

- 前端 `ReposManage.vue`：Issue 标题旁加 ↗ 外链图标，点击新标签打开 `https://gitcode.com/{owner}/{repo}/issues/{issueId}`

---

## 3. 引擎详情任务列表优化

### 改动

- 表头从 5 列扩展到 7 列：ID | Issue | Agent | 技能 | 状态 | 耗时 | 创建时间
- Issue 列：Issue 号链接（跳转 GitCode）+ 📊 执行详情按钮（在 Issue 号后面）
- 状态列：颜色标签（completed 绿/failed 红/running 黄）
- 耗时列：formatDuration(createdAt, completedAt)
- 点击任务行打开日志弹窗
- 「类型」列改名为「Agent」，去掉 Exit 列（无意义）

### 后端

- `toTaskMap` 补 `issueId` + `eventSeq` 字段
- `RuntimeController` 新增 3 个独立 API（不耦合健康中心）：
  - `GET /api/admin/runtime/issues/{issueId}/events` — 按 Issue 查事件列表
  - `GET /api/admin/runtime/tasks/{taskId}/messages` — 任务日志消息
  - `GET /api/admin/runtime/tasks/{taskId}/info` — 任务详情

### Issue 执行详情弹窗

- 点击 📊 按钮打开弹窗（不离开当前页面）
- 两个 tab：📊 时序图（mermaid sequenceDiagram）/ 📋 事件列表
- 时序图：调 issues/{id}/events API，用 mermaid 渲染
- 事件列表：表格（Seq/事件/Agent/Skill/状态/耗时/时间）
- 事件列表不触发日志弹窗（只有任务列表行触发）

---

## 4. 引擎管理加任务数量列

### 改动

- 后端 `RuntimeDeviceService.engineList`：加 `taskCountByRuntime` 统计
- 前端 `EngineManage.vue`：列表表头/数据行/卡片加任务数列

---

## 5. 按钮改为 SVG 图标

### 改动

三个页面的文字按钮改为 SVG 图标按钮（`act-icon-btn` 样式）：

| 页面 | 按钮 | 图标 |
|------|------|------|
| EngineManage | 详情 | 眼睛 👁 |
| EngineManage | 删除 | 垃圾桶 🗑 |
| DeviceManage | 详情 | 眼睛 👁 |
| DeviceManage | 吊销 | 盾牌 🛡 |
| DeviceManage | 回收 | 垃圾桶 🗑 |
| DeviceManage | 删除 | 垃圾桶 🗑 |
| CredentialManage | 吊销 | 盾牌 🛡 |
| CredentialManage | 删除 | 垃圾桶 🗑 |

样式：无边框、hover 蓝底、`title` 属性显示文字提示。

---

## 6. TaskListSection 统一组件

### 设计

抽出 `admin/frontend/src/components/TaskListSection.vue` 组件，设备详情和引擎详情共用。基于 props 配置列显示，通用可复用。

### Props

| Prop | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| tasks | Object | — | 分页数据 `{ content, totalElements, totalPages, filterOptions }` |
| loading | Boolean | false | 加载状态 |
| columns | Object | 见下 | 可配置列显示 |
| engines | Array | null | 引擎列表（设备详情传入，显示引擎筛选下拉框） |

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
| reload | `{ taskPage, taskSize, sortOrder, taskStatus, taskSkill, taskType, engineId }` | 筛选/排序/分页/引擎变化时通知父组件重新加载 |

### 组件内容

- 统计卡（任务总数 + 各状态计数）
- 任务状态分布饼图（echarts，复用实例 + deep watch + requestAnimationFrame）
- 工具栏（引擎筛选下拉 + 清除筛选）
- 任务表格（v-if 控制 each column，colspan 动态计算）
- 列头筛选（agentType / skill / status 下拉）
- 列头排序（创建时间 asc/desc）
- 分页栏（page/size，emitReload 通知父组件）
- Issue 详情弹窗（时序图 mermaid + 事件列表）
- 任务日志弹窗（时间线 + 日志消息列表）

### 父组件用法

```vue
<!-- 设备详情：显示引擎列 + 引擎筛选 -->
<TaskListSection :tasks="detail.tasks" :loading="taskLoading"
  :columns="{ engine: true }" :engines="detail.engines"
  @reload="onTaskReload" />

<!-- 引擎详情：默认不显示引擎列 -->
<TaskListSection :tasks="detail.tasks" :loading="taskLoading"
  @reload="onTaskReload" />
```

### 后端改动

- `toTaskMap` 重载：`toTaskMap(t, engineNameById)` 支持设备详情场景传引擎名映射
- `deviceDetail` 构建 `runtimeId → 引擎名` Map

---

## 7. 饼图丢失修复

### 问题

选择引擎筛选后任务状态分布饼图消失。

### 根因

1. `echarts.init(el)` 每次创建新实例——同一 DOM 元素重复 init 导致图表消失
2. watch 没开 `deep`——对象内部变化不触发
3. `nextTick` 时 DOM 可能还没完成渲染

### 修复

- 保存 `taskChart` 实例，更新时用 `setOption` 复用
- DOM 失效时 `dispose` 旧实例重建（`taskChart.getDom() !== el`）
- watch 加 `deep: true`
- 用 `requestAnimationFrame(() => nextTick(renderTaskChart))` 确保浏览器渲染完成

---

## 改动文件清单

| # | 文件 | 改动 |
|---|------|------|
| 1 | RuntimeDeviceService.java | engineList/engineDetail 加 lastIp + taskCount；toTaskMap 重载加 engineName |
| 2 | RuntimeController.java | 新增 3 个独立 API + 注入 grapeJdbcTemplate |
| 3 | EngineManage.vue | IP 列 + 任务数列 + SVG 图标按钮 |
| 4 | EngineDetail.vue | 引用 TaskListSection 组件 + onTaskReload |
| 5 | DeviceDetail.vue | 引用 TaskListSection 组件 + onTaskReload（含 engineId） |
| 6 | DeviceManage.vue | SVG 图标按钮 |
| 7 | CredentialManage.vue | SVG 图标按钮 |
| 8 | ReposManage.vue | Issue 跳转 GitCode ↗ 链接 |
| 9 | TaskListSection.vue（新建） | 统一任务列表组件 |
| 10 | nginx-grape.conf | 80 端口反代 admin 8083 + SSE 配置 |

### 不改的

- grape 主服务 Python 代码（src/）
- 数据库（字段已有）
- 路由
- 健康中心代码（独立 API 不耦合）

---

## 验证状态

| 验证项 | 结果 |
|--------|:---:|
| 后端 mvn compile | ✅ |
| 前端 npm run build | ✅ |
| admin 服务 health | ✅ |
| 引擎列表 lastIp | ✅ 127.0.0.1 |
| 引擎列表 taskCount | ✅ 11/0/16 |
| 引擎详情 Issue 📊 弹窗 | ✅ 时序图+事件列表 |
| 设备详情引擎筛选 | ✅ |
| 设备详情任务列表 | ✅ 7 列+引擎列 |
| 饼图切换引擎不消失 | ✅ |
| SVG 图标按钮 | ✅ 三个页面 |
| hermes verify | 73 failed / 1226 passed（和 dev 基线一致，0 新增） |
