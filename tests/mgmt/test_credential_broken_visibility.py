"""场景用例：凭据"在库里但读不出来"时，用户面必须区分「没配」与「配坏了」。

实弹背景（2026-11-18 yangaiwu / dev）：
  - `user_credentials` 里 gitcode/gitee/github 共 6 条 PAT 行，`user_repos` 里
    58 + 7 + 16 条仓库缓存都在；
  - 但密文是用**另一份 JWT_SECRET** 派生的密钥写的，当前进程 decrypt 抛
    `ValueError: Decrypt failed:`；
  - `_load_credential_json` 把异常吞成 `{}` → `list_platform_links` 认为"没有 token"
    → `/repos/member` 早退 `_no_vcs_binding`，hint 还写成"绑定的登录渠道不是代码托管平台"；
  - 用户看到的是"项目列表整块空了，而且系统说我绑错了平台"。

口径（2026-11-18，用户拍板为口径乙）：
  ① 凭据行在但解不开 → 平台仍出现在 `list_platform_links`，带
     `token_usable=false` + `credential_error=decrypt_failed`；
  ② 凭据失效平台的仓库**一律不返回**（缓存行也不返回，避免把旧数据当可操作仓库）；
     但每个 200 出口必须带 `hint`（含"已隐藏其仓库"）+ `_credential_broken`，
     `/repos/member` 与 `/repos/sync` 同口径，禁止静默剔除；
  ③ `/repos/sync` 不再拿不可用平台去拉仓打成 502，而是 400 + `needs_reauth`；
     部分平台失效时同步照常返回 200，但同样带 hint；
  ④ `_prune_dangling_links` 在来源扫描失败（None）时**不得**剪枝。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]


def _run(db: Path, code: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GRAPE_DSN_")}
    env.update({
        "DEVOPS_DB_PATH": str(db), "DEVOPS_DB_BACKEND": "sqlite",
        "GRAPE_PROCESS_ROLE": "mgmt", "OBS_LOG_DB": "0",
        "JWT_SECRET": "broken-cred-test-0123456789abcdef0123456789abcdef",
    })
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          env=env, cwd=str(ROOT), timeout=300)


BOOT = """
import sqlite3
import src.api.db_setup as d
d._create_tables()
from src.domains.platform import importer
importer.mark_migrated()
from src.api.platform_object_ddl import PLATFORM_OBJECT_DDL
c = sqlite3.connect({db!r})
c.executescript(PLATFORM_OBJECT_DDL)
c.execute("DELETE FROM external_identities")
c.execute("DELETE FROM user_credentials")
c.execute("DELETE FROM user_repos")
c.execute("DELETE FROM grape_t_user_repo_platform_link")
# 解不开的密文（kid=derived，密文字节是垃圾）——正是"当年用了另一份 JWT_SECRET"的形态
c.execute("INSERT INTO user_credentials (user_id, provider, kind, config_json, updated_at)"
          " VALUES ('u-broken','gitee','pat','derived:AAAAAAAAAAAAAAAA:BBBBBBBBBBBBBBBBBBBBBB','2026-11-14 14:27:23')")
c.execute("INSERT INTO user_credentials (user_id, provider, kind, config_json, updated_at)"
          " VALUES ('u-broken','gitee_pat','','derived:AAAAAAAAAAAAAAAA:BBBBBBBBBBBBBBBBBBBBBB','2026-11-14 14:27:23')")
c.commit(); c.close()
"""


def test_unreadable_pat_is_surfaced_not_silently_dropped(tmp_path):
    db = tmp_path / "broken.db"
    r = _run(db, BOOT.format(db=str(db)) + """
from src.domains.mgmt.user.db import get_credentials
from src.domains.mgmt.auth.grape_account_db import list_platform_links

# 默认语义不变：读不出来的行不出现（老调用方零改动）
assert [c for c in get_credentials("u-broken") if c["provider"] == "gitee"] == []

# 显式要"中间态"时出现，并带原因
surfaced = [c for c in get_credentials("u-broken", include_unreadable=True)
            if c["provider"] == "gitee"]
assert len(surfaced) == 1, surfaced
assert surfaced[0]["credential_error"] == "decrypt_failed", surfaced

# 平台关系里：行在 → 平台在，但 token_usable=False 且说明原因
links = [x for x in list_platform_links("u-broken") if x["provider"] == "gitee"]
assert len(links) == 1, links
assert links[0]["token_usable"] is False, links
assert links[0]["has_pat_token"] is False, links
assert links[0]["credential_error"] == "decrypt_failed", links
print("OK-VISIBLE")
""".format())
    assert "OK-VISIBLE" in r.stdout, (r.stdout, r.stderr)


def test_unreadable_oauth_token_is_classified_as_decrypt_failed(tmp_path):
    """OAuth 密文解不开（旧 kid / 换钥）不能只报"授权已失效"，必须归类为 decrypt_failed。"""
    db = tmp_path / "oauth-broken.db"
    r = _run(db, BOOT.format(db=str(db)) + """
