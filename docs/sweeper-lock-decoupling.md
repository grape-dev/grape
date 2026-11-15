# sweeper 与 db_lock 解耦（方案 B）

## 问题

`sweeper`（后台清扫循环）原先和请求路径共用进程主连接 `session_manager.db`，
所有查询都包在进程级 `db_lock`（`threading.RLock`）里。批量扫描期间，同进程内任何
走 `db` 的请求都排队，表现不是"慢"，而是直接 socket 超时：

- 2026-11-15 `h116-grape` / `orgtest` 两个凭据的四个引擎心跳长期停在 137s；
- `seq=9762` 的 qa 任务跑完 742 条消息后 `grape tasks complete` 连续 7 次 504，
  最后只能被 sweeper 判失败。

心跳 / tags / claim 已经在 `src/domains/runtime/routes/runtime.py::_runtime_cursor`
里改成 runtime 域独立连接，sweeper 是最后一个还挂在 `db_lock` 上的批量路径。

## 设计

开关：`SWEEPER_LOCK_MODE=legacy|own`（默认 `legacy`，行为与改造前完全一致）。

| 维度 | legacy（默认） | own |
| --- | --- | --- |
| 连接 | 进程主连接 `db` | 每轮 `db_registry.new_connection("runtime")`，用完 close |
| 锁 | 进程级 `db_lock` | 不拿 `db_lock`；跨副本用 MySQL `GET_LOCK(name,0)` |
| 提交 | 显式 `db.commit()` | autocommit（`_commit()` 变 no-op，不会误提交主连接） |
| 回落 | — | 无自有 DSN（单库 / SQLite / 测试）时自动回落 legacy |

要点：

1. **判据**用 `db_registry.uses_own_dsn("runtime")`：SQLite 单写者不能被并发写打爆。
2. **跨副本互斥**用 `_ReplicaLock`（`GET_LOCK('grape:sweeper:events'|':tasks'|':merged', 0)`），
   非阻塞抢锁，抢不到就跳过本轮并记 `debug`；锁机制不可用时放行，不把清扫停掉。
3. **并发正确性**靠 CAS：`UPDATE ... WHERE ... AND status IN (...)` + `rowcount` 判定；
   顺带把两处无条件 `UPDATE events` 补上了 `AND status IN ('pending','running')`。
4. **跨域**：IM 三张表归 mgmt 域，own 模式改用 `_sweep_mgmt_cursor()`；其余表全归 runtime。
5. **可观测**：每轮 `sweeper round '<scope>' done in X.XXs, cleaned=N`，≥2s 记 warning；
   own 模式启动时打一条 `SWEEPER_LOCK_MODE=own` 说明行。

## 验证（stage，2026-11-15）

```
候选集合一致性   legacy {done_issues:4, active_sessions:1} / own 完全相同
锁足迹           legacy 单轮 db_lock 获取 3 次 / own 用"一 enter 就抛"的假锁仍跑完 → 0 次
连接身份         own session 57813 ≠ 进程主 session 3523705
跨副本互斥       持有中二次 GET_LOCK 返回 0（抢不到 → 跳过本轮）
实机日志         event: round 'events' 0.10s cleaned=0；worker: round 'tasks' 0.06s cleaned=0
业务热路径       /api/runtime/tags ×40：p50 0.003s / p95 0.004s / max 0.007s
静态检查         pytest tests/arch tests/scripts 89 passed；make lint 通过（async-blocking 265 < 基线 276）
```

说明：stage 表小（agent_tasks 557 行）、目录已清空，单轮仅 0.1s，因此"P99 差值"在这个
环境里测不出来；解耦结论由**锁足迹为 0 + 独立 MySQL 会话**两条结构性证据支撑。

## 回滚

```bash
# 1) 改回旧行为（不换代码）
sed -i 's/SWEEPER_LOCK_MODE=own/SWEEPER_LOCK_MODE=legacy/' \
  /etc/systemd/system/grape@.service.d/20-sweeper-lock.conf
systemctl daemon-reload && systemctl restart grape@event grape@worker
# 2) 或直接删 drop-in
rm /etc/systemd/system/grape@.service.d/20-sweeper-lock.conf && systemctl daemon-reload
```

## 已补：chat_conversations(status) 索引

三条落地路径（因为三库模式下进程**跳过 init_database**，注册迁移只在单库模式生效）：

| 场景 | 谁建 | 位置 |
| --- | --- | --- |
| 单库 / SQLite / 新装库 | 老轨建表段 | `db_setup._create_indexes()` |
| 单库存量库 | 注册迁移 `202611170009`（kind=safe） | `db_setup.register_migration` |
| 三库 MySQL | sweeper 首次取连接时幂等建一次 | `cleanup._ensure_chat_status_index()`（runtime 归属域连接） |

MySQL 侧 `status` 是 TEXT，建索引必须带前缀长度 —— 兼容层会自动补 `(191)`；
三库路径直接在归属域连接上写 `status(191)`。

**实测（stage 209 行，全部 status='active'）**：

```
SHOW INDEX → idx_chat_conv_status on status(191)
EXPLAIN WHERE status='active'  → type=ALL  key=NULL      （基数=1，优化器正确地不用索引）
EXPLAIN WHERE status='closed'  → type=ref  key=idx_chat_conv_status   （值有选择性时被选中）
```

结论：索引可用且已建；stage 因为全部会话都处于 active，优化器选全表扫是**正确行为**；
生产历史会话累积出 closed 之后该索引才会真正生效。

## 后续可做

- 把 `sweep_events_once` 内部的各阶段再细分耗时（当前只有轮级耗时）；
- 若将来多副本常态化，把 `GET_LOCK` 的 key 提到配置里，便于按副本角色分配。