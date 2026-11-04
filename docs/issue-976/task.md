# Issue #976 任务拆解

| # | 任务 | 状态 |
|---|------|------|
| 1 | hermes_acp.py tool_call 分支：rawInput/input/parameters/content 块语义读取 | done |
| 2 | name/title 皆空 → 占位 tool_use 事件（tool=unknown，保留 title/tool_call_id） | done |
| 3 | 超限裁剪契约（truncated/original_bytes/sha256_prefix，env 可调上限） | done |
| 4 | 单测：57KB 保真（4 路径含真实引擎往返） | done |
| 5 | 单测：占位事件两形态 | done |
| 6 | 端到端：/api/task-logs/{id}/messages 取回完整入参 | done |
| 7 | 回归：hermes/hwcloud ACP + task_messages 既有测试 | done（test_runtime_hermes 2 例失败为基线预存，与本改动无关） |
