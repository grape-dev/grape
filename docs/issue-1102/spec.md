# Spec: Issue #1102 — tasks.load_more 提示文本、跳转链接及加载方向修复

> 上游 Spec：Issue #1102 评论（Issue Agent 发布，评级 ready）。反馈单 7879。

## Objective

任务会话页（需求详情 → 流水线卡片 → 事件历史）的 tasks.load_more 组件三项体验缺陷：

1. **提示文本不准/缺 i18n**：weekly-10 已上线 #926 时复用看板 `tasks.load_more` 键；本 issue 新增专用 `tasks.load_earlier_events` 键（3 参 fallback），文案与实际「加载更早的事件」行为一致，不再复用看板滚动语义。
2. **点击异常跳转**：按钮位于整卡 `onclick="_jumpToPipePage()"`（→ `app.nav('pipe', issueNum)` → `#pipe/{issue}`）的卡片内，按钮 onclick 未阻断冒泡 → 点击「加载更多」被跳转到 `/app/#pipe/868`。
3. **加载方向相反**：接口 events 为 seq DESC（最新在前），原实现 `events.slice(events.length - _shown)` 取最旧一段 → 默认展示旧事件、「加载更多」向更新方向展开；预期为默认展示最新、点击向更早方向加载。

## Scope

### In Scope（纯前端，在目标分支 weekly-10 已有的 #926 实现上按修正后行为调整）

- `_renderPipelineSection` 事件历史分批展示：默认最新 20 条，`tasks.load_earlier_events` 按钮（i18n、语义准确）向更早方向本地展开（0 请求，可连续点击直至加载完已拉取事件）。
- 按钮 onclick 首句 `event.stopPropagation()`，杜绝冒泡到卡片 `_jumpToPipePage()` 的 `#pipe/{n}` 跳转。
- zh/en `tasks.json` 落 `load_earlier_events` 键，文案与实际行为一致（加载更早的事件）。
- `_openBoardDetail` 进入新详情重置展开量；index.html 缓存版本号 bump。

### Out of Scope

- 后端接口变更（Spec 明确：仅在前端正确使用现有数据；`/issues/{id}/pipeline` 维持 `get_events(limit=30)`）。
- 任务列表/看板分页（weekly-10 #926/#1077 其余部分）、会话页其它 UI 重构。

## Acceptance Criteria

- [x] 事件历史「加载更早的事件」提示文本完成国际化且语义准确（zh/en `load_earlier_events` 键齐全，非原始 key）；
- [x] 点击「加载更早的事件」不跳转页面（stopPropagation 生效，URL 不变为 #pipe/{n}）；
- [x] 进入详情默认展示最新事件（新事件在列，更早的被折叠）；
- [x] 点击后向更早方向追加事件，可连续加载至已拉取事件全部展示，按钮随之隐藏；
- [x] tests/js/test_issue_1102_load_more.js 全部通过（7 组断言：i18n/不跳转/方向/重置/缓存版本等）。

## Testing Strategy

- seam：`_renderPipelineSection` / `_loadMorePipeEvents` / `_openBoardDetail`（vm 沙箱加载真实 issues.js + mock DOM/I18N，沿 `tests/js/test_issue_539_review_status_render.js` 模式，node 直跑）。
- 运行：`node tests/js/test_issue_1102_load_more.js`；回归 `node tests/js/test_issue_539_review_status_render.js`。
