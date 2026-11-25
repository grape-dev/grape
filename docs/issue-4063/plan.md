# Issue #4063 方案设计（Plan）

## 状态模型（权威 / 投影）
- **权威源**：`issue.status`（单行，写入由状态机迁移控制）。
- **投影**：
  - `issues detail.task_status` = 该行 `status`（`_task_status_map` 直读）；
  - `issues status get.status` = 该行 `status`（同一 `id` 行直读）；
  - `review_point.status` = `status == pending_review ? "pending_review" : ""`（同源派生，不单独落状态列）。
- `issue.review_points` / `issue.review_refs` 仅存**在位审核点内容快照**，用于幂等判据；是否在位仍由 `status` 唯一决定，避免双写漂移。

## 变更点
1. 事件派发（event_pipeline）：`issue_comment` 自动恢复恒 `preserve_review=True`，只恢复 `awaiting_input`；`pending_review` 不再被评论消费。Agent 自身评论在自循环检测中跳过。
2. 置位幂等（issues_center.\_set_issue_pending_review）：
   - 首次置位（active/processing/awaiting_input）落状态 + 内容快照 → `changed:true`；
   - 在位且内容/参考等价 → `changed:false`；
   - 在位内容/参考变更 → `changed:true` 并刷新快照；
   - 存量在位行无快照 → 保持 #725 幂等（无法证明变更时 `changed:false`）。
3. 读模型（routes/issues）：
   - `issues detail` 增加只读 `review_point.{status,references}`；
   - `issues status get` 同样返回 `review_point`，与 detail 同源（`pending_review` 判定统一 strip 归一）；
   - `issues detail` 增加 `id`/`issue_number` 别名，与 `issues status get` 的 `id`/`issue_number` 同口径（收敛 #3981 次要缺陷）；
   - `pending-review` 响应补 `idempotent` 标记（与 #725 契约一致）。
4. 存储迁移：注册式迁移 `202611240003 issue-review-point-snapshot-columns`（合入基线时改号避免与 #4066/#3789 撞号） 补两列（兼容 SQLite/MySQL，存量库与全新库均执行）。
5. 契约说明写入 operation description（detail / status get / pending-review）：权威/投影关系 + 相关 CLI 子命令版本差异（`grape issues status get --issue_id` 需 CLI ≥ 0.3.4，CLI 0.3.2 无该子命令，#4026 附注）。

## 影响与回归
- `issue_comment` 不再作为 pending_review 的隐式“审核通过/继续”信号，必须显式 `review-pass` / `review-reject`（或内容变更重置位）。
- 回归覆盖：置位→评论→读回、重复置位、内容变更、detail/status 投影一致、awaiting_input 恢复对照。