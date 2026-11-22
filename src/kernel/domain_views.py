"""跨库视图的启动期自检与修复（每个进程只修自己库里的视图）。

为什么（2026-11-20 实锤）：非归属库里的同名表是 **VIEW**，它是「建视图时的列快照」。
基表加列（`managed_repos.is_org_repo`）后，任何仍在读视图的路径都是
`1054 Unknown column`，而视图同步只存在于 `scripts/domain_view_freshness.py`
这个**人工 CLI** 里 —— 没人跑就漂移，漂移就 500。

职责边界（不越权、不猜）：
- 只修 **本进程自己的库** 里的视图（视图落在目标库，重建权限也在目标库）；
- 只修「owner 基表存在且唯一」的视图；多份基表（ambiguous）/引用缺失（orphan）
  只告警，不重建 —— 那是数据归属问题，不能靠 DDL 瞎猜；
- 从不新建视图（只 CREATE OR REPLACE 已存在的），新增视图仍归 sync 脚本 + 白名单。
"""
from __future__ import annotations

import os
import re
import sys

IDENT_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
VIEW_REF_RE = re.compile(r"\b(?:from|join)\s+`([^`]+)`\.`([^`]+)`", re.I)


class ViewDDLError(ValueError):
    """库名/表名/列名非法，拒绝拼 DDL。"""


def check_ident(kind: str, value: str) -> str:
    if not value:
        raise ViewDDLError(f"{kind} 不得为空")
    if not IDENT_RE.match(value):
        raise ViewDDLError(f"{kind} 非法（只允许字母/数字/下划线）: {value!r}")
    return value


def build_view_ddl(owner_db: str, table: str, columns: list[str]) -> str:
    """`CREATE OR REPLACE VIEW <table> AS SELECT <owner_db>.<table>.<col> AS <col>, ...`

    调用方负责先切到目标库（本函数不写 USE），视图名不带库前缀。
    """
    check_ident("owner 库名", owner_db)
    check_ident("表名", table)
    if not columns:
        raise ViewDDLError(f"{table} 没有列信息，无法建视图")
    for c in columns:
        check_ident("列名", c)
    select_list = ", ".join(f"`{owner_db}`.`{table}`.`{c}` AS `{c}`" for c in columns)
    return (f"CREATE OR REPLACE VIEW `{table}` AS SELECT {select_list} "
            f"FROM `{owner_db}`.`{table}`")


def columns(conn, db: str, table: str) -> dict[str, str]:
    with conn.cursor() as c:
        c.execute(
            "SELECT COLUMN_NAME, COLUMN_TYPE FROM information_schema.columns "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION",
            (db, table))
        return {r[0]: r[1] for r in c.fetchall()}


def view_refs(conn, db: str, view: str) -> "list[tuple[str, str]] | None":
    """解析视图定义引用的全部 库.表（含 JOIN）；None = 定义不可见，调用方必须告警。"""
    with conn.cursor() as c:
        c.execute("SELECT VIEW_DEFINITION FROM information_schema.views "
                  "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s", (db, view))
        row = c.fetchone()
    if not row or not row[0]:
        return None
    return [(m.group(1), m.group(2)) for m in VIEW_REF_RE.finditer(row[0])]


def scan_own(conn, own_db: str, owner_dbs: "tuple[str, ...]") -> dict:
    """只看 own_db 里的视图，对照 owner_dbs 里的基表列集。

    stale       : [(view, table, owner, missing_cols, extra_cols)]
    type_drift  : [(view, table, owner, [(col, owner_type, view_type)])]
    ambiguous   : [(table, [owner_dbs])]      # 多份基表，归属不明 → 不修
    orphan      : [(view, owner_db, owner_tbl)]
    unreadable  : [view]                      # 无 SHOW VIEW 权限
    """
    with conn.cursor() as c:
        c.execute(
            "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE FROM information_schema.tables "
            "WHERE TABLE_SCHEMA IN (%s)" % ",".join(["%s"] * (len(owner_dbs) + 1)),
            (own_db,) + tuple(owner_dbs))
        rows = c.fetchall()
    types = {(db, tbl): typ for db, tbl, typ in rows}
    base_by_name: dict[str, list[str]] = {}
    for (db, tbl), typ in types.items():
        if typ == "BASE TABLE" and db != own_db:
            base_by_name.setdefault(tbl, []).append(db)

    stale, type_drift, ambiguous, orphan, unreadable = [], [], [], [], []
    own_views = sorted(tbl for (db, tbl), typ in types.items()
                       if db == own_db and typ == "VIEW")
    for view in own_views:
        owners = sorted(base_by_name.get(view, ()))
        if not owners:
            refs = view_refs(conn, own_db, view)
            if refs is None:
                unreadable.append(view)
                continue
            for owner_db, owner_tbl in refs:
                if types.get((owner_db, owner_tbl)) != "BASE TABLE":
                    orphan.append((view, owner_db, owner_tbl))
            continue
        if len(owners) > 1:
            ambiguous.append((view, owners))
            continue
        owner = owners[0]
        owner_cols = columns(conn, owner, view)
        view_cols = columns(conn, own_db, view)
        missing = [col for col in owner_cols if col not in view_cols]
        extra = [col for col in view_cols if col not in owner_cols]
        if missing or extra:
            stale.append((view, view, owner, missing, extra))
            continue
        drift = [(col, owner_cols[col], view_cols[col]) for col in owner_cols
                 if owner_cols[col] != view_cols[col]]
        if drift:
            type_drift.append((view, view, owner, drift))
    return {"stale": stale, "type_drift": type_drift, "ambiguous": ambiguous,
            "orphan": orphan, "unreadable": unreadable, "views": len(own_views)}


