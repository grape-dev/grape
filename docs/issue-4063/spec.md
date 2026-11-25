# Issue #4063 需求说明（Spec）

## 目标
审核点（`pending_review`）稳定驻留与读数一致：
1. 非审核事件（`issue_comment` 等，含 Agent 自身评论）不得把在位审核点从 `pending_review` 回落；仅 `review-pass` / `review-reject` / 审核点置位内容变更可触发流转。
2. `pending-review` 置位幂等：已在位且内容未变返回 `changed:false`；内容变更返回 `changed:true`。
3. `issues detail` 增补只读 `review_point.status` / `review_point.references`，与 `issues status get.status` 同源对齐；detail 返回体 `id` / `issue_number` 与 `issues status get` 口径对齐（收敛 #3981 次要缺陷）。
4. 契约写明：`issue.status` 是权威状态行；`detail.task_status` 与 `status get.status` 是同一行的读投影；`review_point.status` 同源派生；并标注相关 CLI 子命令版本差异（`grape issues status get --issue_id` 需 CLI ≥ 0.3.4，CLI 0.3.2 无该子命令，收敛 #4026 附注）。

## 范围
- 事件消费：`src/domains/runtime/pipeline/event_pipeline.py`（issue_comment 自动恢复仅作用于 `awaiting_input`）。
- 审核点状态机与快照：`src/domains/runtime/issues_center.py`（`_set_issue_pending_review` 幂等/内容变更）。
- 读模型：`src/domains/runtime/routes/issues.py`（detail / status get / pending-review 响应）。
- 存储：`src/api/db_setup.py` 注册迁移补 `issue.review_points` / `issue.review_refs`。

## 非目标
- `issues list` 缓存最终一致性（#4022 / PR #2046）。
- `git-repo issues comments list` 顶层裸数组（#4012）。
- PR 订阅审核点置入（#3593）。

## 验收
AC1 在位连续 5 次读稳定 `pending_review`；AC2 评论（含 Agent 评论）后仍 `pending_review`；
AC3 重复置位 `changed:false`、内容变更 `changed:true`；AC4 detail/status get 读位一致（或契约标注投影），detail 的 `id`/`issue_number` 与 status get 同口径；
AC5 detail 含 `review_point.*` 只读字段；AC6 下游据只读字段幂等判断；现有测试全绿并新增回归。
