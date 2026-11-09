# 巡检 API（A2A 链路巡检器）

> 巡检异常清单的只读查询与人工收敛。数据源为 `grape_t_patrol_findings` 表
> （docs/weekly-09/a2a-chain-patrol-design.md）。巡检参数运行真源=DB meta
> （patrol:interval_seconds / patrol:confirm_minutes / patrol:lookback_mode /
> patrol:compensate_enabled / patrol:compensate_blocklist / patrol:findings_retention_days（终态行 TTL 天数，默认 90，0=永不清理）， 每周期重读改即生效，
> 集群共享）；Phase 2 自动补偿默认开（决策变更 2026-11-02）。

## GET /api/patrol/findings — 巡检异常清单（operation_id=grape_t_patrol_findings_list）

> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）
> **归属过滤（S38）**：admin 全量可见（unowned 置顶；其余按 created_at 倒序，id 仅兜底）；非 admin 仅见 owner=自己的行，unowned 仅 admin。

| Path | `status`（可选，open/claiming/compensated/dismissed/false_positive/permanent_failure/needs_human）、`kind`（可选）、`limit`（默认 100，1-500） |
| 出参 | `{"status":"ok","total":n,"findings":[{id,kind,task_id,event_seq,repo_key,issue_id,owner,detail,suggestion,status,action_meta,created_at,updated_at}]}` |
| 返回 | 200 |
| CLI | **暂未实现**（grape-cli 仓跟进落地 `grape patrol findings/dismiss/scan`；本仓仅 API+文档侧） |

## POST /api/patrol/findings/{finding_id}/dismiss — 误报收敛（operation_id=grape_t_patrol_findings_dismiss）

> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）
> 人工核对判定误报的收敛入口（Phase 2 放开自动补偿的"误报=0 硬线"数据支撑）。
> 仅 open 状态可 dismiss；重复扫描靠唯一约束不重插、不重置已 dismissed 的行。
> **归属校验（S30）**：仅 finding 归属人（owner）或 admin 可收敛；unowned（owner=0）仅 admin。

| Path | `{finding_id}` uuid str（grape_t_patrol_findings.id，UUID 主键） |
| 入参 | `{"reason": "..."}`（可选） |
| 出参 | `{"status":"ok","id":n,"finding_status":"dismissed"}` |
| 返回 | 200；403 非归属人且非 admin；404 finding 不存在或已非 open |

## POST /api/patrol/scan — 手动触发一轮巡检（operation_id=patrol_scan_run）

> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）
> 运营介入面（R12/D22）：不等 5 分钟自动周期，立即跑一轮检测。**只做检测面**
> ——检测 + findings 落库（唯一约束与自动轮幂等汇合）+ 终态行保留清理（R11）；
> **不补偿、不抢主**（补偿仍走选主门控的自动循环，D3/D20）。同步扫描经
> asyncio.to_thread 卸载出事件循环（D23）。

| 入参 | 无（扫描档用运行真源 lookback_mode） |
| 出参 | `{"status":"ok","scan":{"broken":n,"zombie":n,"stalled":n,"inserted":n,"purged":n,"duration_ms":n}}` |
| 返回 | 200；409 一轮巡检已在执行中（single-flight）；500 扫描执行异常（收口不外溢） |
