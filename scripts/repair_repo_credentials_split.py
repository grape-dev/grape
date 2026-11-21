#!/usr/bin/env python3
"""盘点/清理仓库级凭据（grape_t_repo_credentials）在两库之间的分裂。

背景：rc 表归 insight，但历史上有非 owner 域直写（并用另一套密钥派生），
导致同一仓库在 mgmt 库也有一条解不开的行；读侧以 rc 行为权威时被判不可用。

本脚本默认只读：盘点两库的行，按 repo_key/cred_type 比对，打印「owner 有 / 非 owner 有」。
--apply 时先导出 SQL 备份，再删除非 owner 库里的行（insight 是唯一权威，绝不改它）。

用法：
  python scripts/repair_repo_credentials_split.py \
      --mgmt-dsn 'mysql://u:p@h:3306/grape_dev_mgmt' \
      --insight-dsn 'mysql://u:p@h:3306/grape_dev_insight' [--apply] [--backup-dir DIR]
"""
from __future__ import annotations

import argparse
import datetime as _dt
import os
import sys
from urllib.parse import unquote, urlparse


def _connect(dsn: str):
    import pymysql

    u = urlparse(dsn)
    return pymysql.connect(
        host=u.hostname or "127.0.0.1",
        port=int(u.port or 3306),
        user=unquote(u.username or ""),
        password=unquote(u.password or ""),
        database=(u.path or "/").lstrip("/"),
        charset="utf8mb4",
        autocommit=False,
    )


def _rows(conn):
    with conn.cursor() as c:
        c.execute(
            "SELECT id, repo_key, cred_type, value, updated_at "
            "FROM grape_t_repo_credentials ORDER BY repo_key, cred_type"
        )
        return list(c.fetchall() or [])


def _kid(value: str) -> str:
    v = (value or "").strip()
    if v.startswith("enc:v1:"):
        v = v[len("enc:v1:"):]
    return v.split(":", 1)[0] if v.count(":") >= 2 else "plaintext"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mgmt-dsn", default=os.environ.get("GRAPE_DSN_MGMT", ""))
    ap.add_argument("--insight-dsn", default=os.environ.get("GRAPE_DSN_INSIGHT", ""))
    ap.add_argument("--apply", action="store_true", help="删除非 owner 库的行（先备份）")
    ap.add_argument("--backup-dir", default="")
    a = ap.parse_args()
    if not a.mgmt_dsn or not a.insight_dsn:
        print("需要 --mgmt-dsn 与 --insight-dsn（或 GRAPE_DSN_MGMT / GRAPE_DSN_INSIGHT）")
        return 2

    owner = _connect(a.insight_dsn)
    other = _connect(a.mgmt_dsn)          # 非 owner：mgmt 库里不该有这张表的数据
    try:
        own_rows = _rows(owner)
        stray = _rows(other)
        own_keys = {(r[1], r[2]) for r in own_rows}

        print(f"insight(owner) 行数={len(own_rows)}  mgmt(stray) 行数={len(stray)}")
        print(f"{'库':<8}{'repo_key':<46}{'type':<10}{'kid':<18}{'owner有?':<8}")
        for r in own_rows:
            print(f"{'insight':<8}{r[1]:<46}{r[2]:<10}{_kid(r[3]):<18}{'yes':<8}")
        for r in stray:
            has = "yes" if (r[1], r[2]) in own_keys else "no（会回落平台 bot）"
            print(f"{'mgmt':<8}{r[1]:<46}{r[2]:<10}{_kid(r[3]):<18}{has:<8}")

        if not a.apply:
            print("\n只读盘点完成；加 --apply 才会删除 mgmt 里的 stray 行。")
            return 0
        if not stray:
            print("\n没有需要清理的行。")
            return 0

        ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        bdir = a.backup_dir or f"/root/backup-rc-{ts}"
        os.makedirs(bdir, exist_ok=True)
        path = os.path.join(bdir, f"grape_t_repo_credentials_mgmt_{ts}.sql")
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"-- backup {_dt.datetime.now().isoformat()} db=mgmt\n")
            for r in stray:
                vals = ", ".join("NULL" if v is None else repr(v) for v in r)
                f.write("INSERT INTO grape_t_repo_credentials "
                        "(id, repo_key, cred_type, value, updated_at) VALUES "
                        f"({vals});\n")
        print(f"\n备份：{path}（{len(stray)} 行）")

        with other.cursor() as c:
            n = 0
            for r in stray:
                c.execute("DELETE FROM grape_t_repo_credentials WHERE id=%s", (r[0],))
                n += c.rowcount or 0
        if n != len(stray):
            other.rollback()
            print(f"删除行数不符（{n} != {len(stray)}），已回滚")
            return 3
        other.commit()
        print(f"已删除 mgmt 库 stray 行 {n} 条（可回滚：{path}）")
        return 0
    finally:
        owner.close()
        other.close()


if __name__ == "__main__":
    sys.exit(main())