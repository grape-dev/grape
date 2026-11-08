# Plan: Issue #1495 — task_messages 活跃度判定改 epoch 口径

## 测试 seam 协商（编码前）
- 复用已有 seam，不新建：
  - claim 兜底回收 → `src.runtime.service.claim(runtime_id)`（`tests/test_runtime_pool.py` TestStaleRecovery 已覆盖该 seam）；
  - recover_orphans → `POST /api/runtime/runtimes/{rid}/recover-orphans`（`tests/test_recover_orphans_optimization.py` 已覆盖该 seam，TestClient + 真 SQLite 测试库）。
- helper 纯函数行为经上述两个公开 seam 端到端断言，不单测 helper 内部（避免测内部细节）。

## 切片拆解（TDD，一竖切片一红一绿）

### 切片 1：recover_orphans × 异形格式（空格格式 / 无微秒 ISO）
- 红：`tests/test_issue_1495_task_activity_format.py::TestRecoverOrphansFormat`：
  - `test_space_format_message_within_window_not_orphaned`：表默认值形态（`datetime('now')` = `'YYYY-MM-DD HH:MM:SS'` 空格分隔）注入窗口内消息 → recover-orphans 判 `skipped_active`，状态保持 running，attempts 不变（旧字节比较下 `0x20<0x54` 恒判孤儿 → 红）。
  - `test_no_microsecond_iso_message_within_window_not_orphaned`：无微秒 ISO（`...T08:00:00+00:00`）窗口内消息 → 同上（旧字节比较下同秒 `0x2B<0x2E` 塌陷 → 红）。
  - `test_unparseable_message_not_active_and_orphan_recovered`：仅含不可解析 `created_at` 的消息行 → 不计活跃，孤儿照常回炉（保守取舍护栏）。
- 绿：`src/runtime/task_activity.py`（常量 + `has_recent_activity`）+ `runtime.py` recover_orphans 改走 helper。

### 切片 2：claim 兜底 × 异形格式 + 真死任务仍回收
- 红：同文件 `TestClaimBackstopFormat`：
  - `test_space_format_message_blocks_claim_backstop` / `test_no_microsecond_iso_message_blocks_claim_backstop`：dispatched 超时（700s 前 started_at）但消息为异形格式且在窗口内 → claim 后任务不被回收（status 仍 dispatched、attempts=0；旧字节比较下会被回炉 attempts+1 → 红）。
  - `test_dead_task_with_only_stale_messages_recovered_on_claim`：消息存在但全部在 600s 窗外 → claim 照常回收（真死任务不因 helper 引入而漏回收）。
- 绿：`service.py` claim 兜底处改调 helper + 「最近 60s」注释修正为 600s。

### 切片 3：验收 grep 护栏（随实现提交）
- `grep -rn "task_messages WHERE task_id=? AND created_at >" src/` 为 0；
- `grep -rn "600\|1200" src/runtime/service.py src/api/routes/runtime.py` 不再有窗口数值字面量（均引用 `task_activity` 常量）。

## 提交计划（红/绿分提交，可追溯）
1. `test(#1495): task_messages 活跃度异形格式 先写失败测试`（切片 1-2 红测）
2. `feat(#1495): 活跃度判定改 epoch 口径，新增 task_activity helper`（task_activity.py + 两调用点 + 注释修正 + docs/issue-1495）
3. 回归：`pytest tests/test_issue_1495_task_activity_format.py tests/test_recover_orphans_optimization.py tests/test_runtime_pool.py -q`，再跑相关 claim/recover 全量回归。

## 验收对照
| AC | 落点 |
|----|------|
| 1) 两调用点 × 两类异形格式不误回收 | 切片 1/2 各 2 用例 |
| 2) grep 字节比较 SQL 为 0 | 切片 3 |
| 3) 600/1200 单点定义 + 60s 注释修正 | task_activity.py + service.py |
| 4) 现有回归全绿 + 真死任务仍回收 | 切片 2 末用例 + 既有 recover/claim 回归 |
| 5) db_lock 内、无新增 IO | helper 仅用调用方传入 cursor 顺序查询，不另开连接 |
