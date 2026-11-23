# Task Breakdown: Issue #4024

- [x] 定位置位落库路径（`_set_issue_pending_review`）与读回路径
      （`get_issue_status` / `load_issue_business_status` / `derive_review_status`）。
- [x] `issues status get` 新增 `review_status`（+ `stage`），与置位/详情同源。
- [x] `issues pending-review` 响应回读落库真值并携带 `review_status` / `idempotent`。
- [x] `issues detail` 已知内部主键时以该 `issue` 行为单一事实源，消除聚合顺序分歧。
- [x] 新增回归用例：置位→立即读回、pipeline/detail 去滞留、双 runtime 对账、
      聚合口径契约。
- [x] 契约/文档注明聚合口径来源。
- [x] 回归 `#539` / `#2695` 既有用例。