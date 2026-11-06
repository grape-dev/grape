# Spec: Issue #975 — 任务日志上报批次遇 413/4xx 被当成功吞掉

> 来源：Issue #975 Spec（issue agent 发布，评级 ready）| 目标分支：weekly-mys

## Objective
修复 worker→master 任务日志上报链路：批次遇 413/4xx 不得被当成功吞掉（原链路
`client._request >=400 → None → report_messages 返回 {} → agent 清空 batch`），
失败批次走拆批重发 → 补报队列/dropped 占位，批次回执带 seq 范围可对账。

## Scope
- In：`src/runtime/client.py`（_request 状态码分类、report_messages 三态）、
  `src/runtime/agent.py`（失败不清 batch、拆批重发、降级占位）、
  `src/runtime/worker_report_queue.py`（messages 条目补报重放）、
  `src/runtime/service.py` + `src/api/routes/runtime.py`（批次回执附 seq 范围）。
- Out：持久化层（已实测证伪落库截断）、#976/#977、`client_max_body_size` 调优本身。

## Acceptance Criteria
1. mock 413 单测：batch 不静默丢失，走降级/补报路径，占位条目可见（批次 ≥50 条）
2. mock 413 单测：拆批重发成功 → 全部消息送达无缺号
3. 现有测试全过 + 新增测试覆盖新逻辑
4. 线上 client_max_body_size 验证由部署（sre）环节执行
