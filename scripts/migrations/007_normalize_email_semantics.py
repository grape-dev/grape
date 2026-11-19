"""迁移 007_normalize_email_semantics（Issue #3469 N，2026-11-19）——存量邮箱列语义归一。

⚠️ **必须在备份库 / 演练库上先跑通 `--apply`，再对生产执行**；生产执行前另按
   《执行清单》做全库备份与停写（见脚本尾部 __doc__ 与 Issue #3469 报告）。

背景（Issue #3469 N）
--------------------
`grape_accounts` 的邮箱相关列在历史路径上语义分叉，现网实测：
  * `email IS NULL 且 email_is_placeholder=0`  85 行 —— 「没邮箱」但没打占位标；
  * `email LIKE '%noreply%'` 27 行 —— provider 私有/中转邮箱，不可收信却占着 email 唯一键；
  * `email_source IS NULL`  94 行 —— 来源忘了记；
  * 有邮箱但 `email_verified=0`  62 行 —— 其中 `email_source='user'`（用户自己填的、
    自己收过验证码）的应当算已验证；
  * 非邮箱形态字面串（如 gitee 的 `'未公开邮箱'`）1 行。

本脚本按**唯一口径**（与运行时 `ProviderEmailPolicy.for_storage()` 同源）归一：

  R1  非邮箱形态 / 私有中转（`noreply.*`）/ 空串 → `email=NULL`、
      保留原 `email_source`、`email_is_placeholder=1`；
  R2  `email IS NULL 且 email_is_placeholder=0` → `email_is_placeholder=1`；
  R3  持有真实（可投递）邮箱但 `email_verified=0` → `email_verified=1`
      （真实邮箱要么是用户经验证码绑定、要么是 provider 已断言所有权；复审实测
       62 行原规则只认领 1 行，等于 C2 未修，故按"可投递即视为已验证"收敛）；
      其中 `email_source IS NULL` 的行同时补 `email_source='user'`。

幂等：UPDATE 的 WHERE 都只命中「仍然不符合目标形态」的行，重复执行第二次
      dry-run 应显示 0 待改。

运行环境（三库拓扑必读）：脚本连的是**当前 DEPLOY 的 mgmt 库**，须显式给
`DEVOPS_DB_BACKEND=mysql DEVOPS_DB_HOST/PORT/USER/PASSWORD/NAME=<mgmt 库>`；
只给 `GRAPE_DSN_*` 会回落本地 SQLite（连错库且看不出错）。先 dry-run 核对计数、再 `--apply`。

用法
----
    cd <repo-root>
    # 默认 dry-run：只打印分类计数，不改数据
    python3 scripts/migrations/007_normalize_email_semantics.py
    python3 scripts/migrations/007_normalize_email_semantics.py --dry-run --db /path/to/devops.db

    # 真执行：先整表备份到 grape_accounts_bak_<UTC时间戳>，再执行三条 UPDATE，
    # 最后打印改前/改后行数对比
    python3 scripts/migrations/007_normalize_email_semantics.py --apply

生产执行前还要（脚本之外）：
  1. 全库物理备份（mysqldump / 快照），并确认备份可恢复；
  2. 先在本脚本 `--apply` 于备份库跑一次，核对「改前/改后」计数与预期一致；
  3. 多实例部署时停写/停机（或至少停注册与改绑入口）——三条 UPDATE 期间
     若有并发注册写入，归一后的行可能又被旧代码写回 ''；
  4. 执行后核对 `SELECT COUNT(*) ... WHERE email LIKE '%noreply%'` 归零，
     并让 O 条目的迁移（202611190101）随后跑，唯一索引才收敛到活跃状态。
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

TABLE = "grape_accounts"

# 迁移 007 只做「存量数据归一」；目录名保留 007 序号与 001~006 同风格。
_NOREPLY_LIKE = "%noreply%"


def _table_exists(conn, name: str) -> bool:
    """表是否存在（双后端；不走 sqlite_master 以免 MySQL 上 1146）。"""
    try:
        from src.kernel.mysql_compat import get_db_backend
        mysql = get_db_backend() == "mysql"
    except Exception:  # noqa: BLE001
        mysql = False
    if mysql:
        row = conn.execute(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", (name,)).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
            (name,)).fetchone()
    return bool(row and int(row[0] or 0))


def _rows(conn):
    """全表关键列（compat 游标不可迭代，一律 fetchall）。"""
    cur = conn.execute(
        f"SELECT id, email, email_source, email_verified, email_is_placeholder "
        f"FROM {TABLE}")
    return cur.fetchall() or []


def _group_update(conn, set_sql: str, ids) -> int:
    """按 id 分批 UPDATE（每批 500 个占位符），返回实际影响行数。

    走参数化 IN 列表而不是把判定写进 SQL：邮箱形态/可投递性判定在
    `ProviderEmailPolicy`（Python 正则 + 域名后缀表），SQL 侧复刻必然漂移。
    """
    total = 0
    batch = 500
    for i in range(0, len(ids), batch):
        chunk = list(ids[i:i + batch])
        marks = ",".join("?" for _ in chunk)
        cur = conn.execute(f"UPDATE {TABLE} {set_sql} WHERE id IN ({marks})", tuple(chunk))
        total += int(getattr(cur, "rowcount", 0) or 0)
        conn.commit()
    return total


def classify(rows) -> dict:
    """分类待改行（纯函数，dry-run 与 apply 共用同一判据）。

    与运行时口径同源：`ProviderEmailPolicy.for_storage()`（空/非邮箱形态/私有中转
    → (None, 1)，真实邮箱 → (小写邮箱, 0)）。直接复用它，避免脚本里再写一份
    域名后缀表 —— 两份判据必然漂移（Issue #3054 踩过）。
    """
    from src.domains.mgmt.auth.identity_email import ProviderEmailPolicy

    will_null: list = []            # R1：email → NULL
    null_by_kind = {"blank": 0, "non_email_shape": 0, "private_noreply": 0}
    placeholder_fix = 0             # R2：NULL 但没打占位标
    verify_ids: list = []           # R3：真实邮箱但 verified=0 → verified=1
    source_ids: list = []           # R3b：真实邮箱但 email_source 为空 → 'user'
    real_email_other_source = 0     # 真实邮箱、来源非 user 且未验证（会一起修）
    real_email_already_verified = 0

    for rid, email, source, verified, placeholder in rows:
        e = (email or "").strip()
        ph = str(placeholder if placeholder is not None else 0).strip()
        if email is None or e == "":
            if email is not None:
                will_null.append(rid)
                null_by_kind["blank"] += 1
            elif ph not in ("1",):
                # email 本来就是 NULL 且 placeholder=0 → R2
                placeholder_fix += 1
            continue
        stored, _ph = ProviderEmailPolicy.for_storage(e)
        if stored is None:
            will_null.append(rid)
            kind = "non_email_shape" if not ProviderEmailPolicy.is_valid(e) else "private_noreply"
            null_by_kind[kind] += 1
            continue
        # 真实（可投递）邮箱：一律视为已验证（复审：只认 source='user' 会漏 61 行）
        if str(verified if verified is not None else 0).strip() != "1":
            verify_ids.append(rid)
            if str(source or "").strip() != "user":
                real_email_other_source += 1
        else:
            real_email_already_verified += 1
        if not str(source or "").strip():
            source_ids.append(rid)

    return {
        "total_rows": len(rows),
        "will_null": len(will_null),
        "null_by_kind": null_by_kind,
        "placeholder_fix": placeholder_fix,
        "verify_fix": len(verify_ids),
        "real_email_other_source": real_email_other_source,
        "source_fix": len(source_ids),
        "real_email_already_verified": real_email_already_verified,
        "_will_null_ids": will_null,
        "_verify_ids": verify_ids,
        "_source_ids": source_ids,
    }


def snapshot(conn) -> dict:
    """改前/改后行数对比用的指标快照（对应 Issue #3469 现网实测那五类）。"""
    def _one(sql, params=()):
        row = conn.execute(sql, params).fetchone()
        return int(row[0] or 0) if row else 0

    out = {
        "total": _one(f"SELECT COUNT(*) FROM {TABLE}"),
        "email_null": _one(f"SELECT COUNT(*) FROM {TABLE} WHERE email IS NULL"),
        "email_blank": _one(f"SELECT COUNT(*) FROM {TABLE} WHERE email = ''"),
        "email_null_placeholder0": _one(
            f"SELECT COUNT(*) FROM {TABLE} WHERE email IS NULL "
            f"AND COALESCE(email_is_placeholder, 0) = 0"),
        "email_like_noreply": _one(
            f"SELECT COUNT(*) FROM {TABLE} WHERE email LIKE ?", (_NOREPLY_LIKE,)),
        "email_source_null": _one(
            f"SELECT COUNT(*) FROM {TABLE} WHERE email_source IS NULL"),
        "has_email_unverified": _one(
            f"SELECT COUNT(*) FROM {TABLE} WHERE email IS NOT NULL AND email <> '' "
            f"AND COALESCE(email_verified, 0) = 0"),
    }
    # 非邮箱形态（SQL 侧判不出来，走 Python 口径）
    non_email = 0
    from src.domains.mgmt.auth.identity_email import ProviderEmailPolicy
    for (_rid, email, _s, _v, _p) in _rows(conn):
        e = (email or "").strip()
        if e and not ProviderEmailPolicy.is_valid(e):
            non_email += 1
    out["non_email_shape"] = non_email
    return out


