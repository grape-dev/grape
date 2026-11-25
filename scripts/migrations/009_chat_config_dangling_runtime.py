"""迁移 009_chat_config_dangling_runtime（2026-11-25）——chat 配置悬空 runtime_id 清扫。

背景
----
引擎 revoke / 物理删除时 `agent_runtimes` 行被删，但 `chat_agent_session.runtime_id`
的历史存量没有同事务清理（Issue #4045）：读路径 `_chat_config_row_engine()` 命中悬空行
即打 `get_chat_config: runtime_id=N 不存在（引擎行已删除），按未配置处理`（生产 65 条/小时，
3 个已删 runtime：1080 / 1091 / 1202），并静默回落本机直连。

代码侧已补两条腿（本单同 PR）：
  * 删除侧：`runtime.public.purge_runtime_credential()` 同事务 `UPDATE chat_agent_session
    SET runtime_id=0 WHERE runtime_id IN (...)`（**保留 engine_uid**）；
  * 读路径：悬空行若 `engine_uid` 仍能解析到现存引擎，会按 uid 重解析并回写 runtime_id。

本脚本修存量：把指向已不存在引擎行的 `runtime_id` 置 0。

口径
----
* 只处理 `runtime_id > 0` 且 `NOT EXISTS(SELECT 1 FROM agent_runtimes WHERE id=runtime_id)` 的行；
* **保留 `engine_uid`**（issue 定稿：留下「原绑的是哪台引擎」的可审计/可重绑定线索，
  读路径据此可自动重绑定）；旧库缺 `engine_uid` 列时降级为不含该列的扫描/备份（输出标明）；
* 幂等：跑完第二次 dry-run 应为 0 行；逐行 `UPDATE ... WHERE id=? AND runtime_id=?` 防并发改行；
* 先整批备份到 `chat_agent_session_dangling_bak_<UTC>`（id/user_id/repo_key/runtime_id/engine_uid），再改。

三库拓扑（必读）
----------------
`chat_agent_session` / `agent_runtimes` 都在 **runtime 库**，脚本连的是 DEVOPS_DB_* 指的库；
只给 `GRAPE_DSN_*` 会回落本地 SQLite（连错库且不报错）。生产执行（占位符按环境替换）：

    DEVOPS_DB_BACKEND=mysql DEVOPS_DB_HOST=<mysql-host> DEVOPS_DB_PORT=3306 \\
    DEVOPS_DB_USER=<runtime-db-user> DEVOPS_DB_PASSWORD=<password> \\
    DEVOPS_DB_NAME=<runtime-db> \\
    .venv/bin/python scripts/migrations/009_chat_config_dangling_runtime.py [--apply]

用法
----
    python3 scripts/migrations/009_chat_config_dangling_runtime.py          # dry-run
    python3 scripts/migrations/009_chat_config_dangling_runtime.py --apply  # 备份+改写
"""
from __future__ import annotations

import argparse
import os
import sys
import time

_MIGRATION_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_MIGRATION_DIR))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

TABLE = "chat_agent_session"
BACKUP_PREFIX = "chat_agent_session_dangling_bak_"

_SCAN_TAIL = ("WHERE c.runtime_id>0 AND NOT EXISTS "
              "(SELECT 1 FROM agent_runtimes ar WHERE ar.id=c.runtime_id) ")


def has_uid_column(conn) -> bool:
    """chat_agent_session 是否有 engine_uid 列（未迁移旧库降级为不含该列）。"""
    try:
        from src.kernel.ddl_safety import table_columns

        return "engine_uid" in set(table_columns(conn.cursor(), TABLE))
    except Exception:  # noqa: BLE001  # 探列失败=按旧库降级处理（输出标明，不静默改语义）
        return False


def scan(conn) -> dict:
    """扫出悬空行（只读）。返回 {total, dangling, has_uid, rows, by_scope, ids}。

    rows: [(id, user_id, repo_key, runtime_id, engine_uid), ...]
    """
    has_uid = has_uid_column(conn)
    cols = "c.id, c.user_id, c.repo_key, c.runtime_id, c.engine_uid" if has_uid \
        else "c.id, c.user_id, c.repo_key, c.runtime_id"
    rows = conn.execute(f"SELECT {cols} FROM {TABLE} c {_SCAN_TAIL}ORDER BY c.runtime_id, c.id").fetchall() or []
    out = []
    by_scope: dict = {}
    for r in rows:
        uid_val = (r[4] or "") if has_uid else ""
        rec = (r[0], r[1], r[2], int(r[3] or 0), uid_val)
        out.append(rec)
        key = "repo_session" if (rec[2] or "") else "user_config"
        by_scope[key] = by_scope.get(key, 0) + 1
    total = int(conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0] or 0)
    return {"total": total, "dangling": len(out), "has_uid": has_uid,
            "rows": out, "by_scope": by_scope,
            "ids": sorted({r[3] for r in out}),
            "with_uid": sum(1 for r in out if r[4])}


