# Spec: Issue #926 — 需求中心分屏逐步加载（首屏一屏先行 + 滚动增量加载）

> 来源：Issue #926 + Issue Agent Spec 评论（评级 ready）| 目标分支：weekly-mys

## Objective
需求中心由「一次拉全量、前端切片」改为「首屏一屏（per_page=30）先行渲染 + 滚动按屏递增 + 详情分块加载」。
价值：千条级仓库全量列表约 1 MB，首屏空白等待明显；改造后首屏请求 ≤150 KB、渲染 <1.5 s。

## Scope
### In Scope
- 前端分页接入与骨架占位（issues.js loadTasks / IntersectionObserver / 按 id 去重 / has_more 终止）
- 状态重置（tab/筛选/搜索/排序/换仓）与选中项保留；深链 `#tasks/<号>` 不依赖全量；自动刷新按 id 合并
- 详情分块加载：点击即渲染基础卡片（0 请求），`/pipeline`、`/attachments` 到货各自填充；事件时间线最近 20 条 + 加载更多
- 后端最小配套：PR 伪卡片统一并入 LIMIT/OFFSET 分页口径（created_at 合并）；列表接口补 `q`/`statuses` 服务端过滤；分页响应瘦身（attachments 只回 count）；保留 `per_page=0` 全量兼容

### Out of Scope
- 需求状态机 / 事件写入 / A2A 交办逻辑；「需求中心 v2」新页面；/issues/mine 个人维度分页

## Acceptance Criteria
- 首屏请求 ≤30 行且响应瘦身（attachments 不再内联清单）
- 连续滚动 5 屏：按 id 去重无重复无丢失，has_more=False 终止
- 切 tab/筛选/搜索/排序后列表重置到第 1 屏且选中项不丢
- 深链 `#tasks/<号>` 列表未命中时走详情接口直达
- 所有现有测试通过；新增测试覆盖分页合并/过滤/瘦身
