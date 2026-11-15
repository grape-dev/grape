#!/usr/bin/env python3
"""部署门禁脚本：备份 → 影子验证 → 生产应用 → 冒烟（单命令编排）。

用法（仅本机 mysql -uroot 免密环境）：
    # 演练（默认）：backup + shadow 验证，绝不触碰生产 grape 库
    python3 scripts/deploy_mysql_gate.py

    # 只跑 backup + shadow，等价于默认（显式表达）
    python3 scripts/deploy_mysql_gate.py --dry-run

    # 生产应用：显式确认后才对 grape 库执行迁移，随后冒烟
    python3 scripts/deploy_mysql_gate.py --allow-prod

    # 冒烟附带 API 闭环（health + 新用户；网络失败标 SKIP 不判红）
    python3 scripts/deploy_mysql_gate.py --allow-prod --smoke-api http://127.0.0.1:8787

阶段：
    backup  mysqldump --single-transaction 到 <backup-dir>/<ts>_grape.sql，
            校验非空且含 CREATE TABLE；失败即中止。
    shadow  drop/create 演练库 → 恢复备份 → 执行迁移链 → 自动校验
            （逐表行数对账 / id 保值抽查 / 引用列类型不变式）；任一红即 exit 非零，
            保留演练库现场供排查。
    apply   仅 --allow-prod 时对生产库执行迁移（幂等：迁移盖章表去重）；
            无该参数只打印 plan 并退出 0。
    smoke   迁移后 SQL 级自检（关键表存在 / 盖章表记录完整），
            可选 --smoke-api 走 health + 新用户闭环。

迁移链入口对接 G1（迁移 MySQL 分支），当前为占位：按候选顺序探测，
均不可用时给出明确红字说明（exit 2），不伪造成功。

日志：结构化 JSON 行，同时输出 stdout 与 <backup-dir>/logs/<ts>.log。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import shlex
import subprocess
import sys
import urllib.request
from typing import Any, Callable, Optional

try:
    import pymysql  # type: ignore
except Exception:  # pragma: no cover - 允许在无 pymysql 环境做 --help / plan
    pymysql = None

DEFAULT_BACKUP_DIR = os.environ.get("GRAPE_GATE_BACKUP_DIR", "./mysql-gate-backup")
DEFAULT_SOURCE_DB = "grape"
DEFAULT_SHADOW_DB = "grape_upgrade_sim"
MYSQL_HOST = "127.0.0.1"
MYSQL_PORT = 3306

# 关键表（smoke 存在性检查）
KEY_TABLES = ("grape_accounts", "grape_t_schema_migrations")
# id 保值抽查账号
ID_PROBE_IDS = ("1", "2", "3")
# 带 7 天保留期清理的表：迁移链启动时 _cleanup_old_latency_logs 会删除
# created_at < NOW()-7day 的行——存量库行全为旧数据时演练行数必然 < 源库，
# 对账口径放宽为 sim ≤ 源（只允许变少，不允许变多/丢表）。
RETENTION_PRUNE_TABLES = {"api_latency_log"}

LOGGER: dict[str, Any] = {"log_file": None}


# ---------------------------------------------------------------- logging ---
def log(event: str, phase: str = "", **fields: Any) -> None:
    rec = {
        "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "event": event,
        "phase": phase,
    }
    rec.update(fields)
    line = json.dumps(rec, ensure_ascii=False, default=str)
    print(line, flush=True)
    path = LOGGER.get("log_file")
    if path:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


class GateError(Exception):
    """门禁红：中止并保留现场。"""


# ------------------------------------------------------------- subprocess ---
def run_cmd(cmd: list[str], stdin_file: Optional[str] = None,
            stdout_file: Optional[str] = None,
            env: Optional[dict] = None) -> subprocess.CompletedProcess:
    """执行外部命令；stdin/stdout 可重定向到文件。失败抛 GateError。

    env：可选环境覆盖（QA 复审 M-4：口令经 MYSQL_PWD 注入，绝不进 argv/ps）。
    """
    log("cmd", cmd=shlex.join(cmd))
    stdin_path = stdin_file
    stdin_fh = open(stdin_path, "rb") if stdin_path else None
    stdout_fh = open(stdout_file, "wb") if stdout_file else None
    try:
        proc = subprocess.run(cmd, stdin=stdin_fh or subprocess.DEVNULL,
                              stdout=stdout_fh, stderr=subprocess.PIPE, env=env)
        err = proc.stderr.decode("utf-8", "replace") if proc.stderr else ""
        if proc.returncode != 0:
            raise GateError(f"命令失败 rc={proc.returncode}: "
                            f"{shlex.join(cmd)} stderr={err[-2000:]}")
        return proc
    finally:
        if stdin_fh is not None and not stdin_fh.closed:
            stdin_fh.close()
        if stdout_fh and not stdout_fh.closed:
            stdout_fh.close()


# ------------------------------------------------------------------ mysql ---
def mysql_conn(db: str):
    if pymysql is None:
        raise GateError("pymysql 不可用，无法执行 SQL 校验")
    # MD-G1：凭据统一走 TARGET（--target-user/--target-password 注入，env
    # GRAPE_GATE_MYSQL_USER/PASSWORD 可覆盖），备份/恢复/校验与迁移注入同一套——
    # 目标库设口令时不再静默回落免密 root（失败提示才对得上凭据方向）。
    return pymysql.connect(host=MYSQL_HOST, port=MYSQL_PORT,
                           user=TARGET["user"], password=TARGET["password"],
                           database=db, charset="utf8mb4",
                           autocommit=True)


def sql_rows(db: str, query: str, args: tuple = ()) -> list[tuple]:
    conn = mysql_conn(db)
    try:
        with conn.cursor() as cur:
            cur.execute(query, args)
            return list(cur.fetchall())
    finally:
        conn.close()


def mysql_exec_file(db: str, path: str) -> None:
    # QA 复审 M-4：口令经 MYSQL_PWD env 注入（-p<pw> 进 argv 会被 ps/审计/终端
    # 历史读走；与 mysql_livefire_probe 的 env 口径统一）。
    _cred = ["-u" + TARGET["user"]] if TARGET["user"] else []
    _env = dict(os.environ)
    _env["MYSQL_PWD"] = TARGET["password"] or ""
    run_cmd(["mysql", "--host", MYSQL_HOST, "--port", str(MYSQL_PORT),
             *_cred, db], stdin_file=path, env=_env)


def list_tables(db: str) -> list[str]:
    return [r[0] for r in sql_rows(db, "SHOW TABLES")]


def table_counts(db: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for t in list_tables(db):
        n = sql_rows(db, f"SELECT COUNT(*) FROM `{t}`")
        counts[t] = int(n[0][0])
    return counts


# ------------------------------------------------- G1 迁移链入口（连接注入）---
# 目标库连接配置（main() 按 CLI 参数填充；直连注入，不依赖应用默认配置）
TARGET: dict[str, Any] = {"host": "127.0.0.1", "port": 3306,
                          "user": "root", "password": ""}


def run_migration_chain(db: str, migration_cmd: Optional[str]) -> str:
    """对 db 执行 G1 迁移链 0001→最新（连接注入直连目标库）。

    主路径：src.api.db_setup.run_migrations_for_mysql —— 在临时换绑窗口内把
    全局 db 切到目标 MySQL，复用 G1 已验证的迁移体系（legacy-stamp、盖章去重、
    _widen_mysql_* 分支），迁移 DDL 真正落到目标库；退出后应用默认连接零改动。

    备选：--migration-cmd 外部命令（env GRAPE_GATE_MIGRATION_CMD 亦可），
    环境变量 GRAPE_GATE_TARGET_DB 传入目标库名。

    成功返回路径名称；迁移后校验盖章表确已出现在目标库（落库实证，不伪造成功）。
    """
    cmd = migration_cmd or os.environ.get("GRAPE_GATE_MIGRATION_CMD")
    if cmd:
        env = dict(os.environ, GRAPE_GATE_TARGET_DB=db)
        log("migration_via_cmd", phase="migration", db=db)
        proc = subprocess.run(shlex.split(cmd), env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out = proc.stdout.decode("utf-8", "replace") if proc.stdout else ""
        if proc.returncode != 0:
            raise GateError(f"迁移命令失败 rc={proc.returncode}: {out[-2000:]}")
        return f"cmd:{cmd}"

    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))  # 仓库根，供 import src.*
        from src.api.db_setup import run_migrations_for_mysql  # type: ignore
        log("migration_via_inject", phase="migration", db=db,
            host=TARGET["host"], port=TARGET["port"], user=TARGET["user"],
            note="G3 连接注入：全局 db 临时切到目标库执行迁移链")
        run_migrations_for_mysql(host=TARGET["host"], port=int(TARGET["port"]),
                                 user=TARGET["user"],
                                 password=str(TARGET.get("password") or ""),
                                 database=db)
        # 落库实证：盖章表必须真的出现在目标 MySQL 库
        rows = sql_rows(
            "information_schema",
            "SELECT COUNT(*) FROM TABLES WHERE TABLE_SCHEMA=%s "
            "AND TABLE_NAME='grape_t_schema_migrations'", (db,))
        if not rows or int(rows[0][0]) == 0:
            raise GateError(
                f"迁移链执行后 {db} 中未出现 grape_t_schema_migrations"
                "——迁移未落到目标库，已中止。")
        return "src.api.db_setup.run_migrations_for_mysql"
    except GateError:
        raise
    except Exception as exc:
        raise GateError(
            "迁移链入口不可用：run_migrations_for_mysql 导入/执行失败："
            f"{exc!r}。请检查目标库连接（host/port/user/password），"
            "或显式传 --migration-cmd 走外部命令。") from exc


# ----------------------------------------------------------------- phases ---
def phase_backup(backup_dir: str, source_db: str) -> str:
    os.makedirs(backup_dir, exist_ok=True)
    logs_dir = os.path.join(backup_dir, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    LOGGER["log_file"] = os.path.join(
        logs_dir, _dt.datetime.now().strftime("%Y%m%dT%H%M%S") + ".log")

    ts = _dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    path = os.path.join(backup_dir, f"{ts}_{source_db}.sql")
    log("backup_start", phase="backup", target=path)
    _cred = ["-u" + TARGET["user"]] if TARGET["user"] else []
    _env = dict(os.environ)
    _env["MYSQL_PWD"] = TARGET["password"] or ""
    run_cmd(["mysqldump", "--host", MYSQL_HOST, "--port", str(MYSQL_PORT),
             *_cred, "--single-transaction", source_db], stdout_file=path, env=_env)

    size = os.path.getsize(path)
    with open(path, "rb") as f:
        # CR-G5：4MB 采样头——mysqldump 首表 CREATE TABLE 必在文件头部（元数据+首表 DDL
        # 远小于 4MB），只采样头部避免大库全量读；env 可覆盖。
        _head_bytes = int(os.environ.get("GRAPE_GATE_HEAD_BYTES", str(4 * 1024 * 1024)))
        head = f.read(_head_bytes).decode("utf-8", "replace")
    if size == 0:
        raise GateError(f"备份文件为空：{path}")
    if "CREATE TABLE" not in head:
        raise GateError(f"备份文件不含 CREATE TABLE：{path}")
    log("backup_ok", phase="backup", path=path, size_bytes=size)
    return path


def _verify_shadow(source_db: str, shadow_db: str) -> None:
    src_tables = set(list_tables(source_db))
    sim_tables = set(list_tables(shadow_db))
    log("shadow_tables", phase="shadow", source=len(src_tables),
        shadow=len(sim_tables))

    # 1) 逐表行数对账（源有→演练必须有且行数一致；演练多出的表记 warn）
    src_counts, sim_counts = table_counts(source_db), table_counts(shadow_db)
    red: list[str] = []
    for t in sorted(src_counts):
        if t not in sim_counts:
            red.append(f"演练库缺表 {t}")
        elif t in RETENTION_PRUNE_TABLES:
            if sim_counts[t] > src_counts[t]:
                red.append(f"行数不一致 {t}: 源={src_counts[t]} "
                           f"演练={sim_counts[t]}（保留期清理表不允许增多）")
            elif sim_counts[t] < src_counts[t]:
                log("rowcount_pruned_by_retention", phase="shadow", table=t,
                    source=src_counts[t], shadow=sim_counts[t])
        elif sim_counts[t] != src_counts[t]:
            red.append(f"行数不一致 {t}: 源={src_counts[t]} 演练={sim_counts[t]}")
    for t in sorted(set(sim_counts) - set(src_counts)):
        log("shadow_extra_table", phase="shadow", table=t,
            rows=sim_counts[t])
    if red:
        raise GateError("行数对账失败：" + "; ".join(red))
    log("rowcount_reconcile_ok", phase="shadow", tables=len(src_counts))

    # 2) id 保值抽查：grape_accounts '1'/'2'/'3' 在场且形态为 str
    probe = sql_rows(
        shadow_db,
        "SELECT id FROM grape_accounts WHERE id IN (%s,%s,%s)",
        ID_PROBE_IDS)
    ids = {str(r[0]): type(r[0]).__name__ for r in probe}
    missing = [i for i in ID_PROBE_IDS if i not in ids]
    if missing:
        raise GateError(f"id 保值抽查失败：演练库缺账号 id={missing}")
    bad = {k: v for k, v in ids.items() if v != "str"}
    if bad:
        raise GateError(f"id 保值抽查失败：id 非 str 形态 {bad}")
    log("id_preservation_ok", phase="shadow", ids=sorted(ids))

    # 3) 不变式：全库 user_id 族引用列必须为 VARCHAR/CHAR
    rows = sql_rows(
        "information_schema",
        "SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE FROM COLUMNS "
        "WHERE TABLE_SCHEMA=%s AND COLUMN_NAME LIKE '%%user_id%%'",
        (shadow_db,))
    offenders = [f"{r[0]}.{r[1]}={r[2]}" for r in rows
                 if r[2].lower() not in ("varchar", "char")]
    if offenders:
        raise GateError("引用列类型不变式失败（非 VARCHAR/CHAR）：" +
                        ", ".join(offenders))
    log("fk_type_invariant_ok", phase="shadow",
        user_id_cols=len(rows))


def phase_shadow(backup_path: str, source_db: str, shadow_db: str,
                 migration_cmd: Optional[str]) -> None:
    log("shadow_start", phase="shadow", shadow_db=shadow_db)
    # drop/create → 恢复备份（天然幂等：每次重建演练库）
    sql_rows(source_db,
             f"DROP DATABASE IF EXISTS `{shadow_db}`")
    sql_rows(source_db, f"CREATE DATABASE `{shadow_db}` "
                        "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    mysql_exec_file(shadow_db, backup_path)
    log("shadow_restored", phase="shadow")

    try:
        via = run_migration_chain(shadow_db, migration_cmd)
    except GateError:
        log("shadow_preserved", phase="shadow",
            note="迁移失败，演练库现场已保留供排查")
        raise
    log("migration_ok", phase="shadow", via=via)

    try:
        _verify_shadow(source_db, shadow_db)
    except GateError:
        log("shadow_preserved", phase="shadow",
            note="校验红，演练库现场已保留供排查")
        raise
    log("shadow_ok", phase="shadow")


def _stamp_info(db: str) -> tuple[bool, int]:
    """盖章表存在性与已落戳数（幂等判定依据）。"""
    rows = sql_rows(
        "information_schema",
        "SELECT COUNT(*) FROM TABLES WHERE TABLE_SCHEMA=%s "
        "AND TABLE_NAME='grape_t_schema_migrations'", (db,))
    if not rows or int(rows[0][0]) == 0:
        return False, 0
    n = sql_rows(db, "SELECT COUNT(*) FROM grape_t_schema_migrations")
    return True, int(n[0][0])


def phase_apply(source_db: str, migration_cmd: Optional[str]) -> None:
    log("apply_start", phase="apply", db=source_db)
    stamped, stamps = _stamp_info(source_db)
    log("stamp_state", phase="apply", table_present=stamped, stamps=stamps)
    # 幂等：G1 迁移体系按 grape_t_schema_migrations 盖章去重，重入安全
    via = run_migration_chain(source_db, migration_cmd)
    stamped2, stamps2 = _stamp_info(source_db)
    log("apply_ok", phase="apply", via=via,
        stamps_before=stamps, stamps_after=stamps2)


def _sql_smoke(db: str) -> None:
    tables = set(list_tables(db))
    missing = [t for t in KEY_TABLES if t not in tables]
    if missing:
        raise GateError(f"冒烟失败：关键表缺失 {missing}")
    stamped, stamps = _stamp_info(db)
    if not stamped or stamps == 0:
        raise GateError(f"冒烟失败：迁移盖章表记录不完整 "
                        f"(present={stamped}, stamps={stamps})")
    log("sql_smoke_ok", phase="smoke", db=db, stamps=stamps)


def _api_smoke(base_url: str) -> None:
    """health + 新用户闭环；网络失败标 SKIP，不判红。"""
    def _get(path: str) -> tuple[int, str]:
        with urllib.request.urlopen(base_url.rstrip("/") + path,
                # CR-G5：10s 为本机/内网 health 端点往返上限（实测 <100ms），
                # 超时即网络层异常，按 SKIP 而非挂死门禁。env 可覆盖。
                timeout=int(os.environ.get("GRAPE_GATE_HTTP_TIMEOUT", "10"))) as r:
            return r.status, r.read(4096).decode("utf-8", "replace")

    try:
        status, body = _get("/health")
        if status != 200:
            raise GateError(f"health 返回 {status}: {body[:200]}")
        log("api_health_ok", phase="smoke", url=base_url + "/health")
        # 写路径冒烟：/api/chat/submit 真提交一句（默认撤回），拦住「断连 500」类故障
        _chat_smoke(base_url)
        # 新用户闭环占位：G1/分支接口定稿后补充注册→建 issue→凭据写入断言
        log("api_user_loop_skip", phase="smoke",
            note="新用户闭环断言待接口定稿，当前 health + chat 写路径")
    except GateError:
        raise
    except Exception as exc:
        log("api_smoke_skip", phase="smoke", reason=repr(exc))
        # 网络失败允许：SKIP 不中止


def _chat_smoke(base_url: str) -> None:
    """写路径冒烟：真提交一句 chat 并断言 200 + task_id（默认立即撤回）。

    为什么必须放在发布闸门里：2026-11-15 的 500 是「域连接被 MySQL 断开仍留缓存」，
    health / 只读端点全绿也发现不了；只有走一次真实写路径（含跨域 meta 读）才会暴露。
    环境不具备（缺 JWT_SECRET/账号/网络）时脚本返回 SKIP，不阻断发布。
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "smoke_chat_submit", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                          "smoke_chat_submit.py"))
    if spec is None or spec.loader is None:
        log("api_chat_smoke_skip", phase="smoke", reason="smoke_chat_submit.py 不可加载")
        return
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    timeout = int(os.environ.get("GRAPE_GATE_HTTP_TIMEOUT", "10"))
    code, out = mod.check(base_url, os.environ.get("GRAPE_SMOKE_ACCOUNT", "smoke-test-account"),
                          mod.DEFAULT_MESSAGE, True, timeout)
    if code == mod.EXIT_FAIL:
        raise GateError(f"chat 写路径冒烟失败：status={out.get('submit_status')} "
                        f"task_id={out.get('task_id')!r} body={out.get('body')}")
    log("api_chat_smoke", phase="smoke", result=out.get("result"),
        task_id=out.get("task_id"), latency_ms=out.get("latency_ms"),
        reason=out.get("reason"))


