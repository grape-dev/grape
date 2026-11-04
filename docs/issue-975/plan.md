# Plan: Issue #975

## 方案
把「任何 >=400 → None → {} → 清空 batch」的错误吞没链路，改造为显式三态 +
批次级失败降级（拆批重发 → 补报队列/dropped 占位）+ seq 对账回执。

### client.py（三态 + 错误分类）
- `_request` 返回 None 前把最近 HTTP 错误记入 `self.last_http_error`
  （status=0 表示网络/重试耗尽；404 runtime gone 记 404）。
- `report_messages`：
  - 成功 → dict（服务端回执含 seq 范围）
  - 410 → None（终态，中断）
  - 其余 4xx（400/412/413/414/422/431…）→ `raise ReportBatchError(status, text)`
  - 网络错误 → `{}`（可重试，调用方保留 batch）

### message_report.py（新增，共享降级策略）
- `split_resend`：二分拆批递归重发（深度上限 6），单条失败/网络错误保留。
- `degrade_failed_batch`：拆批重发 → 单条仍失败发 dropped 占位
  （`dropped:true`、count、reason、data_head，小 payload 必达）+ 原消息截断后
  落 WorkerReportQueue（kind=messages）。
- agent.py 执行 flush 与 worker_report_queue 补报重放共用，降级策略一致。

### agent.py（flush 三态处理）
- 新增 `_flush_report_batch`：仅成功（status ok）清空 batch；网络错误返回
  "retry" 保留 batch 下窗口重试；ReportBatchError 走 `degrade_failed_batch`
  后按已处理清空；410 → "terminal" 中断执行；CredentialRevokedError 原样上抛。
- 成功回执记录 seq 范围日志（worker 侧累计已确认 seq 对账）。

### worker_report_queue.py（messages 补报）
- 条目支持 `kind=messages`（合成 key `task_id#msg-…`，字段 `task` 存真实 id）。
- `replay_pending_reports`：messages 条目重发成功出队；网络错误保留；
  单条仍 4xx → 占位后出队（防无限重放）；任务终态 → 出队。

### service.py + routes/runtime.py（seq 回执）
- `peek_next_seq`（MAX+1 预读）；`report_messages(task_id, messages, seq_start=None)`
  支持预约 seq 起点；落库后记 seq 范围回执日志。
- `enqueue_async_report` 携带 seq_start（4 元组）；
  /messages 路由同步预约并返回 `{"status":"ok","count","seq_start","seq_end"}`。
  worker 对同一 task 串行上报 → 预约与落库顺序一致。

## 关键决策
| 决策 | 选择 | 理由 |
|------|------|------|
| 失败表达 | 结构化三态 + ReportBatchError | 根因即「假成功」；旧 {} 语义（网络可重试）保留兼容 |
| 413 处理 | 二分拆批 → 单条 → 占位 + 补报队列 | 单条超限概率低；占位小 payload 必达、可观测 |
| 降级落点 | 占位（即时可见）+ 队列（内容补报） | 占位保排障，队列保内容；TTL/容量防积压 |
| seq 回执 | 路由同步预约 + 异步落库沿用 | 不动攒批落库架构；同 task 串行保证顺序 |

## 风险与缓解
- report_messages 语义变更影响其他调用方 → 全仓 grep：其余调用点均已 try/except
  包裹且为单条 fire-and-forget，不受影响；网络错误仍返回 {}。
- 拆批放大 master 压力 → 深度上限 6 + 单条即止；占位后出队防重放风暴。
- 占位污染 seq 序列 → 占位带 dropped:true + reason，明确区分。
