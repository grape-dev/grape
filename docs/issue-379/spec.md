# Spec: Issue #379 — 双主环境实时日志增量推送修复

> 基于 Issue Agent 发布的 Spec（Issue #379 评论 id 194675477），目标分支 weekly-08。

## 目标
修复双主（master-001/002）环境下远端 worker 执行事件的实时日志面板不自动刷新（如 event/14008）；重开页面才可见全量。本地引擎事件（event/14017）不得回归。

## 根因
SSE 实时增量 = 单进程内存 fanout（`task_log_stream._subscribers`），不跨 master 互通。worker 上报落在哪台 master，fanout 只在该进程发生。DB catch-up 仅在 SSE 建连/重连时刻执行。浏览器连的是另一台 master → 增量丢失。全量查询走共享 DB → 重开页面可见。

## 技术方案：SSE 服务端 DB 增量尾随
1. **task_logs.py stream 生成器**：live 循环在订阅队列等待上加短超时 `_TAIL_POLL_S`（1.5s）；每次超时执行 `list_task_messages(task_id, since=last_seq)` 增量补发（带 seq）；无新消息且满 15s 才发 keepalive。`last_seq` 初始化 = catch-up 最大 seq，随推送更新。
2. **seq 贯通**：`report_messages` INSERT 时已知各消息 seq → 构造带 seq 的消息列表传给 fanout；`_to_payload` 透传 seq。前端已有按 seq 去重（events.js:1311 `evt.seq <= _taskLog.seq` 跳过）→ queue 快路径与 DB 尾随双通道不会重复渲染。
3. **同主快路径保留**（queue 命中即时推，延迟≈0）；跨主最坏延迟 = 尾随周期 + ingestion 批处理 0.2s。

## 影响模块
| 模块 | 改动 | 说明 |
|---|---|---|
| src/api/routes/task_logs.py | 修改 | stream 生成器加 DB 尾随 + last_seq 跟踪 |
| src/runtime/service.py | 修改 | report_messages 传带 seq 的消息给 fanout |
| src/runtime/task_log_stream.py | 修改 | _to_payload 透传 seq |
| tests/test_task_messages.py | 新增用例 | 跨主模拟 + fanout seq 贯通；修复既有隔离问题 |

## 验收标准
- [ ] 新单测：模拟"另一台 master 落库"（直接 INSERT task_messages，不触发本进程 fanout），SSE 流在 ≤2×尾随周期内推送该消息
- [ ] 新单测：fanout 载荷含 seq，且与 DB 尾随补发同一消息时前端去重输入一致
- [ ] 既有 tests/test_task_messages.py 全部通过（同主路径不回归）
- [ ] 部署双主后实测：远端 worker 事件实时日志自动刷新；本地引擎事件不回归（sre/qa 部署验证）
