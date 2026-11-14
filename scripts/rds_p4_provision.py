#!/usr/bin/env python3
"""P4/P5 彩排：三台**独立数据库服务**（跨两个节点）上的分域落库与验收。

拓扑（默认）：
  mgmt     → 本机 mysqld 3311（socket /tmp/mysql-mgmt.sock）
  insight  → 本机 mysqld 3312（socket /tmp/mysql-insight.sock）
  runtime  → 兄弟节点 192.168.0.116:3306（MariaDB 10.11）

每个实例只建一个域库 + 一个"只授本库"的应用账号；数据来自参照库（默认 192.168.0.59）
的**基表**（视图一律不复制——独立实例后不可能存在跨域视图）。

幂等：重复执行会 DROP/CREATE 域库并重新灌数据；不改参照库。
输出：`~/.grape-rds-stage.env`（0600）写入 GRAPE_DSN_* + P4 拓扑说明，供冒烟/验收复用。

用法：
  python3 scripts/rds_p4_provision.py --source-host <h> --source-user root --source-password *** \
      --remote-host 192.168.0.116 --remote-user rds_admin --remote-password ***
  --skip-load   只重建账号/库，不灌数据（快速重跑验收）
"""
from __future__ import annotations

import argparse
import os
import secrets
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

DOMAINS = {"mgmt": "grape_mgmt_db", "runtime": "grape_runtime_db", "insight": "grape_insight_db"}
ENV_OUT = Path.home() / ".grape-rds-stage.env"


@dataclass
class Target:
    domain: str
    host: str
    port: int
    socket: str = ""
    admin_user: str = "root"
    admin_password: str = ""


def connect(t: Target):
    import pymysql

    kwargs = dict(user=t.admin_user, password=t.admin_password or None, charset="utf8mb4",
                  connect_timeout=10, read_timeout=600, autocommit=True)
    if t.socket:
        kwargs.update(unix_socket=t.socket, host=None)
    else:
        kwargs.update(host=t.host, port=t.port)
    return pymysql.connect(**kwargs)


def q(conn, sql, args=None):
    with conn.cursor() as c:
        # 无参数时不能传空元组：SQL 里含 MySQL 账号 host 的 '%'，会被当成格式符
        if args:
            c.execute(sql, args)
        else:
            c.execute(sql)
        try:
            return c.fetchall()
        except Exception:  # noqa: BLE001 - DDL 无结果集
            return []


def base_tables(conn, db: str) -> list[str]:
    rows = q(conn, "SELECT table_name AS t FROM information_schema.tables "
                   "WHERE table_schema=%s AND table_type='BASE TABLE' ORDER BY table_name", (db,))
    return [r[0] for r in rows]


def views(conn, db: str) -> list[str]:
    rows = q(conn, "SELECT table_name AS t FROM information_schema.views "
                   "WHERE table_schema=%s ORDER BY table_name", (db,))
    return [r[0] for r in rows]


def counts(conn, db: str, tables: list[str]) -> dict[str, int]:
    out = {}
    for t in tables:
        out[t] = int(q(conn, f"SELECT COUNT(*) FROM `{db}`.`{t}`")[0][0])
    return out


