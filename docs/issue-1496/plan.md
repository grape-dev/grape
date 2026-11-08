"""Issue #1496 实现方案（status 唯一真源收口）。

见 docs/issue-1496/spec.md（需求/裁定/验收）。

切片（TDD 红/绿分提交）：
1. tests/test_agent_runtimes_health_convergence.py —— 红契约（已先行提交）；
2. src/api/db_setup.py —— 新轨迁移 202611080002：遗留 health 列检测 +
   按 derive_runtime_health 口径幂等回填（MySQL information_schema /
   SQLite PRAGMA 双方言，值级幂等：目标值已等不产生行变更）；
3. src/runtime/health.py —— apply_runtime_state(c, runtime_id, status,
   last_seen_at)：统一写入口，同一条 UPDATE 内写 status + last_seen_at
   （+保留列 health 的派生同步值），消除二次写窗口；
4. 写入点复用：runtime.py register（UPDATE 分支）/ heartbeat / probe 旧
   worker 兜底、service.heartbeat_consolidated / reap_stale_runtimes、
   user/db.py save_agent_runtimes 本机直连 upsert；
5. engine_select._health_ok —— 收敛到 parse_dt 派生（status=online 无心跳
   不再放行，与列表口径一致），注释显式裁定 health 不参与派发。

不采纳：health=online ⇒ 强抬 status=online（真失联引擎会被旧 health 值
反向"复活"，掩盖 recently_lost）。