def backup(conn, plan: dict) -> str:
    """悬空行整批备份（表名带 UTC 时间戳），返回备份表名。"""
    # 秒级时间戳 + 进程内纳秒后缀：同秒重复执行不会撞备份表名。
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    bak = f"{BACKUP_PREFIX}{ts}_{int(time.time_ns() % 1_000_000):06d}"
    conn.execute(
        f"CREATE TABLE {bak} (id VARCHAR(191) PRIMARY KEY, user_id VARCHAR(191), "
        f"repo_key VARCHAR(191), runtime_id INTEGER, engine_uid VARCHAR(36), "
        f"backed_up_at VARCHAR(32))")
    conn.commit()
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    n = 0
    for rid, user_id_val, repo_key, runtime_id, engine_uid in plan["rows"]:
        conn.execute(
            f"INSERT INTO {bak} (id, user_id, repo_key, runtime_id, engine_uid, backed_up_at) "
            f"VALUES (?,?,?,?,?,?)",
            (rid, str(user_id_val or ""), str(repo_key or ""), int(runtime_id),
             engine_uid or None, now))
        n += 1
    conn.commit()
    print(f"[009] 已备份 {n} 行 → {bak}")
    return bak


def apply_plan(conn, plan: dict) -> int:
    """把悬空行的 runtime_id 置 0（逐行带 runtime_id 守卫，保留 engine_uid）。"""
    changed = 0
    for rid, _uid, _repo_key, runtime_id, _engine_uid in plan["rows"]:
        cur = conn.execute(
            f"UPDATE {TABLE} SET runtime_id=0 WHERE id=? AND runtime_id=?", (rid, int(runtime_id)))
        changed += int(getattr(cur, "rowcount", 0) or 0)
    conn.commit()
    return changed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="2026-11-25：chat_agent_session 悬空 runtime_id 置 0（默认 dry-run）")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不改数据（默认）")
    parser.add_argument("--apply", action="store_true", help="备份后把悬空 runtime_id 置 0")
    args = parser.parse_args(argv)
    if args.apply and args.dry_run:
        print("[009] --dry-run 与 --apply 不能同时给", file=sys.stderr)
        return 2

    from src.kernel import session_manager

    conn = session_manager.db
    backend = os.environ.get("DEVOPS_DB_BACKEND", "").lower()
    target = (f"mysql {os.environ.get('DEVOPS_DB_HOST', '?')}:"
              f"{os.environ.get('DEVOPS_DB_PORT', '3306')}/"
              f"{os.environ.get('DEVOPS_DB_NAME', '?')}") if backend == "mysql" else \
             os.environ.get("DEVOPS_DB_PATH") or "<session default>"
    print(f"[009] 目标库: {target}")

    try:
        plan = scan(conn)
    except Exception as e:  # noqa: BLE001 - 表不存在/连错库必须显式失败，不静默 0 行
        print(f"[009] 无法读取 {TABLE}（疑似连错库或表不存在）：{e}", file=sys.stderr)
        print("[009] 三库拓扑请用 DEVOPS_DB_* 指向 runtime 库（见文件头「三库拓扑」）。",
              file=sys.stderr)
        return 2
    if plan["total"] == 0:
        print(f"[009] {TABLE} 为 0 行：疑似连错库（三库拓扑需连 runtime 库 DEVOPS_DB_*）。",
              file=sys.stderr)
        return 2
    print(f"[009] chat_agent_session 共 {plan['total']} 行；悬空 runtime_id {plan['dangling']} 行"
          f"（其中带 engine_uid 可重绑定 {plan['with_uid']} 行）；engine_uid 列="
          f"{'有' if plan['has_uid'] else '无（旧库降级）'}")
    print(f"[009] 悬空 runtime_id 清单: {plan['ids']}")
    print(f"[009] 分布: {plan['by_scope']}")
    for rid, uid_val, repo_key, runtime_id, engine_uid in plan["rows"][:10]:
        print(f"     {rid} user={uid_val} repo={repo_key or '-'} runtime_id={runtime_id} "
              f"engine_uid={engine_uid or '-'}")

    if not args.apply:
        print("[009] dry-run 结束（未改数据）。加 --apply 执行。")
        return 0
    if not plan["rows"]:
        print("[009] 无需变更")
        return 0

    backup(conn, plan)
    n = apply_plan(conn, plan)
    left = scan(conn)
    print(f"[009] 已置 0 {n} 行；剩余悬空 {left['dangling']} 行")
    if left["dangling"]:
        print(f"[009] 警告：仍有 {left['dangling']} 行悬空（多为并发新写入，重跑本脚本即可）",
              file=sys.stderr)
        return 1
    print("[009] 自检通过：悬空 runtime_id 归零")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())