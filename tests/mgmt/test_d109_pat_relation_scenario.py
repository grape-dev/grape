"""场景用例：PAT 保存 → 用户↔平台关系 → 用户面关系载荷（D109 全链）。

对应 UC-D109-45~47 / 需求 FR-33。实弹背景（2026-11-16）：平台对象四表未落地时，
PAT 保存钩子写关系表报 `no such table`，用户侧 `/api/auth/platform-links` 恒为空、
仓库页回落「无仓库平台绑定」引导页 —— 但接口层全是 200，看不出错。
本用例用真实 sqlite 库（非 mock ORM）跑「写入关系 → 读回载荷 → 撤销」三步，
保证这条用户可见链路有独立回归锁。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _run(code: str, db_path: Path) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GRAPE_DSN_")}
    env.update({
        "DEVOPS_DB_PATH": str(db_path),
        "DEVOPS_DB_BACKEND": "sqlite",
        "GRAPE_PROCESS_ROLE": "mgmt",
        "OBS_LOG_DB": "0",
        "JWT_SECRET": "pat-relation-scenario-0123456789abcdef0123456789abcdef",
        "GRAPE_PLATFORM_LINK_HOOKS": "1",
    })
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          env=env, cwd=str(ROOT), timeout=300)


_BOOTSTRAP = """
import sqlite3
from sqlalchemy.orm import Session
from src.api.platform_object_ddl import PLATFORM_OBJECT_DDL
from src.kernel.persistence.engine import get_engine
from src.domains.platform.importer import run_startup_import

# meta 表：导入状态（epoch/migrated）与其它系统设置都落这里（runtime 域表，测试里自建）
c = sqlite3.connect({db!r})
c.executescript(PLATFORM_OBJECT_DDL)
c.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
c.commit(); c.close()

with Session(get_engine()) as s:
    # 走真实导入器（历史键名映射 owner_identity_token_fallback → owner_fallback 等口径同一处）
    report = run_startup_import(s, owner="test")
    s.commit()
assert not report.failed, report.failed

# 读路径切换判据（is_table_backed）：导入完成标记，等价的线上动作是 mark_migrated()
from src.domains.platform import importer as _imp
_imp.mark_migrated()
"""


def test_pat_save_writes_relation_and_payload_reflects_it(tmp_path):
    db = tmp_path / "pat.db"
    code = (_BOOTSTRAP + """
import sqlite3, time
from src.domains.platform import hooks
from src.domains.mgmt.routes.auth import _repo_platform_links

# 真实路径：保存 PAT 会同时写凭据行并触发关系钩子
from src.domains.mgmt.user import db as user_db

assert user_db.set_credential("u-p1", "gitee", {{"token": "t-gitee"}}, kind="pat")
assert user_db.set_credential("u-p1", "github", {{"token": "t-gh"}}, kind="pat")
for t in list(__import__("threading").enumerate()):
    if t.name.startswith("d109-projects-"):
        t.join(5)

payload = _repo_platform_links("u-p1")
assert payload and payload["providers"] == ["gitee", "github"], payload
assert payload["has_any"] is True and payload["source"] == "table", payload

c = sqlite3.connect({db!r})
rows = sorted(c.execute(
    "SELECT repo_provider, revoked_at FROM grape_t_user_repo_platform_link WHERE user_id='u-p1'").fetchall())
assert rows == [("gitee", ""), ("github", "")], rows
print("WRITE_OK")
""").format(db=str(db))
    r = _run(code, db)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "WRITE_OK" in r.stdout


def test_pat_delete_revokes_only_that_platform(tmp_path):
    db = tmp_path / "pat2.db"
    code = (_BOOTSTRAP + """
import sqlite3
from src.domains.platform import hooks
from src.domains.mgmt.routes.auth import _repo_platform_links

from src.domains.mgmt.user import db as user_db

assert user_db.set_credential("u-p2", "gitee", {{"token": "t-gitee"}}, kind="pat")
assert user_db.set_credential("u-p2", "github", {{"token": "t-gh"}}, kind="pat")
assert user_db.delete_credential("u-p2", "gitee", kind="pat")
for t in list(__import__("threading").enumerate()):
    if t.name.startswith("d109-projects-"):
        t.join(5)

payload = _repo_platform_links("u-p2")
assert payload and payload["providers"] == ["github"], payload

c = sqlite3.connect({db!r})
gitee = c.execute("SELECT revoked_at FROM grape_t_user_repo_platform_link "
                  "WHERE user_id='u-p2' AND repo_provider='gitee'").fetchone()[0]
github = c.execute("SELECT revoked_at FROM grape_t_user_repo_platform_link "
                   "WHERE user_id='u-p2' AND repo_provider='github'").fetchone()[0]
assert gitee and not github, (gitee, github)
print("REVOKE_OK")
""").format(db=str(db))
    r = _run(code, db)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "REVOKE_OK" in r.stdout

def test_dangling_relation_is_pruned_when_no_source_left(tmp_path):
    """场景 UC-D109-50：关系行还在、来源全无（清 PAT 未触发撤销）→ 读口自愈撤销。

    2026-11-16 实锤：用户清完 PAT 后关系行仍 active → 前端按「已接入」进工作区，
    服务端无凭据 → 页面自相矛盾（一处说已绑定、一处报授权过期）。
    """
    db = tmp_path / "dangling.db"
    code = (_BOOTSTRAP + """
import sqlite3
c = sqlite3.connect({db!r})
c.execute("INSERT INTO grape_t_user_repo_platform_link (id, user_id, repo_provider,"
          " linked_at, revoked_at, revoked_reason, created_at, updated_at)"
          " VALUES ('l1','u-p3','gitee','2026-11-16 10:00:00','','','2026-11-16 10:00:00','2026-11-16 10:00:00')")
c.commit(); c.close()

from src.domains.mgmt.routes.auth import _repo_platform_links

payload = _repo_platform_links("u-p3")
assert payload and payload["providers"] == [], payload
assert payload["has_any"] is False, payload

c = sqlite3.connect({db!r})
revoked = c.execute("SELECT revoked_at, revoked_reason FROM grape_t_user_repo_platform_link"
                    " WHERE user_id='u-p3' AND repo_provider='gitee'").fetchone()
assert revoked and revoked[0], revoked
print("PRUNE_OK")
""").format(db=str(db))
    r = _run(code, db)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PRUNE_OK" in r.stdout