def dump_load(src_host: str, src_user: str, src_pwd: str, src_db: str, tgt: Target,
              tgt_db: str, exclude: list[str]) -> None:
    env = dict(os.environ, MYSQL_PWD=src_pwd)
    args = ["mysqldump", f"-h{src_host}", f"-u{src_user}", "--single-transaction",
            "--no-create-db", "--set-gtid-purged=OFF", "--skip-add-locks",
            "--default-character-set=utf8mb4"]
    for v in exclude:
        args.append(f"--ignore-table={src_db}.{v}")
    args.append(src_db)
    dump = subprocess.run(args, env=env, capture_output=True)
    if dump.returncode != 0:
        raise RuntimeError(f"mysqldump {src_db}: {dump.stderr.decode()[:300]}")
    # 灌数据属迁移动作：用迁移/管理账号（方案 G1：迁移账号只在迁移期使用）；
    # 应用账号只授 DML，正好用来证明"搬完后应用账号不需要 DDL"。
    if tgt.socket:
        load_env = dict(os.environ)
        load_args = ["mysql", "-S", tgt.socket, f"-u{tgt.admin_user}", tgt_db]
    else:
        load_env = dict(os.environ, MYSQL_PWD=tgt.admin_password)
        load_args = ["mysql", f"-h{tgt.host}", f"-P{tgt.port}", f"-u{tgt.admin_user}", tgt_db]
    load = subprocess.run(load_args, env=load_env, input=dump.stdout, capture_output=True)
    if load.returncode != 0:
        raise RuntimeError(f"load {tgt_db}: {load.stderr.decode()[:300]}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="P4/P5 三实例彩排（跨两节点）")
    ap.add_argument("--source-host", required=True)
    ap.add_argument("--source-user", default="root")
    ap.add_argument("--source-password", default=os.environ.get("MYSQL_PWD", ""))
    ap.add_argument("--remote-host", required=True)
    ap.add_argument("--remote-port", type=int, default=3306)
    ap.add_argument("--remote-user", default="rds_admin")
    ap.add_argument("--remote-password", default="")
    ap.add_argument("--prefix", default="grape_stage")
    ap.add_argument("--local-mgmt-port", type=int, default=3311)
    ap.add_argument("--local-insight-port", type=int, default=3312)
    ap.add_argument("--skip-load", action="store_true")
    args = ap.parse_args(argv)
    if not args.source_password:
        print("[FATAL] 需要 --source-password 或 MYSQL_PWD", file=sys.stderr)
        return 2

    targets = {
        "mgmt": Target("mgmt", "127.0.0.1", args.local_mgmt_port, socket="/tmp/mysql-mgmt.sock"),
        "insight": Target("insight", "127.0.0.1", args.local_insight_port,
                          socket="/tmp/mysql-insight.sock"),
        "runtime": Target("runtime", args.remote_host, args.remote_port,
                          admin_user=args.remote_user, admin_password=args.remote_password),
    }
    src = None
    import pymysql  # noqa: F401 - 早失败好过中途失败

    env_lines, problems = [], []
    src = pymysql.connect(host=args.source_host, user=args.source_user,
                          password=args.source_password, charset="utf8mb4",
                          connect_timeout=10, read_timeout=600)
    for domain, target in targets.items():
        src_db, stage_db = DOMAINS[domain], f"{args.prefix}_{domain}"
        app_user, app_pwd = f"{args.prefix}_{domain}_app", "Aa1!" + secrets.token_urlsafe(18)
        admin = connect(target)
        q(admin, f"DROP DATABASE IF EXISTS `{stage_db}`")
        q(admin, f"CREATE DATABASE `{stage_db}` DEFAULT CHARACTER SET utf8mb4")
        q(admin, "DROP USER IF EXISTS %s@'%%'", (app_user,))
        # 口令内联：由本脚本生成（Aa1!+urlsafe，满足 validate_password 常见策略）
        q(admin, f"CREATE USER '{app_user}'@'%' IDENTIFIED BY '{app_pwd}'")
        q(admin, f"GRANT SELECT,INSERT,UPDATE,DELETE ON `{stage_db}`.* TO '{app_user}'@'%'")
        q(admin, "FLUSH PRIVILEGES")

        v = views(src, src_db)
        src_tables = base_tables(src, src_db)
        before = counts(src, src_db, src_tables)
        print(f"[{domain}] {args.source_host}/{src_db} → {target.host}:{target.port}/{stage_db}"
              f"（排除 {len(v)} 视图，账号 {app_user}@% 仅本库）")
        if not args.skip_load:
            dump_load(args.source_host, args.source_user, args.source_password, src_db,
                      target, stage_db, v)
        dst_tables = base_tables(admin, stage_db)
        if dst_tables != src_tables:
            missing = sorted(set(src_tables) - set(dst_tables))[:5]
            problems.append(f"{domain}: 表集合不一致 missing={missing}")
        if views(admin, stage_db):
            problems.append(f"{domain}: 仍有视图 {views(admin, stage_db)[:3]}")
        if not args.skip_load:
            after = counts(admin, stage_db, dst_tables)
            diff = [(t, before[t], after.get(t)) for t in src_tables if after.get(t) != before[t]]
            if diff:
                problems.append(f"{domain}: 行数差异 {diff[:3]}")
        grants = q(admin, f"SHOW GRANTS FOR '{app_user}'@'%'")
        foreign = [g[0] for g in grants
                   if any(f"`{d}`" in g[0] for d2, d in DOMAINS.items()
                          if d2 != domain and f"`{d}`" in g[0])]
        if foreign:
            problems.append(f"{domain}: 跨库授权 {foreign[:2]}")
        print(f"[{domain}] 表 {len(dst_tables)} / 视图 {len(views(admin, stage_db))} / 授权仅本库="
              f"{'是' if not foreign else '否'} / 目标 {target.host}:{target.port}")
        env_lines.append(f"GRAPE_DSN_{domain.upper()}=mysql://{app_user}:{app_pwd}@"
                         f"{target.host}:{target.port}/{stage_db}")
        admin.close()
    src.close()

    # 三库拓扑必须开 lib 开关：否则 db_registry.uses_own_dsn() 恒为 False，
    # 所有「任意拓扑」原语会回落进程库（mgmt 库）→ 查 runtime/insight 表全 1146。
    env_lines.append("GRAPE_DOMAIN_LIB=1")
    ENV_OUT.write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    ENV_OUT.chmod(0o600)
    print(f"\nstage DSN → {ENV_OUT}（0600）")
    if problems:
        print(f"RESULT: FAIL（{len(problems)} 项）", file=sys.stderr)
        for p in problems:
            print("  ✗ " + p, file=sys.stderr)
        return 1
    print("RESULT: PASS（三实例/两节点：表集合一致、行数等于 dump 快照、视图 0、授权仅本库）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())