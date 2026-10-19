# Task Breakdown: Issue #370

## 编码
- [ ] T1: `src/api/routes/runtime.py` — `recover_orphans` 增加 `RECOVER_GRACE_SECONDS`、SELECT 扩展、年龄闸门、`skipped_young` 返回
- [ ] T2: `tests/test_recover_orphans_slots.py` — 扩展 `_seed` + 新增 UT-4/UT-5/UT-6

## 测试
- [ ] T3: 运行 `tests/test_recover_orphans_slots.py` 全部 6 条用例通过
- [ ] T4: 运行 `tests/test_claim_response_lost.py` 回归通过
- [ ] T5: 运行 `tests/test_runtime_protocol.py` 回归通过

## 验收对照

| Spec 验收 | 实现 | 测试 |
|-----------|------|------|
| 新任务 < grace → skipped | 年龄闸门 | UT-4 |
| started_at NULL → 兜底 created_at | `started_at or created_at` | UT-5 |
| 超龄无消息 → 回炉 attempts+1 | 原有逻辑 | UT-2 |
| 有近期消息 → skipped_active | 原有逻辑 | UT-3 |
| attempts=3 → force_failed | 原有逻辑 | UT-1 |
| RECOVER_GRACE_SECONDS 覆盖 | env var | UT-6 |
