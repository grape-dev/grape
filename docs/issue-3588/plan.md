# 方案设计 — Issue #3588

## 根因
overlayfs copy-up 以「删除旧文件 + 新建同名文件」替换 WAL/SHM；进程内已打开的 sqlite 长连接仍持有指向已删除 inode 的 fd → `disk I/O error` 持续 500。新开短连接可恢复（故 bulk_read「偶发 200」），长连接永不恢复。

## 核心机制：`src/kernel/db_health.py`（新增）
- **失效判据（双条件）**：
  - 异常驱动：`sqlite3.OperationalError/DatabaseError` 消息含 `disk i/o error` / `database disk image is malformed` / `unable to open database file` / `file is not a database`；
  - 轻量周期校验（TTL 5s 一次 `os.stat`）：连接建立时记录 `${db}-wal`/`${db}-shm` 的 (存在性, inode)；任一已记录文件变为不存在或 inode 变化 → 失效（说明 fd 已指向已删除 inode）。
- **`SelfHealingSQLiteDB`（连接代理）**：持有 `factory`（重建连接：connect + PRAGMA 重放 + schema 包装）与当前底层连接；`execute/cursor/commit/rollback` 前过周期校验，捕获 stale 异常 → 关旧连接 → factory 重建 → 同语句在新连接上重试一次（cursor 包装器内同样处理）；其余属性 `__getattr__` 透传。因 446 处 `from session_manager import db` 均为**对象引用**传递，代理对象一经建立即全局生效。
- **游标包装 `_StaleAwareCursor`**：`execute` 遇 stale → 触发重建 → 在新连接上重开 cursor 重放同一 SQL/参数并返回自身，fetch 委托最新 cursor。
- **线程安全**：重建持 `threading.RLock`；重建期间其他线程排队（短暂重试窗口）。
- **状态与信号**：`db_health()` 返回 `db_status`（ok/stale/recovered）、`wal_valid`（bool）、`last_stale_at`、`reconnects`；结构化日志 `event=db_stale_detected` / `event=db_reconnect_ok`。

## 接入点
| 位置 | 改动 |
|------|------|
| `src/kernel/session_manager.py` | SQLite 分支改为 `SelfHealingSQLiteDB(_build_sqlite_db, _db_path)`；`_build_sqlite_db` 重放 connect + PRAGMA（journal_mode 经 `DEVOPS_DB_JOURNAL_MODE` 可配，默认 wal）+ SchemaSQLiteDB 包装 |
| `src/domains/runtime/im/engine_wiring.py` | SQLite 分支 `_connect` 返回 stale-aware 代理（factory=开新连接+busy_timeout），outbox 仓储函数零改动获得失效重连 |
| `src/domains/runtime/im/outbox.py` | 消费者 run 循环：连续失败按 1x/2x/4x poll_interval 退避，成功即复位；认领态在库持久化保证不重复投递（无需新增去重） |
| `src/kernel/bulk_read.py` | SQLite 路径：连接打开后先探测 `SELECT 1`，stale 则换新短连接重试一次再交付；`read_all/read_one` 增加 stale 重试 |
| `src/api/health_endpoints.py` | `/healthz` checks 增加 `db_status`/`wal_valid`（来自 db_health，best-effort 不改变终态判定） |
| `docs/DEPLOYMENT.zh.md` / `DEPLOYMENT.md`、`deploy/README.md` | overlayfs 容器部署建议：数据目录落持久卷（`DEVOPS_DB_PATH`）、可选 `DEVOPS_DB_JOURNAL_MODE` |

## 测试 seam
公开接口级测试（`tests/kernel/test_issue3588_db_health.py`）：
1. `SelfHealingSQLiteDB` 在 WAL/SHM 文件被替换（copy-up 模拟：`os.remove` + 新建同名文件）后，execute 能自动恢复（先红后绿的主路径）。
2. `db_health()` 报告失效与恢复。
3. `bulk_read.reader()` 在 WAL 被替换场景下成功返回。
4. outbox `claim_batch` + `_conn()` 在文件被替换后自动恢复。
5. journal_mode 配置项生效（默认不变）。