import sqlite3
c = sqlite3.connect({db!r})
c.execute("INSERT INTO external_identities (id, grape_user_id, provider, external_id,"
          " external_login, external_name, external_avatar, access_token, refresh_token, status)"
          " VALUES ('i1','u-oauth','github','42','u-oauth','','',"
          " 'derived:AAAAAAAAAAAAAAAA:BBBBBBBBBBBBBBBBBBBBBB','',1)")
c.commit(); c.close()

from src.domains.mgmt.auth.grape_account_db import list_platform_links
links = [x for x in list_platform_links("u-oauth") if x["provider"] == "github"]
assert len(links) == 1, links
assert links[0]["token_usable"] is False, links
assert links[0].get("credential_error") == "decrypt_failed", links
print("OK-OAUTH-BROKEN")
""".format(db=str(db)))
    assert "OK-OAUTH-BROKEN" in r.stdout, (r.stdout, r.stderr)


LOSERS = [
    "绑定的登录渠道不是代码托管平台",
]


@pytest.fixture()
def broken_client(monkeypatch):
    """用户只声明了 gitee，但 gitee 的 PAT 解不开；gitcode 完全没有。"""
    from src.api.deps import require_org_member
    from src.api.main import app
    from src.domains.mgmt.auth import grape_account_db as gad
    from src.domains.mgmt.routes import repos as repos_route
    from src.domains.mgmt.user import db as user_db

    user = {"id": "u-broken-route", "username": "u-broken", "auth_type": "jwt", "org_id": 0}
    app.dependency_overrides[require_org_member] = lambda: user

    monkeypatch.setattr(repos_route, "_repo_listable_providers", lambda: ["gitcode", "gitee"])
    monkeypatch.setattr(repos_route, "is_platform_visible", lambda p: True, raising=False)
    monkeypatch.setattr(gad, "list_platform_links", lambda uid: [{
        "provider": "gitee", "source": "pat", "status": 1,
        "has_pat_token": False, "token_usable": False,
        "credential_error": "decrypt_failed", "external_login": "",
    }])
    monkeypatch.setattr(gad, "get_user_identities", lambda uid: [])

    state = {"cached": [], "synced_at": None, "downloads": []}
    monkeypatch.setattr(user_db, "get_user_repos", lambda uid: list(state["cached"]))
    monkeypatch.setattr(user_db, "get_user_repos_synced_at", lambda uid: state["synced_at"])

    def _no_fetch(user_obj, provider, fn):
        state["downloads"].append(provider)
        raise AssertionError("凭据不可用的平台不得去拉仓")

    monkeypatch.setattr(repos_route, "with_oauth_token", _no_fetch)
    try:
        yield TestClient(app), state
    finally:
        app.dependency_overrides.clear()


def test_member_hides_broken_platform_repos_but_warns(broken_client):
    client, state = broken_client
    state["cached"] = [{
        "repo_key": "gitee:yang-aiwu-new/demo", "full_name": "yang-aiwu-new/demo",
        "owner": "yang-aiwu-new", "name": "demo", "is_hosted": 0, "is_joined": 0,
        "is_admin": 1, "role": "Admin", "created_at": "2025-09-21 20:33:13",
    }]
    r = client.get("/api/repos/member")
    assert r.status_code == 200, r.text
    body = r.json()
    # 口径乙：坏平台的缓存行不当可操作仓库返回
    assert body["repos"] == [], body
    assert body.get("_stale") is not True, body
    assert body.get("_credential_broken") == ["gitee"], body
    assert body.get("_reauth_provider") == "gitee", body
    hint = body.get("hint", "")
    assert "已隐藏其仓库" in hint, body
    assert "重新配置" in hint, body
    for banned in LOSERS:
        assert banned not in hint, body
    assert state["downloads"] == [], state


def test_member_without_cache_says_reconfigure_not_wrong_channel(broken_client):
    client, state = broken_client
    state["cached"] = []
    r = client.get("/api/repos/member")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("_no_vcs_binding") is True, body
    assert body.get("_credential_broken") == ["gitee"], body
    assert body.get("_reauth_provider") == "gitee", body
    for banned in LOSERS:
        assert banned not in body.get("hint", ""), body
    assert "无法解密" in body.get("hint", "") or "重新配置" in body.get("hint", ""), body


def test_member_explicit_provider_reports_credential_unavailable(broken_client):
    client, _state = broken_client
    r = client.get("/api/repos/member?provider=gitee")
    assert r.status_code == 400, r.text
    detail = r.json().get("detail") or {}
    assert detail.get("error_code") == "CREDENTIAL_UNAVAILABLE", detail
    assert detail.get("needs_reauth") is True, detail
    assert detail.get("credential_error") == "decrypt_failed", detail
    assert "未配置" not in detail.get("message", ""), detail


def test_sync_rejects_unusable_credentials_instead_of_502(broken_client):
    client, state = broken_client
    r = client.post("/api/repos/sync")
    assert r.status_code == 400, r.text
    detail = r.json().get("detail") or {}
    assert detail.get("error_code") == "CREDENTIAL_UNAVAILABLE", detail
    assert detail.get("needs_reauth") is True, detail
    assert state["downloads"] == [], state


@pytest.fixture()
def mixed_client(monkeypatch):
    """gitcode 凭据可用、gitee 凭据解不开——坏平台仓库必须隐藏，且不得静默剔除。"""
    from src.api.deps import require_org_member
    from src.api.main import app
    from src.domains.mgmt.auth import grape_account_db as gad
    from src.domains.mgmt.routes import repos as repos_route
    from src.domains.mgmt.user import db as user_db

    user = {"id": "u-mixed-route", "username": "u-mixed", "auth_type": "jwt", "org_id": 0}
    app.dependency_overrides[require_org_member] = lambda: user

    monkeypatch.setattr(repos_route, "_repo_listable_providers", lambda: ["gitcode", "gitee"])
    monkeypatch.setattr(repos_route, "is_platform_visible", lambda p: True, raising=False)
    monkeypatch.setattr(gad, "list_platform_links", lambda uid: [
        {"provider": "gitcode", "source": "identity", "status": 1, "token_usable": True,
         "has_pat_token": False, "has_oauth_token": True, "external_login": "u-mixed"},
        {"provider": "gitee", "source": "pat", "status": 1, "has_pat_token": False,
         "token_usable": False, "credential_error": "decrypt_failed", "external_login": ""},
    ])
    state = {"cached": [], "synced_at": None, "downloads": []}
    monkeypatch.setattr(user_db, "get_user_repos", lambda uid: list(state["cached"]))
    monkeypatch.setattr(user_db, "get_user_repos_synced_at", lambda uid: state["synced_at"])
    monkeypatch.setattr(user_db, "save_user_repos",
                        lambda uid, repos, providers=None: True)

    def _fake_fetch(user_obj, provider, fn):
        state["downloads"].append(provider)
        if provider != "gitcode":
            raise AssertionError("凭据不可用的平台不得去拉仓")
        return [{"repo_key": "gitcode:u-mixed/alive", "full_name": "u-mixed/alive",
                 "owner": "u-mixed", "name": "alive", "is_hosted": 0, "is_joined": 0,
                 "is_admin": 1, "role": "Admin", "created_at": "2025-09-21 20:33:13"}]

    monkeypatch.setattr(repos_route, "with_oauth_token", _fake_fetch)
    try:
        yield TestClient(app), state
    finally:
        app.dependency_overrides.clear()


def test_member_mixed_providers_hides_broken_and_warns(mixed_client):
    client, state = mixed_client
    state["cached"] = [
        {"repo_key": "gitcode:u-mixed/alive", "full_name": "u-mixed/alive",
         "owner": "u-mixed", "name": "alive", "is_hosted": 0, "is_joined": 0,
         "is_admin": 1, "role": "Admin", "created_at": "2025-09-21 20:33:13"},
        {"repo_key": "gitee:u-mixed/legacy", "full_name": "u-mixed/legacy",
         "owner": "u-mixed", "name": "legacy", "is_hosted": 0, "is_joined": 0,
         "is_admin": 1, "role": "Admin", "created_at": "2025-09-21 20:33:13"},
    ]
    r = client.get("/api/repos/member")
    assert r.status_code == 200, r.text
    body = r.json()
    assert [x["repo_key"] for x in body["repos"]] == ["gitcode:u-mixed/alive"], body
    assert body.get("_credential_broken") == ["gitee"], body
    hint = body.get("hint", "")
    assert "已隐藏其仓库" in hint, body
    assert "gitee" in hint, body
    assert state["downloads"] == ["gitcode"], state


def test_sync_mixed_providers_warns_instead_of_silent(mixed_client):
    client, state = mixed_client
    r = client.post("/api/repos/sync")
    assert r.status_code == 200, r.text
    body = r.json()
    assert [x["repo_key"] for x in body["repos"]] == ["gitcode:u-mixed/alive"], body
    assert body.get("_credential_broken") == ["gitee"], body
    hint = body.get("hint", "")
    assert "已隐藏其仓库" in hint, body
    assert state["downloads"] == ["gitcode"], state


def test_prune_dangling_links_is_fail_safe_when_scan_unknown(monkeypatch):
    from src.domains.mgmt.routes import auth as auth_route

    revoked = []
    from src.domains.platform import hooks

    monkeypatch.setattr(auth_route, "_identity_providers", lambda uid: None)
    monkeypatch.setattr(hooks, "on_pat_deleted", lambda uid, p: revoked.append(p))

    active = ["gitee", "gitcode"]
    out = auth_route._prune_dangling_links("u-1", active, [])
    assert out == active, out
    assert revoked == [], revoked

    # 对照：扫描正常且确实没有来源 → 照旧撤销（自愈语义不能被这条 fail-safe 抹掉）
    monkeypatch.setattr(auth_route, "_identity_providers", lambda uid: set())
    out2 = auth_route._prune_dangling_links("u-1", active, [])
    assert out2 == [], out2


def test_prune_skipped_when_identity_query_raises(monkeypatch):
    """真实失败形态（2026-11-18 dev 库实锤）：身份查询异常被 get_user_identities
    内部吞成空列表，剪枝不能把它当成"没有身份行"→ 必须整轮跳过。"""
    from src.domains.mgmt.auth import grape_account_db as gad
    from src.domains.mgmt.routes import auth as auth_route
    from src.domains.platform import hooks

    revoked = []
    monkeypatch.setattr(hooks, "on_pat_deleted", lambda uid, p: revoked.append(p))

    def _boom(*a, **kw):
        raise RuntimeError("management db down")

    monkeypatch.setattr(gad._iar, "list_by_user_statuses", _boom)

    # 老语义不破：默认（strict=False）仍然把读失败吞成空列表
    assert gad.get_user_identities("u-1") == []
    # 剪枝链路拿到的是"不知道"，不是"没有"
    assert auth_route._identity_providers("u-1") is None

    active = ["gitee", "gitcode"]
    out = auth_route._prune_dangling_links("u-1", active, [])
    assert out == active, out
    assert revoked == [], revoked


def test_prune_skipped_when_pat_source_scan_degrades(monkeypatch):
    """身份确实读到空集，但 PAT 来源扫描（list_platform_links 内部吞异常）失败 →
    来源仍然是"未知"而不是"全无"，PAT-only 平台不得被撤销。"""
    from src.domains.mgmt.auth import grape_account_db as gad
    from src.domains.mgmt.routes import auth as auth_route
    from src.domains.mgmt.user import db as user_db
    from src.domains.platform import hooks

    revoked = []
    monkeypatch.setattr(hooks, "on_pat_deleted", lambda uid, p: revoked.append(p))
    monkeypatch.setattr(gad, "get_user_identities",
                        lambda uid, strict=False: [])

    def _boom(*a, **kw):
        raise RuntimeError("credential domain down")

    monkeypatch.setattr(user_db, "get_credentials", _boom)

    active = ["gitee", "gitcode"]
    out = auth_route._prune_dangling_links("u-1", active, [])
    assert out == active, out
    assert revoked == [], revoked


@pytest.fixture()
def settings_client(monkeypatch):
    """凭据页（/api/settings/credentials）读口的可控桩：只打桩凭据读取与身份行。"""
    from src.api.deps import require_no_session_token
    from src.api.main import app
    from src.domains.mgmt.auth import grape_account_db as gad
    from src.domains.mgmt.routes import settings as settings_route
    from src.domains.mgmt.user import db as user_db

    user = {"id": "u-cred-page", "username": "u-cred", "auth_type": "jwt", "org_id": 0}
    app.dependency_overrides[require_no_session_token] = lambda: user
    state = {"credentials": {}, "diag": {}, "identities": []}

    def _fake_get_credential(uid, provider, kind="", diagnostics=None):
        key = (provider, kind)
        if diagnostics is not None and state["diag"].get(key):
            diagnostics["credential_error"] = state["diag"][key]
        return dict(state["credentials"].get(key) or {})

    monkeypatch.setattr(settings_route.user_db, "get_credential", _fake_get_credential)
    monkeypatch.setattr(gad, "get_user_identities", lambda uid: list(state["identities"]))
    monkeypatch.setattr(user_db, "get_meta_value", lambda k: "")
    try:
        yield TestClient(app), state
    finally:
        app.dependency_overrides.clear()


def test_settings_credentials_oauth_only_is_not_a_pat(settings_client):
    """AC6：纯 OAuth 账号不得报"PAT 已配置"（"清不掉"的来源），令牌来源单列。"""
    client, state = settings_client
    state["identities"] = [{"provider": "github", "external_login": "demo-gh",
                            "has_oauth_token": True, "status": 1, "created_at": ""}]
    r = client.get("/api/settings/credentials")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["github_pat"]["exists"] is False, body.get("github_pat")
    assert body["github"]["token_exists"] is False, body.get("github")
    auth = body["platform_auth"]["github"]
    assert auth["token_source"] == "oauth", auth
    assert auth["oauth_bound"] is True and auth["oauth_login"] == "demo-gh", auth


def test_settings_credentials_broken_pat_is_reported_broken(settings_client):
    """AC6：PAT 行在但解不开 → exists=false + broken，前端据此提示重配而不是"没配"。"""
    client, state = settings_client
    state["diag"] = {("gitee", "pat"): "decrypt_failed"}
    r = client.get("/api/settings/credentials")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["gitee_pat"]["exists"] is False, body.get("gitee_pat")
    assert body["gitee_pat"]["broken"] == "decrypt_failed", body.get("gitee_pat")
    assert body["platform_auth"]["gitee"]["pat_broken"] == "decrypt_failed", body.get("platform_auth")


def test_transfer_with_token_sets_active_and_expiry(tmp_path):
    """AC1 的另一半：有令牌 → status=1 且 token_expires_at 落库（调用方传 expires_in）。"""
    db = tmp_path / "transfer-token.db"
    r = _run(db, BOOT.format(db=str(db)) + """
