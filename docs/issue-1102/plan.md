# Plan: Issue #1102 — tasks.load_more 三项修复

## 根因定位（对照 weekly-10 已上线的 #926 实现）

| # | 现象 | 根因 | 修复 |
|---|------|------|------|
| 1 | 事件历史按钮文案复用看板滚动语义 | #926 复用 `tasks.load_more`（看板「滚动加载更多」），与事件历史「向更早展开」语义不符 | zh/en `tasks.json` 新增专用 `load_earlier_events` 键 + 调用 3 参 fallback `I18N.t('tasks.load_earlier_events', {}, fallback)` |
| 2 | 点击跳转 `/app/#pipe/868` | 事件历史在流水线卡片内，卡片 `onclick="_jumpToPipePage()"`；按钮未 `stopPropagation`，冒泡触发整页跳转 | 按钮 onclick 首句 `event.stopPropagation();` |
| 3 | 默认展示旧事件、加载方向朝新 | events 为 seq DESC（最新在前），原实现 `events.slice(events.length - _shown)` 取最旧段 | 改 `events.slice(0, _shown)`（最新在前语义下即最新一段），按钮置底部向更早方向展开 |

## 依赖图

```
locales/zh|en/tasks.json (load_earlier_events key)
        │
        └── issues.js _renderPipelineSection（事件历史分批 + 按钮）
                ├── window._loadMorePipeEvents（本地展开，0 请求）
                ├── window._lastPipeData（数据缓存）
                └── _openBoardDetail（进入详情重置展开量）
                        │
                        └── index.html issues.js?v bump（缓存失效）
```

## 关键决策

- **纯前端本地展开**：Spec Out-of-Scope 明确不改后端接口；`get_events(limit=30)` 已返回该 issue 最新 30 条，按钮在其内分批展开（20 → 30），全部展示后隐藏按钮。与 weekly-10 `_loadMorePipeEvents`「缓存数据本地展开（0 请求）」设计一致，仅修正方向与跳转。
- **按钮置列表底部**：事件行渲染保持现有 seq DESC（最新在顶）；更早的事件在下方追加，故按钮放底部（▼），点击向更早方向扩展。文本用准确语义「加载更早的事件 / Load earlier events」。
- **展开量挂在 window**（`_pipeEventsShown`）：15s 轮询重渲染需保留用户已展开量；进入新详情（`_openBoardDetail`）重置为 20，防跨任务串扰。

## 风险

| 风险 | 缓解 |
|------|------|
| 轮询重渲染吞掉展开状态 | `_pipeEventsShown` 为 window 级，重渲染读取同一状态；guard `_shown = min(shown, len)` |
| 缓存版本不 bump → 用户拿旧 JS | 沿 #539 先例：index.html `issues.js?v=132 → 133`，测试断言 ≥133 |
| 与 weekly-10 后续改动冲突 | 直接在 weekly-10 最新代码上做三点调整，改动面限定在事件历史分批块，避免回归看板/列表分页 |
