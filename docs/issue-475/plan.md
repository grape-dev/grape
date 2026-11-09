# Implementation Plan: Issue #475 引擎异常状态可见 + 非健康帮助提示

## Overview
纯前端增强：`runtimes.js` 新增状态→建议映射函数 `_healthHelpHint(rt)` 与帮助按钮渲染 `_helpBtn(key)`；`renderEngines` / `renderWorkers` 卡片在非健康状态旁渲染 ? 按钮（复用页面已注入的 `data-tooltip` 悬浮样式），异常态引擎卡片红色描边强化；zh/en locales 新增 `runtimes.health.hint.*`。

## Architecture Decisions
- **建议优先级**（一个卡片一个气泡，多信号取最严重）：consecutive_failures > heartbeat_lost > about_to_gc > offline > recently_lost > low_score；全部不命中（健康）返回 ''，不渲染按钮。
- **展示形式**：`data-tooltip` 悬浮（页面 `_renderRuntimesSkeleton` 已注入全局 tooltip CSS，零新增样式），按钮 `onclick` stopPropagation 防触发卡片 openEngineDetail。
- **异常视觉强化**：`_isAbnormalEngine(rt)`（hint 非空或 health 非 online）时引擎卡片加 `box-shadow:var(--sh),0 0 0 1px var(--r)` 红色描边；recently_lost 属瞬时态保留黄色左边框，仅 severe（红/灰态或派生信号异常）强制红色左边框。
- **文案管理**：全部走 i18n key（`runtimes.health.hint.*`），无写死中文。

## Task List

### Phase 1: locales 文案
- [x] Task 1: zh/en `runtimes.json` `health.hint` 新增 6 个 key（consecutive_failures / heartbeat_lost / offline / recently_lost / about_to_gc / low_score）

### Phase 2: runtimes.js 渲染增强
- [x] Task 2: `_healthHelpHint(rt)` + `_helpBtn(key)` + `_isAbnormalEngine(rt)` 三个纯函数（置于 `_healthScoreBadge` 附近）
- [x] Task 3: `renderEngines` 卡片——badge 旁追加帮助按钮，异常态红色描边/左边框强化
- [x] Task 4: `renderWorkers` 卡片——badge 旁追加帮助按钮

### Phase 3: 测试
- [x] Task 5: `tests/js/test_issue_475_runtime_health_hint.js`（静态断言 + 逻辑提取测试：hint 优先级、健康不渲染、双语 key 一致）
- [x] Task 6: 回归 `tests/js/test_issue_327_engine_list_render.js`、`tests/js/test_issue_364_engine_detail_fallback.js`、`tests/test_runtimes_stats_i18n.py`