from src.domains.mgmt.auth.grape_account_db import db as _db, transfer_external_identity_in_tx
c = _db.cursor()
c.execute("INSERT INTO external_identities (id, grape_user_id, provider, external_id,"
          " external_login, external_name, external_avatar, access_token, refresh_token, status)"
          " VALUES ('old2','u-old2','gitee','9001','yang-aiwu-new','','','tok','rt',1)")
_db.commit()

ok = transfer_external_identity_in_tx(
    _db.cursor(), new_user_id="u-new2", provider="gitee", external_id="9001",
    external_login="yang-aiwu-new", access_token="newtok", refresh_token="newrt",
    expires_in=3600, original_user_id="u-old2")
assert ok
row = _db.cursor().execute(
    "SELECT status, LENGTH(access_token), token_expires_at FROM external_identities"
    " WHERE grape_user_id='u-new2' AND provider='gitee'").fetchone()
assert row is not None, "转移后目标账号必须有绑定行"
assert row[0] == 1, row
assert row[1] > 0, row          # 令牌以密文落库
assert row[2], row              # 过期时间非空
print("OK-TRANSFER-TOKEN")
""".format(db=str(db)))
    assert "OK-TRANSFER-TOKEN" in r.stdout, (r.stdout, r.stderr)


def test_transfer_without_token_marks_need_reauth(tmp_path):
    db = tmp_path / "transfer.db"
    r = _run(db, BOOT.format(db=str(db)) + """
