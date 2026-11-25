# Issue #3812 · 实施计划（会话失效后高频轮询停机）

> 需求文档（Spec）= Issue #3812 由 issue agent 发布的评论（2026-11-23），本文件只记录**落地方案与任务拆解**，不重复需求。

## 1. 范围（残余部分）

`#3734` / PR #1958 已交付看板侧闭环（`api.js` 广播 + `IssuesView` 停机/横幅 + `lib/pollGuard.js` 纯函数），本次**不重写**，只在同一决策源上补齐两个高频视图：

| 视图 | 定时器 | 节奏 | 旧 catch 行为 |
|---|---|---|---|
| `EventView.vue` | `pollTimer`（事件详情） | 5s | 静默吞错（401 也继续打 5s） |
| `EventView.vue` | `tlStatusTimer`（任务日志弹窗状态） | 5s | 静默降级 |
| `EventView.vue` | 任务日志 SSE 流重连 | 2s 重连 | 401 也重连（裸 fetch，不经 api.js） |
| `AutopilotView.vue` | `logTimer`（执行日志弹窗） | 2s | 静默 |
| `AutopilotView.vue` | `logPollTimers[id]`（执行卡片内联日志） | 3s | 双请求双静默 |

## 2. 方案

1. `lib/pollGuard.js` 新增 `createSessionPoller(options)`：把「定时器唯一性（先清后挂）+
   401/会话失效停机 + 非 401 连续 3 次降频 + `grape:session-expired`/`grape:session-restored`
   订阅」收敛成一个封装；决策仍复用既有 `nextPollAction`，不新增第二套判定。
   新增 `degradeIntervalFor(baseMs, degradeMs)`：高频轮询按 base 成比例降频（5s→15s、2s/3s→10s），
   显式值优先。
2. `api.js`：
   - 「无 token 直接抛错」分支（原 `:298` 区域）补发 `_notifySessionExpired()`——此前只 throw，
     导致已切登录页后各定时器继续打点；
   - 成功响应复位去重标记处，**仅在失效→恢复跳变**时广播 `grape:session-restored`，
     供视图在重新登录且自身仍打开时回启轮询（普通成功响应不广播，避免每请求一次事件）。
3. `EventView.vue` / `AutopilotView.vue`：所有 `setInterval` 换成封装实例
   （`detailPoller` / `tlStatusPoller` / `logDlgPoller` / `logPollers[id]`），
   catch 改走 `noteError(e)`；`markSessionExpired()` 统一停机 + 亮横幅；
   `resumeAfterSessionRestored()` 只回启「仍处于打开态」的详情/日志；
   `onBeforeUnmount` 统一 `dispose()` + 退订。任务日志流 401 不再 2s 重连。
4. 横幅文案复用看板既有词条 `tasks.board.session_expired` / `tasks.board.relogin`
   （zh/en 双包已存在），不在视图新增中文字面量，避免中文棘轮只减不增被破坏。

## 3. 缓存版本棘轮联动（改 chunk 源必须抬版本并重冻指纹）

`tests/api/test_frontend_cache_version_ratchet.py` + `test_frontend_bundle_source_ratchet.py`：

- `index.html`：`api.js?v=38→39`、`event-view.js?v=65→66`、`autopilot-view.js?v=73→74`、
  `issues-view.js?v=72→73`（其源集合含 `lib/pollGuard.js`）、`all-views.css?v=83→84`；
- `register.html` / `forgot.html`：`api.js?v=38→39`；
- entry 注入 CSS：`entries/event-view.js?v=65→66`、`entries/autopilot-view.js?v=69→70`；
- 两张表 `FROZEN`/`SHIPPED` 与 `ALL_VIEWS`（版本, 指纹）配对同步重冻。

## 4. 任务拆解（TDD：先红后绿）

1. `tests/js/test_issue_3812_session_poller.js`：封装行为（挂表/先清后挂/401 停机/降频/事件驱动/dispose）
   + 两个视图接线断言 + api.js 恢复广播断言。
2. `pollGuard.js` 封装实现 → 测试转绿。
3. `api.js` 两处信号补强。
4. 两个视图接线 + 横幅 + 样式。
5. 棘轮表重冻 + 回归（`test_issue_3734_poll_guard.js` 39 项、三套前端棘轮）。

## 5. 验收映射

| Issue 验收标准 | 本次落点 |
|---|---|
| 失效后 5 分钟内不再请求 `/api/*` | 401 → `noteError` 立即清表；事件驱动兜底；日志流不再 2s 重连 |
| 明确「会话已过期，请重新登录」+ 入口 | 两视图 `*-session-banner` + `tasks.board.*` 文案 + `showLogin` 按钮 |
| 三处定时器在 `sessionExpired` 下均清理 | 单测断言 `isRunning() === false` / `Object.keys(logPollers).length === 0` |
| 非 401 连续 3 次降频、成功即复位 | `nextPollAction` + `degradeIntervalFor` |
| 现有棘轮/回归/build/lint 通过 | 版本棘轮重冻；`node tests/js/*`；中文棘轮增量 0（基线存量另见 PR 说明） |