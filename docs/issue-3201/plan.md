# Issue #3201 方案设计

## 核心决策

| 决策 | 选择 | 理由 |
|------|------|------|
| 冻结粒度 | 整 Issue（repo_key + issue） | 接管即冻结同一 Issue 的所有自动派发，避免逐节点漏判 |
| 守门位置 | 建 task 出口：`_create_direct_task` + `_enqueue_pipeline_task` | 本分支 webhook 走 enqueue、A2A direct 走 direct；两个出口都收敛才不漏 |
| 被拦事件 | 入库 `skipped_takeover` | 可审计、不建 task、不重放 |
| force 旁路 | 不提供 | 接管卡住的正解是 abort |
| 过期接管 | 查询时自愈为 `expired` | 与 `list_takeovers` 同口径，避免永久冻结 |

## 数据与顺序

- `active_takeover_for` 命中后，被拦事件 `update_event_status(seq, "skipped_takeover")`，并释放 `event_active_guard`。
- `finish_takeover` 先写会话 `finished` + 吊销 token，再 `_dispatch_next`，因此守门不会拦住 finish 自己触发的派发。
- 忽略计数按 `events.status='skipped_takeover' AND seq > takeover_sessions.event_seq` 统计，避免串到历史接管。

## 状态聚合

`compute_node_statuses` 新增可选 `repo_key/issue_id`：查询 active 接管，命中节点强制 `STATUS_RUNNING`；`skipped_takeover` 事件不参与「最后一条事件」映射。

## 测试 seam

- `_create_direct_task` / `_enqueue_pipeline_task`：公开调用边界断言「不建 task + 事件状态」。
- `active_takeover_for`：过期自愈与跨 Issue 隔离。
- `POST /api/a2a`：409 响应。
- `compute_node_statuses`：纯函数输入事件，锁定与过滤。
- `finish_takeover`：返回 ignored 计数，并断言派发时会话已关闭。
