#!/usr/bin/env python3
"""跨库视图新鲜度检查与修复（MySQL 分域部署的通用守卫）。

动机：库按域拆分后，非归属库只持有跨库视图，而视图列清单是**建视图那一刻**的快照。
owner 基表后续加列 / 删列 / 改列类型 / 新增表 / 删表时，非归属库不会跟随，表现为：
  · 缺列        → (1054, Unknown column ...)
  · 视图多余列  → (1054, Unknown column ...)（owner 已删列，视图仍引用）
  · 缺视图      → (1146, Table ... doesn't exist)
  · 孤儿视图    → (1146, ...)（owner 基表已被 DROP/RENAME，视图仍引用）
  · 列类型变更  → 读写语义偏差（不报错，更隐蔽）

判定口径（"谁拥有谁写"）：同名表在**一个**域库是 BASE TABLE（owner），其余域库必须是 VIEW，
且列名集合与列类型都要与 owner 一致。

安全边界（重要）：
  · 只做 CREATE OR REPLACE VIEW；不删表、不改数据。注意该语句会**重置 DEFINER** 为执行账号，
    若线上视图依赖特定 definer 授权，请用 `--definer user@host` 显式指定。
  · owner 不确定（同名表在多个域库都是基表）→ 不判定、不修复，只登记。
  · 默认**不补建**缺失视图；只有列入 `--create-missing-tables` 清单的表才补建。
  · 视图列是 owner 列的子集时默认按"快照过期"重建（对齐到 owner 全列）；
    若某视图是**有意收窄**，把表登记到 `--narrowed-tables`，脚本就不再补列。
  · 只报告、不自动处理：孤儿视图（需人工确认后删视图/恢复基表）。

执行顺序（部署）：
    迁移 → 本脚本 --repair → 复核 → 重启进程
  迁移发生在进程 import 期（init_database），因此修复必须在**迁移之后**、重启**之前**执行，
  否则只能修到上一版 schema（见 deploy-after-migrate.sh）。

用法：
    python3 scripts/domain_view_freshness.py --check [--json]
    python3 scripts/domain_view_freshness.py --repair [--dry-run]

环境变量：DEVOPS_DB_HOST / DEVOPS_DB_USER / DEVOPS_DB_PASSWORD
          DEVOPS_DB_NAME（当前进程库名，用于判定环境族）
          GRAPE_DOMAIN_DATABASES（域库列表，优先于自动发现）

退出码：0 新鲜/已修复；1 存在缺列/多余列/缺失视图/孤儿视图/类型漂移；
        2 --strict-owner 且存在未登记的多份基表（优先于 1，先定 owner）；
        3 环境不可用（无库/库族不确定/缺少 CREATE VIEW 权限）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from view_ddl_builder import ViewDDLError, build_view_ddl, check_ident  # noqa: E402

DB_NAME_RE = re.compile(r"^grape_(dev_)?(mgmt|runtime|insight)(_db)?$")
VIEW_REF_RE = re.compile(r"\b(?:from|join)\s+`([^`]+)`\.`([^`]+)`", re.I)
DEFINER_RE = re.compile(r"^[A-Za-z0-9_.%-]{1,64}@[A-Za-z0-9_.%-]{1,255}$")
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_VIEW_ALLOWLIST = os.path.join(_REPO_ROOT, "docs", "weekly-11", "view-allowlist.txt")
_PROD_DB_RE = re.compile(r"^grape_(mgmt|runtime|insight)_db$")


def load_view_allowlist(path: str) -> set[str]:
    """读 `<库>.<视图>` 白名单；文件缺失 → 空集合（fail-closed）。"""
    allowed: set[str] = set()
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    allowed.add(line)
    except OSError:
        return set()
    return allowed


def allowlist_applies(dbs) -> bool:
    """白名单只约束三库现网命名；dev/测试库族（grape_dev_*）不套用，避免误拦部署。"""
    return bool(dbs) and all(_PROD_DB_RE.match(d) for d in dbs)


def _connect():
    import pymysql

    host = os.environ.get("DEVOPS_DB_HOST", "")
    if not host:
        return None
    return pymysql.connect(
        host=host,
        port=int(os.environ.get("DEVOPS_DB_PORT", "3306")),
        user=os.environ.get("DEVOPS_DB_USER", "root"),
        password=os.environ.get("DEVOPS_DB_PASSWORD", ""),
        charset="utf8mb4",
        autocommit=True,
        connect_timeout=8,
    )


def _family(name: str) -> str:
    return "dev" if "_dev_" in name else "prod"


def discover_databases(conn, explicit: str = "") -> tuple[str, ...]:
    """确定要比较的域库集合（不做数量启发式：两族共存时必须显式给定族）。"""
    explicit = (explicit or os.environ.get("GRAPE_DOMAIN_DATABASES", "")).strip()
    with conn.cursor() as c:
        c.execute("SELECT SCHEMA_NAME FROM information_schema.schemata")
        schemas = {r[0] for r in c.fetchall()}

    if explicit:
        wanted = tuple(x.strip() for x in explicit.split(",") if x.strip())
        for name in wanted:
            check_ident("域库名", name)
        unknown = [n for n in wanted if n not in schemas]
        if unknown:
            raise ViewDDLError(f"显式指定的库不存在: {unknown}")
        return wanted

    found = [s for s in schemas if DB_NAME_RE.match(s)]
    cur_db = os.environ.get("DEVOPS_DB_NAME", "").strip()
    if cur_db and DB_NAME_RE.match(cur_db):
        return tuple(sorted(x for x in found if _family(x) == _family(cur_db)))

    families = {_family(x) for x in found}
    if len(families) > 1:
        raise ViewDDLError(
            "域库族不唯一（同时存在 dev 与生产域库）；请显式设置 GRAPE_DOMAIN_DATABASES "
            "或 DEVOPS_DB_NAME，脚本不按数量猜测"
        )
    return tuple(sorted(found))


def _columns(conn, db: str, table: str) -> dict[str, str]:
    with conn.cursor() as c:
        c.execute("""
            SELECT COLUMN_NAME, COLUMN_TYPE FROM information_schema.columns
            WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION
        """, (db, table))
        return {r[0]: r[1] for r in c.fetchall()}


def _view_refs(conn, db: str, view: str) -> list[tuple[str, str]] | None:
    """解析视图定义引用的全部 库.表（含 JOIN）。

    返回 None 表示定义不可见（缺少 SHOW VIEW 权限或非常规视图）——调用方必须显式告警，
    不能当作"没有引用"而静默通过（否则孤儿视图检测会在真实权限模型下失效）。
    """
    with conn.cursor() as c:
        c.execute("SELECT VIEW_DEFINITION FROM information_schema.views "
                  "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s", (db, view))
        row = c.fetchone()
    if not row or not row[0]:
        return None
    return [(m.group(1), m.group(2)) for m in VIEW_REF_RE.finditer(row[0])]


def scan(conn, dbs: tuple[str, ...], narrowed: set[str] | None = None) -> dict:
    """扫描各类不一致。

    stale            : [(view_db, table, owner, missing_cols, extra_cols)]
    type_drift       : [(view_db, table, owner, [(col, owner_type, view_type)])]
    missing_view     : [(view_db, table, owner)]
    orphan_view      : [(view_db, view, owner_db, owner_tbl)]   # owner 基表已不存在
    duplicates       : [(table, [dbs_with_base_table])]
    duplicate_drift  : [(table, {db: [cols...]})]               # 多份基表之间列不一致（仅告警）
    total_views      : int
    """
    narrowed = narrowed or set()
    ph = ",".join(["%s"] * len(dbs))
    with conn.cursor() as c:
        c.execute(f"""
            SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE FROM information_schema.tables
            WHERE TABLE_SCHEMA IN ({ph})
        """, (list(dbs)))
        rows = c.fetchall()

    types = {(db, tbl): typ for db, tbl, typ in rows}
    base_by_name: dict[str, list[str]] = {}
    for (db, tbl), typ in types.items():
        if typ == "BASE TABLE":
            base_by_name.setdefault(tbl, []).append(db)

    stale, type_drift, missing_view, duplicates, duplicate_drift, orphan_view = [], [], [], [], [], []
    duplicate_view_stale = []
    for tbl, owners in sorted(base_by_name.items()):
        if len(owners) > 1:
            duplicates.append((tbl, sorted(owners)))
            cols_by_db = {db: _columns(conn, db, tbl) for db in sorted(owners)}
            if len({frozenset(v) for v in cols_by_db.values()}) > 1:
                duplicate_drift.append((tbl, {db: sorted(cols_by_db[db]) for db in cols_by_db}))
            # 白名单表也要校验"非归属库视图"：只要缺了**所有副本都有**的列，就是确定过期
            common = set.intersection(*(set(v) for v in cols_by_db.values())) if cols_by_db else set()
            for (db, dup_tbl), typ in sorted(types.items()):
                if dup_tbl != tbl or typ != "VIEW" or db in owners:
                    continue
                view_cols = set(_columns(conn, db, tbl))
                missing_all = sorted(common - view_cols)
                if missing_all:
                    duplicate_view_stale.append((db, tbl, sorted(owners), missing_all))
            continue
        owner = owners[0]
        owner_cols = _columns(conn, owner, tbl)
        for db in dbs:
            if db == owner:
                continue
            typ = types.get((db, tbl))
            if typ is None:
                missing_view.append((db, tbl, owner))
                continue
            view_cols = _columns(conn, db, tbl)
            missing = [c for c in owner_cols if c not in view_cols]
            extra = [c for c in view_cols if c not in owner_cols]
            if extra or (missing and tbl not in narrowed):
                # narrowed 表：重建时保留"视图已有 ∩ owner 仍有"的投影，绝不补回被收窄的列
                keep = [c for c in view_cols if c in owner_cols] if tbl in narrowed else None
                stale.append((db, tbl, owner, [] if tbl in narrowed else missing, extra, keep))
                continue
            drift = [(c, owner_cols[c], view_cols[c]) for c in owner_cols
                     if c in view_cols and owner_cols[c] != view_cols[c]]
            if drift:
                type_drift.append((db, tbl, owner, drift))

    # 孤儿视图：非归属库里存在视图，但它引用的 owner 基表在所选域库中已不是 BASE TABLE
    known_views = {(db, tbl) for (db, tbl), typ in types.items() if typ == "VIEW"}
    unreadable_views = []
    for db, view in sorted(known_views):
        if view in base_by_name:
            continue                       # 同名基表存在 → 走上面的正常判定
        refs = _view_refs(conn, db, view)
        if refs is None:
            unreadable_views.append((db, view))
            continue
        for owner_db, owner_tbl in refs:
            if (owner_db, owner_tbl) not in types or types[(owner_db, owner_tbl)] != "BASE TABLE":
                orphan_view.append((db, view, owner_db, owner_tbl))

    total_views = sum(1 for t in types.values() if t == "VIEW")
    return {
        "stale": sorted(stale),
        "type_drift": sorted(type_drift),
        "missing_view": sorted(missing_view),
        "orphan_view": sorted(orphan_view),
        "duplicates": sorted(duplicates),
        "duplicate_drift": sorted(duplicate_drift),
        "duplicate_view_stale": sorted(duplicate_view_stale),
        "unreadable_views": sorted(unreadable_views),
        "total_views": total_views,
    }


def _has_create_view_privilege(conn, dbs: tuple[str, ...]) -> list[str]:
    """返回缺少 CREATE VIEW 权限的库（root/SUPER 视为具备）。"""
    with conn.cursor() as c:
        c.execute("SELECT CURRENT_USER()")
        cur_user = (c.fetchone() or [""])[0]
        c.execute("SELECT PRIVILEGE_TYPE FROM information_schema.USER_PRIVILEGES WHERE GRANTEE = CONCAT('''', SUBSTRING_INDEX(CURRENT_USER(), '@', 1), '''@''', SUBSTRING_INDEX(CURRENT_USER(), '@', -1), '''')")
        global_privs = {r[0] for r in c.fetchall()}
        if global_privs & {"SUPER", "CREATE VIEW", "ALL PRIVILEGES"}:
            return []
        missing = []
        for db in dbs:
            c.execute("SELECT PRIVILEGE_TYPE FROM information_schema.SCHEMA_PRIVILEGES "
                      "WHERE TABLE_SCHEMA=%s AND GRANTEE = CONCAT('''', SUBSTRING_INDEX(CURRENT_USER(), '@', 1), '''@''', SUBSTRING_INDEX(CURRENT_USER(), '@', -1), '''')",
                      (db,))
            privs = {r[0] for r in c.fetchall()}
            if "CREATE VIEW" not in privs and "ALL PRIVILEGES" not in privs:
                missing.append(db)
    return missing


def repair(conn, scan_result: dict, dry_run: bool = False,
           create_missing_tables: set[str] | None = None,
           definer: str = "", log=print,
           allowed: set[str] | None = None) -> dict:
    """重建视图。owner 不确定 / 孤儿视图 / 未登记的缺失视图一律跳过。

    `allowed` 为 `<库>.<视图>` 白名单（P0 冻结：只减不增）；不在名单内的视图
    **不重建**，即使它已存在——这保证 sync 脚本之外的第二条建视图路径也受约束。
    """
    create_missing_tables = create_missing_tables or set()
    ambiguous = {tbl for tbl, _dbs in scan_result["duplicates"]}
    plan: dict[tuple[str, str], set[str]] = {}

    projection: dict[tuple[str, str], list[str]] = {}
    for view_db, tbl, owner, _missing, _extra, keep in scan_result["stale"]:
        if tbl not in ambiguous:
            plan.setdefault((owner, tbl), set()).add(view_db)
            if keep is not None:
                projection[(owner, tbl)] = keep
    for view_db, tbl, owner, _drift in scan_result["type_drift"]:
        if tbl not in ambiguous:
            plan.setdefault((owner, tbl), set()).add(view_db)
    for view_db, tbl, owner in scan_result["missing_view"]:
        if tbl not in ambiguous and tbl in create_missing_tables:
            plan.setdefault((owner, tbl), set()).add(view_db)

    fixed = 0
    for (owner_db, tbl), view_dbs in sorted(plan.items()):
        owner_cols = _columns(conn, owner_db, tbl)
        cols = projection.get((owner_db, tbl)) or list(owner_cols)
        try:
            ddl = build_view_ddl(owner_db, tbl, list(cols))
            if definer:
                if not DEFINER_RE.match(definer):
                    raise ViewDDLError(f"definer 非法（应形如 user@host）: {definer!r}")
                ddl = ddl.replace("CREATE OR REPLACE VIEW",
                                  f"CREATE OR REPLACE DEFINER={definer} VIEW", 1)
        except ViewDDLError as exc:
            log(f"SKIP {tbl}: {exc}", file=sys.stderr)
            continue
        for vdb in sorted(view_dbs):
            if allowed is not None and f"{vdb}.{tbl}" not in allowed:
                log(f"DENY {vdb}.{tbl}: 不在视图白名单（只减不增）", file=sys.stderr)
                continue
            try:
                check_ident("域库名", vdb)
            except ViewDDLError as exc:
                log(f"SKIP {vdb}: {exc}", file=sys.stderr)
                continue
            if dry_run:
                log(f"DRY  {vdb}.{tbl}: would CREATE OR REPLACE VIEW ({len(cols)} cols, owner {owner_db})")
                continue
            with conn.cursor() as c:
                c.execute(f"USE `{vdb}`")
                c.execute(ddl)
            log(f"OK   {vdb}.{tbl}: view rebuilt ({len(cols)} cols, owner {owner_db})")
            fixed += 1

    for tbl, dbs_ in scan_result["duplicates"]:
        log(f"SKIP {tbl}: ambiguous owner (base table in {dbs_}) — 需人工指定", file=sys.stderr)
    for view_db, tbl, owner in scan_result["missing_view"]:
        if tbl not in create_missing_tables:
            log(f"SKIP {view_db}.{tbl}: missing view（未列入 --create-missing-tables，仅报告）",
                file=sys.stderr)
    for view_db, view, owner_db, owner_tbl in scan_result["orphan_view"]:
        log(f"SKIP {view_db}.{view}: orphan view（owner {owner_db}.{owner_tbl} 已不存在，需人工确认）",
            file=sys.stderr)
    return {"fixed": fixed, "skipped_ambiguous": sorted(ambiguous)}


def _load_table_list(path: str) -> set[str]:
    if not path:
        return set()
    out: set[str] = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if line:
                out.add(line)
    return out


def _emit_json(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="跨库视图新鲜度检查/修复")
    ap.add_argument("--check", action="store_true", help="只检查（默认）")
    ap.add_argument("--repair", action="store_true",
                    help="重建不一致的视图（写操作；库不可用即失败，不使用 --skip-if-unavailable）")
    ap.add_argument("--dry-run", action="store_true", help="修复前只打印不执行")
    ap.add_argument("--databases", default="", help="逗号分隔的域库名")
    ap.add_argument("--skip-if-unavailable", action="store_true",
                    help="连不上库/非 MySQL 时跳过并 exit 0（仅适合 CI 的 --check）")
    ap.add_argument("--strict", action="store_true", help="库不可用或族不确定即失败（exit 3）")
    ap.add_argument("--strict-owner", action="store_true",
                    help="存在未登记的同名多份基表时 exit 2（优先于漂移的 exit 1）")
    ap.add_argument("--known-duplicates", default="", help="已知多份基表白名单文件")
    ap.add_argument("--create-missing-tables", default="",
                    help="允许补建缺失视图的表清单文件（不在清单内只报告）")
    ap.add_argument("--ignore-missing-tables", default="",
                    help="登记「不需要跨域视图」的表清单（缺失视图不再计入报告）")
    ap.add_argument("--narrowed-tables", default="",
                    help="有意只暴露部分列的表清单（不自动补列）")
    ap.add_argument("--definer", default="", help="重建视图时显式指定的 DEFINER（user@host）")
    ap.add_argument("--view-allowlist", default=DEFAULT_VIEW_ALLOWLIST,
                    help="视图白名单（默认 docs/weekly-11/view-allowlist.txt；只减不增）")
    ap.add_argument("--fail-on-missing-view", action="store_true",
                    help="把未登记的缺视图计为失败（默认只报告，避免 owner-only 新表卡住部署）")
    ap.add_argument("--json", action="store_true", help="纯 JSON 输出")
    args = ap.parse_args(argv)

    strict = args.strict or args.repair

    backend = os.environ.get("DEVOPS_DB_BACKEND", "mysql").lower()
    if backend != "mysql":
        msg = f"skip: DEVOPS_DB_BACKEND={backend}（视图检查仅对 MySQL 生效）"
        if args.json:
            _emit_json({"skipped": True, "reason": msg})
        else:
            print(msg)
        return 3 if strict else 0

    host_set = bool(os.environ.get("DEVOPS_DB_HOST", "").strip())
    try:
        conn = _connect()
    except Exception as exc:  # noqa: BLE001
        conn, err = None, str(exc)[:160]
    else:
        err = ""

    if conn is None:
        # 区分两种"不可用"：没配（可跳过）vs 配了但连不上（按失败处理，不得伪装成功）
        if not host_set:
            msg = "skip: DEVOPS_DB_HOST 未设置（本环境不检查视图）"
            if strict:
                print(msg, file=sys.stderr)
                return 3
            if args.json:
                _emit_json({"skipped": True, "reason": msg})
            else:
                print(msg)
            return 0
        print(f"[FATAL] 数据库已配置但连接失败（{err or '未知错误'}）；"
              f"不得视为通过，修复后重跑", file=sys.stderr)
        return 3

    log = (lambda *a, **kw: print(*a, file=sys.stderr, **kw)) if args.json else print
    rc = 0
    try:
        try:
            dbs = discover_databases(conn, args.databases)
        except ViewDDLError as exc:
            print(f"[FATAL] {exc}", file=sys.stderr)
            return 3
        if len(dbs) < 2:
            msg = f"skip: 发现的域库不足（{dbs}）"
            if strict:
                print(msg, file=sys.stderr)
                return 3
            print(msg if not args.json else json.dumps({"skipped": True, "reason": msg}, ensure_ascii=False))
            return 0

        known = _load_table_list(args.known_duplicates)
        create_missing = _load_table_list(args.create_missing_tables)
        narrowed = _load_table_list(args.narrowed_tables)
        ignore_missing = _load_table_list(args.ignore_missing_tables)

        result = scan(conn, dbs, narrowed=narrowed)
        if ignore_missing:
            filt = [mv for mv in result["missing_view"] if mv[1] not in ignore_missing]
            result["ignored_missing_view"] = [mv for mv in result["missing_view"] if mv[1] in ignore_missing]
            result["missing_view"] = filt
        else:
            result["ignored_missing_view"] = []
        unknown_dups = [(t, d) for t, d in result["duplicates"] if t not in known]

        repair_info = None
        if args.repair:
            missing_priv = _has_create_view_privilege(conn, tuple(dbs))
            if missing_priv:
                print(f"[FATAL] 当前账号缺少 CREATE VIEW 权限: {missing_priv}；"
                      f"请用具备权限的运维账号或授予后重试", file=sys.stderr)
                return 3
            repair_info = repair(conn, result, dry_run=args.dry_run,
                                 create_missing_tables=create_missing,
                                 definer=args.definer, log=log,
                                 allowed=(load_view_allowlist(args.view_allowlist)
                                          if allowlist_applies(dbs) else None))
            if not args.dry_run:
                result = scan(conn, dbs, narrowed=narrowed)
                if ignore_missing:
                    result["ignored_missing_view"] = [mv for mv in result["missing_view"]
                                                      if mv[1] in ignore_missing]
                    result["missing_view"] = [mv for mv in result["missing_view"]
                                              if mv[1] not in ignore_missing]
                else:
                    result["ignored_missing_view"] = []

        bad = bool(result["stale"] or result["type_drift"] or result["orphan_view"]
                   or result["duplicate_view_stale"])
        if args.fail_on_missing_view and result["missing_view"]:
            bad = True
        if result["unreadable_views"] and args.strict:
            bad = True
        if bad:
            rc = 1
        if unknown_dups and args.strict_owner:
            rc = 2          # owner 未定优先于具体漂移，先定归属

        if args.json:
            _emit_json({
                "databases": list(dbs),
                "total_views": result["total_views"],
                "stale": [{"view": f"{d}.{t}", "owner": o, "missing": m, "extra": e}
                          for d, t, o, m, e, _k in result["stale"]],
                "type_drift": [{"view": f"{d}.{t}", "owner": o, "columns": m}
                               for d, t, o, m in result["type_drift"]],
                "missing_view": [{"view": f"{d}.{t}", "owner": o} for d, t, o in result["missing_view"]],
                "ignored_missing_view": [{"view": f"{d}.{t}"} for d, t, _o in result.get("ignored_missing_view", [])],
                "orphan_view": [{"view": f"{d}.{v}", "owner": f"{o}.{t}"}
                                for d, v, o, t in result["orphan_view"]],
                "duplicate_base_tables": [{"table": t, "dbs": d} for t, d in result["duplicates"]],
                "duplicate_drift": [{"table": t, "columns_by_db": c}
                                    for t, c in result["duplicate_drift"]],
                "duplicate_view_stale": [{"view": f"{d}.{t}", "owners": o, "missing": m}
                                         for d, t, o, m in result["duplicate_view_stale"]],
                "unreadable_views": [{"view": f"{d}.{v}"} for d, v in result["unreadable_views"]],
                "unknown_duplicate_tables": [t for t, _d in unknown_dups],
                "repair": repair_info,
            })
            return rc

        if repair_info is not None:
            print(f"修复完成：{repair_info['fixed']} 个视图"
                  f"{'（dry-run）' if args.dry_run else ''}；复核如下")
        print(f"域库: {', '.join(dbs)}  视图 {result['total_views']} 个  "
              f"缺列/多余列 {len(result['stale'])}  缺视图 {len(result['missing_view'])}  "
              f"孤儿视图 {len(result['orphan_view'])}  类型漂移 {len(result['type_drift'])}  "
              f"副本视图漂移 {len(result['duplicate_view_stale'])}")
        if result.get("ignored_missing_view"):
            print(f"  （另有 {len(result['ignored_missing_view'])} 条缺失已按 --ignore-missing-tables 登记为「不需要跨域视图」）")
        for db, tbl, owner, missing, extra, _keep in result["stale"]:
            print(f"  STALE {db}.{tbl} ← {owner}: 缺 {len(missing)} 列 "
                  f"{missing[:6]}{'…' if len(missing) > 6 else ''}"
                  + (f"；多余 {len(extra)} 列 {extra[:6]}" if extra else ""))
        for db, tbl, owner in result["missing_view"]:
            if tbl in create_missing:
                hint = "（已登记，将补建）"
            else:
                hint = "（未登记：不补建、不判红；需要跨域读就登记到 --create-missing-tables）"
            print(f"  MISSING {db}.{tbl} ← {owner}: 非归属库缺少该视图{hint}")
        for db, tbl, owners, missing in result["duplicate_view_stale"]:
            print(f"  STALE {db}.{tbl}: 非归属视图缺列 {missing[:6]}（owner 候选 {owners}，需先定 owner）")
        for db, view in result["unreadable_views"]:
            print(f"  [UNKNOWN] {db}.{view}: 视图定义不可见（缺 SHOW VIEW 权限？），孤儿检测跳过")
        for db, view, owner_db, owner_tbl in result["orphan_view"]:
            print(f"  ORPHAN {db}.{view} → {owner_db}.{owner_tbl}: owner 基表已不存在，需人工确认")
        for db, tbl, owner, drift in result["type_drift"]:
            print(f"  DRIFT {db}.{tbl} ← {owner}: {drift[:3]}{'…' if len(drift) > 3 else ''}")
        if result["duplicates"]:
            tag = "已登记" if not unknown_dups else f"其中未登记 {len(unknown_dups)}"
            print(f"  ⚠ 同名表在多个库都是基表: {len(result['duplicates'])}（{tag}）")
            for tbl, dbs_ in result["duplicates"]:
                mark = "" if tbl in known else "  ← 未登记"
                print(f"    {tbl}: {dbs_}{mark}")
        for tbl, cols_by_db in result["duplicate_drift"]:
            print(f"  ⚠ 多份基表列不一致（仅告警，需人工定 owner 后收口）: {tbl} "
                  f"{ {k: len(v) for k, v in cols_by_db.items()} }")
    finally:
        conn.close()

    return rc


if __name__ == "__main__":
    sys.exit(main())