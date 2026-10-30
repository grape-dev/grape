# Spec — Issue #566 用户取消后也应像失败一样支持重新派发

## 目标
cancelled（用户取消）终态的事件在事件详情页/流水线视图中，获得与 failed 一致的
「重新派发 / 本机重试」操作按钮，点击后按原链路重新派发；failed 既有行为零回归。

## 范围
- 前端：事件详情页（events.js `_evRedispatchVisible` / `_evStatusText`）按钮渲染
  条件由 `status === 'failed'` 扩展为 `failed || cancelled`；cancelled 状态标签与
  颜色独立展示（不再落入 running 前缀误显示）。
- 前端：任务详情流水线节点（issues.js）failed/cancelled 终态节点可点击跳转事件
  详情（即重新派发按钮所在页），与 completed/current 节点一致。
- 后端：`POST /api/events/{seq}/redispatch` 复用既有链路，reset_event_terminal
  对 cancelled 终态同样重置为 running 并清空产出；终态保护保证 cancelled 不被
  延迟 failed 信号覆盖（口径对齐 #269/#320）。
- i18n：zh/en events.json 新增 `status_cancelled`。

## 非目标
- 批量重新派发、历史 cancelled 事件数据迁移、调度策略变更。

## 验收
- [x] cancelled 终态事件详情页出现「重新派发 / 本机重试」按钮（渲染条件扩展）
- [x] reset_event_terminal 支持 cancelled→running 并清 result/structured_output/exit_code
- [x] cancelled 不被延迟 failed 上报改写（update_event_status / update_event_complete 双层保护）
- [x] failed 既有 reset 语义零回归（测试 test_failed_reset_unchanged_semantics）
- [x] 新增测试 tests/test_issue_566_cancelled_redispatch.py（23 项相关测试通过）
