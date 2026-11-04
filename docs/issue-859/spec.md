# Spec: Issue #859 — [P2] Issue 事件列表 output/logs 永不返回（_sel 未使用 + _json 未定义）

## Objective

修复 `src/api/routes/issues.py` `_group_events_by_issue`（306-368）导致 Issue 事件列表 `output`/`logs` 永不返回的缺陷：

- 函数内构造了含 `structured_output, agent_logs` 的 `_sel` 查询（:330-331），但从未使用；
- 实际执行的是硬编码 10 列 SQL（:333-335），`len(r) > 11` 恒为假 → `output`/`logs` 分支永不进入；
- 即便补齐列，:359/:364 使用的 `_json` 在函数作用域未定义（仅导入了 db/db_lock），`_json.loads` 触发 NameError 且被 `except Exception` 吞掉；
- 顺带缺陷：:341 `r[9]` 同时被当作 `_entity_type`（Issue #252 PR 事件过滤）与 :353 的 `runtime`；:354 `r[10]` 取 skill 但查询无 skill 列，skill 恒为空。

## Root Cause

查询列与解析列不一致：`_sel`（含 PRAGMA 动态探测 `structured_output` 列）被遗弃，实际 SQL 缺 `runtime`/`skill`/`structured_output`/`agent_logs` 列；且解析侧引用了未导入的 `_json`。

## Scope

### In
- `_group_events_by_issue` 查询逻辑修复：统一使用 `_sel` 执行查询（补充 `entity_type` 列）
- 函数内 `import json as _json`
- 调整所有 `r[N]` 索引与 `_sel` 列顺序一致（seq, issue_id, event_type, agent_type, title, actor, summary, status, created_at, runtime, skill, entity_type, [structured_output, agent_logs]）
- 新增断言 output/logs 非空的单测（tests/test_issue_859_event_output_logs.py）

### Out
- `_group_events_by_issue` 函数签名/参数变更
- events 表 schema 变更
- 其他路由函数改动

## Acceptance Criteria
- `_sel` 执行查询，不再硬编码列
- `_json` 导入，`_json.loads` 不再 NameError
- `entity_type` 过滤逻辑（Issue #252）保留
- 有 `structured_output`/`agent_logs` 的事件查询时，返回的 `output`/`logs` 非空
- `runtime`/`skill` 字段取值正确（回归顺带缺陷）
- 单测覆盖 + 现有测试通过
