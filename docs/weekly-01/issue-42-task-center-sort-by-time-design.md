# 任务中心按时间排序优化方案（Issue #42）

> 版本：v1.0 ｜ 日期：2026-09-05
> Issue：#42「grape的任务中心支持按时间排序」｜ 状态：open（feature / spec-ready / triaged）
> 优先级：P2
> 说明：VOD Agent 审视 + Issue Agent Spec 已就绪；本方案对其实施路径做补充细化（含 i18n/边界）。

---

## 一、问题现状（代码确认，与机器人分析一致）

### 1.1 后端 `/api/tasks/mine`（tasks.py:895-901）
```sql
ORDER BY CASE WHEN status='active' THEN 0 ELSE 1 END, created_at DESC
```
- 已按 created_at 降序，但**先按 active 优先**——故同屏内 active 排前，其余按时间。

### 1.2 前端 tasks.js:151-157
```js
const activeRows = filtered.filter(t => t.status === 'active');
const procRows  = filtered.filter(t => t.status === 'processing');
const doneRows  = filtered.filter(t => t.status === 'completed');
const allRows = [...activeRows, ...procRows, ...doneRows];  // 状态分组拼接
```
- 前端**拉取全量后重新按状态分组拼接**，完全覆盖了后端的时间降序 → 用户感知"随机排序"。
- 界面无排序指示器/切换入口。

---

## 二、方案

### 2.1 后端（tasks.py）

`/api/tasks/mine` 新增 `sort_order` 查询参数（`desc`/`asc`，默认 `desc`）：

```
sort_order=desc → ORDER BY created_at DESC
sort_order=asc  → ORDER BY created_at ASC
```

- **白名单校验**：`sort_order` 仅接受 `desc`/`asc`，否则 400 或回退默认（防 SQL 注入）。
- **状态优先逻辑**：默认仍保留 `CASE WHEN status='active' THEN 0 ELSE 1 END`（兼容"待处理优先"习惯），
  但增加可选参数 `group` 或由 sort_order 模式覆盖：当用户选"纯时间序"时去掉状态优先。
  - 简化实现：新增 `mode` 参数（`default`=现状状态优先；`time`=纯时间序）。
  - 或更简单：`sort_order` 管方向，前端分组也做"方向反转 + 组内按时间"两种模式。

### 2.2 前端（tasks.js）

1. **请求带参数**：`API.get('/tasks/mine?status=&repo_key=...&sort_order=' + _tasksSortOrder)`
2. **默认按时间降序**：`_tasksSortOrder` 初值 `'desc'`；`_render()` 不再用 `[...active, ...proc, ...done]` 死拼，
   改为按 `_tasksSortOrder` 对 `filtered` 排序：
   - 纯时间模式（默认）：按 `created_at` 降序（或升序）
   - （可选）保留状态优先模式：组内排序 + 组序按 active→processing→completed
3. **排序切换 UI**：任务列表头部加方向切换控件（升序/降序按钮或下拉）。
4. **偏好持久化**：`localStorage['task_sort_'+repoKey]` 存储 `desc`/`asc`，页面加载时读取并应用，
   切换仓库后各仓库偏好独立。

### 2.3 兼容性

- 保留状态 Tab 过滤、搜索、分页逻辑（分组仅影响展示顺序，不影响过滤）。
- 默认行为变化：从"状态分组"变为"纯时间降序"——如需保留原"待处理优先"，作为可切换模式。

---

## 三、改动清单

| 文件 | 改动 |
|---|---|
| `src/api/routes/tasks.py` | `mine` 端点新增 `sort_order`（+可选 `mode`）参数；白名单校验；调整 ORDER BY |
| `src/api/static/js/pages/tasks.js` | 携带参数；`_render()` 排序逻辑（默认时间降序）；排序切换 UI；localStorage 持久化 |
| `src/api/static/locales/**/tasks.json` | 新增排序相关文案（如"按时间排序""升序""降序"） |
| 测试 | 后端：`sort_order` 白名单 / desc / asc 排序断言；前端手工回归 |

---

## 四、验收对照（Issue 验收标准）

- [x] 任务列表默认按创建时间降序排列（最新在前）
- [x] 列表区域提供排序方向切换（升/降）
- [x] 刷新/切仓库后偏好保持（localStorage，按 repo 隔离）
- [x] 排序不影响搜索、状态 Tab、分页

---

## 五、风险

| 风险 | 缓解 |
|---|---|
| 移除状态优先改变既有习惯 | 保留为可切换模式（默认纯时间序，可切回"待处理优先"） |
| localStorage key 冲突 | key 含 repo 标识 |
| SQL 注入 | `sort_order` 白名单（desc/asc） |

---

*方案待实施；与 Issue Agent 已发 Spec 对齐（后端 sort_order 参数 + 前端切换 UI + localStorage）。*