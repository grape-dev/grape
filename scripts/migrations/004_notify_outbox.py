"""迁移 004_notify_outbox（Issue #2261 B1）—— grape_t_channel_outbound_card 补列，跨进程投递。

背景：IM 主动通知（任务分配/待澄清/待审核/完成/失败）原先 fire-and-forget：
_engine() 写死 src.api.main.im_engine，三进程拆分后恒 None → engine_down 静默丢通知。

本迁移给既有表 `grape_t_channel_outbound_card`（原钉钉卡片回调反查台账）**幂等补列**，
将其扩展为通知 outbox（新库由 001 直接建全）：

    payload         JSON（kind/text/repo_key/issue_number/conv_id/user_id/channel_type/installation_id/chat_id）
    status          pending|sending|sent|failed|expired（默认 pending）
    attempt_count   已尝试次数（默认 0）
    next_retry_at   下次可投递时间（UTC 字符串，与 bot_lease 同口径）
    expires_at      过期时间（入队 +10min，UTC 字符串）
    last_error      最近一次失败原因（no_binding/engine_down/offline/send_failed/…）
    updated_at      行更新时间

幂等：列存在则跳过（SQLite PRAGMA table_info / MySQL information_schema）。
可重复执行；DDL 不写 `--` 注释（MySQLCompatDB 按 `;` 切句，注释会破坏方言翻译）。

用法：

    cd <repo-root>
    .venv/bin/python scripts/migrations/004_notify_outbox.py --dry-run
    .venv/bin/python scripts/migrations/004_notify_outbox.py
"""
from __future__ import annotations

import logging
import os
import sys

_log = logging.getLogger(__name__)

_MIGRATION_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_MIGRATION_DIR))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_MIG_001_PATH = os.path.join(_MIGRATION_DIR, "001_channel_tables.py")

# (列名, DDL 片段)。列级定义与 001 新库 DDL 保持一致（勿漂移）。
OUTBOX_COLUMNS: tuple = (
    ("payload", "TEXT NOT NULL DEFAULT '{}'"),
    ("status", "VARCHAR(32) NOT NULL DEFAULT 'pending' "
     "CHECK (status IN ('pending','sending','sent','failed','expired'))"),
    ("attempt_count", "INTEGER NOT NULL DEFAULT 0"),
    ("next_retry_at", "VARCHAR(32) NOT NULL DEFAULT ''"),
    ("expires_at", "VARCHAR(32) NOT NULL DEFAULT ''"),
    ("last_error", "TEXT NOT NULL DEFAULT ''"),
    ("updated_at", "TEXT NOT NULL DEFAULT (datetime('now'))"),
)

_TABLE = "grape_t_channel_outbound_card"
_OUTBOX_STATUS_IDX = "idx_outbound_card_status_retry"


def _ensure_status_index(conn) -> None:
    """(status, next_retry_at) 复合索引（消费者轮询扫描路径；幂等）。

    SQLite：CREATE INDEX IF NOT EXISTS；MySQL：information_schema 探测后直发
    （MySQL 不支持 IF NOT EXISTS——与 001 的 _ensure_legacy_unique_index 同款处理）。
    建索引失败记 warning 不阻断启动（可重跑），不再静默吞错（QA #1314 S6）。
    """
    from src.kernel.db_health import is_sqlite_conn  # 自愈代理接入后 SQLite 判别统一走它
    if is_sqlite_conn(conn):
        try:
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS {_OUTBOX_STATUS_IDX} "
                f"ON {_TABLE} (status, next_retry_at)")
            conn.commit()
        except Exception as e:
            _log.warning("[004] ensure status index failed (sqlite): %s", e)
        return
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM information_schema.statistics "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=? "
            f"AND INDEX_NAME='{_OUTBOX_STATUS_IDX}'", (_TABLE,)).fetchone()
        if row and row[0]:
            return
        conn.execute(
            f"CREATE INDEX {_OUTBOX_STATUS_IDX} ON {_TABLE} (status, next_retry_at)")
        conn.commit()
    except Exception as e:
        _log.warning("[004] ensure status index failed (mysql): %s", e)


def _load_001():
    """跑 001 先保证表存在（老库：表由 001 建；新库：列由 001 一次建全）。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("migrations_001", _MIG_001_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load migration module: {_MIG_001_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("migrations_001", module)
    spec.loader.exec_module(module)
    return module


def _columns(conn, table: str) -> list:
    """列名列表（sqlite PRAGMA / MySQL 走 information_schema 探测）。"""
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    except Exception:
        pass
    try:
        rows = conn.execute(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=?", (table,)
        ).fetchall()
        return [r[0] for r in rows]
    except Exception:
        return []


def ensure_notify_outbox_columns(conn) -> list:
    """幂等补列；返回本次新增的列名列表（已全部存在时为空）。

    conn 支持 sqlite3.Connection 与 mysql_compat 的共享连接代理。
    补列失败：完整 DDL 失败 → log.warning 后退化为裸列（不带 CHECK/DEFAULT，
    MySQL 老版本/翻译层限制）；裸列仍失败 → raise（列缺失会让 outbox 带病运行，
    迁移可重跑，静默通过比失败更危险——QA #1314 S6）。
    """
    _load_001().ensure_channel_tables(conn)   # 表不存在先建（幂等）
    existing = _columns(conn, _TABLE)
    added: list = []
    for name, ddl in OUTBOX_COLUMNS:
        if name in existing:
            continue
        # MySQL 8.0.13 前 TEXT 列不允许 DEFAULT 字面量：mysql_compat 翻译层会把
        # TEXT 默认值剥掉，这里直接发原句，翻译失败时退化为无默认值补列。
        try:
            conn.execute(f"ALTER TABLE {_TABLE} ADD COLUMN {name} {ddl}")
        except Exception as e:
            _log.warning("[004] add column %s with full DDL failed (%s); "
                         "retrying as bare TEXT", name, e)
            # 二次尝试：不带 CHECK/DEFAULT 的裸列（MySQL 老版本/翻译层限制）
            conn.execute(f"ALTER TABLE {_TABLE} ADD COLUMN {name} TEXT")
        added.append(name)
    _ensure_status_index(conn)
    conn.commit()
    return added


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="add notify outbox columns (idempotent)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from src.domains.runtime.im.engine_wiring import _connect, default_db_path, _is_mysql_backend
    backend = "mysql" if _is_mysql_backend() else "sqlite"
    conn = _connect(default_db_path())
    cols_before = _columns(conn, _TABLE)
    missing = [c for c, _ in OUTBOX_COLUMNS if c not in cols_before]
    if args.dry_run:
        print(f"backend={backend} dry_run=True table={_TABLE}")
        for c in missing:
            print(f"  would_add {c}")
        print("done (no changes applied)")
        conn.close()
        return 0
    added = ensure_notify_outbox_columns(conn)
    conn.close()
    print(f"backend={backend} table={_TABLE} added={added or 'nothing (idempotent)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
