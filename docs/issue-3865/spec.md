# Spec: Issue #3865 — runtime 侧按域读取 issue/events，A2A 门禁显式降级

> 基线 `arch-weekly-11-ljt`（`c1820641c`）| 来源：Issue 正文 + 评论中的 Spec（Issue Agent）

## Objective
runtime 侧对 runtime 域表（`issue` / `events`）的读取一律走域连接原语，不再直连
`src.kernel.session_manager.db`（进程主库 = mgmt）；A2A 去重/取消两条前置门禁从
「异常即静默 fail-open」改为「显式降级标记 + 结构化告警」；org-context 快照读取失败
时在任务 meta 上打 `context_degraded=org_snapshot`。

## 问题
三库拓扑下 `grape_prod_mgmt` 只有 mgmt 域表，上述读取必然 1146：
- org-context 快照读不到 → `.org-context/`（AGENTS.md / 规范 / CODEOWNERS）静默不进任务知识；
- 去重门禁异常 → `return 0` 视为「无活跃事件」，重复派发不被拦截；
- 取消门禁异常 → 继续派发，已取消任务可能再次执行；
- 跨仓 issue 存在性/来源映射查询同样 1146。

## Scope
### In
1. `service.py` `collect_knowledge_files()` 第 4 步 org-context 快照读取 →
   `db_registry.cursor_for("runtime")`（域连接 / 单库回落主库 / 缺 DSN 抛 `DomainUnavailable`），
   上下文管理器保证游标关闭；失败写 `meta["context_degraded"] = "org_snapshot"`。
2. `webhook.py` `_has_active_a2a_event` / `_active_a2a_event_seqs` / `_queued_a2a_events`
   → 域读；异常保留 fail-open 派发语义，但返回降级标记 `dedup_unavailable`。
3. `webhook.py` 取消态前置检查抽为 `_a2a_issue_cancelled()` → 域读；异常标记
   `cancel_check_unavailable`。
4. `webhook.py` `_issue_exists_in_db` / `_resolve_cross_repo_issue_number` 的 issue 读取 → 域读。
5. 降级标记在响应 `detail` 中可见（跳过/排队/派发/幂等命中各分支）。
6. 三库 + 单库两套拓扑回归用例。

### Out
- 不改表结构、不做迁移；不动 `context_key` 去重语义。
- 不批量重构 webhook 其余 `session_manager` 导入点（同病另开 Issue）。
- 不动 mgmt 写路径（#3763 已处理）；不引入 `db_pair` 等新原语。

## 决策
| 决策 | 选择 | 理由 |
|---|---|---|
| 门禁异常语义 | 显式降级标记 + 告警（不 fail-closed） | 1146 改域读后消失；残留为瞬时故障，fail-closed 会在 DB 抖动时全量阻断 A2A |
| 读路径原语 | `db_registry.cursor_for("runtime")` | 全仓唯一路由判定；local 分支仍持 `db_lock`（单库零回归），domain 分支上下文管理器关闭游标 |
| 降级载体 | 响应 `detail` + 结构化 WARNING | 零表变更；调用方与运维都能看到 |

## Acceptance
- [ ] 三库拓扑下三条 Loki 查询各为 0（`collect org-context snapshot failed` /
      `A2A: active-event dedup check failed` / `A2A: cancelled check failed`）。
- [ ] 组织子任务 `.org-context/` 真实进入任务知识（单测断言），且读取走 runtime 域游标。
- [ ] org 读取失败 → `meta["context_degraded"] = "org_snapshot"`。
- [ ] 门禁异常 → 响应 `detail` 带 `dedup_unavailable` / `cancel_check_unavailable`。
- [ ] 单库拓扑行为零回归；`pytest` 全绿；无新增不可见吞异常点。