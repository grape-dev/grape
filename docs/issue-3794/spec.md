# Spec: Issue #3794 — purge_stale_event_guards 恒 1205 锁等待超时

> 承接 Issue Agent 已发布的 Spec（见 Issue 评论），本文件为开发侧落地基线。

## 目标
修复 `event_active_guard` 30min TTL 清理 100% 失败（InnoDB `1205 Lock wait timeout exceeded`，54 条/小时 × 4 台主控），恢复守卫自愈，解除对重跑/续办/转交派发的静默阻塞。

## 范围
- `src/domains/runtime/event_store.py::purge_stale_event_guards`：重写为两段短事务按 `seq` 分批清理（BATCH=200，最多 50 轮），候选窗口按 `seq` keyset 严格后移（受保护批不得终止整轮），DELETE 内重新 `NOT EXISTS` 复核防 TOCTOU。
- `src/api/db_setup.py`：**注册式迁移** `202611240009 add-agent-tasks-event-seq-status-index`（`kind=safe`，`domains=("runtime",)`）补 `idx_agent_tasks_event_seq_status(event_seq, status)`，同一迁移回收左前缀冗余索引 `idx_agent_tasks_event`（MySQL 显式 `status(191)` 并吞 1061 重复索引，SQLite 保持 `CREATE INDEX IF NOT EXISTS`；`_CompatCursor` 下 mysql_compat 的自动补前缀特判不生效，必须显式写）。
- 1205/1213 短退避（0.2s + 抖动）重试一次（重试前 `db.rollback()` 开新事务）；失败日志带 seq 集合；rowcount 指标 `grape_event_guard_purged_total{result}`。

## 关键校准
- `event_active_guard` 无 `id` 列（PK=repo_key,issue_id,agent_type），批处理键用 `seq`。
- `agent_tasks` 缺 `(event_seq, status)` 组合索引，`NOT EXISTS` 子查询逐行扫热表。
- `src/api/event_store.py` 是 `sys.modules` 别名，canonical 模块为 `src/domains/runtime/event_store.py`。
- 清理调用链 `src/api/main.py` / `src/api/server_tasks.py` 300s 周期 `asyncio.to_thread` 调用，无需改动。
- **DDL 落点**：老轨 `_create_indexes()` 已冻结（存量库盖 legacy-stamp 202610300001 后整段不跑），补索引必须 `register_migration`。
- **版本号顺延（rebase 到最新 arch-weekly-11-ljt @ `03f9e51`）**：已发布最大号 `202611230012`（`tests/arch/migration_versions_baseline.json`），往后 `202611240001`（#4066）、`202611240002`（#3789）、`202611240003`（#3763）、`202611240004`（#3846 ensure-cloud-sandboxes-in-runtime）、`202611240005`（#3731 convert_fail_count）、`202611240006`（#3773 ACP 指标列）、`202611240007`（#3733 autopilot-task-dispatch-state）、`202611240008`（#3928 backend-capability-circuit-breaker-columns）在基线上均已被占用，本迁移取下一个空闲号 `202611240009`；不占用、不改动其它 Issue 的号（同号会被判「已执行」而永久跳过，`tests/arch/test_migration_version_integrity.py` R1 唯一性守卫可直接暴露同号）。

## 验收
- 过期且无运行中 task 的 guard 行被删（分批、单批 ≤200、>200 行删完）。
- 有运行中 task（`status IN ('dispatched','running')`）的 guard 行不删（§33 不回归）。
- 候选窗口被受保护行占满时仍继续清理后续可删行（200 受保护 + 5 可删 → 一次调用删 5 条，不留头阻塞）。
- 1205/1213 重试生效（首次抛出 → 重试成功照常删除）。
- 组合索引由注册式迁移创建，存量库启动即盖章；老轨 `_create_indexes()` 内不再出现该索引（track guard 拦截）。
- 指标 `grape_event_guard_purged_total` 可见；失败口径维持「WARNING 后返回已删数」。
- 新增 pytest 用例（seed 参照 `tests/runtime/test_event_active_guard.py` / `tests/test_takeover_event_guard.py`），既有测试通过。
