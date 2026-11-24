# Issue #3773 方案与取证

## 1. HERMES_HOME 模式取证

| 证据 | 结论 |
|------|------|
| 告警全集落在 `host=ecs-grape-master-001, role=worker, unit=grape@worker`，其余三台为 0 | 单机现象，指向该 worker 的 `HERMES_HOME` |
| `src/domains/runtime/agents/hermes_acp.py`：overlay 不可用/不可写时 `iso_home = src_home` 且 `_shared_home_mode = True`，state.db 为该 home 下单一文件 | 共享回退模式下所有会话共用一个 `state.db`，与 942MB 单文件一致 |
| 同构 worker 实测：`~/.hermes/state.db` 756MB + `state.db-wal` 233MB，`sessions=6438`、`messages=147010` | 共享单文件模式在同一 worker 安装形态下可复现 |
| 同构 worker 实测：`~/.cache/hermes-acp-homes/<cwd_hash>/state.db` 319 个，最大 43MB、多数 ≤7MB | per-issue 隔离模式存在，但单库体积远低于阈值 |

**结论**：master-001 为**共享单文件**模式，清理作用于其 `HERMES_HOME/state.db`。实现同时兼容
per-issue 隔离模式：运行期 `check_state_db_size(iso_home)` 只清理当前会话库；运维脚本
`discover_state_dbs()` 扫描 `HERMES_HOME/state.db` 与一层 `HERMES_HOME/*/state.db`，覆盖隔离模式。

> 编码沙箱无法直接 SSH 到 master-001，取证链为「告警维度 + 引擎回退代码 + 同构 worker 实测样本」；
> 清理范围按模式自适应，两种模式下均安全。

## 2. 治理路线

就地清理（首选，非分片）：

1. `PRAGMA integrity_check`
2. SQLite backup API 备份到 `~/.cache/hermes-acp-backups/state.db.<ts>.bak`（保留 24h）
3. 事务内按保留窗口删除旧 session 行（`messages` / `session_model_usage` / `compression_locks` /
   `async_delegations` 同步删除），始终保留最近 N 个 session
4. `PRAGMA wal_checkpoint(TRUNCATE)` + `VACUUM`
5. 再次 `integrity_check`；失败自动用备份回滚

清理后仍超阈值：归档备份并新建空库，保留 24h 可回滚；随后任务走 `session/new`。
清理成功则保留 session_id 走 `session/resume`，不再无条件放弃上下文。

默认参数：阈值 50MB、低水位 40MB、保留 7 天、最少保留最近 10 个 session、每机每小时最多清理一次。
环境变量：`ACP_STATE_DB_MAX_MB` / `ACP_STATE_DB_LOW_WATER_MB` / `ACP_STATE_DB_RETENTION_DAYS` /
`ACP_STATE_DB_MIN_SESSIONS` / `ACP_STATE_DB_CLEANUP_MIN_INTERVAL_S` / `ACP_STATE_DB_BACKUP_DIR`。

清理在拉起 hermes 子进程前执行，避免 VACUUM 与运行中的引擎抢 SQLite 写锁；文件锁保证跨进程单实例。

## 3. 可观测

worker 心跳新增 `acp_state_db_bytes` / `acp_state_db_cleanup_counts` /
`acp_state_db_bytes_freed_total` / `acp_resume_skipped_total`，落 `runtime_credentials`。
`/metrics` 采样暴露：

- `grape_acp_state_db_bytes{host}`
- `grape_acp_state_db_cleanup_total{host,result}`
- `grape_acp_state_db_bytes_freed_total{host}`
- `grape_acp_resume_skipped_total{host}`

`deploy/alerts.yml` 新增 >50MB/10min、>100MB/5min、resume 跳过速率 >0/15min 三条规则。
跳过 resume 同时打 `ACP_RESUME_SKIPPED reason=... db=... bytes=...` 结构化日志。

## 4. 运维脚本

`scripts/ops/hermes_acp_state_db_cleanup.py`：支持 `--db-path` / `--hermes-home` /
`--iso-homes-root` / `--backup-dir` / `--retention-days` / `--min-sessions` / `--max-mb` /
`--low-water-mb` / `--dry-run` / `--all` / `--no-archive`；幂等（未超阈值直接跳过）、
执行前备份、执行后完整性校验、失败回滚并以非零退出码报告。

## 5. 测试 seam

- `state_db_maintenance.prune_and_vacuum` / `archive_and_recreate` / `check_state_db_size`：
  公开行为（前后体积、裁剪 session 数、备份、dry-run、节流、失败回滚、归档重建）。
- `context_compaction.check_state_db_size`：布尔兼容语义（`bool(result) == skip_resume`）。
- 运维脚本 `main(argv)`：dry-run 不改库、正式清理缩容、二次执行幂等。