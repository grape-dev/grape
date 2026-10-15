# Plan: Issue #342 — running 状态局部轮询方案

## 根因
`events.js` `_startEventPoll` 每 5s 的回调直接调用 `renderEventDetail(d)`，而
`renderEventDetail` 以 `el.innerHTML = html` 全量重写 `#pageContent`：
- 折叠区块（ctxInfo/promptBody）被重置回默认态；
- 已加载的接收上下文 prompt 丢失（回到占位提示）；
- 对话面板重建（输入草稿、滚动位置、流式气泡丢失）；
- 大面积 DOM 重建造成肉眼可见的整页闪动。

## 方案（局部轮询 + 定向状态更新）
1. **片段化**：将随事件状态变化的区域抽为稳定 id 容器
   （`evInfoSummary`/`evEngineRow`/`evDurationRow`/`evStatusRow`/`evFailReason`/`evLogRow`/`evProcessingBody`），
   构建逻辑抽为模块级 `_evXxxText(data)` 纯函数，全量渲染与局部更新共用（保证两路径一致）。
2. **指纹跳过**：`_evFingerprint(data)` 汇总动态字段（status/duration/result/engine/task/archive…），
   轮询应答指纹未变 → 跳过一切 DOM 写入（零闪动）。
3. **定向更新**：`_patchEventDetail(data)` 仅替换片段容器 innerHTML，不触碰折叠态、prompt、
   对话面板；页面结构缺失（事件切换竞态）时退回全量渲染兜底。
4. **终态处理**：轮询发现 completed/failed/cancelled → `_stopEventPoll()` 后完整渲染一次最终结论，
   此后不再刷新；非 running 不启动轮询（原有逻辑保留）。
5. **离开清理**：`app.js` 离开 event 视图时显式 `_stopEventPoll()`（原实现要等下一 tick 自检，
   存在最长 5s 的残留定时器）。

## 不变量
- 重新派发/本机重试/A2A 补发/终止按钮的显示条件判定逻辑逐字保留（仅迁移到 `_evStatusText`）。
- `navEvent` 切换事件仍走全量渲染（预期行为）。

## 测试
`tests/js/test_issue_342_event_poll_partial.js`（node 直跑，DOM/API mock）：
启停条件、指纹跳过、定向更新不重写 pageContent、终态渲染与请求停止、app.js 清理调用、构建函数双路径复用。
