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

import pytest

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


def _run(path: Path, role: str | None, *, json_out: bool = False,
         contracts_dir: Path | None = None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items()
           if k not in ("GRAPE_PROCESS_ROLE", "DEVOPS_DB_NAME")
           and not k.startswith("GRAPE_DSN_") and k != "GRAPE_DOMAIN_LIB"
           and k != "GRAPE_CONTRACTS_DIR"}
    if role:
        env["GRAPE_PROCESS_ROLE"] = role
    env["DEVOPS_DB_BACKEND"] = "sqlite"
    cmd = [sys.executable, str(SCRIPT), "--db-path", str(path)]
    if contracts_dir is not None:
        cmd += ["--contracts-dir", str(contracts_dir)]
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
        "role_env", "contracts_registry", "db_reachable", "migration_ledger",
        "ledger_vs_registry", "owned_tables_exist"}


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

def test_preflight_fails_when_contracts_registry_missing(tmp_path):
    """Issue #3756：契约登记表未随产物落盘 → 部署预检 exit 1（不得启动）。"""
    db = _seed_db(tmp_path / "ok.db")
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    r = _run(db, "mgmt", contracts_dir=contracts)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "契约登记表" in r.stdout
    assert "contracts" in r.stdout


def _seed_contracts(path: Path, *, omit: str | None = None) -> Path:
    """构造齐全的 contracts/ 目录（可指定缺哪一个必需文件）。"""
    path.mkdir(parents=True)
    (path / "released").mkdir()
    (path / "released" / "2.2.0.json").write_text("{}", encoding="utf-8")
    for name in ("deprecated_endpoints.json", "deprecated_exemptions.json",
                 "openapi_baseline.json", "required_operations.json"):
        if name == omit:
            continue
        (path / name).write_text("{}", encoding="utf-8")
    return path


@pytest.mark.parametrize("omitted", ["openapi_baseline.json", "required_operations.json"])
def test_preflight_fails_when_completeness_gate_input_missing(tmp_path, omitted):
    """缺完整性门禁输入（baseline / required_operations）也必须预检拦下。

    这两件缺失时弃用登记表仍齐备，旧预检会 PASS，但 custom_openapi 的完整性
    门禁照样 fail-fast → /openapi.json 仍 500（Issue #3756 审查 R1）。
    """
    db = _seed_db(tmp_path / "ok.db")
    contracts = _seed_contracts(tmp_path / "contracts", omit=omitted)
    r = _run(db, "mgmt", contracts_dir=contracts)
    assert r.returncode == 1, r.stdout + r.stderr
    assert omitted in r.stdout


def test_preflight_passes_when_contracts_complete(tmp_path):
    """打包清单内的必需文件齐备 + released 快照存在 → 契约项通过。"""
    db = _seed_db(tmp_path / "ok.db")
    contracts = _seed_contracts(tmp_path / "contracts")
    r = _run(db, "mgmt", contracts_dir=contracts, json_out=True)
    assert r.returncode == 0, r.stdout + r.stderr
    check = next(c for c in json.loads(r.stdout)["checks"]
                 if c["check"] == "contracts_registry")
    assert check["ok"] is True, check
