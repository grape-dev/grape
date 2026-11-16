"""平台对象表「台账说谎」自愈迁移（202611170041）场景用例。

实弹背景（2026-11-16）：存量快照库首跑时 202611170030 被盖章（duration 2 ms），
四张平台对象表却不存在，后续 backfill 迁移未盖章；用户配 PAT 后钩子写
`grape_t_user_repo_platform_link` 报 `no such table`，平台关系永远不出现。
本用例锁两件事：
  1. 迁移已登记且版本号在 040 之后（renumber 纪律：新号必须更大）；
  2. 在「台账已盖章但表缺失」的库上执行该迁移后四表齐备，且重复执行幂等。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TABLES = (
    "grape_t_oauth_channels", "grape_t_repo_platforms",
    "grape_t_channel_repo_link", "grape_t_user_repo_platform_link",
)


def _py(code: str, db_path: Path) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GRAPE_DSN_")}
    env.update({
        "DEVOPS_DB_PATH": str(db_path),
        "DEVOPS_DB_BACKEND": "sqlite",
        "GRAPE_PROCESS_ROLE": "mgmt",   # 四表 owner
        "OBS_LOG_DB": "0",
        "JWT_SECRET": "preflight-test-secret-0123456789abcdef0123456789abcdef",
    })
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          env=env, cwd=str(ROOT), timeout=300)


def test_self_heal_migration_registered_after_040():
    code = (
        "import src.api.db_setup as d\n"
        "m = {x.version: x for x in d._MIGRATIONS}\n"
        "assert '202611170041' in m, '自愈迁移未登记'\n"
        "assert m['202611170041'].name == 'ensure-platform-object-tables'\n"
        "assert any(v < '202611170041' for v in m), '新号必须大于既有号'\n"
        "print('OK')\n"
    )
    r = _py(code, Path("/tmp/unused-ledger-check.db"))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "OK" in r.stdout


def test_self_heal_recreates_tables_when_ledger_lies(tmp_path):
    db = tmp_path / "lie.db"
    code = f"""
import sqlite3
c = sqlite3.connect({str(db)!r})
c.execute("CREATE TABLE grape_t_schema_migrations (version TEXT PRIMARY KEY, name TEXT, kind TEXT,"
          " applied_at TEXT, duration_ms INTEGER, checksum TEXT)")
c.execute("INSERT INTO grape_t_schema_migrations VALUES"
          " ('202611170030','create-platform-object-tables','safe','2026-11-16 18:47:01',2,'')")
c.commit(); c.close()

import src.api.db_setup as d
m = {{x.version: x for x in d._MIGRATIONS}}
m['202611170041'].fn()          # 第 1 次：补建
m_again = {{x.version: x for x in d._MIGRATIONS}}
m_again['202611170041'].fn()    # 第 2 次：幂等

c = sqlite3.connect({str(db)!r})
have = {{r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}}
need = {list(TABLES)!r}
missing = [t for t in need if t not in have]
assert not missing, f"自愈后仍缺表: {{missing}}"
print('SELF_HEAL_OK')
"""
    r = _py(code, db)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "SELF_HEAL_OK" in r.stdout


def test_self_heal_is_noop_on_healthy_db(tmp_path):
    """表已在：自愈迁移不得报错、不得改动数据。"""
    db = tmp_path / "healthy.db"
    code = f"""
import sqlite3
from src.api.platform_object_ddl import PLATFORM_OBJECT_DDL
c = sqlite3.connect({str(db)!r}); c.executescript(PLATFORM_OBJECT_DDL)
c.execute("CREATE TABLE grape_t_schema_migrations (version TEXT PRIMARY KEY, name TEXT, kind TEXT,"
          " applied_at TEXT, duration_ms INTEGER, checksum TEXT)")
c.execute("INSERT INTO grape_t_repo_platforms (id, provider, name, enabled)"
          " VALUES ('p1','gitee','Gitee',1)")
c.commit(); c.close()

import src.api.db_setup as d
next(x for x in d._MIGRATIONS if x.version == '202611170041').fn()

c = sqlite3.connect({str(db)!r})
rows = c.execute("SELECT provider, name FROM grape_t_repo_platforms").fetchall()
assert rows == [('gitee', 'Gitee')], rows
print('NOOP_OK')
"""
    r = _py(code, db)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "NOOP_OK" in r.stdout