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

## Affected Modules（QA 复审同步，路径以 canonical 为准）
- `src/domains/runtime/routes/issues.py` — 列表缓存命中核对（短 TTL 状态快照 + 写代校验）、
  `_task_status_map` / `_issue_stage_map` 确定性排序、`issues status get` 共用 status 归一 accessor、
  detail 与独立 `/pipeline` 内嵌业务状态按内部主键回读。
- `src/domains/runtime/issues_center.py` — 新增 `issue_status_text`（共用 status 归一 accessor）、
  `read_issue_row_by_id`（单一主键回读 accessor，返回 `(found, stage, status)`；
  `_read_issue_status_by_id` / `read_issue_business_status_by_id` 为薄包装）。
- `src/kernel/mysql_compat.py` — 新增 `MySQLCompatDB.issue_status_write_generation`（issue.status
  专用写代，只统计影响 issue 行 status 的 INSERT/REPLACE/DELETE 与 SET status 的 UPDATE），
  `total_changes` 保留为通用连接写计数。
- （旧路径 `src/api/issues_center.py` 为兼容别名，指向 canonical `src/domains/runtime/issues_center.py`。）

## Risks & Mitigations
| Risk | Impact | Mitigation |
|------|--------|------------|
| 缓存命中多一次主键回读 | Low | `WHERE id IN (...)`（主键索引），只取 2 列、按页大小分批，O(页大小) |
| 回读异常导致返回过期数据 | High | fail-closed：核对失败按缓存过期处理、重算 |
| 详情改口径影响既有消费方 | Med | 旧字段 `task_status` 语义不变，仅新增 `status` 别名；pipeline 同步同源 |
| 同号多行（镜像仓库）歧义 | Med | 详情优先按内部主键；map 回退按 `repo_keys` 顺序首个命中（确定性） |

## QA 复审修复记录（PR #2046 两轮审查低危项）
- Standards-1：缓存命中核对改「短 TTL(1s) 状态快照 + 连接写代校验（`db.total_changes`）」——
  并发命中复用同一快照不再逐请求抢全局 db_lock；本进程任何写入改变写代 → 下次核对整窗刷新，
  read-your-writes 不因 TTL 削弱；跨进程/外部写入由 1s TTL 兜底。
- Standards-2：`_task_status_map` 出口统一 `or ''` 归一（status 列可空），缓存与回读恒可比。
- Standards-3：`_task_status_map` / `_issue_stage_map` 补 `ORDER BY CASE repo_key`（repo_keys
  优先级 + id）——docstring「首个命中」与实际取行一致，`setdefault` 不再取任意行。
- Standards-4：`issues status get` 与 `_read_issue_status_by_id` 共用 `issue_status_text` accessor。
- Spec-1：detail 内嵌 pipeline 与独立 `/pipeline` 的 `issue_stage`/`issue_task_status` 同步按
  内部主键回读（`read_issue_business_status_by_id`），与顶层钉同一权威行。
- Spec-2：补 review-pass / review-reject / confirm-done 写后即读、fail-closed（回读异常重算）、
  行删除失效、快照合并/写代刷新用例。
- Spec-3：Affected Modules 修正为 canonical `src/domains/runtime/issues_center.py`。

## QA 复审修复记录（PR #2046 round 2 @ d6019e502 → 本轮）
- Standards 阻断（写代来源错配）：新增 `_runtime_status_write_gen()`——写代必须与 `_rt_db`
  读连接同源。三库拓扑（mgmt 进程 + `uses_own_dsn(runtime)`）下 `_rt_db.cursor()` 每次取新
  runtime 连接，进程主库 `total_changes` 不计其写入 → 返回 None、禁用状态快照复用（每次回读），
  「首次读回即见」不再依赖 1s TTL；读走进程主库时用 issue.status 专用写代，否则回退 `total_changes`。
- Standards 判断项（削峰退化）：MySQL 兼容层新增 issue.status 专用写代，events/agent_tasks
  等无关高频写不再让快照失效。
- Standards 判断项（列表出口归一）：`_query_tasks` 出口 `status` 改用 `issue_status_text`，
  与 detail/status get/快照核对同口径；快照值同样归一。
- Standards 判断项（issue_state 兜底排序）：`_issue_stage_map` 的 issue_state 兜底查询补同一
  `ORDER BY CASE repo_key ... , id ASC`，过渡期同号多行取行确定。
- Spec 低危（空权威 status）：`read_issue_business_status_by_id` 改为返回 `(found, stage, status)`，
  `_read_issue_status_by_id` 无行/异常返回 None（区分「无行」与「权威值为空」）；detail 内嵌
  pipeline 与独立 `/pipeline` 在权威行存在时无条件按权威 status 覆盖（含空串），不再透出镜像行值。
- Spec 测试补强：新增三库 own-DSN 拓扑用例（写代不可同源 → 每次回读、写后即见）与
  空权威 status 不被镜像行泄露用例。
## QA 复审修复记录（PR #2046 round 3 @ 21b575141 → 本轮）
- Standards 阻断（schema 模式写代永不推进）：`_is_issue_status_write` 不再直接匹配裸
  `issue`，改为取目标表标识后归一（剥离 schema 限定 / 反引号 / `rt_|mgmt_|ins_` 前缀）再
  精确比较；`GRAPE_SCHEMA_MODE=1` 下 `grape_runtime.issue` / `rt_issue` 的 UPDATE/INSERT/
  DELETE 均正确推进 `issue_status_write_generation`。
- Standards 判断项（专用写代缺 MySQL 侧用例）：新增 `tests/kernel/test_mysql_compat.py::
  TestIssueStatusWriteGeneration`，覆盖 MySQL schema 限定名与 SQLite 前缀名下的写代推进，
  以及无关表/非 status 写不推进。
- Standards 判断项（主键回读重复 helper）：合并为单一 `read_issue_row_by_id`，两个旧入口
  改为薄包装（不再各自维护一份 SQL/失败契约）。
- Spec 测试说服力：快照用例改用可控的专用写代替身，验证「无关表写使 `total_changes` 变化
  但专用写代不变时仍复用快照」与「issue 写推进专用写代后整窗刷新」；另补专用写代优先于
  `total_changes` 的选择逻辑用例。