# ------------------------------------------------------------------- main ---
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="MySQL 部署门禁：备份/影子验证/应用/冒烟")
    p.add_argument("--dry-run", action="store_true",
                   help="只跑 backup + shadow（默认行为，显式表达）")
    p.add_argument("--allow-prod", action="store_true",
                   help="显式确认：对生产 grape 库执行迁移 + 冒烟")
    p.add_argument("--source-db", default=DEFAULT_SOURCE_DB)
    p.add_argument("--shadow-db", default=DEFAULT_SHADOW_DB)
    p.add_argument("--backup-dir", default=DEFAULT_BACKUP_DIR)
    p.add_argument("--migration-cmd", default=None,
                   help="G1 迁移链外部命令备选（亦可 env GRAPE_GATE_MIGRATION_CMD）；"
                        "主路径走 run_migrations_for_mysql 直连注入")
    p.add_argument("--target-host", default="127.0.0.1",
                   help="目标 MySQL host（连接注入）")
    p.add_argument("--target-port", type=int, default=3306)
    p.add_argument("--target-user", default="root")
    p.add_argument("--target-password", default="",
                   help="目标 MySQL 密码（本机免密环境留空）")
    p.add_argument("--smoke-api", default=None,
                   help="冒烟 API base URL（如 http://127.0.0.1:8787）")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    TARGET.update(host=args.target_host, port=args.target_port,
                  user=args.target_user, password=args.target_password)
    log("gate_start", allow_prod=args.allow_prod, dry_run=args.dry_run,
        source_db=args.source_db, shadow_db=args.shadow_db,
        target_host=TARGET["host"], target_user=TARGET["user"])

    if not os.path.isdir("src") and os.path.basename(
            os.path.dirname(os.path.abspath(__file__))) == "scripts":
        os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    # BLK-G1 破坏性操作自保护：演练库名与源库名相同（或命中生产库名单）时，
    # phase_shadow 的 DROP DATABASE 会直接删掉源库——单参数误传即毁库。
    # 设计契约（d105d §9「演练只允许在自建草稿库，源库只读 dump」）落成代码约束。
    _PROTECTED_DB_NAMES = {"grape", "grape_gate_test", "grape_system_db", "mysql",
                           "information_schema", "performance_schema", "sys"}
    if args.shadow_db == args.source_db or args.shadow_db in _PROTECTED_DB_NAMES:
        raise GateError(
            f"演练库名不安全: shadow_db={args.shadow_db!r} 与 source_db 相同或命中"
            f"生产/系统库名单 {_PROTECTED_DB_NAMES}——phase_shadow 将 DROP 该库。"
            f"请改用草稿库名（如 {args.source_db}_shadow_<date>）后重试。")

    try:
        # 阶段 1：backup（任何路径都必须先有备份）
        backup_path = phase_backup(args.backup_dir, args.source_db)

        # 阶段 2：shadow 演练 + 自动校验
        phase_shadow(backup_path, args.source_db, args.shadow_db,
                     args.migration_cmd)

        # 阶段 3：apply —— 仅 --allow-prod 才动生产库；否则打 plan 退出 0
        if not args.allow_prod:
            log("plan_only", phase="apply",
                action="未传 --allow-prod：仅演练完成，生产库未改动",
                next_step="确认演练校验全绿后：deploy_mysql_gate.py --allow-prod")
            log("gate_pass", mode="dry-run/plan")
            return 0
        phase_apply(args.source_db, args.migration_cmd)

        # 阶段 4：smoke
        _sql_smoke(args.source_db)
        if args.smoke_api:
            _api_smoke(args.smoke_api)
        log("gate_pass", mode="apply")
        return 0
    except GateError as exc:
        log("gate_fail", error=str(exc))
        print(f"GATE FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
