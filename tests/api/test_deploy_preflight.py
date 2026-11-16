"""部署前自检（scripts/deploy_preflight.py）场景用例。

对应需求 FR-33 / 设计分片 d109 §20；实弹背景：2026-11-16 现网库副本上迁移台账
盖章（202611170030，2 ms）而四张平台对象表未落地，PAT 保存钩子写关系表报 1146，
表现为「配了 PAT 但平台关系不出现」，而接口层 200 —— 靠预检把这类「台账说谎」拦在启动前。
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "deploy_preflight.py"

sys.path.insert(0, str(ROOT))
from src.api.platform_object_ddl import PLATFORM_OBJECT_DDL  # noqa: E402

LEDGER_DDL = (
    "CREATE TABLE IF NOT EXISTS grape_t_schema_migrations ("
    " version TEXT PRIMARY KEY, name TEXT, kind TEXT, applied_at TEXT,"
    " duration_ms INTEGER, checksum TEXT)"
)
REQUIRED = (
    "grape_t_oauth_channels", "grape_t_repo_platforms",
    "grape_t_channel_repo_link", "grape_t_user_repo_platform_link",
)


def _seed_db(path: Path, *, tables: bool = True, ledger_rows: bool = True,
             drop: tuple[str, ...] = (), keep_meta: bool = True) -> Path:
    conn = sqlite3.connect(path)
    if tables:
        conn.executescript(PLATFORM_OBJECT_DDL)
        conn.execute("CREATE TABLE IF NOT EXISTS grape_t_repo_credentials ("
                     " id TEXT, repo_key TEXT, cred_type TEXT, value TEXT)")
        if keep_meta:
            conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT, value TEXT)")
    conn.execute(LEDGER_DDL)
    if ledger_rows:
        conn.execute(
            "INSERT INTO grape_t_schema_migrations (version, name, kind, applied_at, duration_ms, checksum)"
            " VALUES ('202611170030','create-platform-object-tables','safe','2026-11-16 18:47:01',2,'')")
    for t in drop:
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.commit()
    conn.close()
    return path


def _run(path: Path, role: str | None, *, json_out: bool = False) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items()
           if k not in ("GRAPE_PROCESS_ROLE", "DEVOPS_DB_NAME")
           and not k.startswith("GRAPE_DSN_") and k != "GRAPE_DOMAIN_LIB"}
    if role:
        env["GRAPE_PROCESS_ROLE"] = role
    env["DEVOPS_DB_BACKEND"] = "sqlite"
    cmd = [sys.executable, str(SCRIPT), "--db-path", str(path)]
    if json_out:
        cmd.append("--json")
    return subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=180)


def test_preflight_passes_on_healthy_db(tmp_path):
    db = _seed_db(tmp_path / "ok.db")
    r = _run(db, "mgmt", json_out=True)
    assert r.returncode == 0, r.stdout + r.stderr
    data = json.loads(r.stdout)
    assert data["pass"] is True
    assert {c["check"] for c in data["checks"]} >= {
        "role_env", "db_reachable", "migration_ledger", "ledger_vs_registry", "owned_tables_exist"}


def test_preflight_fails_when_ledger_claims_but_table_missing(tmp_path):
    """台账盖章、实表缺失（本次实弹原样复刻）→ 必须拦。"""
    db = _seed_db(tmp_path / "lie.db", drop=("grape_t_user_repo_platform_link",))
    r = _run(db, "mgmt")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "grape_t_user_repo_platform_link" in r.stdout
    assert "202611170041" in r.stdout  # 给出自愈迁移的处置提示


def test_preflight_fails_without_role(tmp_path):
    """缺 GRAPE_PROCESS_ROLE：守卫会按非 owner 跳过本域 DDL → 必须拦。"""
    db = _seed_db(tmp_path / "norole.db")
    r = _run(db, None)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "角色未设置" in r.stdout


def test_preflight_fails_on_empty_ledger(tmp_path):
    """空台账 = 空库首跑，必须先起 runtime（meta 归 runtime）。"""
    db = _seed_db(tmp_path / "empty.db", ledger_rows=False)
    r = _run(db, "mgmt")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "台账为空" in r.stdout
    assert "runtime" in r.stdout


def test_preflight_fails_when_single_db_meta_missing(tmp_path):
    """单库拓扑下 meta 必须在本库（跨域读回落进程库）。"""
    db = _seed_db(tmp_path / "nometa.db", keep_meta=False, drop=("meta",))
    r = _run(db, "runtime")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "meta" in r.stdout