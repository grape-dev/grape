# Spec: Issue #1496 — agent_runtimes.status 与 health 不收敛（唯一真源收口）

> 来源：Issue #1496 Spec（Issue Agent 发布）+ 本仓只读代码核对 | 2026-11-08
> 开发基准分支：`weekly-10`（GRAPE_WORK_BRANCH 注入）

## Objective
`status` 是 `agent_runtimes` 在线态的**唯一真源**（`online`/`idle`/`offline`，派发判据 = `status` + `last_seen_at` 新鲜度）；`health` 仅允许作为 `derive_runtime_health(status, last_seen_at)` 的**派生展示值**，不参与派发。消除库内遗留 `health` 列与 `status` 的自相矛盾（`health='online' AND status<>'online'` 存量 4 条：id 23259–23262），让引擎列表展示与派发可用性判定对同一引擎同结论。

## 现状核实（本仓只读核对）
- DDL `src/api/db_setup.py::_AGENT_RUNTIMES_DDL` 只有 `healthy INTEGER`，**没有 `health` 列**；`health` 是读时派生：`src/runtime/health.py::derive_runtime_health(status, last_seen_at)`。
- 库中出现的 `health` 列是**历史版本遗留列**（DDL 未声明、现读路径不消费）：旧写入方单独写 `health` 但不同步 `status` → 矛盾行；测试直读库即红。
- 列表侧（dashboard `_stats`/`/runtimes`、settings `_runtime_health`）已用派生值；派发侧 `engine_select._health_ok` 用 `status='online'` + `last_seen_at` 新鲜度——两侧本就同以 `status` 为判据，收口是把这一事实固化 + 掐掉增量分叉入口。

## 裁定（Spec 拍板）
1. **`status` 唯一真源**；不采纳「health=online ⇒ 强抬 status=online」（会把真失联引擎反向"复活"，掩盖 `recently_lost`）。
2. 遗留 `health` 列：**先幂等回填 + 登记待删除**；短期保留期间，凡写入 `health` 的路径必须与 `status` 同事务写入（由 `apply_runtime_state` 收口）。
3. 读路径只信 `derive_runtime_health` 派生值，**禁止直读 `health` 列**（注释 + review 约定）。

## 实现路径
1. **迁移**（`db_setup.py` 新轨 `register_migration`，版本 `202611080002`，避开已占号段）：
   - 检测遗留 `health` 列（MySQL: information_schema；SQLite: PRAGMA table_info），无列 no-op；
   - 有列则按 `derive_runtime_health` 同口径回填（Python 逐行计算，带 `last_seen_at` 新鲜度判断，防真失联引擎被旧 health 值复活）：
     - `status='online'` 且心跳新鲜 → `health='online'`；
     - `status='online'` 且无心跳/心跳过期 → `health='recently_lost'`；
     - 其余按 `status` 映射（`offline`/`about_to_gc` 按 last_seen 距离判定）；
   - 幂等：按目标值幂等（值已等不再 UPDATE）；重复执行零变更。
2. **写入收口**：`src/runtime/health.py::apply_runtime_state(c, runtime_id, status, last_seen_at)` —— 单条 UPDATE 同时写 `status` + `last_seen_at`（+保留列 `health` 的派生同步值），供 register/heartbeat/本机直连刷新/本地探活复活/心跳合并入口复用，消除二次写窗口。
3. **读路径**：`derive_runtime_health` 唯一派生入口（现状已满足）；`engine_select._health_ok` 注释显式声明「status 为唯一真源、health 不参与派发」。
4. **回归用例** `tests/test_agent_runtimes_health_convergence.py`：
   - 存量矛盾行回填后 `len(bad)==0`（**不弱化**，断言空集）；
   - 回填幂等（第二次执行零行变更）;
   - 派生值与 `status`/`last_seen_at` 一致（含 stale 边界）;
   - 写入口收敛：heartbeat 后库内无矛盾行；
   - 列表口径（`derive_runtime_health`）与派发口径（`_health_ok`）对同一 runtime 同结论。

## 受影响模块
| 模块 | 改动 |
|---|---|
| `src/api/db_setup.py` | 新增迁移 `202611080002 normalize_legacy_health_column` |
| `src/runtime/health.py` | 新增 `apply_runtime_state`；`derive_runtime_health` 补唯一真源注释 |
| `src/runtime/engine_select.py` | `_health_ok` docstring 显式裁定（行为不变） |
| `src/api/routes/runtime.py` | register UPDATE / heartbeat UPDATE / probe 旧 worker 兜底改走 `apply_runtime_state` |
| `src/api/main.py` | 本地探活复活路径改走 `apply_runtime_state` |
| `src/runtime/service.py` | `heartbeat_consolidated` / `reap_stale_runtimes` 改走 `apply_runtime_state` |
| `tests/test_agent_runtimes_health_convergence.py` | 新增（红→绿） |

## 验收（Acceptance）
- [x] 只读 SQL `SELECT id,status,health FROM agent_runtimes WHERE health='online' AND status<>'online'` 在回填后返回空集（用例断言 `len(bad)==0`，覆盖 23259–23262 同构矛盾行）。
- [x] 回填幂等：连续两次执行，第二次零变更（0 行 UPDATE）。
- [x] 心跳写入后 `status`/`last_seen_at`/`health` 同事务一致，无中间态（写入口收敛用例）。
- [x] 列表口径与派发口径同结论（同构用例锁定）。
- [x] 现有健康态测试全绿（`test_runtime_protocol.py::TestHealth` 等）。
- [ ] 双库（MySQL）验证：迁移内分支方言（information_schema / PRAGMA），SQLite 实测通过；MySQL 走同口径 information_schema 检查 + 参数化 UPDATE，由部署侧启动迁移段自动执行。

## 风险与对策
- 遗留 `health` 列仍有未知外部写入方 → 迁移只规范化不删列；登记待删除；写侧收口后增量不再分叉。
- 版本号撞号（#1412 教训）→ `202611080002`（全库 register_migration 现存最大 `202611060004` 之后，日期段顺延），`test_migration_track_guard` 唯一性校验兜底。
- 回填误复活失联引擎 → 回填值必须由 `status`+`last_seen_at` 派生，绝不信旧 `health` 值。
