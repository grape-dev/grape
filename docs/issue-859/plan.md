# Plan: Issue #859

## 方案

将 `_group_events_by_issue` 中硬编码的 10 列 SQL 替换为已构造的 `_sel`，并在 `_sel` 中补充 `entity_type` 列：

```
_sel = "SELECT seq, issue_id, event_type, agent_type, title, actor, summary, status, created_at, runtime, skill, entity_type"
        + (", structured_output, agent_logs" if _has_so else "")
```

列索引映射（`r[N]`）：

| N | 列 | 旧代码问题 |
|---|----|-----------|
| 0 | seq | - |
| 1 | issue_id | - |
| 2 | event_type | - |
| 3 | agent_type | - |
| 4 | title | - |
| 5 | actor | - |
| 6 | summary | - |
| 7 | status | - |
| 8 | created_at | - |
| 9 | runtime | 旧 SQL 无此列，r[9] 被误作 entity_type |
| 10 | skill | 旧 SQL 无此列，skill 恒空 |
| 11 | entity_type | 旧 r[9]，保留 Issue #252 过滤 |
| 12 | structured_output（仅 _has_so） | 旧 len(r)>11 恒假 |
| 13 | agent_logs（仅 _has_so） | 同上 |

## 设计决策

| 决策 | 选择 | 理由 |
|------|------|------|
| 查询统一 | 用已构造的 `_sel` | `_sel` 已含 `PRAGMA table_info` 动态探测 `structured_output` 列，逻辑健壮 |
| `_json` 导入位置 | 函数内局部 `import json as _json` | 与现有 `from src.session_manager import db, db_lock` 局部导入风格一致，最小改动 |
| entity_type 列 | 在 `_sel` 中补充 | 保留 Issue #252 的 PR 事件过滤逻辑 |
| output/logs 分支判断 | 用 `_has_so` 标志（从 PRAGMA 探测结果传出锁外） | 比 `len(r) > 11` 更明确；列探测已在锁内完成，标志传出复用 |

## 测试计划（tests/test_issue_859_event_output_logs.py）

1. RED-GREEN：事件含 structured_output + agent_logs → `output`/`logs` 非空且内容正确
2. output/logs 解析失败（非法 JSON）→ 不崩溃，回退 None/[]
3. 无 structured_output 的事件 → 不含 output 字段（向后兼容，旧事件正常分组）
4. entity_type='pull_request' 事件仍被过滤（回归 Issue #252）
5. runtime/skill 字段取值正确（回归顺带缺陷）
6. numbers 收窄参数仍生效

## 实现步骤（TDD）

1. T1 spec.md / plan.md / task.md
2. T2 新增单测（先 RED：当前实现 output/logs 为空 → 断言失败）
3. T3 修改 `_group_events_by_issue`（_sel 统一 + import _json + 索引对齐）
4. T4 新测试 GREEN + 相关既有测试不回归（test_issue_252_entity_type / test_issue_events_api / test_issues_api 等）
5. T5 自审查 + pre-pr 检查 → 提交推送 → PR → 评论 + A2A QA