def repair_own(conn, own_db: str, scan_result: dict, dry_run: bool = False) -> list[str]:
    """重建本库里列集/类型不一致的视图，返回重建成功的视图名列表。"""
    check_ident("目标库名", own_db)
    fixed: list[str] = []
    targets = [(view, owner) for view, _t, owner, _m, _e in scan_result["stale"]]
    targets += [(view, owner) for view, _t, owner, _d in scan_result["type_drift"]]
    for view, owner in sorted(set(targets)):
        cols = list(columns(conn, owner, view))
        ddl = build_view_ddl(owner, view, cols)
        if dry_run:
            fixed.append(view)
            continue
        with conn.cursor() as c:
            c.execute(ddl)
        fixed.append(view)
    return fixed


def legacy_view_dbs() -> "tuple[str, ...]":
    """除本域外的其他域库（用于对照基表）。"""
    from src.kernel.domain_topology import domain_dbs, own_domain

    own = own_domain()
    return tuple(name for dom, name in sorted(domain_dbs().items()) if dom != own)


def reconcile_on_startup(dry_run: bool = False) -> dict:
    """启动期：扫本库视图 → 修列集/类型漂移 → 复扫；修不掉就是真问题。

    返回 {"skipped": bool, "reason": str, "scanned": int, "fixed": [..],
          "ambiguous": [...], "orphan": [...], "unreadable": [...],
          "stale_after": [...]}。
    严格模式（默认）下 stale_after 非空 → SystemExit(7)：宁可拒绝启动，
    也不要带着会 1054 的视图对外服务。`GRAPE_VIEW_AUTOSYNC=0` 只检查不修
    （排查用），`GRAPE_VIEW_STRICT=0` 只告警不退出。
    """
    from src.kernel.domain_topology import domain_dbs, multi_db_mode, own_db

    report = {"skipped": True, "reason": "", "scanned": 0, "fixed": [],
              "ambiguous": [], "orphan": [], "unreadable": [], "stale_after": []}
    if not multi_db_mode():
        report["reason"] = "single-db topology"
        return report
    db = own_db()
    owners = legacy_view_dbs()
    if not db or not owners:
        report["reason"] = "topology incomplete"
        return report

    conn = _connect_own()
    if conn is None:
        report["reason"] = "no db host configured"
        return report
    try:
        conn.autocommit(True)
        scan = scan_own(conn, db, owners)
        report.update(scanned=scan["views"], ambiguous=scan["ambiguous"],
                      orphan=scan["orphan"], unreadable=scan["unreadable"],
                      skipped=False)
        autosync = (os.environ.get("GRAPE_VIEW_AUTOSYNC") or "1").strip() != "0"
        if (scan["stale"] or scan["type_drift"]) and autosync:
            report["fixed"] = repair_own(conn, db, scan, dry_run=dry_run)
            scan = scan_own(conn, db, owners)
        report["stale_after"] = scan["stale"]
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass

    from src.kernel.config import log

    if report["fixed"]:
        log.warning(f"[db-guard] 视图自检：重建 {len(report['fixed'])} 个视图 "
                    f"({db}): {', '.join(report['fixed'][:8])}"
                    f"{' …' if len(report['fixed']) > 8 else ''}")
    if report["stale_after"]:
        detail = "; ".join(f"{v}(owner {o}, 缺 {m})"
                           for v, _t, o, m, _e in report["stale_after"][:6])
        log.error(f"[db-guard] 视图自检失败（{db}）：仍有过期视图 {len(report['stale_after'])} 个 — {detail}")
        if (os.environ.get("GRAPE_VIEW_STRICT") or "1").strip() != "0":
            raise SystemExit(7)
    for view in report["unreadable"]:
        log.warning(f"[db-guard] 视图 {db}.{view} 定义不可见（缺 SHOW VIEW 权限？）无法判定新鲜度")
    for view, owner_db, owner_tbl in report["orphan"]:
        log.warning(f"[db-guard] 孤儿视图 {db}.{view} 引用的 {owner_db}.{owner_tbl} 不是基表")
    for view, owners_ in report["ambiguous"]:
        log.warning(f"[db-guard] 视图 {db}.{view} 对应多份基表 {owners_}，归属不明，跳过不修")
    return report


def _connect_own():
    """用进程自己的 DEVOPS_DB_* 建一条一次性连接（启动期，单线程）。"""
    host = (os.environ.get("DEVOPS_DB_HOST") or "").strip()
    if not host:
        return None
    import pymysql

    return pymysql.connect(
        host=host,
        port=int(os.environ.get("DEVOPS_DB_PORT", "3306")),
        user=os.environ.get("DEVOPS_DB_USER", "root"),
        password=os.environ.get("DEVOPS_DB_PASSWORD", ""),
        database=os.environ.get("DEVOPS_DB_NAME", ""),
        charset="utf8mb4",
        autocommit=True,
        # 与 mysql_compat 的连接口径一致（既有 10s）：启动期视图检查若更短，
        # 多库拓扑下会被误判成网络故障而跳过一致性检查。
        connect_timeout=10,
    )


def _cli() -> int:  # pragma: no cover - 手工排查入口
    rep = reconcile_on_startup()
    print(rep)
    return 0 if not rep["stale_after"] else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_cli())