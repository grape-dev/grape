# Plan: Issue #1102 — tasks.load_more 三项修复

## 根因定位（对照 weekly-10 已上线的 #926 实现）

| # | 现象 | 根因 | 修复 |
|---|------|------|------|
| 1 | 页面显示原始 key `tasks.load_more` | `I18N.t('tasks.load_more')||'加载更多'`：t() 缺 key 返回 key 本身（truthy），`\|\|` 兜底永不生效，且语言包无 key | zh/en `tasks.json` 落 `load_more` 键 + 调用改 3 参 fallback `I18N.t(key, {}, fallback)` |
| 2 | 点击跳转 `/app/#pipe/868` | 事件历史在流水线卡片内，卡片 `onclick="_jumpToPipePage()"`；按钮未 `stopPropagation`，冒泡触发整页跳转 | 按钮 onclick 首句 `event.stopPropagation();` |
| 3 | 默认展示旧事件、加载方向朝新 | events 为 seq DESC（最新在前），原实现 `events.slice(events.length - _shown)` 取最旧段 | 改 `events.slice(0, _shown)`（最新在前语义下即最新一段），按钮置底部向更早方向展开 |

## 依赖图

```
locales/zh|en/tasks.json (load_more key)
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
| 缓存版本不 bump → 用户拿旧 JS | 沿 #539 先例：index.html `issues.js?v=126 → 127`，测试断言 ≥127 |
| 与 weekly-10 合并冲突 | 目标分支本就无该组件，按修正版实现；冲突时以修正版为准（方向/stopPropagation/i18n 三点） |
