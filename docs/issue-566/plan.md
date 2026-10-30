# Plan — Issue #566

## 方案
复用 failed 的重新派发链路，不新建 cancelled 专用链路：

1. **前端渲染条件扩展**（`src/api/static/js/pages/events.js`）
   - `_evRedispatchVisible`：新增 `data.status === 'cancelled'` 分支；该函数同时
     驱动事件详情页（evStatusRow 片段）与详情状态行的按钮渲染，单点改动覆盖两视图。
   - `_evStatusText`：cancelled 使用独立标签（`events.status_cancelled`）与灰色
     `t3` 样式，修复此前 cancelled 被拼成「⏳ cancelled」的误展示。
2. **流水线视图可达性**（`src/api/static/js/pages/issues.js`）
   - 节点点击条件由 `(current || completed)` 扩展为含 `failed || cancelled`，
     终态节点可进入事件详情页执行重新派发。
3. **i18n**：zh/en `events.json` 增加 `status_cancelled`（⊘ 已取消 / ⊘ Cancelled）。
4. **后端**：`/events/{seq}/redispatch` 本身无事件终态白名单（终态/缺失任务走
   快照重建分支），`reset_event_terminal` 为通用终态重置——cancelled 天然放行；
   仅补文档口径（docs/openapi/08-events.md、help/api/08-events.md）与测试。
5. **口径对齐 #269/#320**：终态保护（`update_event_status`/`update_event_complete`）
   已阻止 cancelled→failed 交叉覆盖，取消不会被误判为失败。

## 风险
- 前端多视图遗漏 → 按钮统一收敛在 events.js 单点函数，流水线节点跳详情复用同页。
- cancelled/failed 口径残留 → 测试覆盖延迟 failed 上报不得覆盖 cancelled。

## 测试
- 新增 `tests/test_issue_566_cancelled_redispatch.py`（6 用例）。
- 回归：`test_event_status_protection.py`、`test_issue_521_redispatch_queued.py`、
  `test_issue_453_outbound_ledger.py` 全部通过；`test_openapi.py` 3 项失败在基线
  分支同样存在（与本改动无关，已验证）。
