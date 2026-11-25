# Tasks: Issue #3794

- [x] T1 新增测试 `tests/runtime/test_issue_3794_guard_purge.py`（红：分批清理/§33/1205 重试用例在旧实现下失败或不成立）
- [x] T2 组合索引 `idx_agent_tasks_event_seq_status(event_seq, status)`（**注册式迁移** `202611240009`，runtime 域；初版误落老轨，QA #1981 C1 修正）
- [x] T3 重写 `purge_stale_event_guards`：两段短事务按 seq 分批（200×50）、DELETE 内 NOT EXISTS 复核、1205 短退避重试一次、失败日志带 `seqs_n`/`seqs_range`、DELETE 前压 3s 行锁上限并在退出时还原、候选 SELECT 后提交丢弃快照
- [x] T4 指标 `grape_event_guard_purged_total{result}` + `purged=N` 日志（口径：`ok` 按删除行数、`failed` 每条失败计 1）
- [x] T5 跑既有相关测试（test_event_active_guard.py、test_takeover_event_guard.py、test_issue_372_claim_marker_guard.py）回归
- [x] T6 自审查（5 轴 + pre-pr 增量检查）→ 建 PR（base=arch-weekly-11-ljt）→ Issue 评论 → A2A QA
- [x] T7（QA #1981 R2）候选窗口 keyset 按 seq 推进，去掉 `deleted == 0 → break`；200 受保护 + 5 可删一次调用删满 5 条
- [x] T8（QA #1981 M1）1205 重试前 `db.rollback()`；1213 deadlock 一并纳入重试判定
- [x] T9（QA #1981 M3）同一迁移回收左前缀冗余索引 `idx_agent_tasks_event`
- [x] T10（QA #1981 R3）新增用例接入 `.github/workflows/windows-smoke.yml` linux-backend 门禁
- [x] T11（QA #1981 经验沉淀）`test_migration_track_guard.py` 增加老轨索引 DDL 冻结守卫（`LEGACY_INDEX_DDL_FROZEN`）
- [x] T12（QA #1981 经验沉淀）docs 补「DDL 落点校准」小节，spec/plan/task 与实现对齐

## 指标口径（固化）

| 指标 | label | 计数口径 |
|---|---|---|
| `grape_event_guard_purged_total` | `result="ok"` | 本次调用累计删除**行数**（rowcount 累加） |
| `grape_event_guard_purged_total` | `result="failed"` | 每条失败事件计 1（select 失败 / 重试耗尽） |

返回值为累计删除行数；失败时返回已删数（部分成功）或 0，不抛出（300s 后台循环不中断）。

## 交付状态

| 项 | 状态 | 证据 |
|---|---|---|
| 守卫自愈（分批 + keyset + §33 复核） | ✅ | `tests/runtime/test_issue_3794_guard_purge.py` 18 例通过 |
| 组合索引存量库生效 | ✅ | 迁移 `202611240009` 登记 + `tests/api/test_issue_3794_event_seq_status_index.py` 5 例 |
| 老轨不再新增补索引 | ✅ | `test_migration_track_guard.py::test_legacy_index_ddl_is_frozen` |
| CI 门禁 | ✅ | `.github/workflows/windows-smoke.yml` linux-backend 新增 pytest 步骤 |