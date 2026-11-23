# Plan: Issue #4022 — pending-review 置位后首读即见新状态（read-your-writes）

## 方案概述
根因是列表接口 5s 结果缓存无写后失效路径，置位写入后首次读回命中旧缓存。采用
「缓存命中时按卡片主键核对权威 status」的读侧一致性策略：每次缓存命中仅按主键回读本页
卡片的 `issue.status`（主键索引、2 列、分批），任一卡片漂移或行被删除即判定缓存过期并
重算——所有状态写路径（pending-review / review-pass/reject / PUT status / confirm-done /
cancel / resume / 后台结单等）无需逐个接入失效钩子即可写后即读；回读异常 fail-closed 重算。

详情读路径统一由内部主键回读权威 `issue.status`（与 `issues status get` 同一行、同一
语句形态），并新增 `status` 同值别名、内嵌 pipeline 业务状态同源，消除同一响应内的口径分裂。

## 改动点
| 模块 | 改动 |
|------|------|
| `src/domains/runtime/routes/issues.py` | 新增 `_issue_status_by_ids`（主键批量回读 status）、`_cached_list_is_current`（缓存命中核对）；`_cached_repo_list` 命中时核对、漂移/异常即重算；`_task_status_map` 支持 `numbers` 收窄 + `setdefault` 确定性；`_assemble_issue` 按内部主键取 `task_status` 并新增 `status` 别名；详情改用收窄 status map 且 pipeline 业务状态与顶层同源 |
| `src/domains/runtime/issues_center.py` | 新增 `_read_issue_status_by_id`（内部主键回读权威 status，供详情统一口径） |
| `tests/runtime/test_issue_4022_read_your_writes.py` | 新增回归：缓存预热后置位即读、连续多次不抖动、任意写路径即时可见、list/detail/status get 口径一致 |
| `docs/issue-4022/*` | Spec / Plan / Task |

## 关键决策
| 决策 | 选择 | 理由 |
|------|------|------|
| 读一致性策略 | 缓存命中按主键核对 status，漂移即重算 | 写路径 60+ 处状态写入无需逐个接入；核对成本 O(页大小)，不破缓存收益 |
| 核对失败 | fail-closed 重算 | 宁可多查一次也不返回可能过期的状态 |
| 详情 status 口径 | 内部主键回读 + `status` 别名 | 与 `issues status get` 同行同语句；新增字段兼容旧消费方 |
| 同号多行 map | `repo_keys` 顺序首个命中（`setdefault`） | 消除查询行序漂移导致的相反读数；主键路径已优先 |
| pipeline 业务状态 | 顶层 `task_status` 非空时同源覆盖 | 避免同一 detail 响应中两处状态相反 |

## 风险与缓解
- 缓存命中多一次查询：主键 IN、2 列、分批，分页默认 20 条，成本可忽略；分块上限 500。
- 详情新增字段：纯增量，`task_status` 语义不变；旧调用方不受影响。
- 语义边界：仅收敛「读可见性/口径」，不改状态机本身；回归用独立仓库避免缓存跨用例污染。