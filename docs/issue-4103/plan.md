# Plan: Issue #4103 — prior_status 与条件写入

## 方案概述

`_set_issue_pending_review` 保留布尔返回以兼容既有调用方，新增
`_set_issue_pending_review_with_prior` 作为对账实现：

1. 在同一 `db_lock` + 游标内实读 `prior_status`（复用 `_read_issue_status` 的游标入参）。
2. `prior_status == pending_review` → 不属于可覆盖源状态，不迁移状态。沿用 #4063 内容幂等：
   review_points/references 等价时短路零写入并返回 `(pending_review, pending_review, False)`
   （满足「无写入」断言）；内容变更时仅条件刷新快照，`changed` 取本次 rowcount；
   存量在位行无内容快照时按幂等处理。
3. `prior_status ∈ {active, processing, awaiting_input}` → 执行
   `UPDATE ... WHERE repo_key=? AND issue_number=? AND status=prior_status` 条件写入；
   读后被并发改走时 CAS 不命中，不覆盖新状态。
4. 写后复读 `status`；`changed = (cursor.rowcount > 0)`，只表示本次真实写入，
   不按 `prior_status != status` 反推，避免 CAS 未命中时的假阳性。
5. 其他状态（终态/未知）不写入，仅复读真实值。

## 回执契约

`POST /api/issues/{issue_id}/pending-review` 返回：

```json
{"ok": true, "issue_id": 1, "prior_status": "active", "status": "pending_review", "changed": true}
```

既有 `ok/issue_id/status/changed` 语义保留；`idempotent` 继续作为幂等提示（`not changed`）。
会话/通知副作用的触发条件收敛为「本次真实写入 pending_review」，即
`changed（rowcount>0） && status == "pending_review"`，避免 CAS 未命中时误发通知。

## 测试方案

新增 `tests/runtime/test_issue_4103_pending_review_prior_status.py`：

- 透过 SQLite 触发器探针记录 `issue.status` 的真实 UPDATE，断言幂等路径零写入。
- 透过 `GET /api/issues/{id}/status` 取得写入前状态，断言与 `prior_status` 一致。
- 直接模拟派发挤出 `pending_review → active`，断言复置回执为
  `prior_status=active / status=pending_review / changed=true`。
- 模拟 SELECT 与 UPDATE 之间他进程抢先置位（CAS 命中 0 行），断言 `changed=false`、
  `idempotent=true`、探针无本次写入且不触发通知。
- `teardown_module` 删除探针触发器/表，避免全局 CREATE TRIGGER 污染共享测试库。

## 风险与缓解

| 风险 | 缓解 |
|------|------|
| 读与写之间状态被并发改走 | CAS 条件写入，不覆盖读取后发生的新状态 |
| 幂等路径出现隐式写入 | 短路分支不执行 UPDATE，并用触发器探针测试锁定 |
| 回执新字段影响既有调用方 | 仅新增字段，既有字段语义不变 |
