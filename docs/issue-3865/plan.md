# Plan: Issue #3865

## 方案概述
不新增原语：4 类读取点统一收口到 `src.kernel.db_registry.cursor_for("runtime")`
（内部 `route_of`：域连接 / 本域或单库回落主库 / 三库缺 `GRAPE_DSN_RUNTIME` 抛
`DomainUnavailable`）。门禁异常保持 fail-open 派发语义，但把「降级」写进响应 `detail`
与结构化 WARNING，使「查询失败」不再等价于「无活跃/未取消」。

## 实现步骤

### 1. `src/domains/runtime/engine/service.py` — org-context 快照
`collect_knowledge_files()` 第 4 步（当前 `1263-1278`）：
```python
    if issue_id and _inum:
        try:
            with cursor_for("runtime") as _c:
                _row = _c.execute(
                    "SELECT org_context_snapshot_id FROM issue "
                    "WHERE repo_key=? AND issue_number=?", (repo_key, _inum)).fetchone()
            ...
        except Exception as _e:
            if isinstance(_pim, dict):
                _pim["context_degraded"] = "org_snapshot"
            log.warning(f"org-context snapshot read degraded repo={repo_key} issue={issue_id} "
                        f"route={_route} pid=... host=...: {_e}")
```
不使用 `_rt_db`：local 回落分支没有 `db_lock`，`collect_knowledge_files` 会被线程池
（`warm_knowledge_cache`）并发调用，单库拓扑会退化成共享连接无锁访问；`cursor_for`
两条路由都安全（local 持锁、domain 独立连接由上下文管理器关闭）。

### 2. `src/domains/runtime/routes/webhook.py` — 门禁域读 + 降级标记
新增模块级夹具：
- `_a2a_gate_degraded(flag, ...)`：结构化 WARNING（repo/issue/agent/route/target_db/pid/host）+ 返回标记 dict；
- `_a2a_stamp_degraded(resp, degraded)`：把标记并入 `detail`（兼容 `dict` / `JSONResponse`）。

改造点：
| 函数 | 改动 |
|---|---|
| `_has_active_a2a_event(..., degraded=None)` | `cursor_for("runtime")` 取连接；异常 → 标记 `dedup_unavailable`，仍 `return 0` |
| `_active_a2a_event_seqs(..., degraded=None)` | 同上；异常 → `[]` + 标记 |
| `_queued_a2a_events(..., degraded=None)` | 同上（排队幂等/force 取代读同一张 `events`） |
| 取消检查（内联块 → `_a2a_issue_cancelled(repo_key, issue_number, degraded=None)`） | 域读；异常 → 标记 `cancel_check_unavailable`，返回 False（继续派发） |
| `_issue_exists_in_db` / `_resolve_cross_repo_issue_number` | 域读；异常保留原返回值，日志带 route/pid/host |

调用侧：`a2a_endpoint` 在 `try` 前建 `_a2a_degraded: dict = {}`，两条门禁与
`_active_a2a_event_seqs` 传入同一 sink；跳过/排队/幂等命中/派发响应统一经
`_a2a_stamp_degraded` 输出 `detail` 标记。`_cross_pipeline_handoff` 的去重调用同样带 sink，
经返回值 `_degraded` 透传给 `a2a_endpoint` 后再打标。

### 3. 测试 `tests/runtime/test_3865_runtime_domain_reads.py`
- 三库拓扑：`monkeypatch.setenv("GRAPE_DSN_RUNTIME", ...)` + `domain_db` 注入假连接
  （先例 `test_3350_runtime_domain_db_routing.py`）。
- A org 注入：`collect_knowledge_files` 命中 `.org-context/`，SQL 落在域游标且游标关闭，meta 无降级键。
- B org 失败：`meta["context_degraded"] == "org_snapshot"`，且不再出现旧日志字面量。
- C 单库回落：`GRAPE_DOMAIN_LIB=0` → 仍从进程库读并注入（零回归）。
- D 去重域读：`_has_active_a2a_event` 命中 seq、SQL 在域游标；异常 → `dedup_unavailable`。
- E `_active_a2a_event_seqs` 异常 → `[]` + 标记；`_a2a_issue_cancelled` 命中/降级两态。
- F `_issue_exists_in_db` 域读命中；异常返回 False 且日志结构化。
- G 端点级：选择性让「去重 / 取消」查询抛错（monkeypatch `cursor_for` 返回抛错代理游标），
  断言 `/api/a2a` 响应 `detail.dedup_unavailable` / `detail.cancel_check_unavailable` 为 True
  且事件仍正常创建（fail-open 语义保持）。
- H 静态守门（AST）：本 Issue 4 个读取点所在函数体内不再出现 `db.cursor()`。

## 测试 seam
公开 seam = `collect_knowledge_files` / A2A 门禁函数 / `POST /api/a2a` 响应体；
拓扑 seam 复用 `db_registry`（既有先例）。不测 `_DomainDbProxy` 内部实现。

## 风险与回归
- 单库日志：旧 WARNING 字面量被新文案替换（Loki 三条查询由「有」变 0，符合验收）。
- `meta` 新增键向后兼容（调用方按 dict 读取，缺省无键）。
- 去重/取消仍 fail-open，仅新增可见性；`force=true` / `queue=true` 旁路语义不变。