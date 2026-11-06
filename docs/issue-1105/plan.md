# Plan: Issue #1105 终态事件强制 completed_at

## 切片
1. **切片 1（红→绿）**：`update_event_status` 非 failed 终态（completed/cancelled）写入 completed_at；已非空则保留原值（不覆盖）。
2. **切片 2（红→绿）**：`update_event_complete` 调用方传 `completed_at=""` 时兜底赋当前时间（终态保护跳过的行不动）。
3. **切片 3（红→绿）**：admin-confirm 收敛分支 `UPDATE events SET status='completed'` 补 `completed_at=COALESCE(NULLIF(completed_at,''), datetime('now'))`。
4. **切片 4（迁移）**：db_setup.py 注册 safe 数据迁移 `backfill_events_terminal_completed_at`：分批 UPDATE 终态且 completed_at 为空的行，取 created_at 回填（created_at 空则当前时间），幂等可重跑。

## 测试 seam
- 复用 `tests/test_event_status_protection.py` 的 fixture 模式（临时 SQLite + monkeypatch db）。
- 迁移函数直接对临时库调用 `fn()`，断言回填结果与幂等性。

## 提交拆分（TDD 红/绿分提交）
- test(#1105): 失败测试 ×3 切片
- feat(#1105): 3 处写入口收口 + 迁移
