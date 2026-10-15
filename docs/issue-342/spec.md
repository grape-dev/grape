# Spec: Issue #342 — 事件详情页 running 状态一直整页刷新

> 来源：Issue Agent 可开发性分析（ready），本文件为开发侧落档副本。

## Objective
- 修复事件详情页在事件 running 状态下每 5s 整页重写造成的持续闪动/交互重置。
- running 期间数据通过局部轮询自动更新；终态停止；既有入口回归无损。

## Scope
- In：`src/api/static/js/pages/events.js`（事件详情页组件轮询逻辑）、`src/api/static/js/app.js`（离开视图的轮询清理）。
- Out：后端接口、其他页面轮询、刷新频率配置化。

## Acceptance Criteria
- [x] running 期间无整 pageContent 重写（指纹不变时零 DOM 写入）
- [x] 状态/引擎/时长/日志入口/结论定向自动同步
- [x] 终态停止轮询并完整渲染一次最终结论；非 running 不启动
- [x] 重新派发/本机重试/终止等按钮条件逻辑保持原判定
- [x] 新增回归测试 `node tests/js/test_issue_342_event_poll_partial.js` 通过
