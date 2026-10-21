# Plan & Task Breakdown: Issue #379

## 任务拆解（原子提交）

### Task 1: seq 贯通 fanout 载荷
- **service.py `report_messages`**：INSERT 循环中收集每条消息的 seq，构造 `[{**m, "seq": seq}]` 列表传给 `fanout`（替代原始 `messages`）。
- **task_log_stream.py `_to_payload`**：透传 `m.get("seq")` 到 payload（已有的 `created_at` 保持不变）。
- **task_log_stream.py `fanout`**：接收带 seq 的消息（向后兼容：无 seq 不带）。
- 测试：fanout 载荷含 seq。

### Task 2: SSE stream 生成器 DB 增量尾随
- **task_logs.py `gen()`**：
  - catch-up 阶段：发完存量后记录 `last_seq = 末条 seq`（无则 = since）。
  - live 循环：`asyncio.wait_for(q.get(), timeout=_TAIL_POLL_S)`（1.5s）。
    - 收到 payload：yield，更新 `last_seq = max(last_seq, payload.seq)`。
    - 超时：调 `list_task_messages(task_id, since=last_seq)` 补发，更新 `last_seq`。
    - keepalive 逻辑：累计空闲时间达 `_HEARTBEAT_S`（15s）才发 keepalive。
- 测试：绕过 fanout 直插 DB，SSE 在 ≤2×尾随周期补发。

### Task 3: 修复既有测试隔离 + 新增跨主模拟用例
- 修复 `_clean` fixture：清理 `t1` 等无 repo_key 的测试任务残留。
- 新增 `TestCrossMasterTail`：模拟跨主落库（直接 INSERT，不 fanout），验证 SSE 尾随补发。
- 新增 `TestFanoutSeq`：验证 fanout 载荷含 seq。

## 风险
- DB 轮询压力：(task_id, seq) 走 UNIQUE 索引，单查询 <1ms，每面板 1 次/1.5s。
- 双通道重复：seq 贯通 + 前端既有去重覆盖；单测验证。
- async gen 内同步 DB 调用：查询轻量，与现有 catch-up 同款先例。
