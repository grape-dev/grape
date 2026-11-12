"""S30/S31/S10a/S11a/S19 补充用例（扫雷第六轮）。"""
import json
import time
import uuid
from unittest.mock import patch

import pytest

from src.kernel.session_manager import db, db_lock
from src.domains.runtime.vcs.platform_card import invalidate_platform_card_cache


GITEE_CARD = {
    "name": "Gitee", "dialect": "github", "host": "gitee.com",
    "webhook_path": "/webhook/gitee", "event_header": "X-Gitee-Event",
    "api_base": "https://gitee.com/api/v5", "api_auth": "query_token",
    "webhook_verify": "token_compare", "clone_auth": "basic_password",
}


@pytest.fixture()
def gitee_card():
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('vcs_providers_config', ?)",
            (json.dumps({"gitee": GITEE_CARD}),))
        db.commit()
    invalidate_platform_card_cache()
    yield GITEE_CARD
    with db_lock:
        db.cursor().execute("DELETE FROM meta WHERE key='vcs_providers_config'")
        db.commit()
    invalidate_platform_card_cache()


class _FakeResp:
    def __init__(self, data):
        self._data = json.dumps(data).encode()
        self.headers = {}

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _capture(monkeypatch):
    captured = []

    class _Req:
        def __init__(self, url, data=None, method="", headers=None):
            self.url, self.data, self.method, self.headers = url, data, method, headers or {}

    import urllib.request
    monkeypatch.setattr(urllib.request, "Request", _Req)
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=30: (captured.append(req), _FakeResp(
                            {"number": 11, "id": 11, "title": "T"}))[1])
    return captured


# ---------------------------------------------------------------- S30

def test_s30_whitelist_column_from_card(gitee_card, monkeypatch):
    """登录名变更白名单列名取登录渠道卡（gitee 联动其列，未配平台跳过）。"""
    from src.domains.mgmt.auth.providers import invalidate_provider_cache
    _oauth = {"gitee": {"name": "Gitee", "auth_url": "https://gitee.com/oauth/authorize",
                        "token_url": "https://gitee.com/oauth/token",
                        "user_url": "https://gitee.com/api/v5/user", "scope": "user_info",
                        "id_field": "id", "login_field": "login", "pkce_enabled": False,
                        "whitelist_column": "gitee_username"}}
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('oauth_providers_config', ?)",
            (json.dumps(_oauth),))
        # login_whitelist 需要 gitee_username 列（_ensure_whitelist_columns 启动时建；
        # 测试直接手动补列，模拟启动迁移后状态）
        cols = [r[1] for r in db.cursor().execute("PRAGMA table_info(login_whitelist)").fetchall()]
        if "gitee_username" not in cols:
            db.cursor().execute("ALTER TABLE login_whitelist ADD COLUMN gitee_username TEXT DEFAULT ''")
        db.cursor().execute(
            "INSERT OR REPLACE INTO login_whitelist (gitee_username, remark) VALUES ('old_login', 'x')")
        db.commit()
    invalidate_provider_cache()
    try:
        from src.domains.mgmt.auth.grape_account_db import sync_external_login_on_rename
        sync_external_login_on_rename(0, "gitee", "new_login", "old_login")
        with db_lock:
            row = db.cursor().execute(
                "SELECT gitee_username FROM login_whitelist WHERE remark='x'").fetchone()
        assert row and row[0] == "new_login"
    finally:
        with db_lock:
            db.cursor().execute("DELETE FROM login_whitelist WHERE remark='x'")
            db.cursor().execute("DELETE FROM meta WHERE key='oauth_providers_config'")
            db.commit()
        invalidate_provider_cache()


# ---------------------------------------------------------------- S31

def test_s31_create_issue_gitee_form_semantics(gitee_card, monkeypatch):
    """gitee 建 issue：query_token + form-encoded（Gitee 系 v5 实测语义）。"""
    from src.domains.runtime.vcs.api_client import VcsApiClient
    captured = _capture(monkeypatch)
    c = VcsApiClient("gitee", "TK", "o", "r")
    resp = c.create_issue("T1", "body text", assignee="u1")
    assert "error" not in resp
    req = captured[-1]
    assert req.url == "https://gitee.com/api/v5/repos/o/r/issues?access_token=TK"
    assert req.headers.get("Content-Type") == "application/x-www-form-urlencoded"
    assert b"title=T1" in req.data and b"assignee=u1" in req.data


def test_s31_create_issue_gitlab_dialect_json(monkeypatch, gitee_card):
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('vcs_providers_config', ?)",
            (json.dumps({"gls": {"dialect": "gitlab", "host": "gls.internal",
                                 "api_base": "https://gls.internal/api/v4",
                                 "api_auth": "bearer_header"}}),))
        db.commit()
    invalidate_platform_card_cache()
    try:
        captured = _capture(monkeypatch)
        from src.domains.runtime.vcs.api_client import VcsApiClient
        c = VcsApiClient("gls", "TK", "grp", "repo")
        c.create_issue("T", "desc")
        req = captured[-1]
        assert req.url == "https://gls.internal/api/v4/projects/grp%2Frepo/issues"
        assert req.headers.get("Content-Type") == "application/json"
        assert json.loads(req.data) == {"title": "T", "description": "desc"}
    finally:
        with db_lock:
            db.cursor().execute("DELETE FROM meta WHERE key='vcs_providers_config'")
            db.commit()
        invalidate_platform_card_cache()


