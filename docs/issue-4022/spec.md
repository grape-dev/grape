# Spec: Issue #4022 — pending-review 置位后首读即见新状态（read-your-writes）

## Objective
`issues pending-review` 写入返回 `changed:true` 后，同一 Issue 的读接口（`issues list` /
`issues detail` / `issues status get`）立即返回新状态 `pending_review`，调用方（qa-cli 等）
无需轮询兜底；各读接口对同一 Issue 的 `task_status` / `status` 口径一致，不出现相反读数。

## 现状（缺陷）
- `issues list`（`GET /api/issues/{owner}/{repo}`）带 5s 结果缓存 `@_cached_repo_list(5.0)`，
  且写入后无失效路径：置位成功后 5s 内的读回仍是缓存里的旧值 `active`（qa-cli 实测需连续
  读 5 次才稳定）。
- `issues detail` 的 `task_status` 走按 `issue_number` 聚合的 map（镜像 repo_key 同号多行时
  可能取到另一行），与 `issues status get`（按内部主键回读）存在口径分裂风险。

## Scope
### In Scope
- 列表结果缓存命中时按卡片主键回读权威 `issue.status`，漂移即重算（写后即读，写路径零接入）。
- 详情 `task_status` 统一由内部主键回读权威行派生，并新增 `status` 同值别名；内嵌 pipeline
  业务状态与顶层 `task_status` 同源。
- `_task_status_map` 支持按目标 Issue 收窄 + 同号行按 `repo_keys` 顺序首个命中（确定性）。
- 回归测试：缓存预热后置位 → 立即读列表为 `pending_review`、连续多次读不抖动；任意写路径
  （PUT 状态）即时可见；list/detail/status get 口径一致。

### Out of Scope
- 状态机流转语义变更（审核点审批逻辑本身）。
- 其它接口的缓存策略改造、读模型全量重建。
- grape-skills（qa-cli runbook）侧改动。

## Acceptance Criteria
- [x] 置位成功后立即 `issues list` 返回 `pending_review`，连续多次无 `active` 抖动。
- [x] `issues detail.task_status` 与 `issues status get.status` 对同一 Issue 读数一致
      （并新增 `detail.status` 同值别名）。
- [x] 调用方无需轮询/重试即可确认置位生效；其它状态写路径同样即时可见。
- [x] 新增回归测试覆盖写后即读与口径一致；相关测试无回归（既有基线失败除外）。

## Risks & Mitigations
| Risk | Impact | Mitigation |
|------|--------|------------|
| 缓存命中多一次主键回读 | Low | `WHERE id IN (...)`（主键索引），只取 2 列、按页大小分批，O(页大小) |
| 回读异常导致返回过期数据 | High | fail-closed：核对失败按缓存过期处理、重算 |
| 详情改口径影响既有消费方 | Med | 旧字段 `task_status` 语义不变，仅新增 `status` 别名；pipeline 同步同源 |
| 同号多行（镜像仓库）歧义 | Med | 详情优先按内部主键；map 回退按 `repo_keys` 顺序首个命中（确定性） |