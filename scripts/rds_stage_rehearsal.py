#!/usr/bin/env python3
"""三库独立化 · 分域隔离彩排（P4/P5 的可重复演练）。

在**同一台 MySQL 实例上用三个独立库 + 三个只授本库权限的账号**模拟"实例隔离"：
  · 结构/数据从现网三库复制（**只复制基表，视图一律不复制**——独立实例后视图不可能存在）；
  · 每域一个账号，`GRANT SELECT,INSERT,UPDATE,DELETE ON <own_db>.*`，不给其他库；
  · 校验：表集合一致、逐表行数一致、视图数 = 0、`SHOW GRANTS` 只含本库。

这验证的是 P4 的搬迁口径（schema/data 复制 + 校验）与 P5 的验收口径（视图 0 / 授权仅本库），
**不替代**真正的三实例网络隔离（那需要云上实例；见 docs/weekly-11/independent-rds-upgrade-plan.md §8）。

用法：
  python3 scripts/rds_stage_rehearsal.py --host <src-host> --user root --password <pwd> \
      [--prefix grape_stage] [--drop]        # --drop: 彩排后删除 stage 库与账号

环境变量：MYSQL_PWD 优先于 --password（避免命令行泄露）。
输出：校验报告（stdout）；stage 账号口令写入 `~/.grape-rds-stage.env`（0600），不进仓库。
"""
from __future__ import annotations

import argparse
import os
import secrets
import subprocess
import sys
from pathlib import Path

DOMAINS = {"mgmt": "grape_mgmt_db", "runtime": "grape_runtime_db", "insight": "grape_insight_db"}
ENV_OUT = Path.home() / ".grape-rds-stage.env"


def _connect(host: str, port: int, user: str, password: str):
    import pymysql

    return pymysql.connect(host=host, port=port, user=user, password=password,
                           charset="utf8mb4", connect_timeout=10, read_timeout=600)


def base_tables(conn, db: str) -> list[str]:
    with conn.cursor() as c:
        c.execute("SELECT table_name FROM information_schema.tables "
                  "WHERE table_schema=%s AND table_type='BASE TABLE' ORDER BY table_name", (db,))
        return [r[0] for r in c.fetchall()]


def views(conn, db: str) -> list[str]:
    with conn.cursor() as c:
        c.execute("SELECT table_name FROM information_schema.views "
                  "WHERE table_schema=%s ORDER BY table_name", (db,))
        return [r[0] for r in c.fetchall()]


def row_counts(conn, db: str, tables: list[str]) -> dict[str, int]:
    out = {}
    with conn.cursor() as c:
        for t in tables:
            c.execute(f"SELECT COUNT(*) FROM `{db}`.`{t}`")
            out[t] = int(c.fetchone()[0])
    return out


