#!/usr/bin/env python3
"""三库模式冷启动（新实例 bootstrap）。

背景（2026-11-20 实测，部署四进程 + 三库时踩到）：
三库运行时（`GRAPE_DOMAIN_LIB=1` + 每进程 `GRAPE_MIGRATION_OWNER`）**只跑登记的增量迁移**，
基表由历史单库时代的 legacy 轨道建出来。于是「三个全新空库」直接起四进程会立刻失败：

    init_database → run_pending_migrations → 版本表为空 → 退回全量 _run_all_migrations
    → _create_tables 被表归属守卫跳过别家域的表（meta 属 runtime，本进程是 mgmt）
    → _seed_from_json 写 meta → (1146) Table '...meta' doesn't exist → 启动失败

本脚本把「先按单库一次建齐，再切三库」这条唯一可行路径固化下来：

1. 对每个域库，用**单库模式**（不带 GRAPE_DSN_* / GRAPE_MIGRATION_OWNER）+ `GRAPE_DDL_OWNER_GUARD=0`
   跑一次 `db_setup.init_database()`：基表、版本台账、种子数据一次建齐（幂等，可重复跑）；
2. 之后按 GRAPE_DSN_* + 每进程 owner 起四进程（mgmt/event/worker/insight）。

同时会拒绝另一类"看起来能用其实会炸"的库：被 `scripts/migrate_schemas.py`（旧 schema-split
工具）改过的库，其 `grape_t_schema_migrations` 是 `(table_name, target_schema, migrated_at)`，
与应用要的 `(version, name, ...)` 形状不同；`CREATE TABLE IF NOT EXISTS` 会静默跳过，版本表读不了
→ 退回 legacy → 同样死在 meta。这类库必须换新库（或人工处理后）再 bootstrap。

用法：
    # 三个库都建（DSN 从 env 文件读，建议 600 权限）
    python scripts/bootstrap_3db.py --env-file /root/.grape-3db.env
    # 只查三个库的状态，不动数据
    python scripts/bootstrap_3db.py --env-file /root/.grape-3db.env --check
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LEDGER = "grape_t_schema_migrations"
# 应用版本台账的形状（src/domains/mgmt/schema_ddl.py::_SCHEMA_MIGRATIONS_DDL）
REQUIRED_LEDGER_COLUMNS = {"version", "name", "kind", "applied_at"}


def load_env_file(path: str) -> dict:
    """读 env 文件（只认 `export K=V` / `K=V`，值两端的引号去掉）。"""
    env = {}
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip("'\"")
    return env


def _parse_dsn(dsn: str) -> dict:
    from urllib.parse import unquote, urlparse
    u = urlparse(dsn)
    return {
        "host": u.hostname or "127.0.0.1",
        "port": u.port or 3306,
        "user": unquote(u.username or ""),
        "password": unquote(u.password or ""),
        "database": (u.path or "/").lstrip("/").split("?")[0],
    }


def _db_state(conn_kwargs: dict) -> dict:
    """库状态：表数、版本台账形状/条数、是否被旧 split 工具改过。"""
    import pymysql
    state = {"database": conn_kwargs["database"], "tables": 0, "ledger": None,
             "ledger_rows": 0, "incompatible": False}
    conn = pymysql.connect(**conn_kwargs)
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=%s",
                    (conn_kwargs["database"],))
        state["tables"] = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=%s "
                    "AND table_name=%s", (conn_kwargs["database"], LEDGER))
        if cur.fetchone()[0]:
            cur.execute("SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema=%s AND table_name=%s",
                        (conn_kwargs["database"], LEDGER))
            cols = {r[0].lower() for r in cur.fetchall()}
            state["ledger"] = sorted(cols)
            state["incompatible"] = not REQUIRED_LEDGER_COLUMNS.issubset(cols)
            if not state["incompatible"]:
                cur.execute(f"SELECT COUNT(*) FROM `{conn_kwargs['database']}`.`{LEDGER}`")
                state["ledger_rows"] = cur.fetchone()[0]
    finally:
        conn.close()
    return state


def bootstrap_one(env: dict, dsn_key: str, dry_run: bool) -> int:
    dsn = env.get(dsn_key, "").strip()
    if not dsn:
        print(f"[bootstrap] SKIP {dsn_key}: 未配置（env 文件里缺这条 DSN）", file=sys.stderr)
        return 1
    conn_kwargs = _parse_dsn(dsn)
    state = _db_state(conn_kwargs)
    print(f"[bootstrap] {dsn_key} -> {conn_kwargs['database']} "
          f"tables={state['tables']} ledger={state['ledger']} rows={state['ledger_rows']}")
    if state["incompatible"]:
        print(f"[bootstrap] FAIL {conn_kwargs['database']}: `{LEDGER}` 是被旧 schema-split 工具"
              f"改过的形状（{state['ledger']}），与应用的 (version,name,...) 不兼容；"
              "请换一个新的空库，或先把该表改名后重跑（改名会让所有迁移重跑，需人工确认）。",
              file=sys.stderr)
        return 2
    if dry_run:
        print(f"[bootstrap] (dry-run) 将用单库模式 + GRAPE_DDL_OWNER_GUARD=0 建 "
              f"{conn_kwargs['database']}")
        return 0

    child = {
        "DEVOPS_DB_BACKEND": "mysql",
        "DEVOPS_DB_HOST": str(conn_kwargs["host"]),
        "DEVOPS_DB_PORT": str(conn_kwargs["port"]),
        "DEVOPS_DB_USER": conn_kwargs["user"],
        "DEVOPS_DB_PASSWORD": conn_kwargs["password"],
        "DEVOPS_DB_NAME": conn_kwargs["database"],
        # 单库模式：不带三库 DSN / owner；归属守卫关掉，否则别家域的表被跳过 → 种子写 meta 失败
        "GRAPE_DDL_OWNER_GUARD": "0",
        "PYTHONPATH": str(REPO),
    }
    for k in ("GRAPE_DSN_MGMT", "GRAPE_DSN_RUNTIME", "GRAPE_DSN_INSIGHT",
              "GRAPE_MIGRATION_OWNER", "GRAPE_DOMAIN_LIB"):
        child.pop(k, None)
    run_env = {k: v for k, v in os.environ.items()
               if k not in ("GRAPE_DSN_MGMT", "GRAPE_DSN_RUNTIME", "GRAPE_DSN_INSIGHT",
                            "GRAPE_MIGRATION_OWNER", "GRAPE_DOMAIN_LIB")}
    run_env.update(child)
    py = str(REPO / ".venv" / "bin" / "python")
    if not Path(py).exists():
        py = sys.executable
    code = ("import sys; sys.path.insert(0, %r); "
            "from src.api.db_setup import init_database; init_database(); "
            "print('BOOTSTRAP_OK')" % str(REPO))
    print(f"[bootstrap] init_database() on {conn_kwargs['database']} …", flush=True)
    r = subprocess.run([py, "-c", code], cwd=str(REPO), env=run_env,
                       capture_output=True, text=True)
    Path("/tmp").mkdir(exist_ok=True)
    log = Path(f"/tmp/grape-bootstrap-{conn_kwargs['database']}.log")
    log.write_text(r.stdout + r.stderr, encoding="utf-8")
    if r.returncode != 0 or "BOOTSTRAP_OK" not in r.stdout:
        tail = "\n".join((r.stdout + r.stderr).splitlines()[-12:])
        print(f"[bootstrap] FAIL {conn_kwargs['database']} (rc={r.returncode})，日志 {log}:\n{tail}",
              file=sys.stderr)
        return 3
    after = _db_state(conn_kwargs)
    print(f"[bootstrap] OK {conn_kwargs['database']} tables={after['tables']} "
          f"ledger_rows={after['ledger_rows']}（日志 {log}）")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="三库模式冷启动 bootstrap")
    ap.add_argument("--env-file", default="/root/.grape-3db.env",
                    help="含 GRAPE_DSN_MGMT/RUNTIME/INSIGHT 的 env 文件（建议 600）")
    ap.add_argument("--check", action="store_true", help="只检查三个库状态，不建表")
    ap.add_argument("--dry-run", action="store_true", help="打印将做的事，不落库")
    args = ap.parse_args(argv)

    env = load_env_file(args.env_file)
    rc = 0
    for key in ("GRAPE_DSN_MGMT", "GRAPE_DSN_RUNTIME", "GRAPE_DSN_INSIGHT"):
        r = bootstrap_one(env, key, args.check or args.dry_run) if args.check or args.dry_run \
            else bootstrap_one(env, key, False)
        rc = max(rc, r)
    if rc == 0:
        print("[bootstrap] 三个库就绪：按 GRAPE_DOMAIN_LIB=1 + 每进程 GRAPE_MIGRATION_OWNER "
              "起四进程即可（mgmt=mgmt / event=runtime / insight=insight / worker 留空）")
    return rc


if __name__ == "__main__":
    sys.exit(main())