def _print_counts(title: str, counts: dict) -> None:
    print(f"[007] {title}")
    for k in ("total", "email_null", "email_blank", "email_null_placeholder0",
              "email_like_noreply", "email_source_null", "non_email_shape",
              "has_email_unverified"):
        print(f"    {k:<28} {counts.get(k, 0)}")


def _backup(conn) -> str:
    """整表备份到 grape_accounts_bak_<UTC时间戳>，并校验行数守恒。

    `CREATE TABLE ... AS SELECT *` 两栈通用（MySQL 也可，但只复制数据、不带索引，
    对备份正好合适）；不用 `CREATE TABLE ... LIKE` —— 那条形式会被 mysql_compat
    的 CREATE TABLE 深度翻译改写。
    """
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    bak = f"{TABLE}_bak_{ts}"
    if _table_exists(conn, bak):
        raise RuntimeError(f"备份表 {bak} 已存在，拒绝覆盖")
    conn.execute(f"CREATE TABLE {bak} AS SELECT * FROM {TABLE}")
    conn.commit()
    src = int(conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0] or 0)
    dst = int(conn.execute(f"SELECT COUNT(*) FROM {bak}").fetchone()[0] or 0)
    if src != dst:
        raise RuntimeError(f"备份行数不守恒（源 {src} / 备份 {dst}），中止")
    print(f"[007] 已备份整表 → {bak}（{dst} 行）")
    return bak


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Issue #3469 N：grape_accounts 存量邮箱语义归一（默认 dry-run）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印分类计数，不改数据（默认行为）")
    parser.add_argument("--apply", action="store_true",
                        help="先整表备份，再执行三条 UPDATE，最后打印改前/改后对比")
    parser.add_argument("--db", default=os.environ.get("DEVOPS_DB_PATH", ""),
                        help="SQLite 库路径（默认走 session_manager 的库）")
    args = parser.parse_args(argv)

    if args.apply and args.dry_run:
        print("[007] --dry-run 与 --apply 不能同时给", file=sys.stderr)
        return 2
    apply_changes = bool(args.apply)

    if args.db:
        os.environ["DEVOPS_DB_PATH"] = args.db
    from src.kernel import session_manager
    conn = session_manager.db

    # 表存在性守卫（跨域视图库里 grape_accounts 可能不存在）
    if not _table_exists(conn, TABLE):
        print(f"[007] 本库没有 {TABLE} 表（非 owner 域？），无事可做")
        return 0

    rows = _rows(conn)
    plan = classify(rows)
    pending = (plan["will_null"] + plan["placeholder_fix"] + plan["verify_fix"]
               + plan["source_fix"])

    # MySQL 下 DEVOPS_DB_PATH 恒空（输出"<session default>"看不出连了哪个库），
    # 这里按后端打真实连接目标（复审 P3）。
    if os.environ.get("DEVOPS_DB_BACKEND", "").lower() == "mysql" or os.environ.get("DEVOPS_DB_HOST"):
        _target = (f"mysql {os.environ.get('DEVOPS_DB_HOST', '?')}:"
                   f"{os.environ.get('DEVOPS_DB_PORT', '3306')}/"
                   f"{os.environ.get('DEVOPS_DB_NAME', '?')}")
    else:
        _target = os.environ.get('DEVOPS_DB_PATH') or '<session default>'
    print(f"[007] 目标库: {_target}")
    print(f"[007] 扫描 {plan['total_rows']} 行；待改 {pending} 行")
    print(f"      R1 email→NULL          {plan['will_null']:>6}  "
          f"(空串 {plan['null_by_kind']['blank']} / 非邮箱形态 "
          f"{plan['null_by_kind']['non_email_shape']} / noreply "
          f"{plan['null_by_kind']['private_noreply']})")
    print(f"      R2 placeholder 0→1     {plan['placeholder_fix']:>6}")
    print(f"      R3 verified 0→1        {plan['verify_fix']:>6}  "
          f"(真实邮箱；其中来源≠user {plan['real_email_other_source']})")
    print(f"      R3b source NULL→'user' {plan['source_fix']:>6}  "
          f"已 verified  {plan['real_email_already_verified']:>6}")

    if not apply_changes:
        print("[007] dry-run 结束（未改任何数据）。加 --apply 执行。")
        return 0

    if pending == 0:
        print("[007] 已符合目标口径，无需变更（不建备份表）")
        return 0

    before = snapshot(conn)
    _backup(conn)

    n1 = _group_update(
        # R1 置空后该行不再持有邮箱，verified 必须随之归零（复审 P3：只置空会把
        # "email 为空但 verified=1" 的畸形留给后续逻辑）。
        conn, "SET email = NULL, email_is_placeholder = 1, email_verified = 0",
        plan["_will_null_ids"])
    cur2 = conn.execute(
        f"UPDATE {TABLE} SET email_is_placeholder = 1 "
        f"WHERE email IS NULL AND COALESCE(email_is_placeholder, 0) = 0")
    n2 = int(getattr(cur2, "rowcount", 0) or 0)
    conn.commit()
    n3 = _group_update(conn, "SET email_verified = 1", plan["_verify_ids"])
    n3b = _group_update(conn, "SET email_source = 'user'", plan["_source_ids"])

    after = snapshot(conn)
    print(f"[007] 已执行：R1 {n1} 行，R2 {n2} 行，R3 {n3} 行，R3b {n3b} 行")
    _print_counts("改前", before)
    _print_counts("改后", after)

    # 幂等自检：再分类一次，应 0 待改
    left = classify(_rows(conn))
    left_pending = (left["will_null"] + left["placeholder_fix"] + left["verify_fix"]
                    + left["source_fix"])
    if left_pending:
        print(f"[007] 警告：仍有 {left_pending} 行未达目标口径，请复查", file=sys.stderr)
        return 1
    print("[007] 幂等自检通过：0 行待改")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())