from src.domains.mgmt.auth.grape_account_db import db as _db, transfer_external_identity_in_tx
# 原账号（占位账号）上的旧绑定
c = _db.cursor()
c.execute("INSERT INTO external_identities (id, grape_user_id, provider, external_id,"
          " external_login, external_name, external_avatar, access_token, refresh_token, status)"
          " VALUES ('old','u-old','gitee','13450218','yang-aiwu-new','','','tok','rt',1)")
_db.commit()

ok = transfer_external_identity_in_tx(
    _db.cursor(), new_user_id="u-new", provider="gitee", external_id="13450218",
    external_login="yang-aiwu-new", original_user_id="u-old")
assert ok
row = _db.cursor().execute(
    "SELECT status, access_token, refresh_token FROM external_identities"
    " WHERE grape_user_id='u-new' AND provider='gitee'").fetchone()
assert row is not None, "转移后目标账号必须有绑定行"
assert row[0] == 4, row            # 无令牌 → 待重新授权，不能是 status=1
assert row[1] == "" and row[2] == "", row
# 原账号不再持有该平台任何活跃绑定（旧行先被置 status=0，再按 UNIQUE 键清理删除）
old = _db.cursor().execute(
    "SELECT status FROM external_identities WHERE grape_user_id='u-old' AND provider='gitee'"
    " AND status IN (1, 4)").fetchone()
assert old is None, old
print("OK-TRANSFER")
""".format())
    assert "OK-TRANSFER" in r.stdout, (r.stdout, r.stderr)
