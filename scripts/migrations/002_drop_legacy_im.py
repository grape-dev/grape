"""A2 收尾 Phase B：旧 IM 列 / 旧表一次性下线（先回填，再 DROP）。

背景：新引擎的凭证真相源是 `grape_t_channel_installation`（M1~M3 + Phase A 已把读写路径
全部迁过去）。这里把最后剩下的 legacy 结构删掉：

    chat_agent_session.im_app_id / im_app_secret / im_provider
    devops_agent_session.im_app_id / im_app_secret / im_provider
    global_im_bot_config（整表；M3 起租约已迁到 grape_t_channel_lease）

顺序（**幂等**：列/表存在才动，重跑是 no-op）：

    1) 若 legacy 列还在 → 先跑 001 的 backfill_installations（读旧列补建安装行），
       老库（M1 之前）直接升级也不会掉线；
    2) 校验：回填后安装表非空（active 安装数 > 0）；生产确认可用 --dry-run 先看；
    3) ALTER TABLE ... DROP COLUMN ×3（两张表各三列）；
    4) DROP TABLE IF EXISTS global_im_bot_config。

用法：

    cd <repo-root>
    .venv/bin/python scripts/migrations/002_drop_legacy_im.py --dry-run   # 只看计划
    .venv/bin/python scripts/migrations/002_drop_legacy_im.py             # 真执行

注意（已踩过的坑）：
  - DDL 里不写 `--` 注释（MySQLCompatDB.executescript 按 `;` 切句会让修正失效）；
  - MySQL 的 DROP COLUMN 与 SQLite 语法一致，但方言翻译层不处理 DDL，故一条一句。
"""
from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys

_MIGRATION_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_MIGRATION_DIR))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)      # 允许 `python scripts/migrations/002_...py` 直接跑
_MIG_001_PATH = os.path.join(_MIGRATION_DIR, "001_channel_tables.py")

LEGACY_COLUMNS = ("im_app_id", "im_app_secret", "im_provider")
LEGACY_TABLES = ("chat_agent_session", "devops_agent_session")
DROP_TABLE = "global_im_bot_config"


def _load_001():
    spec = importlib.util.spec_from_file_location("migrations_001", _MIG_001_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("migrations_001", module)
    spec.loader.exec_module(module)
    return module


def _columns(conn, table: str) -> list:
    """列名列表（sqlite PRAGMA / MySQL 走 mysql_compat 的 information_schema 翻译）。"""
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    except Exception:
        return []


def _table_exists(conn, table: str) -> bool:
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        return bool(row)
    except Exception:
        return False


def plan(conn) -> dict:
    """只读盘点：哪些列/表还在，需不需要回填。"""
    pending_cols = {}
    for table in LEGACY_TABLES:
        cols = _columns(conn, table)
        pending_cols[table] = [c for c in LEGACY_COLUMNS if c in cols]
    return {
        "legacy_columns": pending_cols,
        "legacy_table": _table_exists(conn, DROP_TABLE),
        "needs_backfill": any(pending_cols.values()),
        "nothing_to_do": not any(pending_cols.values()) and not _table_exists(conn, DROP_TABLE),
    }


def _legacy_rows(conn) -> dict:
    """legacy 侧非空行数（回填后用于对账展示）。"""
    out = {}
    for table in LEGACY_TABLES:
        cols = [c for c in LEGACY_COLUMNS if c in _columns(conn, table)]
        if not cols:
            out[table] = 0
            continue
        where = " OR ".join(f"COALESCE({c},'')<>''" for c in cols)
        try:
            out[table] = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0]
        except Exception:
            out[table] = 0
    return out


def _installations_count(conn) -> dict:
    try:
        active = conn.execute(
            "SELECT COUNT(*) FROM grape_t_channel_installation WHERE status='active'"
        ).fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM grape_t_channel_installation").fetchone()[0]
        return {"active": int(active), "total": int(total)}
    except Exception:
        return {"active": 0, "total": 0}


def run(conn, dry_run: bool = False) -> dict:
    """执行（或演练）整个下线流程；返回各步结果。"""
    state = plan(conn)
    report = {"before": state, "backfill": None, "dropped_columns": [], "dropped_table": False,
              "after": None, "dry_run": dry_run}

    if state["nothing_to_do"]:
        report["after"] = plan(conn)
        report["note"] = "legacy 列/表已不存在，无需操作（幂等重跑）"
        return report

    if dry_run:
        # 演练：不回填、不动结构，只报告"如果执行会做什么"
        report["legacy_rows"] = _legacy_rows(conn) if state["needs_backfill"] else {}
        report["installations"] = _installations_count(conn)
        report["after"] = state
        return report

    # 1) 老库升级路径：先回填，再删源
    if state["needs_backfill"]:
        migrations = _load_001()
        migrations.ensure_channel_tables(conn)
        stats = migrations.backfill_installations(conn)
        conn.commit()
        report["backfill"] = {"user": stats.get("user", 0), "repo": stats.get("repo", 0)}
        report["legacy_rows"] = _legacy_rows(conn)
        report["installations"] = _installations_count(conn)
        if report["installations"]["active"] == 0:
            raise RuntimeError(
                "回填后 grape_t_channel_installation 仍无 active 安装，拒绝 DROP（先查 legacy 凭证是否为空）"
            )

    # 2) 删列（一表一列一句；表不存在 / 列不存在则跳过）
    for table, cols in state["legacy_columns"].items():
        for col in cols:
            try:
                conn.execute(f"ALTER TABLE {table} DROP COLUMN {col}")
                conn.commit()
                report["dropped_columns"].append(f"{table}.{col}")
            except Exception as e:
                report.setdefault("drop_errors", []).append(f"{table}.{col}: {e}")

    # 3) 删表
    if state["legacy_table"]:
        try:
            conn.execute(f"DROP TABLE IF EXISTS {DROP_TABLE}")
            conn.commit()
            report["dropped_table"] = True
        except Exception as e:
            report.setdefault("drop_errors", []).append(f"{DROP_TABLE}: {e}")

    report["after"] = plan(conn)
    return report


if __name__ == "__main__":  # 运维手动执行
    from src.session_manager import db, db_lock

    _dry = "--dry-run" in sys.argv
    with db_lock:
        result = run(db, dry_run=_dry)
    print("=" * 60)
    print(f"A2 Phase B legacy IM drop {'(DRY-RUN)' if _dry else ''}")
    print("  before :", result["before"])
    if result.get("backfill") is not None:
        print("  backfill:", result["backfill"], "| legacy rows:", result.get("legacy_rows"),
              "| installations:", result.get("installations"))
    print("  dropped cols:", result["dropped_columns"])
    print("  dropped table:", result["dropped_table"])
    print("  after  :", result["after"])
    if result.get("drop_errors"):
        print("  errors :", result["drop_errors"])
    print("=" * 60)
    sys.exit(0 if not result.get("drop_errors") else 1)