def test_s31_builtin_providers_card_path(monkeypatch):
    """D22 统一：存量两平台建 issue 走卡路径，请求形态与存量序列逐字节一致。

    github = JSON + Authorization: token <t>；gitcode = form + access_token query
    （即 _post_github_issue / _post_gitcode_issue 的实测语义，平移到统一命令层）。
    """
    from src.domains.runtime.vcs.api_client import VcsApiClient
    captured = _capture(monkeypatch)
    VcsApiClient("github", "TK", "o", "r").create_issue("T", "b")
    req = captured[-1]
    assert req.url == "https://api.github.com/repos/o/r/issues"
    assert req.headers.get("Authorization") == "token TK"
    assert json.loads(req.data) == {"title": "T", "body": "b"}
    VcsApiClient("gitcode", "TK", "o", "r").create_issue("T", "b")
    req2 = captured[-1]
    assert req2.url == "https://gitcode.com/api/v5/repos/o/r/issues?access_token=TK"
    assert req2.headers.get("Content-Type") == "application/x-www-form-urlencoded"
    assert b"title=T" in req2.data


# ---------------------------------------------------------------- S19 补：GenericVcsProvider 命令层

def test_s19_generic_provider_collaborator_and_webhook(gitee_card, monkeypatch):
    from src.kernel.token_resolver import create_provider
    captured = _capture(monkeypatch)
    p = create_provider("gitee")
    assert p.add_collaborator("TK", "o", "r", "botuser", "maintainer")
    req = captured[-1]
    assert req.url == "https://gitee.com/api/v5/repos/o/r/collaborators/botuser?access_token=TK"
    assert req.method == "PUT"
    wid = p.setup_webhook("TK", "o", "r", "http://cb/webhook/gitee", "sec")
    assert wid == 11  # mock 返回 id
    wh = captured[-1]
    assert wh.url == "https://gitee.com/api/v5/repos/o/r/hooks?access_token=TK"
    assert json.loads(wh.data)["url"] == "http://cb/webhook/gitee"


# ---------------------------------------------------------------- S10a/S11a 显式化

def _mk_user(prefix):
    from src.domains.mgmt.auth.grape_account_db import create_grape_account, create_external_identity
    acc = create_grape_account(username=f"{prefix}_{uuid.uuid4().hex[:8]}",
                               email="", password="pw")
    assert acc
    create_external_identity(acc["id"], "gitcode", f"{prefix}_{uuid.uuid4().hex[:8]}",
                             access_token="at_oauth")
    return acc


def test_s10_password_revocation_does_not_touch_repo_creds(gitee_card):
    """改密码吊销全部 JWT（token_version+1）→ PAT/OAuth token 可用性不受影响。"""
    from src.domains.mgmt.auth.grape_account_db import increment_token_version, get_token_version
    from src.api.deps import get_user_token_for
    acc = _mk_user("s10")
    v0 = get_token_version(acc["id"])
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO user_credentials (user_id, provider, config_json) "
            "VALUES (?, 'gitee_pat', ?)",
            (acc["id"], json.dumps({"token": "pat_s10"})))
        db.commit()
    try:
        increment_token_version(acc["id"])
        assert get_token_version(acc["id"]) == v0 + 1       # JWT 域确实吊销了
        assert get_user_token_for(acc["username"], acc["id"], "gitcode") == "at_oauth"
        from src.kernel.token_resolver import create_provider
        assert create_provider("gitee").fetch_user_token(acc["username"], acc["id"]) == "pat_s10"
    finally:
        with db_lock:
            db.cursor().execute("DELETE FROM user_credentials WHERE user_id=?", (acc["id"],))
            db.commit()


def test_s11_pat_delete_does_not_touch_login(gitee_card):
    """删除 gitee PAT → JWT 版本与 OAuth 身份零改动。"""
    from src.domains.mgmt.auth.grape_account_db import get_token_version, get_user_identities_with_tokens
    from src.api.deps import get_user_token_for
    acc = _mk_user("s11")
    v0 = get_token_version(acc["id"])
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO user_credentials (user_id, provider, config_json) "
            "VALUES (?, 'gitee_pat', ?)", (acc["id"], json.dumps({"token": "pat_s11"})))
        db.commit()
        db.cursor().execute("DELETE FROM user_credentials WHERE user_id=? AND provider='gitee_pat'",
                            (acc["id"],))
        db.commit()
    assert get_token_version(acc["id"]) == v0                      # 登录域零改动
    assert get_user_token_for(acc["username"], acc["id"], "gitcode") == "at_oauth"
    idents = get_user_identities_with_tokens(acc["id"])
    assert any(i.get("provider") == "gitcode" for i in idents)
