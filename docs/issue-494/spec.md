# Spec: Issue #494 — 任务执行超时失败无错误原因

> Spec 权威来源：Issue #494 评论 id=196637838（issue agent 生成，已确认）。本文件为开发侧副本摘要。

## 目标
- 超时类失败（step `stopReason=timeout` / 进程被 timeout 杀死 / returncode=124 或 SIGTERM）时，`agent_tasks.errors` 写入「执行超时（Ns）」明确归因，并透出到事件流 result 与前端失败态。
- 前端失败态展示 errors 摘要（截断 + 展开全文），而非仅 failed / exit_code=-1。
- 回归：非超时类失败（exit!=0 且 stderr 空）归因不丢失（#309 口径对齐）。

## 范围
- In：超时特征识别 → 显式归因文案 → errors/事件流双写；前端失败态 errors 摘要；新增单测。
- Out：#421 readline 阻塞根因治理、#309 逻辑重构、存量数据回补、引擎侧资源配额治理。

## 关键决策
| 决策 | 选择 | 理由 |
|------|------|------|
| 超时归因来源 | returncode + stopReason 构造显式文案 | timeout 杀进程 stderr 常为空，尾采拿不到归因 |
| errors 透出范围 | agent_tasks + 事件流 + 前端 | 只落库不透出仍「无原因」 |