def dump_load(host: str, port: int, user: str, password: str, src_db: str,
              dst_db: str, exclude_views: list[str]) -> None:
    """mysqldump 单库基表 → mysql 导入（视图用 --ignore-table 排除）。"""
    env = dict(os.environ, MYSQL_PWD=password)
    args = ["mysqldump", f"-h{host}", f"-P{port}", f"-u{user}",
            "--single-transaction", "--no-create-db", "--set-gtid-purged=OFF",
            "--skip-add-locks", "--default-character-set=utf8mb4"]
    for v in exclude_views:
        args.append(f"--ignore-table={src_db}.{v}")
    args.append(src_db)
    dump = subprocess.run(args, env=env, capture_output=True)
    if dump.returncode != 0:
        raise RuntimeError(f"mysqldump {src_db} 失败：{dump.stderr.decode()[:400]}")
    load = subprocess.run(["mysql", f"-h{host}", f"-P{port}", f"-u{user}", dst_db],
                          env=env, input=dump.stdout, capture_output=True)
    if load.returncode != 0:
        raise RuntimeError(f"import {dst_db} 失败：{load.stderr.decode()[:400]}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="三库独立化彩排（独立库 + 只授本库账号）")
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, default=3306)
    ap.add_argument("--user", default="root")
    ap.add_argument("--password", default=os.environ.get("MYSQL_PWD", ""))
    ap.add_argument("--prefix", default="grape_stage")
    ap.add_argument("--drop", action="store_true", help="彩排后删除 stage 库与账号")
    args = ap.parse_args(argv)
    if not args.password:
        print("[FATAL] 需要 --password 或 MYSQL_PWD", file=sys.stderr)
        return 2

    src = _connect(args.host, args.port, args.user, args.password)
    problems: list[str] = []
    env_lines: list[str] = []
    stage_dbs: dict[str, str] = {}
    for domain, src_db in DOMAINS.items():
        stage_db = f"{args.prefix}_{domain}"
        stage_dbs[domain] = stage_db
        user = f"{args.prefix}_{domain}_app"
        # 满足 MySQL validate_password 常见策略（大小写+数字+符号）：
        # 固定前缀保证复杂度，后缀取足熵；避免纯 base64url 触发 1819 策略拒绝。
        pwd = "Aa1!" + secrets.token_urlsafe(18)
        env_lines.append(f"GRAPE_DSN_{domain.upper()}="
                         f"mysql://{user}:{pwd}@{args.host}:{args.port}/{stage_db}")
        with src.cursor() as c:
            c.execute(f"DROP DATABASE IF EXISTS `{stage_db}`")
            c.execute(f"CREATE DATABASE `{stage_db}` DEFAULT CHARACTER SET utf8mb4")
            c.execute(f"DROP USER IF EXISTS '{user}'@'%'")
            # user/pwd 均由本脚本生成（token_urlsafe：A-Za-z0-9-_），直接内联避免
            # pymysql 的 %-format 与 MySQL 账号 host 的 '%' 冲突
            c.execute(f"CREATE USER '{user}'@'%' IDENTIFIED BY '{pwd}'")
            c.execute(f"GRANT SELECT,INSERT,UPDATE,DELETE ON `{stage_db}`.* TO '{user}'@'%'")
        v = views(src, src_db)
        # 先取源行数快照：dump 用 --single-transaction，导入后应与"dump 前"一致；
        # 若与"导入后现网"比，会被复制窗口内的实时写入干扰（实测 obs_* 每秒都写）。
        src_tables_pre = base_tables(src, src_db)
        src_rows = row_counts(src, src_db, src_tables_pre)
        print(f"[{domain}] {src_db} → {stage_db}（排除 {len(v)} 个视图，账号 {user} 仅本库）")
        dump_load(args.host, args.port, args.user, args.password, src_db, stage_db, v)

        # 校验
        stage = _connect(args.host, args.port, args.user, args.password)
        try:
            src_tables = base_tables(src, src_db)
            dst_tables = base_tables(stage, stage_db)
            if src_tables != dst_tables:
                missing = sorted(set(src_tables) - set(dst_tables))
                extra = sorted(set(dst_tables) - set(src_tables))
                problems.append(f"{domain}: 表集合不一致 missing={missing[:5]} extra={extra[:5]}")
            if views(stage, stage_db):
                problems.append(f"{domain}: stage 库仍含视图 {views(stage, stage_db)[:5]}")
            dst_rows = row_counts(stage, stage_db, dst_tables)
            diff = [(t, src_rows[t], dst_rows.get(t)) for t in src_tables
                    if dst_rows.get(t) != src_rows[t]]
            if diff:
                problems.append(f"{domain}: 行数差异 {diff[:5]}")
            with stage.cursor() as c:
                c.execute(f"SHOW GRANTS FOR '{user}'@'%'")
                grants = [r[0] for r in c.fetchall()]
                foreign = [g for g in grants
                           if any(db in g for db in DOMAINS.values() if db != src_db)
                           or any(f"{args.prefix}_{d}" in g for d in DOMAINS if d != domain)]
            if foreign:
                problems.append(f"{domain}: 账号存在跨库授权 {foreign[:3]}")
            print(f"[{domain}] 校验：基表 {len(dst_tables)} 张 / 行数据一致="
                  f"{'是' if not diff else '否'} / 视图 {len(views(stage, stage_db))} / 授权仅本库="
                  f"{'是' if not foreign else '否'}")
        finally:
            stage.close()

    src.close()
    if env_lines:
        ENV_OUT.write_text("\n".join(env_lines) + "\n", encoding="utf-8")
        ENV_OUT.chmod(0o600)
        print(f"\nstage DSN 已写入 {ENV_OUT}（0600，勿提交）")

    if args.drop:
        src = _connect(args.host, args.port, args.user, args.password)
        with src.cursor() as c:
            for domain, stage_db in stage_dbs.items():
                c.execute(f"DROP DATABASE IF EXISTS `{stage_db}`")
                c.execute(f"DROP USER IF EXISTS '{args.prefix}_{domain}_app'@'%'")
        src.close()
        print("已删除 stage 库与账号（--drop）")

    if problems:
        print(f"\nRESULT: FAIL（{len(problems)} 项）", file=sys.stderr)
        for p in problems:
            print("  ✗ " + p, file=sys.stderr)
        return 1
    print("\nRESULT: PASS（三域：表集合/行数一致、视图 0、授权仅本库）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())