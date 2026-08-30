# 引擎详情任务列表优化方案（V3 最终版）

> 分支：`feat/admin-optimization`

## 设计

引擎详情任务列表保持**平铺表格**（ID | Issue | 类型 | 技能 | 状态 | 耗时 | Exit | 创建时间），在 Issue 列增加一个「执行详情」按钮，点击弹出和健康中心一样的时序图 + 事件列表。

### 交互方式

1. 平铺任务列表表格（保留筛选/分页/排序）
2. Issue 列：Issue 号（可点击跳转 GitCode）+ 旁边一个「📊」按钮
3. 点击「📊」按钮 → 打开 Issue 详情弹窗（不离开当前页面）
4. 弹窗内两个 tab：📊 时序图（mermaid） / 📋 事件列表
5. 事件列表点击有 taskId 的行 → 打开任务日志弹窗
6. 点击任务行（非 Issue 按钮）→ 打开任务日志弹窗（原有功能保留）

### 后端 API（已有，不新增）

- `GET /api/admin/runtime/issues/{issueId}/events` → `{ issueId, events: [{ seq, eventType, agentType, skill, status, duration, createdAt, actor, taskId }] }`
- `GET /api/admin/runtime/tasks/{taskId}/messages` → `[{ seq, type, tool, content, createdAt }]`
- `GET /api/admin/runtime/tasks/{taskId}/info` → `{ id, repoKey, agentType, status, issueId, ... }`

### 前端改动（EngineDetail.vue）

1. Issue 列加「📊」按钮：`@click.stop="openIssueDetail(t.issueId)"`
2. 新增 Issue 详情弹窗（issueDetailModal）：
   - 标题：Issue 号 + 仓库名
   - 两个 tab：时序图 / 事件列表
   - 时序图：调 issues/{id}/events API，用 mermaid 渲染
   - 事件列表：表格（Seq | 事件 | Agent | Skill | 状态 | 耗时 | 时间）
3. 从 HealthCenter 复制 mermaid 函数：getMermaid / buildSequence / applySequenceColors / mermaidId / actionText / renderMermaidForIssue
4. 事件列表行点击 → 打开日志弹窗（已有 openLogModal）
5. CSS：复用 HealthCenter 的 .tl-view-tabs / .tl-diagram-container / .tl-diagram-svg

## 改动清单

| # | 文件 | 改动 |
|---|------|------|
| 1 | EngineDetail.vue | Issue 列加 📊 按钮 |
| 2 | EngineDetail.vue | 新增 Issue 详情弹窗（时序图 + 事件列表） |
| 3 | EngineDetail.vue | mermaid 渲染函数 |
| 4 | EngineDetail.vue | CSS 样式 |

不改后端（API 已有），不改数据库。
