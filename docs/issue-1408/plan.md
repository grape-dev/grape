# Plan: Issue #1408 — 已完成需求可手动撤销回待审核

## 方案概述
在既有需求状态机 `_TASK_STATUS_FLOWS` 增加唯一出口 `completed → pending_review`，并复用 `PUT /api/issues/{issue_id}/status` 写入口：命中该迁移时在同一事务内以单条 UPDATE 把 `status` 置为 `pending_review`、`stage` 同步回退为 `pending_review`、清空 `completed_at`/`completed_reason` 并刷新 `updated_at`，仅更新上述列以保留 `assignee`/`owner_agent`/`a2a_seq` 等归属字段；UPDATE 带 `AND status='completed'` 守卫，`rowcount==0` 时返回 409 并跳过审计；审计为 best-effort（失败仅告警，不回滚状态）。其它迁移维持原行为。

## 改动点
| 模块 | 改动 |
|------|------|
| `src/api/issues_center.py` | `_TASK_STATUS_FLOWS["completed"]` 由 `()` 改为 `("pending_review",)` |
| `src/domains/runtime/routes/issues.py` | `change_issue_status` 识别 `completed → pending_review`，同一条 UPDATE 清完成字段、回退 status/stage、刷新 `updated_at`（保留归属字段），带 `AND status='completed'` 并发守卫（不命中 → 409 且不写审计）+ 审计留痕（实际 from_stage、撤销前 completed_at/completed_reason） |
| `src/domains/mgmt/auth/audit.py` | `EVENT_TYPES` 新增 `issue_reverted_to_pending_review` |
| `tests/runtime/test_issue_627_production_release.py` | 终态断言更新为单出口（docstring 同步） |
| `tests/runtime/test_issue_1408_revert_completed.py` | 新增状态机/权限/幂等/审计/并发守卫/回归测试 |

## 关键决策
| 决策 | 选择 | 理由 |
|------|------|------|
| stage 回退目标 | 固定 `pending_review` | 与目标 status 一致、语义可预测 |
| 权限 | 复用 `require_no_session_token` + 属主校验 | `tk_` 会话 token 已 403；属主校验既有 |
| 幂等 | `cur == ns` 时维持既有成功路径 | 重复提交成功、无非法迁移 |
| 事务性 | status 更新 + stage 同步在同一 `db_lock` 内，单条 UPDATE 原子写入 | 避免 status/stage 不一致 |
| 并发保护 | UPDATE 带 `AND status='completed'` 守卫，`rowcount==0` → 409 | 多进程部署下不覆盖并发写入的终态（与 `src/api/session_store.py` 状态写路径惯例一致） |
| `updated_at` | 撤销时刷新为 `datetime('now')` | 与 `_sync_issue_fields` 的写路径惯例一致 |

## 风险与缓解
- 放开终态影响统计/流水线：出口仅一条 `pending_review`，回归覆盖 `pending_review` 后续流转与 cancel/resume。
- 越权撤销：复用属主校验并补充非属主、会话 token 测试。
- 审计缺失：新增审计事件类型并断言落库；审计 detail 保留撤销前完成时间/原因，便于回溯。
- 并发覆盖：status 守卫 + 409 提示刷新重试，避免把并发写入的终态静默改回待审核。