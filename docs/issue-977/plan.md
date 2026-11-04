# Plan: Issue #977 — /info 增补 wrote_files

## 方案
1. `src/runtime/service.py` 新增 `list_wrote_files(task_id)`：SELECT `seq, tool, input_json, created_at FROM task_messages WHERE task_id=? AND type='tool_use' ORDER BY seq`，Python 侧按工具名（write/write_file/patch 及含 write/patch 变体）+ `input` 含 `path` 与 `content`/`new_string` 精筛；`bytes = payload.encode("utf-8")` 长度（patch 取 `new_string`）；同 path 多次写入各记一条；`first_seen_at = normalize_event_time(created_at)`。
2. `src/api/routes/task_logs.py`：`get_task_runtime_info` 响应尾部追加 `"wrote_files": list_wrote_files(task_id)`；通过路由 `responses={200: schema}` 登记 `wrote_files` 响应 schema（不改运行时序列化）；docstring 补充字段说明。
3. `help/api/09-task-logs.md`：`/info` 出参行同步 `wrote_files` 说明。
4. `tests/test_issue_977_wrote_files.py`：2 条单测（对齐 test_issue_701 直调函数 + 真实测试库种数据风格）。

## 风险
- hermes 任务在 #976 修复前无真实 write 入参 → 返回 `[]`（兼容语义），非阻塞。
- 超大消息解析延迟 → 仅解析 path/content 字段，量级与 list_task_messages 对齐。
