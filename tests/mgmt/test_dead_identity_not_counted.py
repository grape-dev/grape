"""场景用例：身份行在、但 token 不可用 → 不算「已接入仓库平台」。

实弹（2026-11-16 yangaiwu）：清完 PAT 后账号仍被判「已接入 gitcode」——因为
`external_identities` 里有一条 gitcode 身份行（status=1），但它的 access_token 是历史
`derived:` 派生密钥、当前解不出来，`refresh_token` 为空。结果是：
  - `/auth/platform-links` 说 has_any=true（关系/身份都在）
  - `/repos/member` 去拉仓必然 401 → 页面报「gitcode 授权已过期」
两处结论相反 = 用户看到的「显示不对」。

口径（FR-33 R7）：入口判据 = 关系行 ∩ 凭据可用（身份 token 能解出非空，或有可用 PAT）。
不可用的平台从 providers 里剔除，单独放进 `needs_reauth`，前端据此提示重新授权。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _run(db: Path, code: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GRAPE_DSN_")}
    env.update({
        "DEVOPS_DB_PATH": str(db), "DEVOPS_DB_BACKEND": "sqlite",
        "GRAPE_PROCESS_ROLE": "mgmt", "OBS_LOG_DB": "0",
        "JWT_SECRET": "dead-identity-test-0123456789abcdef0123456789abcdef",
    })
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          env=env, cwd=str(ROOT), timeout=300)


BOOT = """
import sqlite3
import src.api.db_setup as d
d._create_tables()                      # 真库表结构（身份域 / 平台对象 / meta）
from src.domains.platform import importer
importer.mark_migrated()
from src.api.platform_object_ddl import PLATFORM_OBJECT_DDL
c = sqlite3.connect({db!r})
c.executescript(PLATFORM_OBJECT_DDL)   # 平台对象四表由注册迁移建，_create_tables 不含
c.execute("DELETE FROM external_identities")
c.execute("DELETE FROM grape_t_user_repo_platform_link")
# 身份行在、token 空（老密钥解不出来）——正是现场形态
c.execute("INSERT INTO external_identities (id, grape_user_id, provider, external_id, external_login,"
          " external_name, external_avatar, access_token, refresh_token, status)"
          " VALUES ('i1','u-dead','gitcode','x','yangaiwu','yangaiwu','','','',1)")
c.execute("INSERT INTO grape_t_user_repo_platform_link (id, user_id, repo_provider, linked_at,"
          " revoked_at, revoked_reason, created_at, updated_at)"
          " VALUES ('l1','u-dead','gitcode','2026-11-16 10:00:00','','','2026-11-16 10:00:00','2026-11-16 10:00:00')")
c.commit(); c.close()
"""


def test_dead_identity_is_not_counted_as_binding(tmp_path):
    db = tmp_path / "dead.db"
    code = BOOT.format(db=str(db)) + """
from src.domains.mgmt.auth.grape_account_db import list_platform_links
from src.domains.mgmt.routes.auth import _repo_platform_links

links = list_platform_links('u-dead')
assert [(i['provider'], bool(i.get('token_usable'))) for i in links] == [('gitcode', False)], links

payload = _repo_platform_links('u-dead')
assert payload['providers'] == [], payload
assert payload['has_any'] is False, payload
assert payload.get('needs_reauth') == ['gitcode'], payload
print('DEAD_IDENTITY_OK')
"""
    r = _run(db, code)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "DEAD_IDENTITY_OK" in r.stdout


def test_live_pat_still_counts(tmp_path):
    """同一身份行 + 可用 PAT → 必须算已接入（避免过度收紧）。"""
    db = tmp_path / "pat_ok.db"
    code = BOOT.format(db=str(db)) + """
from src.domains.mgmt.user import db as user_db
from src.domains.mgmt.routes.auth import _repo_platform_links

assert user_db.set_credential('u-dead', 'gitcode', {'token': 'live-pat'}, kind='pat')
payload = _repo_platform_links('u-dead')
assert payload['providers'] == ['gitcode'], payload
assert payload['has_any'] is True, payload
assert not payload.get('needs_reauth'), payload
print('LIVE_PAT_OK')
"""
    r = _run(db, code)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "LIVE_PAT_OK" in r.stdout